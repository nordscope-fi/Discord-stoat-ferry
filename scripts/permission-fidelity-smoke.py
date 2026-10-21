"""Explicit synthetic-server proof using Ferry's existing migration and repair owners."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import uuid
from contextlib import AsyncExitStack
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urlsplit

import aiohttp

from discord_ferry.config import FerryConfig
from discord_ferry.core.atomicio import atomic_write_text
from discord_ferry.core.engine import run_migration, run_permission_role_backfill, run_repair
from discord_ferry.core.http import new_session
from discord_ferry.core.permissions import backend_creation_default
from discord_ferry.core.security import register_secret
from discord_ferry.discord import client
from discord_ferry.discord.metadata import (
    ChannelMeta,
    DiscordMetadata,
    PermissionPair,
    RoleMeta,
    RoleOverride,
    capture_permission_source,
    save_discord_metadata,
)
from discord_ferry.discord.models import DiscordChannel, DiscordRole, PermissionOverwrite
from discord_ferry.discord.permissions import translate_permissions
from discord_ferry.errors import PermissionSafetyError
from discord_ferry.migrator.api import (
    _resolve_429_delay_seconds,
    api_delete_channel,
    api_delete_role,
)
from discord_ferry.parser.dce_parser import parse_export_directory
from discord_ferry.state import is_stoat_id

VIEW, SEND = 1 << 10, 1 << 11
GUILD = "111"


class ProofRefusedError(Exception):
    """Stop without publishing response text or credential-bearing exceptions."""


def settings_from_file(path: str) -> dict[str, Any]:
    """Accept only explicit isolated target selection and exact credential references."""
    value = json.loads(Path(path).read_text())
    keys = {
        "isolated_synthetic",
        "api_base",
        "backend_version",
        "server_id",
        "owner_credential",
        "observer_credential",
        "candidate_manifest",
    }
    if not isinstance(value, dict) or set(value) != keys or value["isolated_synthetic"] is not True:
        raise ProofRefusedError
    base = urlsplit(value["api_base"])
    if (
        base.scheme != "https"
        or not base.hostname
        or base.username
        or base.password
        or base.query
        or base.fragment
        or base.path not in {"", "/"}
        or not is_stoat_id(value["server_id"])
        or backend_creation_default(value["backend_version"]) is None
        or value["owner_credential"] == value["observer_credential"]
    ):
        raise ProofRefusedError
    for name in ("owner_credential", "observer_credential"):
        parts = value[name].split("/")
        if (
            len(parts) != 3
            or parts[2] != "Secret"
            or any(
                not item or item.startswith("-") or any(char in item for char in "\r\n")
                for item in parts
            )
        ):
            raise ProofRefusedError
    value["api_base"] = value["api_base"].rstrip("/")
    return value


def candidate_identity(root: Path, manifest: Path) -> dict[str, str]:
    """Reuse snapshot digests and compare captured Git blobs with the running checkout."""
    root = root.resolve()
    if not manifest.is_absolute():
        manifest = root / manifest
    if manifest.is_symlink() or manifest.name == ".mcp.json":
        raise ProofRefusedError
    manifest = manifest.resolve(strict=True)
    if not manifest.is_relative_to(root / "docs/plans/.review"):
        raise ProofRefusedError
    helper = Path(__file__).resolve().parent / "agent-compat/chunk-snapshot.mjs"
    script = (
        "import {sha256,stableJson} from "
        + json.dumps(helper.as_uri())
        + ";\n"
        + r"""
import {readFileSync,lstatSync,realpathSync,readlinkSync} from 'node:fs';
import {spawnSync} from 'node:child_process';
import {resolve,relative,sep,isAbsolute} from 'node:path';
const [root,manifest]=process.argv.slice(1), progress=JSON.parse(readFileSync(manifest));
const path=resolve(root,progress.snapshots.at(-1));
function inside(parent,path){
 const rel=relative(parent,path);
 return rel&&rel!=='..'&&!rel.startsWith('..'+sep)&&!isAbsolute(rel);
}
if(!inside(resolve(root,'docs/plans/.review'),path))throw Error('Snapshot outside evidence');
if(relative(path,realpathSync(path))!==''||lstatSync(path).isSymbolicLink())
 throw Error('Linked snapshot refused');
const record=JSON.parse(readFileSync(path)),{digest_sha256,...unsigned}=record;
if(record.kind!=='chunk-snapshot'||sha256(stableJson(unsigned))!==digest_sha256)
 throw Error('Snapshot changed');
if(!/^[a-f0-9]{40}$/.test(record.ending_git_tree))throw Error('Exact Git tree required');
function git(args){
 const r=spawnSync('git',args,{cwd:root,encoding:'utf8'});
 if(r.status!==0)throw Error('Git identity unavailable');
 return r.stdout;
}
if(git(['rev-parse','HEAD']).trim()!==record.starting_commit)throw Error('Starting commit changed');
const docLinks=new Map([
 ['docs/changelog.md','../CHANGELOG.md'],
 ['docs/contributing.md','../.github/CONTRIBUTING.md'],
]);
const treeEntries=git(['ls-tree','-rz',record.ending_git_tree]).split('\0').filter(Boolean);
const entries=treeEntries.flatMap(line=>{
 const tab=line.indexOf('\t'),[mode,type,oid]=line.slice(0,tab).split(' '),name=line.slice(tab+1);
 if(type==='blob'&&mode==='120000'&&docLinks.has(name)){
  const local=resolve(root,name),target=docLinks.get(name);
  if(!lstatSync(local).isSymbolicLink()||readlinkSync(local)!==target
     ||git(['cat-file','blob',oid])!==target)throw Error('Documentation link changed');
  return [];
 }
 if(type!=='blob'||!['100644','100755'].includes(mode)||name.split('/').includes('.mcp.json'))
  throw Error('Unsafe tree entry');
 const local=resolve(root,name),info=lstatSync(local);
 if(!inside(root,local)||!info.isFile()||info.isSymbolicLink()
    ||relative(local,realpathSync(local))!==''
    ||(info.mode&0o111?'100755':'100644')!==mode)throw Error('Tree path changed');
 return [{name,oid}];
});
const actual=git(['hash-object','--',...entries.map(e=>e.name)]).trim().split('\n');
if(actual.length!==entries.length||entries.some((e,i)=>e.oid!==actual[i]))
 throw Error('Candidate bytes changed');
console.log(JSON.stringify({application_commit:record.starting_commit,candidate_tree:record.ending_git_tree,
 candidate_snapshot_digest:digest_sha256,source_paths:entries.map(e=>e.name).filter(p=>p.startsWith('src/')&&p.endsWith('.py')||p==='scripts/permission-fidelity-smoke.py')}));
"""
    )
    process = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(root), str(manifest)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if process.returncode != 0:
        raise ProofRefusedError
    result: dict[str, Any] = json.loads(process.stdout)
    source_paths = sorted(
        path.relative_to(root).as_posix() for path in (root / "src").rglob("*.py")
    )
    if (root / "scripts/permission-fidelity-smoke.py").exists():
        source_paths.append("scripts/permission-fidelity-smoke.py")
    if sorted(source_paths) != sorted(result.pop("source_paths")):
        raise ProofRefusedError
    if not all(isinstance(value, str) for value in result.values()):
        raise ProofRefusedError
    return {
        key: result[key]
        for key in ("application_commit", "candidate_tree", "candidate_snapshot_digest")
    }


def credential(reference: str) -> str:
    """Read just the named Secret field through the already installed Proton CLI."""
    executable = shutil.which("pass-cli")
    if executable is None:
        raise ProofRefusedError
    vault, item, field = reference.split("/")
    result = subprocess.run(
        [executable, "item", "view", "--vault-name", vault, "--item-title", item, "--field", field],
        capture_output=True,
        timeout=30,
        check=False,
    )
    token = result.stdout.decode().strip() if result.returncode == 0 else ""
    if not token or any(char.isspace() for char in token):
        raise ProofRefusedError
    register_secret("permission-smoke-" + item, token)
    return token


def write_evidence(path: Path, result: dict[str, Any]) -> None:
    """Write structural values only, never response bodies, credentials or content."""
    atomic_write_text(path, json.dumps(result, indent=2) + "\n")


def source_access(facts: Any, assigned: frozenset[str], channel_id: str) -> tuple[bool, bool]:
    """Independent Discord arithmetic, without Ferry's compiler or predicates."""
    roles = {role.id: role for role in facts.roles}
    permissions = roles[facts.guild_id].permissions
    for identity in assigned:
        permissions |= roles[identity].permissions
    if permissions & 8:
        return True, True
    channel = next(item for item in facts.channels if item.id == channel_id)
    everyone = next((cell for cell in channel.overwrites if cell.role_id == facts.guild_id), None)
    if everyone is not None:
        permissions = (permissions & ~everyone.deny) | everyone.allow
    allow = deny = 0
    for cell in channel.overwrites:
        if cell.role_id in assigned:
            allow |= cell.allow
            deny |= cell.deny
    permissions = (permissions & ~deny) | allow
    visible = bool(permissions & VIEW)
    return visible, visible and bool(permissions & SEND)


def synthetic_source(
    work: Path, prefix: str
) -> tuple[DiscordMetadata, Path, list[DiscordRole], list[DiscordChannel]]:
    """Capture explicit synthetic raw facts and generate ordinary DCE-shaped exports."""
    exports = work / "exports"
    output = work / "output"
    exports.mkdir()
    roles = [
        client._parse_role(
            {
                "id": sid,
                "name": prefix + sid,
                "permissions": str(mask),
                "position": position,
                "managed": False,
            }
        )
        for sid, mask, position in (
            (GUILD, 0, 0),
            ("112", VIEW | SEND, 1),
            ("113", VIEW, 2),
            ("114", 8, 3),
        )
    ]
    channels = []
    for sid, grants in (
        ("222", (("112", VIEW | SEND, 0), ("113", VIEW, SEND))),
        ("333", (("112", VIEW, SEND), ("113", VIEW, SEND))),
        ("444", (("112", VIEW, SEND), ("113", VIEW | SEND, 0))),
    ):
        cells = [(GUILD, 0, VIEW | SEND), *grants]
        channels.append(
            client._parse_channel(
                {
                    "id": sid,
                    "name": prefix + sid,
                    "type": 0,
                    "permission_overwrites": [
                        {"id": role, "type": 0, "allow": str(allow), "deny": str(deny)}
                        for role, allow, deny in cells
                    ],
                }
            )
        )
        document = {
            "guild": {"id": GUILD, "name": "Synthetic permission proof"},
            "channel": {"id": sid, "name": prefix + sid, "type": 0},
            "messageCount": 1,
            "messages": [
                {
                    "id": "9" + sid,
                    "type": "Default",
                    "timestamp": "2026-10-21T00:00:00+00:00",
                    "content": "Synthetic permission proof",
                    "author": {
                        "id": "115",
                        "name": "Synthetic",
                        "roles": [
                            {"id": role.id, "name": role.name, "position": role.position}
                            for role in roles
                            if role.id != GUILD
                        ],
                    },
                }
            ],
        }
        atomic_write_text(exports / (sid + ".json"), json.dumps(document))
    facts = capture_permission_source(GUILD, roles, channels)
    metadata = DiscordMetadata(
        guild_id=GUILD,
        fetched_at="2026-10-21T00:00:00+00:00",
        server_default_permissions=0,
        role_permissions={
            role.id: PermissionPair(translate_permissions(role.permissions), 0)
            for role in roles
            if role.id != GUILD
        },
        role_metadata={
            role.id: RoleMeta(position=role.position, name=role.name)
            for role in roles
            if role.id != GUILD
        },
        channel_metadata={
            channel.id: ChannelMeta(
                False,
                default_override=PermissionPair(
                    0, translate_permissions(VIEW | SEND, in_overwrite=True)
                ),
                role_overrides=[
                    RoleOverride(
                        cell.id,
                        translate_permissions(cell.allow, in_overwrite=True),
                        translate_permissions(cell.deny, in_overwrite=True),
                    )
                    for cell in channel.permission_overwrites
                    if cell.id != GUILD
                ],
            )
            for channel in channels
        },
        permission_source=facts,
    )
    save_discord_metadata(metadata, output)
    return metadata, exports, roles, channels


async def run_live(
    settings: dict[str, Any],
    owner_token: str,
    observer_token: str,
    work: Path,
    evidence: Path,
    result: dict[str, Any],
) -> None:
    """Run actual application owners, then observe access as an ordinary test member."""
    base, server_id = settings["api_base"], settings["server_id"]
    trace = aiohttp.TraceConfig()

    async def started(session: aiohttp.ClientSession, context: Any, params: Any) -> None:
        if params.method not in {"GET", "HEAD"}:
            if not str(params.url).startswith(base + "/"):
                raise ProofRefusedError
            result["target_write_attempts"] += 1
            write_evidence(evidence, result)

    async def ended(session: aiohttp.ClientSession, context: Any, params: Any) -> None:
        if params.method not in {"GET", "HEAD"} and 200 <= params.response.status < 300:
            result["target_writes_acknowledged"] += 1

    trace.on_request_start.append(started)
    trace.on_request_end.append(ended)

    def traced_session(**kwargs: Any) -> aiohttp.ClientSession:
        kwargs["trace_configs"] = [*kwargs.get("trace_configs", ()), trace]
        return new_session(**kwargs)

    async with AsyncExitStack() as stack:
        # Observe sessions created by migration without changing their owner,
        # request implementation, timeout defaults or lifecycle.
        stack.enter_context(patch("discord_ferry.core.engine.new_session", traced_session))
        session = await stack.enter_async_context(
            traced_session(timeout=aiohttp.ClientTimeout(total=30))
        )

        async def request(
            method: str,
            path: str,
            token: str = owner_token,
            body: dict[str, Any] | None = None,
        ) -> tuple[int, Any]:
            for attempt in range(3):
                async with session.request(
                    method,
                    base + path,
                    headers={"x-session-token": token},
                    json=body,
                    allow_redirects=False,
                ) as response:
                    if response.status == 429 and attempt < 2:
                        delay = await _resolve_429_delay_seconds(response)
                    else:
                        data = await response.json() if response.status != 204 else {}
                        return response.status, data
                # Only explicit rate rejection is retried. An uncertain send
                # never gets a new request from this observation helper.
                await asyncio.sleep(delay)
            raise ProofRefusedError

        async def read(path: str, token: str = owner_token) -> dict[str, Any]:
            status, data = await request("GET", path, token)
            if status != 200 or not isinstance(data, dict):
                raise ProofRefusedError
            return data

        root = await read("/")
        result["stage"] = "authentication"
        owner = await read("/users/@me")
        observer = await read("/users/@me", observer_token)
        server = await read("/servers/" + server_id)
        if (
            root.get("revolt") != settings["backend_version"]
            or server.get("_id") != server_id
            or server.get("owner") != owner.get("_id")
            or not is_stoat_id(observer.get("_id", ""))
            or observer["_id"] == owner.get("_id")
        ):
            raise ProofRefusedError
        member_path = f"/servers/{server_id}/members/{observer['_id']}"
        member = await read(member_path)
        if member.get("roles", []) != [] or member.get("timeout"):
            raise ProofRefusedError
        await read("/servers/" + server_id, observer_token)
        result.update(
            backend_version=root["revolt"],
            server_id=server_id,
            observer_is_non_owner=True,
            stage="migration",
        )
        prefix = "ferry-proof-" + uuid.uuid4().hex[:6] + "-"
        metadata, export_dir, raw_roles, raw_channels = synthetic_source(work, prefix)
        config = FerryConfig(
            export_dir=export_dir,
            output_dir=work / "output",
            stoat_url=base,
            token=owner_token,
            server_id=server_id,
            discord_server_id=GUILD,
            skip_export=True,
            skip_avatars=True,
            skip_emoji=True,
            skip_reactions=True,
            create_invite=False,
            session=session,
        )
        state = await run_migration(config, lambda event: None)
        if (
            state.permission_runtime is None
            or not state.permission_runtime.outcome.verified
            or state.failed_messages
        ):
            raise ProofRefusedError
        result["created_channels"] = dict(state.channel_map)
        result["created_roles"] = dict(state.role_map)
        assert metadata.permission_source is not None
        result["source_fingerprint"] = metadata.permission_source.fingerprint
        result["source_inputs"] = {
            "guild_id": GUILD,
            "roles": [
                {
                    "id": role.id,
                    "permissions": role.permissions,
                    "position": role.position,
                    "managed": role.managed,
                }
                for role in raw_roles
            ],
            "channels": [
                {
                    "id": channel.id,
                    "type": channel.type,
                    "overwrites": [
                        {"role_id": cell.id, "allow": cell.allow, "deny": cell.deny}
                        for cell in channel.permission_overwrites
                    ],
                }
                for channel in raw_channels
            ],
        }
        result["observations"] = []
        changed_member = False

        async def assign(assigned: frozenset[str]) -> None:
            nonlocal changed_member
            changed_member = True
            roles = [state.role_map[sid] for sid in sorted(assigned)]
            status, actual = await request("PATCH", member_path, body={"roles": roles})
            if status != 200 or set(actual.get("roles", [])) != set(roles):
                raise ProofRefusedError

        async def observe(label: str) -> None:
            for assigned in (
                frozenset(),
                frozenset({"112"}),
                frozenset({"113"}),
                frozenset({"112", "113"}),
                frozenset({"114"}),
            ):
                await assign(assigned)
                for sid, cid in state.channel_map.items():
                    expected_view, expected_send = source_access(
                        metadata.permission_source, assigned, sid
                    )
                    view_status, _ = await request("GET", "/channels/" + cid, observer_token)
                    send_status, _ = await request(
                        "POST",
                        f"/channels/{cid}/messages",
                        observer_token,
                        {"content": "Synthetic access probe"},
                    )
                    observed_view = view_status == 200
                    observed_send = send_status in {200, 201}
                    valid = (
                        view_status in {200, 403, 404}
                        and send_status in {200, 201, 403, 404}
                        and (expected_view, expected_send) == (observed_view, observed_send)
                    )
                    result["observations"].append(
                        {
                            "stage": label,
                            "source_channel_id": sid,
                            "source_roles": sorted(assigned),
                            "expected_view": expected_view,
                            "expected_send": expected_send,
                            "view_status": view_status,
                            "send_status": send_status,
                            "passed": valid,
                        }
                    )
                    write_evidence(evidence, result)
                    if not valid:
                        raise ProofRefusedError

        try:
            await observe("initial")
            await assign(frozenset())
            result["stage"] = "recreation"
            # These are deliberate recreation scenarios, not cleanup. Delete
            # only identities this invocation just created, with exact name binding.
            live = await read("/servers/" + server_id)
            rid, cid = state.role_map["112"], state.channel_map["333"]
            channel = await read("/channels/" + cid)
            if (
                live.get("roles", {}).get(rid, {}).get("name") != prefix + "112"
                or channel.get("name") != prefix + "333"
                or channel.get("server") != server_id
            ):
                raise ProofRefusedError
            await api_delete_role(session, base, owner_token, server_id, rid)
            await api_delete_channel(session, base, owner_token, cid)
            exports = parse_export_directory(export_dir, metadata_only=True)
            repaired = await run_repair(config, state, exports, lambda event: None, session=session)
            if (
                not repaired.permissions.verified
                or len(repaired.recreated_roles) != 1
                or len(repaired.recreated_channels) != 1
                or state.failed_messages
            ):
                raise ProofRefusedError
            await observe("recreated")
            await assign(frozenset())
            result["stage"] = "refusal-controls"
            controls = []
            original = metadata.permission_source
            for kind in ("unverified", "unsupported"):
                metadata.permission_source = None
                if kind == "unsupported":
                    member_channel = replace(
                        raw_channels[0],
                        permission_overwrites=[
                            *raw_channels[0].permission_overwrites,
                            PermissionOverwrite("115", 1, VIEW, 0),
                        ],
                    )
                    metadata.permission_source = capture_permission_source(
                        GUILD, raw_roles, [member_channel, *raw_channels[1:]]
                    )
                save_discord_metadata(metadata, config.output_dir)
                before = result["target_write_attempts"]
                try:
                    await run_migration(replace(config, incremental=True), lambda event: None)
                except PermissionSafetyError as exc:
                    if exc.outcome.reason != kind:
                        raise ProofRefusedError from None
                else:
                    raise ProofRefusedError
                controls.append(
                    {
                        "kind": kind,
                        "target_write_attempts": result["target_write_attempts"] - before,
                    }
                )
                if result["target_write_attempts"] != before:
                    raise ProofRefusedError
            metadata.permission_source = original
            save_discord_metadata(metadata, config.output_dir)
            # Reverse the raw ranks without a destination edit. Either exact
            # synthesis succeeds through the owner or strict preflight refuses.
            reversed_roles = [
                replace(role, position=3 - role.position) if role.id in {"112", "113"} else role
                for role in raw_roles
            ]
            metadata.permission_source = capture_permission_source(
                GUILD, reversed_roles, raw_channels
            )
            for role in reversed_roles:
                if role.id in metadata.role_metadata:
                    metadata.role_metadata[role.id].position = role.position
            save_discord_metadata(metadata, config.output_dir)
            before = result["target_write_attempts"]
            try:
                await run_permission_role_backfill(config, state, exports, lambda event: None)
            except PermissionSafetyError:
                if result["target_write_attempts"] != before:
                    raise ProofRefusedError from None
                result["opposite_rank"] = {"status": "refused", "target_write_attempts": 0}
            else:
                await run_repair(config, state, exports, lambda event: None, session=session)
                await observe("opposite-rank")
                result["opposite_rank"] = {"status": "proved"}
            result["refusal_controls"] = controls
        finally:
            if changed_member:
                status, member = await request("PATCH", member_path, body={"roles": []})
                result["observer_roles_restored"] = status == 200 and not member.get("roles")
                if not result["observer_roles_restored"]:
                    raise ProofRefusedError
        if result["target_write_attempts"] <= 0 or not result["observations"]:
            raise ProofRefusedError
        result.update(status="passed", stage="complete")


def main(argv: list[str] | None = None) -> int:
    """Require explicit prerequisites and retain unavailable/failed evidence as nonzero."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args(argv)
    evidence = args.evidence
    if evidence.exists() or evidence.is_symlink():
        print("Evidence already exists. Inspect it; do not replay this attempt.")
        return 1
    result: dict[str, Any] = {
        "schema": "ferry.permission-fidelity/v1",
        "status": "unavailable",
        "stage": "prerequisites",
        "target_write_attempts": 0,
        "target_writes_acknowledged": 0,
        "forum_correction_issue": 1217,
        "real_dce_forum_index_support": False,
        "automatic_resource_cleanup": False,
    }
    try:
        evidence.parent.mkdir(parents=True, exist_ok=True)
        write_evidence(evidence, result)
        selected = os.environ.get("FERRY_PERMISSION_TEST_TARGET")
        if not selected:
            return 1
        settings = settings_from_file(selected)
        result.update(
            candidate_identity(
                Path(__file__).resolve().parents[1], Path(settings["candidate_manifest"])
            )
        )
        owner_token = credential(settings["owner_credential"])
        observer_token = credential(settings["observer_credential"])
        if owner_token == observer_token:
            raise ProofRefusedError
        work = evidence.with_name(evidence.stem + "-work")
        work.mkdir(mode=0o700)
        package = Path(__file__).resolve().parents[1] / "src" / "discord_ferry"
        source_hash = hashlib.sha256()
        for path in sorted(package.rglob("*.py")):
            source_hash.update(str(path.relative_to(package)).encode() + b"\0" + path.read_bytes())
        result["application_source_sha256"] = source_hash.hexdigest()
        result["harness_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        result["status"] = "failed"
        asyncio.run(run_live(settings, owner_token, observer_token, work, evidence, result))
    except PermissionSafetyError as exc:
        result["status"] = "failed" if result["target_write_attempts"] else "unavailable"
        result["permission_outcome"] = {
            "reason": exc.outcome.reason,
            "stage": exc.outcome.stage,
            "effects": exc.outcome.effects,
        }
    except Exception:
        # Deliberately exclude exception messages and response bodies from this artifact.
        result["status"] = "failed" if result["target_write_attempts"] else "unavailable"
    finally:
        if evidence.parent.exists():
            write_evidence(evidence, result)
    print("Permission fidelity: " + result["status"])
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
