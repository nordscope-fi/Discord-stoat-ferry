"""Migration report generator."""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING

from discord_ferry.discord.metadata import load_discord_metadata

if TYPE_CHECKING:
    from pathlib import Path

    from discord_ferry.config import FerryConfig
    from discord_ferry.discord.metadata import DiscordMetadata
    from discord_ferry.parser.models import DCEExport
    from discord_ferry.state import MigrationState


def generate_report(
    config: FerryConfig,
    state: MigrationState,
    exports: list[DCEExport],
) -> dict[str, object]:
    """Generate a migration report and write it to output_dir/migration_report.json.

    Args:
        config: Ferry configuration, used for output_dir.
        state: Current migration state with all ID maps and logs.
        exports: List of parsed DCE exports, used for guild info and message counts.

    Returns:
        The report dict that was serialised to disk.
    """
    duration_seconds = _calculate_duration(state.started_at, state.completed_at)

    source_guild: dict[str, str]
    if exports:
        guild = exports[0].guild
        source_guild = {"id": guild.id, "name": guild.name}
    else:
        source_guild = {"id": "", "name": ""}

    total_messages = sum(e.message_count for e in exports)
    messages_imported = len(state.message_map)
    messages_skipped = max(0, total_messages - messages_imported)

    threads_flattened = sum(1 for e in exports if e.is_thread)

    report: dict[str, object] = {
        "started_at": state.started_at,
        "completed_at": state.completed_at,
        "duration_seconds": duration_seconds,
        "source_guild": source_guild,
        "target_server_id": state.stoat_server_id,
        "summary": {
            "channels_created": len(state.channel_map),
            "roles_created": len(state.role_map),
            "categories_created": len(state.category_map),
            "messages_imported": messages_imported,
            "messages_skipped": messages_skipped,
            "attachments_uploaded": state.attachments_uploaded,
            "attachments_skipped": state.attachments_skipped,
            "emoji_created": len(state.emoji_map),
            "reactions_added": state.reactions_applied,
            "pins_restored": state.pins_applied,
            "threads_flattened": threads_flattened,
            "errors": len(state.errors),
            "warnings": len(state.warnings),
        },
        "warnings": state.warnings,
        "errors": state.errors,
        "maps": {
            "channels": state.channel_map,
            "roles": state.role_map,
            "emoji": state.emoji_map,
        },
    }

    # Post-migration validation results
    if state.validation_results:
        report["validation"] = state.validation_results

    # Failed message tracking (dead-letter queue)
    report["failed_messages"] = len(state.failed_messages)
    report["failed_message_ids"] = [fm.discord_msg_id for fm in state.failed_messages]

    # Orphan upload tracking
    orphaned_ids = [aid for aid in state.autumn_uploads if aid not in state.referenced_autumn_ids]
    report["orphaned_uploads"] = len(orphaned_ids)
    if orphaned_ids:
        report["orphaned_ids"] = orphaned_ids

    # Build post-migration checklist
    discord_meta = load_discord_metadata(config.output_dir)
    checklist = _build_checklist(
        state,
        has_permissions=discord_meta is not None,
        discord_meta=discord_meta,
    )
    report["checklist"] = checklist

    _write_report(config.output_dir, report)

    return report


def _build_checklist(
    state: MigrationState,
    has_permissions: bool,
    discord_meta: DiscordMetadata | None = None,
) -> list[dict[str, str]]:
    """Build a dynamic post-migration checklist of manual steps.

    Args:
        state: Migration state with maps and counters.
        has_permissions: Whether Discord permissions were migrated.
        discord_meta: Optional Discord metadata for user override info.

    Returns:
        List of checklist items with 'task' and 'status' keys.
    """
    items: list[dict[str, str]] = []

    # Always present items
    items.append(
        {
            "task": "Verify channel order and category assignments in Stoat",
            "status": "todo",
        }
    )
    items.append(
        {
            "task": "Check message formatting in a few channels",
            "status": "todo",
        }
    )

    # Permission-dependent items
    if has_permissions:
        items.append(
            {
                "task": "Review migrated role permissions in Stoat server settings",
                "status": "todo",
            }
        )
        items.append(
            {
                "task": "Verify channel permission overrides are correct",
                "status": "todo",
            }
        )
    else:
        items.append(
            {
                "task": "Set up role permissions manually (not migrated — no Discord token)",
                "status": "todo",
            }
        )

    # User-specific permission overrides (Stoat doesn't support these)
    if discord_meta and discord_meta.user_override_channels:
        count = len(discord_meta.user_override_channels)
        names = ", ".join(str(ch["channel_name"]) for ch in discord_meta.user_override_channels[:5])
        suffix = f" and {count - 5} more" if count > 5 else ""
        items.append(
            {
                "task": (
                    f"Re-apply user-specific permission overrides manually in {count} "
                    f"channel(s): {names}{suffix}. Stoat only supports role-based overrides — "
                    "use roles to replicate per-user access control."
                ),
                "status": "todo",
            }
        )

    # Conditional items based on state
    if state.emoji_map:
        items.append(
            {
                "task": "Verify custom emoji are rendering correctly",
                "status": "todo",
            }
        )

    if state.warnings:
        items.append(
            {
                "task": f"Review {len(state.warnings)} warning(s) in the report",
                "status": "todo",
            }
        )

    if state.errors:
        items.append(
            {
                "task": (
                    f"Investigate {len(state.errors)} error(s) — some content may not have migrated"
                ),
                "status": "todo",
            }
        )

    # Final items
    items.append(
        {
            "task": "Invite members to the new Stoat server",
            "status": "todo",
        }
    )

    return items


def _calculate_duration(started_at: str, completed_at: str) -> float:
    if not started_at or not completed_at:
        return 0
    try:
        start = datetime.fromisoformat(started_at)
        end = datetime.fromisoformat(completed_at)
        return (end - start).total_seconds()
    except ValueError:
        return 0


def generate_markdown_report(
    config: FerryConfig,
    state: MigrationState,
    exports: list[DCEExport],
) -> None:
    """Generate a human-readable markdown migration report.

    Args:
        config: Ferry configuration, used for output_dir.
        state: Current migration state with all ID maps and logs.
        exports: List of parsed DCE exports (unused but kept for signature parity).
    """
    lines: list[str] = []
    lines.append("# Migration Report\n")
    lines.append(f"**Started:** {state.started_at}")
    lines.append(f"**Completed:** {state.completed_at}\n")

    lines.append("## Summary\n")
    lines.append("| Metric | Count |")
    lines.append("|--------|-------|")
    lines.append(f"| Channels created | {len(state.channel_map)} |")
    lines.append(f"| Roles created | {len(state.role_map)} |")
    lines.append(f"| Emoji created | {len(state.emoji_map)} |")
    lines.append(f"| Messages imported | {len(state.message_map)} |")
    lines.append(f"| Messages failed | {len(state.failed_messages)} |")
    lines.append(f"| Attachments uploaded | {state.attachments_uploaded} |")
    lines.append(f"| Attachments skipped | {state.attachments_skipped} |")
    lines.append(f"| Reactions applied | {state.reactions_applied} |")
    lines.append(f"| Pins restored | {state.pins_applied} |")
    lines.append("")

    lines.append("## Errors\n")
    if state.failed_messages:
        for fm in state.failed_messages:
            lines.append(f"- Message `{fm.discord_msg_id}`: {fm.error}")
    else:
        lines.append("No errors.\n")

    lines.append("\n## Warnings\n")
    if state.warnings:
        for w in state.warnings:
            lines.append(f"- [{w.get('type', 'unknown')}] {w.get('message', '')}")
    else:
        lines.append("No warnings.\n")

    config.output_dir.mkdir(parents=True, exist_ok=True)
    output_path = config.output_dir / "migration_report.md"
    output_path.write_text("\n".join(lines), encoding="utf-8")


def _write_report(output_dir: Path, report: dict[str, object]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "migration_report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
