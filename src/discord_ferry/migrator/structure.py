"""Phases 3-6: SERVER, ROLES, CATEGORIES, CHANNELS migration."""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from discord_ferry.core.events import MigrationEvent
from discord_ferry.discord.metadata import load_discord_metadata
from discord_ferry.errors import MigrationError
from discord_ferry.migrator.api import (
    api_create_channel,
    api_create_role,
    api_create_server,
    api_edit_role,
    api_edit_server,
    api_fetch_server,
    api_pin_message,
    api_send_message,
    api_set_channel_default_permissions,
    api_set_channel_role_permissions,
    api_set_role_permissions,
    api_set_server_default_permissions,
    api_upsert_categories,
    get_session,
)
from discord_ferry.migrator.sanitize import truncate_name
from discord_ferry.parser.dce_parser import stream_messages
from discord_ferry.uploader.autumn import upload_with_cache

if TYPE_CHECKING:
    from discord_ferry.config import FerryConfig
    from discord_ferry.core.events import EventCallback
    from discord_ferry.parser.models import DCEChannel, DCEExport, DCERole
    from discord_ferry.state import MigrationState

logger = logging.getLogger(__name__)

# Minimum permissions for the Ferry account to operate:
# ManageRole(3), ManageCustomisation(4), ViewChannel(20), ReadMessageHistory(21),
# SendMessage(22), ManageMessages(23), SendEmbeds(26), UploadFiles(27), Masquerade(28), React(29)
FERRY_MIN_PERMISSIONS = (
    8  # ManageRole (3)
    | 16  # ManageCustomisation (4)
    | 1_048_576  # ViewChannel (20)
    | 2_097_152  # ReadMessageHistory (21)
    | 4_194_304  # SendMessage (22)
    | 8_388_608  # ManageMessages (23)
    | 67_108_864  # SendEmbeds (26)
    | 134_217_728  # UploadFiles (27)
    | 268_435_456  # Masquerade (28)
    | 536_870_912  # React (29)
)  # == 1_022_361_624


def make_unique_channel_name(name: str, existing_names: set[str]) -> str:
    """Return a name unique within ``existing_names``, truncating to 32 chars.

    Args:
        name: Desired channel name.
        existing_names: Set of already-used names. Updated in place with the returned name.

    Returns:
        A name that is not already in ``existing_names`` (at most 32 chars).
    """
    base = name[:32]
    if base not in existing_names:
        existing_names.add(base)
        return base
    # Reserve space for the suffix (e.g. "-1", "-99")
    counter = 1
    while True:
        suffix = f"-{counter}"
        candidate = f"{name[: 32 - len(suffix)]}{suffix}"
        if candidate not in existing_names:
            existing_names.add(candidate)
            return candidate
        counter += 1


def _generate_category_id() -> str:
    """Generate a unique category ID (1-32 char string)."""
    return uuid.uuid4().hex[:26]


async def run_server(
    config: FerryConfig,
    state: MigrationState,
    exports: list[DCEExport],
    on_event: EventCallback,
) -> None:
    """Phase 3 — Create or attach to a Stoat server and apply the guild icon.

    Args:
        config: Ferry configuration.
        state: Migration state; ``stoat_server_id`` will be set.
        exports: Parsed DCE exports used to read guild name and icon.
        on_event: Event callback for progress reporting.

    Raises:
        MigrationError: If the existing server cannot be fetched or server creation fails.
    """
    if config.dry_run:
        state.stoat_server_id = f"dry-server-{exports[0].guild.id}" if exports else "dry-server"
        on_event(
            MigrationEvent(
                phase="server",
                status="completed",
                message=f"[DRY RUN] Server: {state.stoat_server_id}",
            )
        )
        return

    async with get_session(config) as session:
        if config.server_id:
            # Use an existing server — verify it exists.
            await api_fetch_server(session, config.stoat_url, config.token, config.server_id)
            state.stoat_server_id = config.server_id
            on_event(
                MigrationEvent(
                    phase="server",
                    status="progress",
                    message=f"Using existing server {config.server_id}",
                )
            )
        else:
            # Determine name from config or first export's guild name.
            name: str
            if config.server_name:
                name = config.server_name
            elif exports:
                name = exports[0].guild.name
            else:
                name = "Ferry Server"

            result = await api_create_server(session, config.stoat_url, config.token, name)
            state.stoat_server_id = result["_id"]
            on_event(
                MigrationEvent(
                    phase="server",
                    status="progress",
                    message=f"Created server '{name}' ({state.stoat_server_id})",
                )
            )

        # Upload and apply guild icon if available.
        if exports:
            icon_url = exports[0].guild.icon_url
            if icon_url:
                icon_path = Path(icon_url)
                if icon_path.exists():
                    try:
                        icon_id = await upload_with_cache(
                            session,
                            state.autumn_url,
                            "icons",
                            icon_path,
                            config.token,
                            state.upload_cache,
                            config.upload_delay,
                        )
                        await api_edit_server(
                            session,
                            config.stoat_url,
                            config.token,
                            state.stoat_server_id,
                            icon=icon_id,
                        )
                        on_event(
                            MigrationEvent(
                                phase="server",
                                status="progress",
                                message="Applied server icon",
                            )
                        )
                    except Exception as exc:  # noqa: BLE001
                        state.warnings.append(
                            {
                                "phase": "server",
                                "type": "icon_upload_failed",
                                "message": f"Icon upload failed: {exc}",
                            }
                        )
                        on_event(
                            MigrationEvent(
                                phase="server",
                                status="warning",
                                message=f"Icon upload failed, continuing: {exc}",
                            )
                        )

        # Upload and apply server banner if available from Discord metadata.
        discord_meta = load_discord_metadata(config.output_dir)
        if discord_meta and discord_meta.banner_hash:
            guild_id = discord_meta.guild_id
            banner_url = (
                f"https://cdn.discordapp.com/banners/{guild_id}/"
                f"{discord_meta.banner_hash}.png?size=1024"
            )
            try:
                banner_dir = config.output_dir / "banners"
                banner_dir.mkdir(parents=True, exist_ok=True)
                banner_path = banner_dir / f"{guild_id}.png"
                headers: dict[str, str] = {}
                if config.discord_token:
                    headers["Authorization"] = config.discord_token
                async with session.get(banner_url, headers=headers) as resp:
                    if resp.status == 200:
                        banner_path.write_bytes(await resp.read())
                        banner_id = await upload_with_cache(
                            session,
                            state.autumn_url,
                            "banners",
                            banner_path,
                            config.token,
                            state.upload_cache,
                            config.upload_delay,
                        )
                        await api_edit_server(
                            session,
                            config.stoat_url,
                            config.token,
                            state.stoat_server_id,
                            banner=banner_id,
                        )
                        on_event(
                            MigrationEvent(
                                phase="server",
                                status="progress",
                                message="Applied server banner",
                            )
                        )
                    else:
                        state.warnings.append(
                            {
                                "phase": "server",
                                "type": "banner_download_failed",
                                "message": f"Banner download returned status {resp.status}",
                            }
                        )
            except Exception as exc:  # noqa: BLE001
                state.warnings.append(
                    {
                        "phase": "server",
                        "type": "banner_upload_failed",
                        "message": f"Banner migration failed: {exc}",
                    }
                )

        # Bootstrap minimum permissions on the server's default role.
        try:
            await api_edit_server(
                session,
                config.stoat_url,
                config.token,
                state.stoat_server_id,
                default_permissions=FERRY_MIN_PERMISSIONS,
            )
            on_event(
                MigrationEvent(
                    phase="server",
                    status="progress",
                    message="Set server permissions for migration",
                )
            )
        except Exception as exc:  # noqa: BLE001
            state.warnings.append(
                {
                    "phase": "server",
                    "type": "permission_bootstrap",
                    "message": (
                        f"Could not set server permissions: {exc}. "
                        "Masquerade colours require ManageRole (bit 3). "
                        "Grant permissions manually if needed."
                    ),
                }
            )
            on_event(
                MigrationEvent(
                    phase="server",
                    status="warning",
                    message=(
                        f"Permission bootstrap failed: {exc}. "
                        "Grant ManageRole manually for masquerade colours."
                    ),
                )
            )


async def run_roles(
    config: FerryConfig,
    state: MigrationState,
    exports: list[DCEExport],
    on_event: EventCallback,
) -> None:
    """Phase 4 — Create server roles and map Discord role IDs to Stoat role IDs.

    Args:
        config: Ferry configuration.
        state: Migration state; ``role_map`` will be populated.
        exports: Parsed DCE exports; roles are extracted from message authors.
        on_event: Event callback for progress reporting.

    Raises:
        MigrationError: If any API call fails unrecoverably.
    """
    # Determine the @everyone role ID (same as guild ID) to skip it.
    guild_id = exports[0].guild.id if exports else ""

    # Collect unique roles by ID across all exports and messages.
    seen_ids: set[str] = set()
    unique_roles: list[DCERole] = []
    for export in exports:
        msg_iter = (
            stream_messages(export.json_path)
            if export.json_path is not None
            else iter(export.messages)
        )
        for msg in msg_iter:
            for role in msg.author.roles:
                if role.id not in seen_ids:
                    seen_ids.add(role.id)
                    unique_roles.append(role)

    # Filter out the @everyone role.
    roles_to_create = [r for r in unique_roles if r.id != guild_id]

    if config.dry_run:
        for role in roles_to_create:
            state.role_map[role.id] = f"dry-role-{role.id}"
        on_event(
            MigrationEvent(
                phase="roles",
                status="completed",
                message=f"[DRY RUN] Mapped {len(roles_to_create)} roles",
            )
        )
        return

    async with get_session(config) as session:
        for idx, role in enumerate(roles_to_create, start=1):
            result = await api_create_role(
                session,
                config.stoat_url,
                config.token,
                state.stoat_server_id,
                truncate_name(role.name),
            )
            stoat_role_id: str = result["id"]
            state.role_map[role.id] = stoat_role_id

            # Apply colour if present (British spelling required by Stoat API).
            if role.color:
                color_str = role.color.lstrip("#")
                try:
                    colour_int = int(color_str, 16)
                    await api_edit_role(
                        session,
                        config.stoat_url,
                        config.token,
                        state.stoat_server_id,
                        stoat_role_id,
                        colour=colour_int,
                    )
                except (ValueError, MigrationError) as exc:
                    state.warnings.append(
                        {
                            "phase": "roles",
                            "type": "role_colour_failed",
                            "message": f"Failed to set colour for role '{role.name}': {exc}",
                        }
                    )

            on_event(
                MigrationEvent(
                    phase="roles",
                    status="progress",
                    message=f"Created role '{role.name}'",
                    current=idx,
                    total=len(roles_to_create),
                )
            )

    # Second pass: set role rank from DCE position (best-effort).
    ranked_roles = sorted(roles_to_create, key=lambda r: r.position)
    async with get_session(config) as session:
        for role in ranked_roles:
            rank_role_id = state.role_map.get(role.id)
            if not rank_role_id or role.position == 0:
                continue
            try:
                await api_edit_role(
                    session,
                    config.stoat_url,
                    config.token,
                    state.stoat_server_id,
                    rank_role_id,
                    rank=role.position,
                )
            except Exception as exc:  # noqa: BLE001
                state.warnings.append(
                    {
                        "phase": "roles",
                        "type": "role_rank_failed",
                        "message": (f"Failed to set rank for role '{role.name}': {exc}"),
                    }
                )

    # Third pass: apply translated permissions from Discord metadata.
    discord_metadata = load_discord_metadata(config.output_dir)
    if discord_metadata and not config.dry_run:
        async with get_session(config) as session:
            for role in roles_to_create:
                stoat_role_id_or_none = state.role_map.get(role.id)
                if not stoat_role_id_or_none:
                    continue
                stoat_role_id = stoat_role_id_or_none
                role_perms = discord_metadata.role_permissions.get(role.id)
                if role_perms:
                    try:
                        await api_set_role_permissions(
                            session,
                            config.stoat_url,
                            config.token,
                            state.stoat_server_id,
                            stoat_role_id,
                            allow=role_perms.allow,
                            deny=role_perms.deny,
                        )
                    except Exception as exc:  # noqa: BLE001
                        state.warnings.append(
                            {
                                "phase": "roles",
                                "type": "role_permissions_failed",
                                "message": (
                                    f"Failed to set permissions for role '{role.name}': {exc}"
                                ),
                            }
                        )

            # Apply @everyone server default permissions (merged with ferry minimum).
            if discord_metadata.server_default_permissions:
                merged = discord_metadata.server_default_permissions | FERRY_MIN_PERMISSIONS
                try:
                    await api_set_server_default_permissions(
                        session,
                        config.stoat_url,
                        config.token,
                        state.stoat_server_id,
                        permissions=merged,
                    )
                except Exception as exc:  # noqa: BLE001
                    state.warnings.append(
                        {
                            "phase": "roles",
                            "type": "server_default_permissions_failed",
                            "message": f"Failed to set server default permissions: {exc}",
                        }
                    )

    on_event(
        MigrationEvent(
            phase="roles",
            status="progress",
            message=f"Created {len(roles_to_create)} roles",
        )
    )


async def run_categories(
    config: FerryConfig,
    state: MigrationState,
    exports: list[DCEExport],
    on_event: EventCallback,
) -> None:
    """Phase 5 — Create server categories and map Discord category IDs to Stoat category IDs.

    Args:
        config: Ferry configuration.
        state: Migration state; ``category_map`` will be populated.
        exports: Parsed DCE exports; category metadata is read from channel info.
        on_event: Event callback for progress reporting.

    Raises:
        MigrationError: If any API call fails unrecoverably.
    """
    # Collect unique categories (id -> name), skipping entries with empty IDs.
    seen_cat_ids: set[str] = set()
    unique_categories: list[tuple[str, str]] = []  # (discord_cat_id, category_name)
    for export in exports:
        cat_id = export.channel.category_id
        cat_name = export.channel.category
        if cat_id and cat_id not in seen_cat_ids:
            seen_cat_ids.add(cat_id)
            unique_categories.append((cat_id, cat_name))

    if config.dry_run:
        for discord_cat_id, _cat_name in unique_categories:
            state.category_map[discord_cat_id] = f"dry-cat-{discord_cat_id}"
        on_event(
            MigrationEvent(
                phase="categories",
                status="completed",
                message=f"[DRY RUN] Mapped {len(unique_categories)} categories",
            )
        )
        return

    async with get_session(config) as session:
        stoat_categories: list[dict[str, Any]] = []
        for idx, (discord_cat_id, cat_name) in enumerate(unique_categories, start=1):
            stoat_cat_id = _generate_category_id()
            state.category_map[discord_cat_id] = stoat_cat_id
            stoat_categories.append(
                {
                    "id": stoat_cat_id,
                    "title": truncate_name(cat_name),
                    "channels": [],
                }
            )

            on_event(
                MigrationEvent(
                    phase="categories",
                    status="progress",
                    message=f"Created category '{cat_name}'",
                    current=idx,
                    total=len(unique_categories),
                )
            )

        if stoat_categories:
            await api_upsert_categories(
                session,
                config.stoat_url,
                config.token,
                state.stoat_server_id,
                stoat_categories,
            )

    on_event(
        MigrationEvent(
            phase="categories",
            status="progress",
            message=f"Created {len(unique_categories)} categories",
        )
    )


async def run_channels(
    config: FerryConfig,
    state: MigrationState,
    exports: list[DCEExport],
    on_event: EventCallback,
) -> None:
    """Phase 6 — Create channels, handle thread flattening, and assign channels to categories.

    Args:
        config: Ferry configuration.
        state: Migration state; ``channel_map`` will be populated.
        exports: Parsed DCE exports; channel metadata drives creation.
        on_event: Event callback for progress reporting.

    Raises:
        MigrationError: If any API call fails unrecoverably.
    """
    discord_metadata = load_discord_metadata(config.output_dir)

    # Deduplicate exports by channel ID and skip category channels (type 4).
    seen_channel_ids: set[str] = set()
    # Each entry: (channel, stoat_type, unique_name, effective_category_id, is_thread)
    channels_to_create: list[tuple[DCEChannel, str | None, str, str, bool]] = []
    existing_names: set[str] = set()
    # Forum categories: forum_cat_key -> forum display name (created lazily).
    forum_categories: dict[str, str] = {}

    for export in exports:
        channel: DCEChannel = export.channel

        # Skip category channels — handled in Phase 5.
        if channel.type == 4:
            continue

        # Skip thread/forum exports when skip_threads is enabled.
        if config.skip_threads and export.is_thread:
            continue

        # In merge/archive mode, threads are NOT created as channels.
        if export.is_thread and config.thread_strategy in ("merge", "archive"):
            continue

        # Skip already-seen channel IDs (deduplicate).
        if channel.id in seen_channel_ids:
            continue
        seen_channel_ids.add(channel.id)

        # Map Discord channel type to Stoat type string.
        stoat_type: str | None
        match channel.type:
            case 2:
                stoat_type = "Voice"
            case 0 | 5 | 11 | 12 | 15 | 16:
                stoat_type = "Text"
            case _:
                stoat_type = "Text"

        # Build channel name; prefix with parent name for threads.
        ch_name = channel.name
        if export.is_thread and export.parent_channel_name:
            if config.thread_strategy == "flatten":
                # Add tree branch prefix for visual thread hierarchy.
                ch_name = f"\u251c\u2500 {ch_name}"
            else:
                ch_name = f"{export.parent_channel_name}-{ch_name}"

        unique_name = make_unique_channel_name(ch_name, existing_names)

        # For forum/media channel threads (type 15/16), use a dedicated forum
        # category keyed by parent name instead of the original Discord category.
        effective_cat_id = channel.category_id
        if channel.type in (15, 16) and export.is_thread and export.parent_channel_name:
            forum_cat_key = f"forum-{export.parent_channel_name}"
            if forum_cat_key not in forum_categories:
                forum_categories[forum_cat_key] = export.parent_channel_name
            effective_cat_id = forum_cat_key

        channels_to_create.append(
            (channel, stoat_type, unique_name, effective_cat_id, export.is_thread)
        )

    # Reserve channel slots for forum index channels (one per forum category).
    index_channel_slots = len(forum_categories) if forum_categories else 0
    effective_max = config.max_channels - index_channel_slots

    if len(channels_to_create) > effective_max:
        overflow = len(channels_to_create) - effective_max
        # Sort: main channels first (False < True), then threads by message_count
        # descending so higher-traffic threads survive truncation.
        export_by_ch = {exp.channel.id: exp for exp in exports}
        channels_to_create.sort(
            key=lambda t: (
                t[4],  # is_thread: False (main) before True (thread)
                -(export_by_ch[t[0].id].message_count if t[0].id in export_by_ch else 0),
            )
        )
        dropped = channels_to_create[effective_max:]
        channels_to_create = channels_to_create[:effective_max]
        dropped_names = [entry[2] for entry in dropped]
        on_event(
            MigrationEvent(
                phase="channels",
                status="warning",
                message=(
                    f"Total channels ({effective_max + overflow}) exceeds Stoat limit "
                    f"of {config.max_channels}. Dropped {overflow} channel(s): "
                    f"{', '.join(dropped_names[:10])}"
                    f"{'...' if len(dropped_names) > 10 else ''}"
                ),
            )
        )
        state.warnings.append(
            {
                "phase": "channels",
                "type": "channel_limit",
                "message": (
                    f"Dropped {overflow} channel(s) exceeding {config.max_channels} limit: "
                    f"{', '.join(dropped_names)}"
                ),
            }
        )

    # Create forum-derived categories (before dry_run check so they get mapped).
    if forum_categories and not config.dry_run:
        for forum_key, forum_name in forum_categories.items():
            stoat_forum_id = _generate_category_id()
            state.category_map[forum_key] = stoat_forum_id
            state.forum_category_names[forum_key] = forum_name  # S15: track for REPORT rebuild
            on_event(
                MigrationEvent(
                    phase="channels",
                    status="progress",
                    message=f"Created forum category '{forum_name}'",
                )
            )
    elif forum_categories and config.dry_run:
        for forum_key, forum_name in forum_categories.items():
            state.category_map[forum_key] = f"dry-cat-{forum_key}"
            state.forum_category_names[forum_key] = forum_name  # S15: track for REPORT rebuild

    if config.dry_run:
        for channel, _stoat_type, _unique_name, _discord_cat_id, _is_thread in channels_to_create:
            state.channel_map[channel.id] = f"dry-ch-{channel.id}"
        on_event(
            MigrationEvent(
                phase="channels",
                status="completed",
                message=f"[DRY RUN] Mapped {len(channels_to_create)} channels",
            )
        )
        return

    # stoat_category_id -> list of stoat_channel_ids (built during creation).
    category_channels: dict[str, list[str]] = {}
    # Build an export lookup so we can retrieve message_count for forum posts.
    export_by_channel: dict[str, DCEExport] = {exp.channel.id: exp for exp in exports}
    # forum_cat_key -> list of (stoat_channel_id, unique_name, message_count)
    forum_channel_info: dict[str, list[tuple[str, str, int]]] = {}

    async with get_session(config) as session:
        for idx, (channel, stoat_type, unique_name, discord_cat_id, _is_thread) in enumerate(
            channels_to_create, start=1
        ):
            ch: DCEChannel = channel
            description = ch.topic if ch.topic else None

            ch_meta = discord_metadata.channel_metadata.get(ch.id) if discord_metadata else None
            nsfw = ch_meta.nsfw if ch_meta else False

            stoat_channel_id: str
            try:
                result = await api_create_channel(
                    session,
                    config.stoat_url,
                    config.token,
                    state.stoat_server_id,
                    name=unique_name,
                    channel_type=stoat_type,
                    description=description,
                    nsfw=nsfw,
                )
                stoat_channel_id = result["_id"]
            except MigrationError as exc:
                if stoat_type == "Voice":
                    # Voice channels may fail (Bug #194) — retry as text.
                    state.warnings.append(
                        {
                            "phase": "channels",
                            "type": "voice_channel_bug",
                            "message": (
                                f"Voice channel '{unique_name}' failed, retrying as text: {exc}"
                            ),
                        }
                    )
                    on_event(
                        MigrationEvent(
                            phase="channels",
                            status="warning",
                            message=f"Voice channel '{unique_name}' failed, retrying as text",
                        )
                    )
                    result = await api_create_channel(
                        session,
                        config.stoat_url,
                        config.token,
                        state.stoat_server_id,
                        name=unique_name,
                        channel_type="Text",
                        description=description,
                        nsfw=nsfw,
                    )
                    stoat_channel_id = result["_id"]
                else:
                    raise

            state.channel_map[ch.id] = stoat_channel_id

            # Track forum post info for index channel generation.
            if discord_cat_id.startswith("forum-"):
                exp = export_by_channel.get(ch.id)
                msg_count = exp.message_count if exp else 0
                forum_channel_info.setdefault(discord_cat_id, []).append(
                    (stoat_channel_id, unique_name, msg_count)
                )
                # S15: Track discord channel membership for REPORT phase rebuild.
                state.forum_channel_members.setdefault(discord_cat_id, []).append(ch.id)

            # Apply channel permission overrides from Discord metadata.
            if ch_meta and not config.dry_run:
                if ch_meta.default_override:
                    try:
                        await api_set_channel_default_permissions(
                            session,
                            config.stoat_url,
                            config.token,
                            stoat_channel_id,
                            allow=ch_meta.default_override.allow,
                            deny=ch_meta.default_override.deny,
                        )
                        await asyncio.sleep(config.upload_delay)
                    except Exception as exc:  # noqa: BLE001
                        state.warnings.append(
                            {
                                "phase": "channels",
                                "type": "channel_default_perm_failed",
                                "message": f"Default override for '{unique_name}': {exc}",
                            }
                        )
                for ow in ch_meta.role_overrides:
                    stoat_role_id = state.role_map.get(ow.discord_role_id)
                    if stoat_role_id:
                        try:
                            await api_set_channel_role_permissions(
                                session,
                                config.stoat_url,
                                config.token,
                                stoat_channel_id,
                                stoat_role_id,
                                allow=ow.allow,
                                deny=ow.deny,
                            )
                            await asyncio.sleep(config.upload_delay)
                        except Exception as exc:  # noqa: BLE001
                            state.warnings.append(
                                {
                                    "phase": "channels",
                                    "type": "channel_role_perm_failed",
                                    "message": f"Role override for '{unique_name}': {exc}",
                                }
                            )

            # Track which Stoat category this channel belongs to.
            if discord_cat_id and discord_cat_id in state.category_map:
                stoat_cat_id = state.category_map[discord_cat_id]
                category_channels.setdefault(stoat_cat_id, []).append(stoat_channel_id)

            on_event(
                MigrationEvent(
                    phase="channels",
                    status="progress",
                    message=f"Created channel '{unique_name}'",
                    current=idx,
                    total=len(channels_to_create),
                )
            )

        # Create forum index channels — one per forum category.
        for forum_key, forum_name in forum_categories.items():
            forum_cat_stoat_id = state.category_map.get(forum_key)
            if not forum_cat_stoat_id:
                continue
            try:
                index_name = make_unique_channel_name(f"{forum_name}-index", existing_names)
                idx_result = await api_create_channel(
                    session,
                    config.stoat_url,
                    config.token,
                    state.stoat_server_id,
                    name=index_name,
                    channel_type="Text",
                )
                index_channel_id: str = idx_result["_id"]
                await asyncio.sleep(config.upload_delay)

                # Build the index message content (max 2000 chars).
                posts = forum_channel_info.get(forum_key, [])
                if posts:
                    lines = [f"**Forum: {forum_name}**\n"]
                    for post_ch_id, _post_name, post_count in posts:
                        lines.append(f"- <#{post_ch_id}> — {post_count} messages")
                    content = "\n".join(lines)
                    # Truncate to fit Stoat's 2000-char message limit.
                    if len(content) > 2000:
                        while lines and len("\n".join(lines)) > 1950:
                            lines.pop()
                        remaining = len(posts) - (len(lines) - 1)
                        lines.append(f"\n*...and {remaining} more posts*")
                        content = "\n".join(lines)
                else:
                    content = "No posts migrated."

                msg_result = await api_send_message(
                    session,
                    config.stoat_url,
                    config.token,
                    index_channel_id,
                    content=content,
                    masquerade={"name": "Discord Ferry"},
                    idempotency_key=f"ferry-forum-index-{forum_key}",
                )
                await asyncio.sleep(config.upload_delay)

                index_msg_id: str = msg_result["_id"]
                await api_pin_message(
                    session,
                    config.stoat_url,
                    config.token,
                    index_channel_id,
                    index_msg_id,
                )
                await asyncio.sleep(config.upload_delay)

                # Insert at position 0 so it appears at the top of the category.
                category_channels.setdefault(forum_cat_stoat_id, []).insert(0, index_channel_id)
                state.channel_map[f"forum-index-{forum_key}"] = index_channel_id

                on_event(
                    MigrationEvent(
                        phase="channels",
                        status="progress",
                        message=f"Created forum index for '{forum_name}'",
                    )
                )
            except Exception as exc:  # noqa: BLE001
                state.warnings.append(
                    {
                        "phase": "channels",
                        "type": "forum_index_failed",
                        "message": (f"Failed to create forum index for '{forum_name}': {exc}"),
                    }
                )
                on_event(
                    MigrationEvent(
                        phase="channels",
                        status="warning",
                        message=f"Forum index for '{forum_name}' failed: {exc}",
                    )
                )

        # Assign channels to categories via a single PATCH.
        # This replaces the categories array set by run_categories(). Safe because
        # cat_titles is built from state.category_map (all categories) and every
        # category has at least one channel (DCE only exports non-empty categories).
        if category_channels:
            cat_titles: dict[str, str] = {}
            for export in exports:
                cat_id = export.channel.category_id
                if cat_id and cat_id in state.category_map:
                    cat_titles[state.category_map[cat_id]] = truncate_name(export.channel.category)
            # Add forum category titles.
            for forum_key, forum_name in forum_categories.items():
                stoat_forum_cat_id = state.category_map.get(forum_key)
                if stoat_forum_cat_id:
                    cat_titles[stoat_forum_cat_id] = truncate_name(forum_name)

            # Build the full categories array.
            all_categories: list[dict[str, Any]] = []
            for stoat_cat_id, title in cat_titles.items():
                all_categories.append(
                    {
                        "id": stoat_cat_id,
                        "title": title,
                        "channels": category_channels.get(stoat_cat_id, []),
                    }
                )

            await api_upsert_categories(
                session,
                config.stoat_url,
                config.token,
                state.stoat_server_id,
                all_categories,
            )

    on_event(
        MigrationEvent(
            phase="channels",
            status="progress",
            message=f"Created {len(channels_to_create)} channels",
        )
    )
