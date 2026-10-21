"""Save/load Discord guild metadata to/from discord_metadata.json."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from discord_ferry.core.atomicio import atomic_write_text
from discord_ferry.core.permissions import (
    PermissionProblem,
    PermissionSourceEvidence,
    SourceChannel,
    SourceOverwrite,
    SourceRole,
)
from discord_ferry.discord.models import normalize_channel_position
from discord_ferry.discord.permissions import ALL_STOAT_PERMISSIONS, translate_permissions
from discord_ferry.errors import MigrationError

if TYPE_CHECKING:
    from collections.abc import Collection
    from pathlib import Path

    from discord_ferry.config import FerryConfig
    from discord_ferry.discord.models import DiscordChannel, DiscordRole
    from discord_ferry.parser.models import DCEExport


@dataclass
class PermissionPair:
    """Stoat allow/deny permission bitfield pair."""

    allow: int
    deny: int


@dataclass
class RoleOverride:
    """Per-role channel permission override (Stoat bit space, Discord role ID)."""

    discord_role_id: str
    allow: int
    deny: int


@dataclass
class RoleMeta:
    """Per-role Discord attributes not carried by the DCE export."""

    hoist: bool = False
    position: int = 0
    icon_hash: str = ""
    unicode_emoji: str = ""
    name: str = ""
    color: str = ""  # hex "#rrggbb", or "" for no color


@dataclass
class ChannelMeta:
    """Per-channel metadata fetched from Discord API, translated to Stoat bit space."""

    nsfw: bool
    default_override: PermissionPair | None = None
    role_overrides: list[RoleOverride] = field(default_factory=list)
    slowmode: int = 0
    user_limit: int = 0
    position: int | None = None


@dataclass
class DiscordMetadata:
    """Translated Discord guild metadata, persisted to discord_metadata.json."""

    guild_id: str
    fetched_at: str
    server_default_permissions: int
    role_permissions: dict[str, PermissionPair]
    channel_metadata: dict[str, ChannelMeta]
    user_override_channels: list[dict[str, object]] = field(default_factory=list)
    banner_hash: str = ""
    role_metadata: dict[str, RoleMeta] = field(default_factory=dict)
    category_positions: dict[str, int] = field(default_factory=dict)
    guild_description: str = ""
    guild_nsfw: bool = False
    permission_source: PermissionSourceEvidence | None = None


_PERMISSION_TRANSLATION_VERSION = "discord-to-stoat-v1"


def _source_integer(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _capture_source_channel(channel: DiscordChannel) -> SourceChannel:
    overwrites = None
    member_count = None
    if "permission_overwrites" in channel.capture_fields and all(
        type(ow.type) is int and ow.type in (0, 1) for ow in channel.permission_overwrites
    ):
        overwrites = tuple(
            SourceOverwrite(ow.id, ow.allow, ow.deny)
            for ow in sorted(channel.permission_overwrites, key=lambda ow: ow.id)
            if ow.type == 0
        )
        member_count = sum(ow.type == 1 for ow in channel.permission_overwrites)
    return SourceChannel(
        channel.id,
        _source_integer(channel.type) if "type" in channel.capture_fields else None,
        overwrites,
        member_count,
        channel.capture_fields,
    )


def capture_permission_source(
    guild_id: str, roles: Collection[DiscordRole], channels: Collection[DiscordChannel]
) -> PermissionSourceEvidence:
    """Snapshot structural facts before translation, without inventing absent values."""
    source = PermissionSourceEvidence(
        guild_id,
        _PERMISSION_TRANSLATION_VERSION,
        roles=tuple(
            SourceRole(
                role.id,
                _source_integer(role.permissions) if "permissions" in role.capture_fields else None,
                _source_integer(role.position) if "position" in role.capture_fields else None,
                role.managed
                if "managed" in role.capture_fields and type(role.managed) is bool
                else None,
                role.capture_fields,
            )
            for role in sorted(roles, key=lambda role: role.id)
        ),
        channels=tuple(_capture_source_channel(c) for c in sorted(channels, key=lambda c: c.id)),
        covered_channel_ids=frozenset(channel.id for channel in channels),
    )
    return replace(source, fingerprint=_permission_source_fingerprint(source))


def _permission_source_payload(source: PermissionSourceEvidence) -> dict[str, Any]:
    return {
        "schema_version": source.schema_version,
        "guild_id": source.guild_id,
        "translation_version": source.translation_version,
        "roles": [
            {
                "id": r.id,
                "permissions": r.permissions,
                "position": r.position,
                "managed": r.managed,
                "capture_fields": sorted(r.capture_fields),
            }
            for r in source.roles
        ],
        "channels": [
            {
                "id": c.id,
                "type": c.type,
                "overwrites": None
                if c.overwrites is None
                else [
                    {"role_id": ow.role_id, "allow": ow.allow, "deny": ow.deny}
                    for ow in c.overwrites
                ],
                "member_overwrite_count": c.member_overwrite_count,
                "capture_fields": sorted(c.capture_fields),
            }
            for c in source.channels
        ],
        "covered_channel_ids": sorted(source.covered_channel_ids),
    }


def _permission_source_fingerprint(source: PermissionSourceEvidence) -> str:
    data = _permission_source_payload(source)
    data["roles"].sort(key=lambda role: role["id"])
    data["channels"].sort(key=lambda channel: channel["id"])
    for channel in data["channels"]:
        if channel["overwrites"] is not None:
            channel["overwrites"].sort(key=lambda ow: (ow["role_id"], ow["allow"], ow["deny"]))
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _permission_source_to_dict(source: PermissionSourceEvidence) -> dict[str, Any]:
    return {**_permission_source_payload(source), "fingerprint": source.fingerprint}


def _source_list(value: object) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError("Source evidence collection must be a list")
    return value


def _source_fields(value: object) -> frozenset[str]:
    values = _source_list(value)
    if any(type(item) is not str for item in values):
        raise ValueError("Source evidence fields must be strings")
    return frozenset(values)


def _dict_to_permission_source(value: object) -> PermissionSourceEvidence | None:
    if not isinstance(value, dict):
        return None
    try:
        if not isinstance(value["fingerprint"], str):
            return None
        return PermissionSourceEvidence(
            guild_id=value["guild_id"],
            translation_version=value["translation_version"],
            schema_version=value["schema_version"],
            fingerprint=value["fingerprint"],
            roles=tuple(
                SourceRole(
                    r["id"],
                    r.get("permissions"),
                    r.get("position"),
                    r.get("managed"),
                    _source_fields(r["capture_fields"]),
                )
                for r in _source_list(value["roles"])
            ),
            channels=tuple(
                SourceChannel(
                    c["id"],
                    c.get("type"),
                    None
                    if c["overwrites"] is None
                    else tuple(
                        SourceOverwrite(ow["role_id"], ow["allow"], ow["deny"])
                        for ow in _source_list(c["overwrites"])
                    ),
                    c.get("member_overwrite_count"),
                    _source_fields(c["capture_fields"]),
                )
                for c in _source_list(value["channels"])
            ),
            covered_channel_ids=_source_fields(value["covered_channel_ids"]),
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        # Invalid optional evidence does not erase otherwise readable legacy metadata.
        return None


def validate_permission_source(
    meta: DiscordMetadata, channel_ids: frozenset[str]
) -> tuple[PermissionProblem, ...]:
    """Check source evidence only, without granting target authority or changing metadata."""
    return validate_source_snapshot(meta, channel_ids)


def _matches_source_pair(pair: object, allow: int, deny: int) -> bool:
    return (
        isinstance(pair, PermissionPair)
        and _source_integer(pair.allow) == allow
        and _source_integer(pair.deny) == deny
    )


def validate_source_snapshot(
    meta: DiscordMetadata, channel_ids: frozenset[str]
) -> tuple[PermissionProblem, ...]:
    """Validate binding, complete selected facts and the existing translated permission cells."""

    def refused(guidance: str) -> tuple[PermissionProblem, ...]:
        entity_ids = tuple(sorted(cid for cid in channel_ids if _bad_id("channel", cid) is None))
        return (PermissionProblem("unverified", entity_ids, None, guidance),)

    source = meta.permission_source
    if source is None:
        return refused("Refresh source evidence")
    if (
        type(source.schema_version) is not int
        or source.schema_version != 1
        or source.translation_version != _PERMISSION_TRANSLATION_VERSION
        or _bad_id("guild", source.guild_id) is not None
        or source.guild_id != meta.guild_id
    ):
        return refused("Refresh source evidence for this guild and supported translation version")
    roles = {role.id: role for role in source.roles}
    channels = {channel.id: channel for channel in source.channels}
    if (
        len(roles) != len(source.roles)
        or len(channels) != len(source.channels)
        or source.guild_id not in roles
        or source.covered_channel_ids != frozenset(channels)
        or not channel_ids <= source.covered_channel_ids
        or any(_bad_id("role", rid) is not None for rid in roles)
        or any(_bad_id("channel", cid) is not None for cid in channels)
    ):
        return refused("Refresh complete, unique source inventories and selected-channel coverage")
    for role in source.roles:
        if (
            not {"id", "permissions", "position", "managed"} <= role.capture_fields
            or _source_integer(role.permissions) is None
            or _source_integer(role.position) is None
            or type(role.managed) is not bool
        ):
            return refused("Refresh required raw role permission, position and managed facts")
    if roles[source.guild_id].managed is not False:
        return refused("Refresh the source everyone role inventory")
    for cid in sorted(channel_ids):
        channel = channels[cid]
        if (
            not {"id", "type", "permission_overwrites"} <= channel.capture_fields
            or _source_integer(channel.type) is None
            or channel.overwrites is None
            or _source_integer(channel.member_overwrite_count) is None
        ):
            return refused("Refresh required selected-channel type and overwrite facts")
        overwrite_ids = [ow.role_id for ow in channel.overwrites]
        if len(set(overwrite_ids)) != len(overwrite_ids) or any(
            ow.role_id not in roles
            or _source_integer(ow.allow) is None
            or _source_integer(ow.deny) is None
            for ow in channel.overwrites
        ):
            return refused("Refresh unique source overwrites bound to the source role inventory")
    if source.fingerprint != _permission_source_fingerprint(source):
        return refused("Refresh source evidence after a structural fingerprint mismatch")
    everyone_permissions = roles[source.guild_id].permissions
    assert everyone_permissions is not None  # Required role facts were checked above.
    if _source_integer(meta.server_default_permissions) != translate_permissions(
        everyone_permissions
    ):
        return refused("Refresh inconsistent translated server default permissions")
    ordinary = {
        rid: role for rid, role in roles.items() if rid != source.guild_id and not role.managed
    }
    if set(meta.role_permissions) != set(ordinary):
        return refused("Refresh translated role permissions to match the raw role inventory")
    for rid, role in ordinary.items():
        assert role.permissions is not None  # Required role facts were checked above.
        if not _matches_source_pair(
            meta.role_permissions[rid], translate_permissions(role.permissions), 0
        ):
            return refused("Refresh inconsistent translated role permissions")
    for cid in sorted(channel_ids):
        channel = channels[cid]
        translated = meta.channel_metadata.get(cid)
        if not isinstance(translated, ChannelMeta):
            return refused("Refresh missing translated selected-channel permissions")
        assert channel.overwrites is not None  # Required selected facts were checked above.
        expected = {ow.role_id: ow for ow in channel.overwrites}
        default = expected.pop(source.guild_id, None)
        if default is None:
            if translated.default_override is not None:
                return refused("Refresh inconsistent translated channel default permissions")
        elif not _matches_source_pair(
            translated.default_override,
            translate_permissions(default.allow, in_overwrite=True),
            translate_permissions(default.deny, in_overwrite=True),
        ):
            return refused("Refresh inconsistent translated channel default permissions")
        if any(not isinstance(ow, RoleOverride) for ow in translated.role_overrides):
            return refused("Refresh malformed translated channel role permissions")
        observed = {ow.discord_role_id: ow for ow in translated.role_overrides}
        if len(observed) != len(translated.role_overrides) or set(observed) != set(expected):
            return refused("Refresh translated channel role permissions to match raw overwrites")
        for rid, raw in expected.items():
            pair = observed[rid]
            if _source_integer(pair.allow) != translate_permissions(
                raw.allow, in_overwrite=True
            ) or _source_integer(pair.deny) != translate_permissions(raw.deny, in_overwrite=True):
                return refused("Refresh inconsistent translated channel role permissions")
    return ()


def save_discord_metadata(meta: DiscordMetadata, output_dir: Path) -> None:
    """Save to output_dir/discord_metadata.json using atomic write."""
    output_dir.mkdir(parents=True, exist_ok=True)
    data = _meta_to_dict(meta)
    atomic_write_text(output_dir / "discord_metadata.json", json.dumps(data, indent=2))


def load_discord_metadata(output_dir: Path) -> DiscordMetadata | None:
    """Load from output_dir/discord_metadata.json, or return None if missing."""
    path = output_dir / "discord_metadata.json"
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    return _dict_to_meta(raw)


def _meta_to_dict(meta: DiscordMetadata) -> dict[str, Any]:
    data = {
        "guild_id": meta.guild_id,
        "fetched_at": meta.fetched_at,
        "server_default_permissions": meta.server_default_permissions,
        "role_permissions": {
            k: {"allow": v.allow, "deny": v.deny} for k, v in meta.role_permissions.items()
        },
        "channel_metadata": {k: _channel_meta_to_dict(v) for k, v in meta.channel_metadata.items()},
        "user_override_channels": meta.user_override_channels,
        "banner_hash": meta.banner_hash,
        "role_metadata": {
            k: {
                "hoist": v.hoist,
                "position": v.position,
                "icon_hash": v.icon_hash,
                "unicode_emoji": v.unicode_emoji,
                "name": v.name,
                "color": v.color,
            }
            for k, v in meta.role_metadata.items()
        },
        "category_positions": meta.category_positions,
        "guild_description": meta.guild_description,
        "guild_nsfw": meta.guild_nsfw,
    }
    if meta.permission_source is not None:
        data["permission_source"] = _permission_source_to_dict(meta.permission_source)
    return data


def _channel_meta_to_dict(cm: ChannelMeta) -> dict[str, Any]:
    d: dict[str, Any] = {"nsfw": cm.nsfw}
    if cm.default_override is not None:
        d["default_override"] = {
            "allow": cm.default_override.allow,
            "deny": cm.default_override.deny,
        }
    d["role_overrides"] = [
        {
            "discord_role_id": ro.discord_role_id,
            "allow": ro.allow,
            "deny": ro.deny,
        }
        for ro in cm.role_overrides
    ]
    d["slowmode"] = cm.slowmode
    d["user_limit"] = cm.user_limit
    position = normalize_channel_position(cm.position)
    if position is not None:
        d["position"] = position
    return d


def _known_bits(value: Any) -> Any:
    """Drop bits Stoat does not define from a saved permission value.

    ``translate_permissions`` drops Discord bits that have no Stoat equivalent, so
    a fresh fetch can never produce a bit outside ``STOAT_PERMISSION_BITS``. A saved
    value holding one was not written by Ferry. Dropping it matches the fetch path
    and can only narrow what is sent. A value that is not an int that is not a bool and not negative
    is returned untouched for ``validate_cached_metadata`` to reject.
    """
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value & ALL_STOAT_PERMISSIONS
    return value


def _dict_to_meta(data: dict[str, Any]) -> DiscordMetadata:
    return DiscordMetadata(
        guild_id=data["guild_id"],
        fetched_at=data["fetched_at"],
        server_default_permissions=_known_bits(data.get("server_default_permissions", 0)),
        role_permissions={
            k: PermissionPair(allow=_known_bits(v["allow"]), deny=_known_bits(v["deny"]))
            for k, v in data.get("role_permissions", {}).items()
        },
        channel_metadata={
            k: _dict_to_channel_meta(v) for k, v in data.get("channel_metadata", {}).items()
        },
        user_override_channels=data.get("user_override_channels", []),
        banner_hash=data.get("banner_hash", ""),
        role_metadata={
            k: RoleMeta(
                hoist=v.get("hoist", False),
                position=v.get("position", 0),
                icon_hash=v.get("icon_hash", ""),
                unicode_emoji=v.get("unicode_emoji", ""),
                name=v.get("name", ""),
                color=v.get("color", ""),
            )
            for k, v in data.get("role_metadata", {}).items()
        },
        category_positions=data.get("category_positions", {}),
        guild_description=data.get("guild_description", ""),
        guild_nsfw=data.get("guild_nsfw", False),
        permission_source=_dict_to_permission_source(data.get("permission_source")),
    )


def _dict_to_channel_meta(data: dict[str, Any]) -> ChannelMeta:
    default_override = None
    if "default_override" in data:
        do = data["default_override"]
        default_override = PermissionPair(
            allow=_known_bits(do["allow"]), deny=_known_bits(do["deny"])
        )
    return ChannelMeta(
        nsfw=data.get("nsfw", False),
        default_override=default_override,
        role_overrides=[
            RoleOverride(
                discord_role_id=ro["discord_role_id"],
                allow=_known_bits(ro["allow"]),
                deny=_known_bits(ro["deny"]),
            )
            for ro in data.get("role_overrides", [])
        ],
        slowmode=data.get("slowmode", 0),
        user_limit=data.get("user_limit", 0),
        position=normalize_channel_position(data.get("position")),
    )


# --- Resume checks -----------------------------------------------------------
#
# discord_metadata.json is reused without a fetch on a resume, and read by every
# later phase on any run. These checks bind it to the current run (#971). The raw
# loader above stays permissive: the engine's own checks use it and so do the
# repair tools, which have no export.

_SNOWFLAKE = re.compile(r"[0-9]{1,20}")


def _shown(value: object) -> str:
    return repr(value)[:40]


def _bad_id(label: str, value: object) -> str | None:
    if isinstance(value, str) and _SNOWFLAKE.fullmatch(value):
        return None
    return f"{label} {_shown(value)} is not a Discord id (digits only)"


def _bad_perm(label: str, value: object) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return None
    return f"{label} {_shown(value)} is not a non-negative whole number"


def _problems(meta: DiscordMetadata, *, check_keys: bool) -> list[str]:
    found: list[str | None] = [
        _bad_id("guild_id", meta.guild_id),
        _bad_perm("server_default_permissions", meta.server_default_permissions),
    ]
    for rid, pair in meta.role_permissions.items():
        found.append(_bad_id("role_permissions key", rid) if check_keys else None)
        found.append(_bad_perm(f"role {rid} allow", pair.allow))
        found.append(_bad_perm(f"role {rid} deny", pair.deny))
    for cid, cm in meta.channel_metadata.items():
        found.append(_bad_id("channel_metadata key", cid) if check_keys else None)
        if cm.default_override is not None:
            found.append(_bad_perm(f"channel {cid} default allow", cm.default_override.allow))
            found.append(_bad_perm(f"channel {cid} default deny", cm.default_override.deny))
        for ro in cm.role_overrides:
            if check_keys:
                found.append(_bad_id(f"channel {cid} override role id", ro.discord_role_id))
            found.append(_bad_perm(f"channel {cid} override allow", ro.allow))
            found.append(_bad_perm(f"channel {cid} override deny", ro.deny))
    # The review step and the report read these keys from each entry.
    for index, entry in enumerate(meta.user_override_channels):
        if not (
            isinstance(entry, dict)
            and isinstance(entry.get("channel_name"), str)
            and isinstance(entry.get("override_count"), int)
            and not isinstance(entry.get("override_count"), bool)
        ):
            found.append(f"user_override_channels entry {index} is malformed")
    if check_keys:
        for rid in meta.role_metadata:
            found.append(_bad_id("role_metadata key", rid))
        for cat_id in meta.category_positions:
            found.append(_bad_id("category_positions key", cat_id))
    return [p for p in found if p is not None]


def _raw_permission_values(data: dict[str, Any]) -> list[object]:
    """Every permission value in a raw metadata dict, before any masking."""
    values: list[object] = [data.get("server_default_permissions", 0)]
    for pair in data.get("role_permissions", {}).values():
        values += [pair["allow"], pair["deny"]]
    for ch in data.get("channel_metadata", {}).values():
        if "default_override" in ch:
            values += [ch["default_override"]["allow"], ch["default_override"]["deny"]]
        for ro in ch.get("role_overrides", []):
            values += [ro["allow"], ro["deny"]]
    return values


@dataclass(frozen=True)
class CachedMetadata:
    """The outcome of reading discord_metadata.json for the current run.

    ``meta`` is None when the file is absent or unusable. ``problem`` says why it
    is unusable, and is None when the file is absent or fine.
    """

    meta: DiscordMetadata | None = None
    problem: str | None = None
    dropped_unknown_bits: bool = False


def read_cached_metadata(
    output_dir: Path,
    *,
    export_guild_ids: Collection[str] = (),
    configured_guild_id: str | None = None,
    check_keys: bool = False,
) -> CachedMetadata:
    """Read discord_metadata.json and check it belongs to this run. Never raises.

    The file is unusable when it cannot be read, holds a malformed identifier or
    permission value, names a guild other than ``configured_guild_id``, or names a
    guild that none of ``export_guild_ids`` is. Empty or None means that check is
    not made. The guild id and every permission value are always checked. The
    identifier keys of the maps are checked only with ``check_keys``, which a
    resume sets: a fresh fetch always writes real ids there, so a stale file with
    odd keys cannot match anything. Permission bits Stoat does not define are
    dropped, as ``translate_permissions`` drops unmapped Discord bits on a fresh fetch, and
    ``dropped_unknown_bits`` records that it happened.
    """
    path = output_dir / "discord_metadata.json"
    if not path.exists():
        return CachedMetadata()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        meta = _dict_to_meta(raw)
        dropped = any(
            isinstance(v, int) and not isinstance(v, bool) and v >= 0 and v & ~ALL_STOAT_PERMISSIONS
            for v in _raw_permission_values(raw)
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return CachedMetadata(
            problem=f"{path} is not a valid Ferry metadata file ({type(exc).__name__})"
        )
    problems = _problems(meta, check_keys=check_keys)
    if problems:
        more = f" (and {len(problems) - 1} more)" if len(problems) > 1 else ""
        return CachedMetadata(problem=f"{path} holds invalid values: {problems[0]}{more}")
    if configured_guild_id and meta.guild_id != configured_guild_id:
        return CachedMetadata(
            problem=(
                f"{path} belongs to Discord server {meta.guild_id}, but the configured "
                f"server is {configured_guild_id}"
            )
        )
    if export_guild_ids and meta.guild_id not in export_guild_ids:
        return CachedMetadata(
            problem=(
                f"{path} belongs to Discord server {meta.guild_id}, but the export is from "
                f"{', '.join(sorted(export_guild_ids))}"
            )
        )
    return CachedMetadata(meta=meta, dropped_unknown_bits=dropped)


def load_discord_metadata_for_resume(
    output_dir: Path, *, configured_guild_id: str | None = None
) -> DiscordMetadata | None:
    """Read the cached metadata for a resume, or None when there is no file.

    Raises ``MigrationError`` when the file is unusable. A resume does not fall
    back to a fresh fetch: that needs a Discord token, which a resume may not
    carry, and a mismatch means the output directory or the configuration is
    wrong, which a refetch would paper over.
    """
    cached = read_cached_metadata(
        output_dir, configured_guild_id=configured_guild_id, check_keys=True
    )
    if cached.problem is not None:
        raise MigrationError(resume_refusal(cached.problem))
    return cached.meta


def resume_refusal(problem: str) -> str:
    """The error text for a resume that cannot use its cached metadata."""
    return (
        f"Cannot resume: {problem}. Delete it to fetch fresh metadata, or point at "
        "the right output directory."
    )


def load_bound_discord_metadata(
    config: FerryConfig, exports: Collection[DCEExport] = ()
) -> DiscordMetadata | None:
    """The cached metadata if it belongs to this run, else None.

    Every phase that reads discord_metadata.json goes through here, so another
    server's role and channel permissions are never applied to this one. An
    unusable file reads as absent. ``run_migration`` reports why, once.
    """
    return read_cached_metadata(
        config.output_dir,
        export_guild_ids={e.guild.id for e in exports},
        configured_guild_id=config.discord_server_id,
    ).meta
