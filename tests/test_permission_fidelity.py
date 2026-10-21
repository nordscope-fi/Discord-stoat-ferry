"""Compose actual permission callers with controlled HTTP and independent access arithmetic."""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import re
import runpy
import subprocess
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from aioresponses import CallbackResult, aioresponses

from discord_ferry import discord
from discord_ferry.core import engine
from discord_ferry.core.engine import run_migration, run_repair
from discord_ferry.core.http import new_session
from discord_ferry.discord import client
from discord_ferry.discord.metadata import (
    RoleOverride,
    capture_permission_source,
    load_discord_metadata,
    save_discord_metadata,
)
from discord_ferry.discord.permissions import translate_permissions
from discord_ferry.errors import PermissionSafetyError
from discord_ferry.parser.dce_parser import parse_export_directory
from discord_ferry.state import load_state, save_state
from tests.stoat_ids import stoat_id
from tests.test_core_permissions import source_oracle, target_truth
from tests.test_engine import _permission_preflight_case


def smoke_module():
    return runpy.run_path("scripts/permission-fidelity-smoke.py")


def test_live_harness_requires_explicit_prerequisites(tmp_path, monkeypatch):
    monkeypatch.delenv("FERRY_PERMISSION_TEST_TARGET", raising=False)
    evidence = tmp_path / "result.json"
    assert smoke_module()["main"](["--evidence", str(evidence)]) != 0
    result = json.loads(evidence.read_text())
    assert result["status"] == "unavailable"
    assert result["target_write_attempts"] == 0
    assert result["forum_correction_issue"] == 1217


@pytest.mark.parametrize(
    "change",
    [
        {"isolated_synthetic": False},
        {"api_base": "http://api.test"},
        {"api_base": "https://user:synthetic-secret@api.test"},
        {"backend_version": "unknown"},
        {"server_id": "not-a-server"},
        {"observer_credential": "Personal/STOAT_TOKEN/Secret"},
    ],
)
def test_live_harness_refuses_unsafe_selection_before_credentials(tmp_path, monkeypatch, change):
    settings = {
        "isolated_synthetic": True,
        "api_base": "https://api.test",
        "backend_version": "0.16.2",
        "server_id": stoat_id(31000),
        "owner_credential": "Personal/STOAT_TOKEN/Secret",
        "observer_credential": "Personal/STOAT_TEST_BASELINE/Secret",
        "candidate_manifest": "docs/plans/.review/explicit-candidate.json",
    }
    settings.update(change)
    selected = tmp_path / "target.json"
    selected.write_text(json.dumps(settings))
    monkeypatch.setenv("FERRY_PERMISSION_TEST_TARGET", str(selected))
    monkeypatch.setattr(
        "subprocess.run", lambda *args, **kwargs: pytest.fail("Unsafe target read a credential")
    )
    evidence = tmp_path / "result.json"
    module = smoke_module()
    monkeypatch.setitem(
        module["main"].__globals__,
        "candidate_identity",
        lambda *args: pytest.fail("Unsafe target reached candidate validation"),
    )
    assert module["main"](["--evidence", str(evidence)]) != 0
    result = json.loads(evidence.read_text())
    assert result["status"] == "unavailable"
    assert result["target_write_attempts"] == 0
    assert "synthetic-secret" not in evidence.read_text()


def test_live_harness_never_overwrites_previous_evidence(tmp_path, monkeypatch):
    monkeypatch.delenv("FERRY_PERMISSION_TEST_TARGET", raising=False)
    evidence = tmp_path / "result.json"
    evidence.write_text('{"preserve":"previous-attempt"}')
    before = evidence.read_bytes()
    assert smoke_module()["main"](["--evidence", str(evidence)]) != 0
    assert evidence.read_bytes() == before


def test_live_harness_requires_candidate_before_credential_access(tmp_path, monkeypatch):
    module = smoke_module()
    settings = {
        "isolated_synthetic": True,
        "api_base": "https://api.test",
        "backend_version": "0.16.2",
        "server_id": stoat_id(31000),
        "owner_credential": "Personal/STOAT_TOKEN/Secret",
        "observer_credential": "Personal/STOAT_TEST_BASELINE/Secret",
    }
    target = tmp_path / "target.json"
    target.write_text(json.dumps(settings))
    monkeypatch.setenv("FERRY_PERMISSION_TEST_TARGET", str(target))
    monkeypatch.setattr("shutil.which", lambda name: "pass-cli")
    monkeypatch.setattr(
        "subprocess.run", lambda *args, **kwargs: pytest.fail("Unbound candidate read credentials")
    )
    assert module["main"](["--evidence", str(tmp_path / "result.json")]) != 0


def test_live_harness_candidate_binding_reuses_snapshot_owner(tmp_path):
    from tests.test_chunk_snapshot import assert_fixture_ok

    module = smoke_module()
    binding = module.get("candidate_identity")
    assert callable(binding), "Live evidence must bind the actual captured candidate"
    assert_fixture_ok(tmp_path, "interruption")
    root = tmp_path / "repo"
    evidence_root = root / "docs/plans/.review/chunk-snapshots"
    manifest = evidence_root / "candidate.json"
    manifest.write_text(
        json.dumps(
            {
                "snapshots": [
                    "docs/plans/.review/chunk-snapshots/chunks/1001/snapshot.json",
                    "docs/plans/.review/chunk-snapshots/chunks/1010/snapshot.json",
                ]
            }
        )
    )
    identity = binding(root, manifest)
    last = json.loads((evidence_root / "chunks/1010/snapshot.json").read_text())
    assert identity["candidate_tree"] == last["ending_git_tree"]
    assert identity["candidate_snapshot_digest"] == last["digest_sha256"]
    assert identity["application_commit"] == last["starting_commit"]
    (root / "shared.txt").write_text("changed after capture\n")
    with pytest.raises(module["ProofRefusedError"]):
        binding(root, manifest)


def test_live_harness_candidate_binding_checks_existing_doc_links(tmp_path):
    from tests.test_chunk_snapshot import assert_fixture_ok

    assert_fixture_ok(tmp_path, "interruption")
    root = tmp_path / "repo"
    evidence = root / "docs/plans/.review/chunk-snapshots"
    snapshot = evidence / "chunks/1010/snapshot.json"
    record = json.loads(snapshot.read_text())
    subprocess.run(["git", "read-tree", record["ending_git_tree"]], cwd=root, check=True)
    (root / ".github").mkdir(exist_ok=True)
    (root / "CHANGELOG.md").write_text("Synthetic changes\n")
    (root / ".github/CONTRIBUTING.md").write_text("Synthetic contributing guide\n")
    links = {
        "docs/changelog.md": "../CHANGELOG.md",
        "docs/contributing.md": "../.github/CONTRIBUTING.md",
    }
    for name, target in links.items():
        (root / name).symlink_to(target)
    subprocess.run(
        ["git", "add", "CHANGELOG.md", ".github/CONTRIBUTING.md", *links], cwd=root, check=True
    )
    tree = subprocess.check_output(["git", "write-tree"], cwd=root, text=True).strip()
    record["ending_git_tree"] = tree
    del record["digest_sha256"]
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
    record["digest_sha256"] = hashlib.sha256(canonical.encode()).hexdigest()
    snapshot.write_text(json.dumps(record))
    manifest = evidence / "candidate.json"
    manifest.write_text(json.dumps({"snapshots": [str(snapshot.relative_to(root))]}))
    module = smoke_module()
    assert module["candidate_identity"](root, manifest)["candidate_tree"] == tree
    for name in links:
        path = root / name
        path.unlink()
        path.symlink_to("../shared.txt")
        with pytest.raises(module["ProofRefusedError"]):
            module["candidate_identity"](root, manifest)
        path.unlink()
        path.symlink_to(links[name])


def test_live_harness_independent_source_expectations(tmp_path):
    module = smoke_module()
    metadata, *_ = module["synthetic_source"](tmp_path, "ferry-test-")
    source = metadata.permission_source
    access = module["source_access"]
    assert access(source, frozenset(), "222") == (False, False)
    assert access(source, frozenset({"112"}), "222") == (True, True)
    assert access(source, frozenset({"113"}), "222") == (True, False)
    assert access(source, frozenset({"112", "113"}), "444") == (True, True)
    assert access(source, frozenset({"112"}), "333") == (True, False)
    assert all(
        access(source, frozenset({"114"}), cid) == (True, True) for cid in ("222", "333", "444")
    )


class ControlledDestination:
    """Replace HTTP only; retain the payloads and reads made by real application owners."""

    def __init__(self):
        self.server_id, self.user_id = stoat_id(31000), stoat_id(31001)
        self.server = {
            "_id": self.server_id,
            "owner": self.user_id,
            "name": "Synthetic destination",
            "description": "",
            "default_permissions": 0,
            "roles": {},
            "channels": [],
            "categories": [],
        }
        self.channels = {}
        self.messages = {}
        self.requests = []
        self.unknown = []
        self.next_id = 31100
        self.reject_permission_write = None
        self.permission_writes = 0
        self.reject_content = False
        self.observe_request = None

    def identity(self):
        self.next_id += 1
        return stoat_id(self.next_id)

    def install(self, remote):
        for method in ("get", "post", "put", "patch", "delete"):
            verb = method.upper()
            getattr(remote, method)(
                re.compile(r"^https://api\.test/.*$"),
                callback=lambda url, method=verb, **kwargs: self.call(method, url, kwargs),
                repeat=True,
            )

    def call(self, method, url, kwargs):
        parsed = urlsplit(str(url))
        path, query = parsed.path, parse_qs(parsed.query)
        body = copy.deepcopy(kwargs.get("json") or {})
        self.requests.append((method, path, body))
        if self.observe_request is not None:
            self.observe_request(method, path, body)
        if method == "PUT" and "/permissions/" in path:
            self.permission_writes += 1
            if self.permission_writes == self.reject_permission_write:
                return CallbackResult(status=403, payload={"type": "MissingPermission"})
        result = None
        status = 200
        if method == "GET" and path == "/":
            result = {
                "revolt": "0.15.7",
                "features": {
                    "autumn": {"url": "https://autumn.test"},
                    "limits": {"global": {"server_roles": 200}},
                },
            }
        elif method == "GET" and path == "/users/@me":
            result = {"_id": self.user_id}
        elif method == "GET" and path == f"/servers/{self.server_id}/emojis":
            result = []
        elif path == f"/servers/{self.server_id}":
            if method == "GET":
                result = copy.deepcopy(self.server)
                if query.get("include_channels") == ["true"]:
                    result["channels"] = list(copy.deepcopy(self.channels).values())
            elif method == "PATCH":
                self.server.update(body)
                result = self.server
        elif path == f"/servers/{self.server_id}/roles" and method == "POST":
            identity = self.identity()
            role = {
                "_id": identity,
                "name": body["name"],
                "rank": len(self.server["roles"]),
                "permissions": {"a": 0, "d": 0},
            }
            self.server["roles"][identity] = role
            result = {"id": identity, "role": role}
        elif path == f"/servers/{self.server_id}/roles/ranks" and method == "PATCH":
            assert set(body["ranks"]) == set(self.server["roles"])
            for rank, identity in enumerate(body["ranks"]):
                self.server["roles"][identity]["rank"] = rank
            result = self.server
        elif path.startswith(f"/servers/{self.server_id}/roles/") and method == "PATCH":
            role = self.server["roles"][path.rsplit("/", 1)[1]]
            role.update(body)
            result = role
        elif path.startswith(f"/servers/{self.server_id}/permissions/") and method == "PUT":
            identity = path.rsplit("/", 1)[1]
            if identity == "default":
                self.server["default_permissions"] = body["permissions"]
            else:
                pair = body["permissions"]
                self.server["roles"][identity]["permissions"] = {
                    "a": pair["allow"],
                    "d": pair["deny"],
                }
            result = {}
        elif path == f"/servers/{self.server_id}/channels" and method == "POST":
            identity = self.identity()
            channel = {
                "_id": identity,
                "server": self.server_id,
                "name": body["name"],
                "channel_type": "TextChannel",
                "default_permissions": {"a": 0, "d": 0},
                "role_permissions": {},
            }
            self.channels[identity] = channel
            self.server["channels"].append(identity)
            result = channel
        elif path.startswith("/channels/"):
            parts = path.strip("/").split("/")
            identity = parts[1]
            channel = self.channels.get(identity)
            if channel is None:
                return CallbackResult(status=404, payload={"type": "NotFound"})
            if len(parts) == 2 and method == "GET":
                result = channel
            elif len(parts) == 2 and method == "PATCH":
                channel.update(body)
                result = channel
            elif len(parts) == 4 and parts[2] == "permissions" and method == "PUT":
                pair = {"a": body["permissions"]["allow"], "d": body["permissions"]["deny"]}
                if parts[3] == "default":
                    channel["default_permissions"] = pair
                else:
                    channel["role_permissions"][parts[3]] = pair
                result = {}
            elif len(parts) == 3 and parts[2] == "messages" and method == "POST":
                if self.reject_content:
                    return CallbackResult(
                        status=403,
                        payload={"type": "MissingPermission", "permission": "SendMessage"},
                    )
                message = {
                    "_id": self.identity(),
                    "channel": identity,
                    "author": self.user_id,
                    **body,
                }
                self.messages[message["_id"]] = message
                result = message
            elif (
                len(parts) == 3 and parts[2] in {"messages", "search"} and method in {"GET", "POST"}
            ):
                result = list(
                    reversed([m for m in self.messages.values() if m["channel"] == identity])
                )
            elif len(parts) == 4 and parts[2] == "messages" and method == "GET":
                result = self.messages.get(parts[3])
                status = 200 if result is not None else 404
                result = result or {"type": "NotFound"}
            elif len(parts) == 4 and parts[2] == "messages" and method == "PATCH":
                self.messages[parts[3]].update(body)
                result = self.messages[parts[3]]
            elif (
                len(parts) == 5
                and parts[2] == "messages"
                and parts[4] == "pin"
                and method == "POST"
            ):
                self.messages[parts[3]]["pinned"] = True
                result = {}
        if result is None:
            self.unknown.append((method, path))
            return CallbackResult(status=404, payload={"type": "NotFound"})
        return CallbackResult(status=status, payload=copy.deepcopy(result))


def integration_context(tmp_path, *, common=False):
    output = tmp_path / "output"
    exports = tmp_path / "exports"
    exports.mkdir()
    config, _, metadata = _permission_preflight_case(
        output,
        source_ids=("222", "333"),
        ordinary=(("112", (1 << 10) | (1 << 11), 1), ("113", 1 << 10, 2), ("114", 8, 3)),
    )
    for identity, role in metadata.role_metadata.items():
        role.name = "Synthetic " + identity
    raw_roles = [
        client._parse_role(
            {
                "id": role.id,
                "name": role.id,
                "permissions": str(role.permissions),
                "position": role.position,
                "managed": role.managed,
            }
        )
        for role in metadata.permission_source.roles
    ]
    overrides = [
        {"id": "112", "type": 0, "allow": "0", "deny": str(1 << 11)},
        {"id": "113", "type": 0, "allow": str(1 << 11), "deny": "0"},
    ]
    raw_channels = [
        client._parse_channel(
            {
                "id": source,
                "name": source,
                "type": 0,
                "permission_overwrites": overrides if source == "222" or common else [],
            }
        )
        for source in ("222", "333")
    ]
    metadata.permission_source = capture_permission_source("111", raw_roles, raw_channels)
    metadata.channel_metadata["222"].role_overrides = [
        RoleOverride("112", 0, translate_permissions(1 << 11, in_overwrite=True)),
        RoleOverride("113", translate_permissions(1 << 11, in_overwrite=True), 0),
    ]
    metadata.channel_metadata["333"].role_overrides = []
    if common:
        metadata.channel_metadata["333"].role_overrides = copy.deepcopy(
            metadata.channel_metadata["222"].role_overrides
        )
    save_discord_metadata(metadata, output)
    fixture = json.loads((Path(__file__).parent / "fixtures" / "simple_channel.json").read_text())
    for index, source in enumerate(("222", "333")):
        data = copy.deepcopy(fixture)
        data["guild"] = {"id": "111", "name": "Synthetic source"}
        data["channel"].update(id=source, name="selected-" + source, categoryId=None, category=None)
        message = data["messages"][0]
        message.update(id=str(401 + index), content="Synthetic integration content")
        message["author"]["roles"] = [
            {"id": identity, "name": role.name, "position": role.position}
            for identity, role in metadata.role_metadata.items()
        ]
        data["messages"] = [message]
        (exports / (source + ".json")).write_text(json.dumps(data))
    destination = ControlledDestination()
    config = dataclasses.replace(
        config,
        export_dir=exports,
        server_id=destination.server_id,
        skip_avatars=True,
        skip_emoji=True,
        skip_reactions=True,
        create_invite=False,
        message_rate_limit=0,
        upload_delay=0,
    )
    return config, metadata, destination


@pytest.mark.parametrize("wrong_observer_access", [False, True])
async def test_live_harness_composes_real_owners_and_rejects_false_access(
    tmp_path, monkeypatch, wrong_observer_access
):
    module = smoke_module()
    destination = ControlledDestination()
    observer_id = stoat_id(31002)
    member_roles = []
    rate_limited_member_edit = False
    traces = []
    created_sessions = []
    intercepted_writes = []

    def traced_session(**kwargs):
        session = new_session(**kwargs)
        created_sessions.append(session)
        for trace in session.trace_configs:
            if trace not in traces:
                traces.append(trace)
        return session

    monkeypatch.setitem(module["run_live"].__globals__, "new_session", traced_session)
    monkeypatch.setattr("discord_ferry.core.engine.new_session", traced_session)
    result = {
        "status": "failed",
        "target_write_attempts": 0,
        "target_writes_acknowledged": 0,
        "forum_correction_issue": 1217,
        "real_dce_forum_index_support": False,
    }
    settings = {
        "api_base": "https://api.test",
        "server_id": destination.server_id,
        "backend_version": "0.16.2",
    }
    member_path = f"/servers/{destination.server_id}/members/{observer_id}"

    def observed_permissions(cid):
        channel = destination.channels[cid]
        value = destination.server["default_permissions"]
        ordered = sorted(member_roles, key=lambda rid: -destination.server["roles"][rid]["rank"])
        for rid in ordered:
            pair = destination.server["roles"][rid]["permissions"]
            value = (value | pair["a"]) & ~pair["d"]
        pair = channel["default_permissions"]
        value = (value | pair["a"]) & ~pair["d"]
        for rid in ordered:
            pair = channel["role_permissions"].get(rid, {"a": 0, "d": 0})
            value = (value | pair["a"]) & ~pair["d"]
        return value

    async def transport(url, method, **kwargs):
        nonlocal member_roles, rate_limited_member_edit
        for trace in traces:
            for handler in trace.on_request_start:
                await handler(None, None, SimpleNamespace(method=method, url=url))
        path = urlsplit(str(url)).path
        body = kwargs.get("json") or {}
        observer = kwargs.get("headers", {}).get("x-session-token") == "synthetic-observer"
        reply = None
        if path == "/users/@me" and observer:
            reply = CallbackResult(payload={"_id": observer_id})
        elif path == member_path:
            if method == "PATCH" and not wrong_observer_access and not rate_limited_member_edit:
                rate_limited_member_edit = True
                reply = CallbackResult(
                    status=429, headers={"X-RateLimit-Reset-After": "1"}, payload={}
                )
            elif method == "PATCH":
                member_roles = list(body["roles"])
            if reply is None:
                reply = CallbackResult(payload={"roles": member_roles})
        elif path.startswith("/channels/") and observer:
            cid = path.split("/")[2]
            value = observed_permissions(cid)
            can_view = bool(value & (1 << 20)) or wrong_observer_access
            can_send = can_view and bool(value & (1 << 22))
            if method == "GET":
                reply = CallbackResult(status=200 if can_view else 403, payload={})
            elif not can_send:
                reply = CallbackResult(
                    status=403, payload={"type": "MissingPermission", "permission": "SendMessage"}
                )
        elif path.startswith(f"/servers/{destination.server_id}/roles/") and method == "DELETE":
            del destination.server["roles"][path.rsplit("/", 1)[1]]
            reply = CallbackResult(status=204)
        elif path.startswith("/channels/") and method == "DELETE":
            cid = path.rsplit("/", 1)[1]
            del destination.channels[cid]
            destination.server["channels"].remove(cid)
            reply = CallbackResult(status=204)
        if reply is None:
            reply = destination.call(method, url, kwargs)
            if path == "/" and method == "GET":
                reply.payload["revolt"] = "0.16.2"
        # aioresponses bypasses aiohttp signals. Constructor observations below
        # independently require the real trace on every owner-created session.
        if method not in {"GET", "HEAD"}:
            intercepted_writes.append((method, path))
        for trace in traces:
            for handler in trace.on_request_end:
                await handler(
                    None,
                    None,
                    SimpleNamespace(
                        method=method, url=url, response=SimpleNamespace(status=reply.status or 200)
                    ),
                )
        return reply

    with aioresponses() as remote:
        for method in ("get", "post", "put", "patch", "delete"):
            verb = method.upper()

            async def callback(url, verb=verb, **kwargs):
                return await transport(url, verb, **kwargs)

            getattr(remote, method)(
                re.compile(r"^https://api\.test/.*$"), callback=callback, repeat=True
            )
        evidence = tmp_path / "result.json"
        if wrong_observer_access:
            with pytest.raises(module["ProofRefusedError"]):
                await module["run_live"](
                    settings, "synthetic-owner", "synthetic-observer", tmp_path, evidence, result
                )
            assert result["status"] != "passed"
        else:
            await module["run_live"](
                settings, "synthetic-owner", "synthetic-observer", tmp_path, evidence, result
            )
            assert result["status"] == "passed"
            assert len(result["observations"]) >= 30
            assert all(row["passed"] for row in result["observations"])
            assert result["refusal_controls"] == [
                {"kind": "unverified", "target_write_attempts": 0},
                {"kind": "unsupported", "target_write_attempts": 0},
            ]
            assert result["source_inputs"]["guild_id"] == "111"
            assert len(result["source_inputs"]["roles"]) == 4
            assert len(result["source_inputs"]["channels"]) == 3
        assert result["observer_roles_restored"] and not member_roles
        assert result["target_write_attempts"] == len(intercepted_writes)
        assert len(created_sessions) >= 2
        assert all(session.trace_configs == traces for session in created_sessions)
        assert engine.new_session is traced_session
    assert not destination.unknown
    assert "synthetic-owner" not in evidence.read_text()
    assert "synthetic-observer" not in evidence.read_text()


def assert_independent_access(metadata, destination, state):
    """Compare raw source truth with actual outgoing target cells, not compiler results."""
    source = metadata.permission_source
    identities = tuple(state.role_map)
    ordered = sorted(
        identities, key=lambda sid: -destination.server["roles"][state.role_map[sid]]["rank"]
    )
    for channel in source.channels:
        observed = destination.channels[state.channel_map[channel.id]]
        facts = SimpleNamespace(guild_id=source.guild_id, roles=source.roles, channels=(channel,))
        for source_bit in (10, 11):
            mask = translate_permissions(1 << source_bit)
            base = bool(destination.server["default_permissions"] & mask)
            pair = observed["default_permissions"]
            grants = sum(
                1 << index
                for index, identity in enumerate(ordered)
                if destination.server["roles"][state.role_map[identity]]["permissions"]["a"] & mask
            )
            assert all(
                not destination.server["roles"][state.role_map[identity]]["permissions"]["d"] & mask
                for identity in ordered
            )
            if pair["a"] & mask:
                base = True
            elif pair["d"] & mask:
                base, grants = False, 0
            operations = []
            for identity in ordered:
                cell = observed["role_permissions"].get(state.role_map[identity], {"a": 0, "d": 0})
                operations.append(True if cell["a"] & mask else False if cell["d"] & mask else None)
            actual = target_truth(tuple(operations), grants, base)
            for member_bits in range(1 << len(ordered)):
                members = frozenset(
                    identity for index, identity in enumerate(ordered) if member_bits & (1 << index)
                )
                assert actual[member_bits] == source_oracle(facts, members, source_bit)


async def test_integration_capture_create_and_deliver_through_actual_owners(tmp_path):
    config, metadata, destination = integration_context(tmp_path)
    assert load_discord_metadata(config.output_dir) == metadata
    events = []
    with aioresponses() as remote:
        destination.install(remote)
        state = await run_migration(config, events.append)
    assert not destination.unknown
    assert len(state.message_map) == 2
    assert not state.failed_messages
    assert len(destination.server["roles"]) == 3
    assert_independent_access(metadata, destination, state)
    assert state.permission_runtime.outcome.verified
    assert load_state(config.output_dir).permission_runtime is None
    writes = [
        (index, path)
        for index, (method, path, _) in enumerate(destination.requests)
        if method != "GET"
    ]
    first_content = next(index for index, path in writes if path.endswith("/messages"))
    assert any(
        method == "GET" and path.startswith("/channels/")
        for method, path, _ in destination.requests[:first_content]
    )
    assert any(event.phase == "report" and event.status == "completed" for event in events)


async def test_integration_legacy_source_refresh_is_local_before_explicit_repair(tmp_path):
    config, metadata, destination = integration_context(tmp_path)
    facts = metadata.permission_source
    with aioresponses() as remote:
        destination.install(remote)
        state = await run_migration(config, lambda event: None)
        recorded = dict(state.message_map)
        roles_before = dict(state.role_map)
        channels_before = dict(state.channel_map)
        metadata.permission_source = None
        save_discord_metadata(metadata, config.output_dir)
        destination.requests.clear()
        with pytest.raises(PermissionSafetyError):
            await run_migration(dataclasses.replace(config, incremental=True), lambda event: None)
        exports = parse_export_directory(config.export_dir, metadata_only=True)
        refused = await run_repair(config, state, exports, lambda event: None)
        assert not refused.permissions.verified
        assert all(method == "GET" for method, _, _ in destination.requests)
        before = {
            str(path.relative_to(config.output_dir)): path.read_bytes()
            for path in config.output_dir.rglob("*")
            if path.is_file()
        }
        base = f"{client.DISCORD_API}/guilds/111"
        remote.get(base, payload={"id": "111", "name": "Synthetic source"})
        remote.get(
            base + "/roles",
            payload=[
                {
                    "id": role.id,
                    "name": metadata.role_metadata[role.id].name
                    if role.id in metadata.role_metadata
                    else "everyone",
                    "permissions": str(role.permissions),
                    "position": role.position,
                    "managed": role.managed,
                }
                for role in facts.roles
            ],
        )
        remote.get(
            base + "/channels",
            payload=[
                {
                    "id": channel.id,
                    "name": "selected-" + channel.id,
                    "type": channel.type,
                    "permission_overwrites": [
                        {
                            "id": cell.role_id,
                            "type": 0,
                            "allow": str(cell.allow),
                            "deny": str(cell.deny),
                        }
                        for cell in channel.overwrites
                    ],
                }
                for channel in facts.channels
            ],
        )
        destination.requests.clear()
        async with new_session() as session:
            refreshed = await discord.refresh_permission_metadata(
                session, "synthetic-source-refresh", "111", exports, config.output_dir
            )
        assert not destination.requests
        after = {
            str(path.relative_to(config.output_dir)): path.read_bytes()
            for path in config.output_dir.rglob("*")
            if path.is_file()
        }
        assert after.pop("discord_metadata.json") != before.pop("discord_metadata.json")
        assert after == before
        repaired = await run_repair(config, state, exports, lambda event: None)
        assert repaired.permissions.verified
        assert state.role_map == roles_before
        assert state.channel_map == channels_before
        assert state.message_map == recorded
        assert len(destination.messages) == 2
        assert_independent_access(refreshed, destination, state)
    assert not destination.unknown


async def test_integration_incremental_reuse_and_stale_index_scope_refusal(
    tmp_path,
):
    config, metadata, destination = integration_context(tmp_path, common=True)
    with aioresponses() as remote:
        destination.install(remote)
        state = await run_migration(config, lambda event: None)
        before = dict(state.message_map)
        source_path = config.export_dir / "222.json"
        data = json.loads(source_path.read_text())
        added = copy.deepcopy(data["messages"][0])
        added.update(id="403", content="Synthetic incremental content")
        data["messages"].append(added)
        source_path.write_text(json.dumps(data))
        destination.requests.clear()
        incremental = dataclasses.replace(config, incremental=True)
        repeated = await run_migration(incremental, lambda event: None)
        assert {key: repeated.message_map[key] for key in before} == before
        assert len(repeated.message_map) == 3
        assert repeated.permission_runtime.outcome.verified
        assert not any(method == "PUT" for method, _, _ in destination.requests)
        assert not any(
            method == "POST" and path.startswith("/servers/")
            for method, path, _ in destination.requests
        )
        assert_independent_access(metadata, destination, repeated)
        original = copy.deepcopy(destination.channels[state.channel_map["222"]])
        changed_cell = destination.channels[state.channel_map["222"]]["role_permissions"][
            state.role_map["112"]
        ]
        view = translate_permissions(1 << 10)
        changed_cell["a"] &= ~view
        changed_cell["d"] |= view
        destination.requests.clear()
        with pytest.raises(PermissionSafetyError) as mismatch:
            await run_migration(incremental, lambda event: None)
        assert mismatch.value.outcome.effects == "none_confirmed"
        assert all(method == "GET" for method, _, _ in destination.requests)
        destination.channels[state.channel_map["222"]] = original
        changed = load_state(config.output_dir)
        group, index_key = "forum-combined", "forum-index-forum-combined"
        changed.channel_categories = {source: group for source in ("222", "333")}
        changed.forum_channel_members[group] = ["222", "333"]
        changed.forum_category_names[group] = "Combined"
        changed.channel_map[index_key] = stoat_id(31501)
        changed.created_channel_names[index_key] = "Combined index"
        save_state(changed, config.output_dir)
        destination.requests.clear()
        with pytest.raises(PermissionSafetyError) as missing:
            await run_migration(incremental, lambda event: None)
        assert missing.value.outcome.effects == "none_confirmed"
        assert all(method == "GET" for method, _, _ in destination.requests)
    assert not destination.unknown
    assert len(destination.messages) == 3


async def test_integration_partial_write_reload_refuses_then_explicit_repair_delivers(tmp_path):
    config, metadata, destination = integration_context(tmp_path)
    with aioresponses() as remote:
        destination.install(remote)
        completed = await run_migration(config, lambda event: None)
        receipts = dict(completed.message_map)
        exports = parse_export_directory(config.export_dir, metadata_only=True)
        for channel in destination.channels.values():
            channel["role_permissions"] = {}
        destination.permission_writes = 0
        destination.reject_permission_write = 2
        partial = await run_repair(config, completed, exports, lambda event: None)
        assert partial.permissions.effects == "changes_possible"
        assert not partial.permissions.verified
        assert destination.permission_writes == 2
        assert len(destination.messages) == 2
        interrupted = load_state(config.output_dir)
        assert interrupted.permission_runtime is None
        identities = dict(interrupted.channel_map)
        destination.reject_permission_write = None
        destination.requests.clear()
        with pytest.raises(PermissionSafetyError) as resume:
            await run_migration(config, lambda event: None)
        assert resume.value.outcome.effects == "none_confirmed"
        assert all(method == "GET" for method, _, _ in destination.requests)
        destination.requests.clear()
        repaired = await run_repair(config, interrupted, exports, lambda event: None)
        assert repaired.permissions.verified
        assert interrupted.channel_map == identities
        assert_independent_access(metadata, destination, interrupted)
        assert len(destination.messages) == 2
        source_path = config.export_dir / "222.json"
        data = json.loads(source_path.read_text())
        added = copy.deepcopy(data["messages"][0])
        added.update(id="403", content="Synthetic post-repair content")
        data["messages"].append(added)
        source_path.write_text(json.dumps(data))
        resumed = await run_migration(
            dataclasses.replace(config, incremental=True), lambda event: None
        )
        assert len(resumed.message_map) == 3
        assert {key: resumed.message_map[key] for key in receipts} == receipts
        assert not resumed.failed_messages
        assert_independent_access(metadata, destination, resumed)
    assert not destination.unknown


@pytest.mark.parametrize("external_rank_edit", [False, True])
async def test_integration_role_recreation_and_nested_retry_preserve_receipts(
    tmp_path, external_rank_edit
):
    config, metadata, destination = integration_context(tmp_path)
    destination.reject_content = True
    with aioresponses() as remote:
        destination.install(remote)
        state = await run_migration(config, lambda event: None)
        assert len(state.failed_messages) == 2
        assert not state.message_map
        assert not destination.messages
        old_role = state.role_map["112"]
        del destination.server["roles"][old_role]
        destination.reject_content = False
        destination.requests.clear()
        events = []
        authorities = []
        sent = []

        def observe(method, path, body):
            if method == "PUT" and "/permissions/" in path:
                authorities.append(state.permission_runtime)
            if method == "POST" and path.endswith("/messages"):
                authorities.append(state.permission_runtime)
                sent.append(path)
                if external_rank_edit:
                    first = destination.server["roles"][state.role_map["112"]]
                    second = destination.server["roles"][state.role_map["113"]]
                    first["rank"], second["rank"] = second["rank"], first["rank"]

        destination.observe_request = observe
        repaired = await run_repair(
            config,
            state,
            parse_export_directory(config.export_dir, metadata_only=True),
            events.append,
        )
        assert len(repaired.recreated_roles) == 1
        assert state.role_map["112"] != old_role
        assert authorities and all(runtime is authorities[0] for runtime in authorities)
        if external_rank_edit:
            assert not repaired.permissions.verified
            assert repaired.permissions.reason == "readback_mismatch"
            assert len(sent) == 1
            assert len(state.message_map) == 1
            assert len(destination.messages) == 1
            assert not any(
                event.phase == "retry" and event.status == "completed" for event in events
            )
        else:
            assert repaired.permissions.verified
            assert not state.failed_messages
            assert len(state.message_map) == 2
            assert repaired.dead_letter == {"drained": 2, "remaining": 0}
            assert any(event.phase == "retry" and event.status == "completed" for event in events)
            assert_independent_access(metadata, destination, state)
        assert load_state(config.output_dir).permission_runtime is None
    assert not destination.unknown
