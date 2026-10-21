"""Compose actual permission callers with controlled HTTP and independent access arithmetic."""

from __future__ import annotations

import copy
import dataclasses
import json
import re
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from aioresponses import CallbackResult, aioresponses

from discord_ferry import discord
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
