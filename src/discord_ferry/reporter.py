"""Migration report generator."""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING

from discord_ferry.core.atomicio import atomic_write_text
from discord_ferry.core.security import safe_sanitize, sanitize_secrets, scrub_document
from discord_ferry.discord.metadata import load_discord_metadata

if TYPE_CHECKING:
    from pathlib import Path

    from discord_ferry.config import FerryConfig
    from discord_ferry.core.security import SecureTokenStore
    from discord_ferry.discord.metadata import DiscordMetadata
    from discord_ferry.parser.models import DCEExport
    from discord_ferry.state import MigrationState


def _clamp01(value: float) -> float:
    """Clamp a ratio into [0.0, 1.0].

    Defends against scope-mismatched numerators (Batch 7 S3): in incremental mode the
    denominator is this-run's partial source total while failed_count is carried
    cumulatively, so an unclamped ratio could go negative (or >1). A no-op for any ratio
    already in range.
    """
    return max(0.0, min(1.0, value))


def compute_fidelity_score(
    total_messages: int,
    failed_count: int,
    attachments_uploaded: int,
    attachments_skipped: int,
    embeds_total: int = 0,
    embeds_dropped: int = 0,
    replies_linked: int = 0,
    replies_total: int = 0,
    reactions_applied: int = 0,
    reactions_total: int = 0,
) -> dict[str, float]:
    """Compute a quantified fidelity score for the migration.

    The overall score is a weighted combination across 5 categories:
    - 40% weight on message success rate
    - 25% weight on attachment success rate
    - 15% weight on embed preservation rate
    - 10% weight on reply linkage rate
    - 10% weight on reaction application rate

    Args:
        total_messages: Total messages in exports.
        failed_count: Number of messages that failed to migrate.
        attachments_uploaded: Number of attachments successfully uploaded.
        attachments_skipped: Number of attachments that could not be uploaded.
        embeds_total: Total embeds encountered across all messages.
        embeds_dropped: Embeds that could not be migrated (beyond cap, no title/description).
        replies_linked: Reply references successfully resolved to a Stoat message ID.
        replies_total: Total reply references encountered.
        reactions_applied: Reactions successfully applied in native mode.
        reactions_total: Total reactions queued for application.

    Returns:
        Dict with 'overall', 'messages', 'attachments', 'embeds', 'replies',
        and 'reactions' scores (0-100).
    """
    msg_ratio = _clamp01((total_messages - failed_count) / max(total_messages, 1))
    att_ratio = _clamp01(attachments_uploaded / max(attachments_uploaded + attachments_skipped, 1))
    embed_ratio = _clamp01((embeds_total - embeds_dropped) / embeds_total) if embeds_total else 1.0
    reply_ratio = _clamp01(replies_linked / replies_total) if replies_total else 1.0
    reaction_ratio = (
        _clamp01(reactions_applied / max(reactions_total, 1)) if reactions_total else 1.0
    )
    overall = (
        msg_ratio * 0.40
        + att_ratio * 0.25
        + embed_ratio * 0.15
        + reply_ratio * 0.10
        + reaction_ratio * 0.10
    )
    return {
        "overall": round(overall * 100, 1),
        "messages": round(msg_ratio * 100, 1),
        "attachments": round(att_ratio * 100, 1),
        "embeds": round(embed_ratio * 100, 1),
        "replies": round(reply_ratio * 100, 1),
        "reactions": round(reaction_ratio * 100, 1),
    }


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
    duration_seconds = calculate_duration(state.started_at, state.completed_at)

    source_guild: dict[str, str]
    if exports:
        guild = exports[0].guild
        source_guild = {"id": guild.id, "name": guild.name}
    else:
        source_guild = {"id": "", "name": ""}

    total_messages = state.source_messages_total or sum(e.message_count for e in exports)
    messages_imported = len(state.message_map)
    messages_skipped = max(0, total_messages - messages_imported)

    threads_flattened = sum(1 for e in exports if e.is_thread)

    # S18: Compute migration fidelity score.
    fidelity = compute_fidelity_score(
        total_messages=total_messages,
        failed_count=len(state.failed_messages),
        attachments_uploaded=state.attachments_uploaded,
        attachments_skipped=state.attachments_skipped,
        embeds_total=state.embeds_total,
        embeds_dropped=state.embeds_dropped,
        replies_linked=state.replies_linked,
        replies_total=state.replies_total,
        reactions_applied=state.reactions_applied,
        reactions_total=(
            state.reactions_applied
            + state.reactions_capped
            + state.reactions_dropped
            + len(state.pending_reactions)
        ),
    )

    # Delta stats: messages migrated in this run vs cumulatively
    this_run_messages = messages_imported - state.prior_messages_total
    cumulative_messages = messages_imported

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
        "delta": {
            "this_run": this_run_messages,
            "cumulative": cumulative_messages,
            "prior_run_total": state.prior_messages_total,
        },
        "fidelity": fidelity,
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
    report["invite"] = {"code": state.invite_code, "url": state.invite_url}
    report["native_fidelity"] = dict(state.native_fidelity_counts)

    _write_report(config.output_dir, report, config.token_store)

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
    if state.invite_code:
        invite_target = state.invite_url or state.invite_code
        items.append({"task": f"Invite members using: {invite_target}", "status": "todo"})
    else:
        items.append(
            {
                "task": "Invite members to the new Stoat server",
                "status": "todo",
            }
        )

    return items


def calculate_duration(started_at: str, completed_at: str) -> float:
    """Compute elapsed seconds between two ISO-8601 timestamps.

    Returns 0.0 when either timestamp is empty or cannot be parsed.
    Used by both ``reporter.generate_markdown_report`` and
    ``stats.summarize_state`` so the two surfaces agree on duration math.

    Args:
        started_at: ISO-8601 timestamp string for migration start.
        completed_at: ISO-8601 timestamp string for migration end.

    Returns:
        Elapsed seconds as a float. 0.0 if either input is missing or invalid.
    """
    if not started_at or not completed_at:
        return 0.0
    try:
        start = datetime.fromisoformat(started_at)
        end = datetime.fromisoformat(completed_at)
        return (end - start).total_seconds()
    except ValueError:
        return 0.0


def generate_markdown_report(
    config: FerryConfig,
    state: MigrationState,
    exports: list[DCEExport],
) -> None:
    """Generate a human-readable markdown migration report.

    Args:
        config: Ferry configuration, used for output_dir.
        state: Current migration state with all ID maps and logs.
        exports: List of parsed DCE exports, used for message counts.
    """
    lines: list[str] = []
    lines.append("# Migration Report\n")
    lines.append(f"**Started:** {state.started_at}")
    lines.append(f"**Completed:** {state.completed_at}\n")

    # S18: Fidelity score section.
    total_msgs = state.source_messages_total or sum(e.message_count for e in exports)
    fidelity = compute_fidelity_score(
        total_messages=total_msgs,
        failed_count=len(state.failed_messages),
        attachments_uploaded=state.attachments_uploaded,
        attachments_skipped=state.attachments_skipped,
        embeds_total=state.embeds_total,
        embeds_dropped=state.embeds_dropped,
        replies_linked=state.replies_linked,
        replies_total=state.replies_total,
        reactions_applied=state.reactions_applied,
        reactions_total=(
            state.reactions_applied
            + state.reactions_capped
            + state.reactions_dropped
            + len(state.pending_reactions)
        ),
    )
    lines.append("## Fidelity Score\n")
    lines.append(f"**Overall:** {fidelity['overall']}%  ")
    lines.append(f"**Messages:** {fidelity['messages']}%  ")
    lines.append(f"**Attachments:** {fidelity['attachments']}%  ")
    lines.append(f"**Embeds:** {fidelity['embeds']}%  ")
    lines.append(f"**Replies:** {fidelity['replies']}%  ")
    lines.append(f"**Reactions:** {fidelity['reactions']}%\n")

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

    if state.invite_code:
        lines.append("\n## Invite\n")
        lines.append(f"- {state.invite_url or state.invite_code}\n")

    # The same free text that reaches report.json reaches this file, so it gets the
    # same redaction. Applied per value rather than to the finished markdown: the
    # document also carries Discord message identifiers, and masking works by
    # substring replacement, so scrubbing the whole string would rewrite them.
    # Issue #140, ADR-014.
    def _text(value: str) -> str:
        return sanitize_secrets(safe_sanitize(config.token_store, value))

    lines.append("## Errors\n")
    if state.failed_messages:
        for fm in state.failed_messages:
            lines.append(f"- Message `{fm.discord_msg_id}`: {_text(fm.error)}")
    else:
        lines.append("No errors.\n")

    lines.append("\n## Warnings\n")
    if state.warnings:
        for w in state.warnings:
            lines.append(f"- [{w.get('type', 'unknown')}] {_text(w.get('message', ''))}")
    else:
        lines.append("No warnings.\n")

    nf = state.native_fidelity_counts
    if nf:
        lines.append("\n## Native Fidelity\n")
        if nf.get("slowmode"):
            lines.append(f"- Slowmode set on {nf['slowmode']} channel(s)\n")
        if nf.get("user_limit"):
            lines.append(f"- Voice user-limit set on {nf['user_limit']} channel(s)\n")
        if nf.get("role_icons"):
            lines.append(f"- Icons set on {nf['role_icons']} role(s)\n")
        if nf.get("structural_roles"):
            lines.append(
                f"- Recovered {nf['structural_roles']} structural role(s) absent from the export\n"
            )

    config.output_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(config.output_dir / "migration_report.md", "\n".join(lines))


def _write_report(
    output_dir: Path, report: dict[str, object], token_store: SecureTokenStore | None
) -> None:
    """Serialise *report* to migration_report.json, redacting free text on the way out.

    ``token_store`` is required rather than defaulting to None. Passing None is valid
    and means registry-only redaction, but it has to be chosen: a second caller that
    simply forgot the argument would otherwise get weaker redaction and no error.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    # Redact here rather than at the ~59 append sites feeding warnings and errors: a
    # call site cannot forget a scrub it never has to make, and forgetting was the
    # defect. This file is what the bug report template asks users to attach, so
    # anything in it is effectively published. Issue #140, ADR-014.
    scrubbed = scrub_document(report, token_store)
    atomic_write_text(output_dir / "migration_report.json", json.dumps(scrubbed, indent=2))
