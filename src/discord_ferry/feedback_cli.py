"""Interactive command-line feedback flow."""

from __future__ import annotations

import platform
import sys
from pathlib import Path

import aiohttp
import click

from discord_ferry import __version__
from discord_ferry.core.security import sanitize_secrets_for_public_output
from discord_ferry.feedback import (
    FeedbackClient,
    FeedbackDiagnostics,
    FeedbackDraft,
    FeedbackKind,
    FeedbackServiceError,
    FeedbackValidationError,
    RuntimeContext,
    build_diagnostics,
    render_diagnostics,
)

_KIND_LABELS = {
    "Bug": FeedbackKind.BUG,
    "Idea": FeedbackKind.IDEA,
    "General": FeedbackKind.GENERAL,
}
_SUBMISSION_ERRORS = (
    FeedbackServiceError,
    FeedbackValidationError,
    aiohttp.ClientError,
    TimeoutError,
    OSError,
)


def _print_feedback_hint(*, to_stderr: bool = False) -> None:
    """Point a handled command failure to the opt-in feedback flow."""

    click.echo("Report this problem with: ferry feedback", err=to_stderr)


def _choice(label: str, choices: tuple[str, ...]) -> str:
    return str(
        click.prompt(
            label,
            type=click.Choice(choices, case_sensitive=False),
            show_choices=True,
        )
    )


def _optional_prompt(label: str) -> str | None:
    value = str(click.prompt(label, default="", show_default=False))
    return value or None


def _runtime_context() -> RuntimeContext:
    return RuntimeContext(
        ferry_version=__version__,
        operating_system=sys.platform,
        architecture=platform.machine(),
        interface="cli",
        stage="setup",
        last_error=None,
    )


def _default_log_path() -> Path:
    return Path.home() / ".discord-ferry" / "logs" / "ferry.log"


def _edit_diagnostics(diagnostics: FeedbackDiagnostics) -> FeedbackDiagnostics:
    last_error = _optional_prompt("New last error (blank removes it)")
    log_excerpt = _optional_prompt("New recent log text (blank removes it)")
    return FeedbackDiagnostics.from_mapping(
        {
            **diagnostics.to_mapping(),
            "last_error": last_error,
            "log_excerpt": log_excerpt,
        }
    )


def _review_diagnostics(
    diagnostics: FeedbackDiagnostics,
) -> FeedbackDiagnostics | None:
    while True:
        click.echo("\nDiagnostic preview")
        click.echo(render_diagnostics(diagnostics))
        click.echo("Warning: credentials are removed, but names or message content may remain.")
        action = _choice("Diagnostics action", ("Include", "Edit", "Remove"))
        if action.casefold() == "edit":
            diagnostics = _edit_diagnostics(diagnostics)
            continue
        if action.casefold() == "remove":
            return None
        if click.confirm(
            "I understand these diagnostics will be public on GitHub",
            default=False,
        ):
            return diagnostics


def _collect_diagnostics() -> FeedbackDiagnostics | None:
    if not click.confirm("Include diagnostics?", default=False):
        return None
    diagnostics = build_diagnostics(
        _runtime_context(),
        include_logs=click.confirm("Include recent Ferry log text?", default=False),
        log_path=_default_log_path(),
    )
    return _review_diagnostics(diagnostics)


def _capture_draft() -> FeedbackDraft:
    kind_label = _choice("Type", tuple(_KIND_LABELS))
    diagnostics = None
    draft = FeedbackDraft(
        kind=_KIND_LABELS[kind_label.title()],
        description=str(click.prompt("What would you like to share?", type=str)),
        expected=_optional_prompt("Expected result (optional)"),
        reproduction=_optional_prompt("Reproduction steps (optional)"),
        contact_email=_optional_prompt("Private contact email (optional)"),
    )
    diagnostics = _collect_diagnostics()
    if diagnostics is not None:
        draft.edit_diagnostics(diagnostics)
        draft.acknowledge_diagnostics()
    return draft


def _edit_draft(draft: FeedbackDraft) -> None:
    field = _choice(
        "Edit field",
        (
            "Type",
            "Report",
            "Expected result",
            "Reproduction steps",
            "Diagnostics",
            "Contact email",
        ),
    )
    match field.casefold():
        case "type":
            kind_label = _choice("Type", tuple(_KIND_LABELS))
            draft.edit_kind(_KIND_LABELS[kind_label.title()])
        case "report":
            draft.edit_description(str(click.prompt("Report", type=str)))
        case "expected result":
            draft.edit_expected(_optional_prompt("Expected result (blank removes it)"))
        case "reproduction steps":
            draft.edit_reproduction(_optional_prompt("Reproduction steps (blank removes them)"))
        case "diagnostics":
            diagnostics = _collect_diagnostics()
            draft.edit_diagnostics(diagnostics)
            if diagnostics is not None:
                draft.acknowledge_diagnostics()
        case "contact email":
            draft.edit_contact_email(_optional_prompt("Private contact email (blank removes it)"))


def _review_and_confirm(draft: FeedbackDraft) -> bool:
    while True:
        click.echo("\nPublic preview")
        click.echo(draft.render_public_body())
        action = _choice("Review action", ("Continue", "Edit", "Cancel"))
        if action.casefold() == "edit":
            _edit_draft(draft)
            continue
        if action.casefold() == "cancel":
            return False
        if not click.confirm(
            "I understand this report will be public on GitHub",
            default=False,
        ):
            return False
        draft.acknowledge_public()
        return click.confirm("Send this report now?", default=False)


def _print_draft(draft: FeedbackDraft) -> None:
    click.echo("\nComplete public draft")
    click.echo(draft.copy_text())


def _save_draft(draft: FeedbackDraft) -> None:
    path = Path(str(click.prompt("Save path", type=click.Path(path_type=Path)))).expanduser()
    include_contact = draft.contact_email is not None and click.confirm(
        "Include the private contact email in this local file?",
        default=False,
    )
    try:
        draft.save(path, include_contact=include_contact)
    except OSError as exc:
        click.echo(f"Could not save the draft: {sanitize_secrets_for_public_output(str(exc))}")
    else:
        click.echo(f"Draft saved to {path}")


async def _submit_draft(draft: FeedbackDraft) -> bool:
    async with FeedbackClient() as client:
        while True:
            try:
                receipt = await client.submit(draft)
            except _SUBMISSION_ERRORS as exc:
                message = sanitize_secrets_for_public_output(str(exc))
                click.echo(f"Feedback could not be sent: {message}")
                while True:
                    action = _choice(
                        "Recovery action",
                        ("Retry", "Print", "Save", "Edit", "Cancel"),
                    )
                    if action.casefold() == "retry":
                        break
                    if action.casefold() == "print":
                        _print_draft(draft)
                        continue
                    if action.casefold() == "save":
                        _save_draft(draft)
                        continue
                    if action.casefold() == "edit":
                        _edit_draft(draft)
                        return True
                    return False
            else:
                click.echo(f"Feedback shared: {receipt.url}")
                return False


async def run_feedback_cli() -> None:
    """Run the command-line feedback flow."""

    draft = _capture_draft()
    while _review_and_confirm(draft):
        if not await _submit_draft(draft):
            return
    click.echo("Feedback cancelled.")


__all__ = ["_print_feedback_hint", "run_feedback_cli"]
