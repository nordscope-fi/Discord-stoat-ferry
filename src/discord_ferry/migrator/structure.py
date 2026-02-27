"""Phases 3-6: SERVER, ROLES, CATEGORIES, CHANNELS migration."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from discord_ferry.core.events import MigrationEvent
from discord_ferry.errors import MigrationError
from discord_ferry.migrator.api import (
    api_create_category,
    api_create_channel,
    api_create_role,
    api_create_server,
    api_edit_category,
    api_edit_role,
    api_edit_server,
    api_fetch_server,
    get_session,
)
from discord_ferry.uploader.autumn import upload_with_cache

if TYPE_CHECKING:
    from discord_ferry.config import FerryConfig
    from discord_ferry.core.events import EventCallback
    from discord_ferry.parser.models import DCEChannel, DCEExport, DCERole
    from discord_ferry.state import MigrationState

logger = logging.getLogger(__name__)

# Minimum permissions for the ferry bot to operate:
# ManageRole(3), ViewChannel(20), ReadMessageHistory(21), SendMessage(22),
# ManageMessages(23), SendEmbeds(26), UploadFiles(27), Masquerade(28), React(29)
FERRY_MIN_PERMISSIONS = 1_071_644_680


def make_unique_channel_name(name: str, existing_names: set[str]) -> str:
    """Return a name unique within ``existing_names``, truncating to 64 chars.

    Args:
        name: Desired channel name.
        existing_names: Set of already-used names. Updated in place with the returned name.

    Returns:
        A name that is not already in ``existing_names`` (at most 64 chars).
    """
    base = name[:64]
    if base not in existing_names:
        existing_names.add(base)
        return base
    # Reserve space for the suffix (e.g. "-1", "-99")
    counter = 1
    while True:
        suffix = f"-{counter}"
        candidate = f"{name[: 64 - len(suffix)]}{suffix}"
        if candidate not in existing_names:
            existing_names.add(candidate)
            return candidate
        counter += 1


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
                            {"phase": "server", "message": f"Icon upload failed: {exc}"}
                        )
                        on_event(
                            MigrationEvent(
                                phase="server",
                                status="warning",
                                message=f"Icon upload failed, continuing: {exc}",
                            )
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
        for msg in export.messages:
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
                session, config.stoat_url, config.token, state.stoat_server_id, role.name
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
        for idx, (discord_cat_id, cat_name) in enumerate(unique_categories, start=1):
            result = await api_create_category(
                session, config.stoat_url, config.token, state.stoat_server_id, cat_name
            )
            state.category_map[discord_cat_id] = result["id"]

            on_event(
                MigrationEvent(
                    phase="categories",
                    status="progress",
                    message=f"Created category '{cat_name}'",
                    current=idx,
                    total=len(unique_categories),
                )
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
    # Deduplicate exports by channel ID and skip category channels (type 4).
    seen_channel_ids: set[str] = set()
    # Each entry: (channel, stoat_type, unique_name, discord_category_id, is_thread)
    channels_to_create: list[tuple[DCEChannel, str | None, str, str, bool]] = []
    existing_names: set[str] = set()

    for export in exports:
        channel: DCEChannel = export.channel

        # Skip category channels — handled in Phase 5.
        if channel.type == 4:
            continue

        # Skip thread/forum exports when skip_threads is enabled.
        if config.skip_threads and export.is_thread:
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
            ch_name = f"{export.parent_channel_name}-{ch_name}"

        unique_name = make_unique_channel_name(ch_name, existing_names)
        channels_to_create.append(
            (channel, stoat_type, unique_name, channel.category_id, export.is_thread)
        )

    if len(channels_to_create) > config.max_channels:
        overflow = len(channels_to_create) - config.max_channels
        # Sort so threads come last, then truncate — preserves main channels.
        channels_to_create.sort(key=lambda t: t[4])  # False (main) before True (thread)
        dropped = channels_to_create[config.max_channels :]
        channels_to_create = channels_to_create[: config.max_channels]
        dropped_names = [entry[2] for entry in dropped]
        on_event(
            MigrationEvent(
                phase="channels",
                status="warning",
                message=(
                    f"Total channels ({config.max_channels + overflow}) exceeds Stoat limit "
                    f"of {config.max_channels}. Dropped {overflow} channel(s): "
                    f"{', '.join(dropped_names[:10])}"
                    f"{'...' if len(dropped_names) > 10 else ''}"
                ),
            )
        )
        state.warnings.append(
            {
                "phase": "channels",
                "message": (
                    f"Dropped {overflow} channel(s) exceeding {config.max_channels} limit: "
                    f"{', '.join(dropped_names)}"
                ),
            }
        )

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

    async with get_session(config) as session:
        for idx, (channel, stoat_type, unique_name, discord_cat_id, _is_thread) in enumerate(
            channels_to_create, start=1
        ):
            ch: DCEChannel = channel
            description = ch.topic if ch.topic else None

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
                )
                stoat_channel_id = result["_id"]
            except MigrationError as exc:
                if stoat_type == "Voice":
                    # Voice channels may fail (Bug #194) — retry as text.
                    state.warnings.append(
                        {
                            "phase": "channels",
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
                    )
                    stoat_channel_id = result["_id"]
                else:
                    raise

            state.channel_map[ch.id] = stoat_channel_id

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

        # Assign channels to categories (two-step process).
        for stoat_cat_id, stoat_channel_ids in category_channels.items():
            await api_edit_category(
                session,
                config.stoat_url,
                config.token,
                state.stoat_server_id,
                stoat_cat_id,
                stoat_channel_ids,
            )

    on_event(
        MigrationEvent(
            phase="channels",
            status="progress",
            message=f"Created {len(channels_to_create)} channels",
        )
    )
