"""GUI tool pages and their shared safety runner (issue #484 and children).

These pages let a GUI user reach the CLI's post-migration and preflight commands
(check, repair, retry, probe, blueprint, build, and the diagnostic three) without
a terminal. Every page that calls the Stoat API goes through :func:`prepare_tool_call`
first, so the token is registered for redaction and the rate limiter exists before
any request.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from nicegui import app, background_tasks, ui

import discord_ferry.migrator.api as _api
from discord_ferry.blueprint import blueprint_from_exports, export_blueprint
from discord_ferry.config import FerryConfig
from discord_ferry.core.engine import run_repair, run_retry_failed
from discord_ferry.core.http import format_proxy_notices
from discord_ferry.core.security import register_secret, sanitize_secrets
from discord_ferry.errors import CheckError, MigrationError, StateError
from discord_ferry.migrator.probe import run_probe
from discord_ferry.migrator.verify import UNREPAIRED_WARNING_TYPES, run_check
from discord_ferry.parser.dce_parser import parse_export_directory
from discord_ferry.state import load_state

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from discord_ferry.blueprint import ServerBlueprint
    from discord_ferry.core.events import MigrationEvent
    from discord_ferry.migrator.probe import ProbeReport
    from discord_ferry.migrator.verify import CheckReport, RepairOutcome
    from discord_ferry.state import MigrationState

T = TypeVar("T")


#: One colour per report status. Covers the CheckReport vocabulary
#: (ok/warn/fail/unverifiable) and the ProbeReport subset (ok/warn/fail). The
#: GUI's own event colour map does not cover these, so the tool pages carry
#: their own. Unknown statuses fall back to grey rather than raising.
_STATUS_COLOURS: dict[str, str] = {
    "ok": "green",
    "warn": "amber",
    "fail": "red",
    "unverifiable": "grey",
}


def _status_colour(status: str) -> str:
    """The badge colour for a report status, grey for anything unrecognised."""
    return _STATUS_COLOURS.get(status, "grey")


def _check_rows(report: CheckReport) -> list[dict[str, str]]:
    """One table row per check result, carrying its badge colour.

    The name and detail are server-controlled (entity names, prose from the
    Stoat API), and the table is a rendered sink the logging formatter does not
    cover, so both are sanitized here, the same reason ``_safe_push`` sanitizes
    the log widget. The status and colour are internal enums, not sanitized.
    """
    return [
        {
            "name": sanitize_secrets(r.name),
            "status": r.status,
            "detail": sanitize_secrets(r.detail),
            "colour": _status_colour(r.status),
        }
        for r in report.results
    ]


def _check_verdict(report: CheckReport) -> tuple[str, str]:
    """The overall pass/fail line and its colour, from ``report.has_failures``.

    Uses the same signal as the CLI check exit code (`1 if has_failures else 0`),
    so the GUI verdict and the CLI verdict cannot diverge for the same report.
    """
    if report.has_failures:
        return "Check found problems.", "text-red-600"
    return "Check passed.", "text-green-600"


def _repair_verdict(outcome: RepairOutcome, state: MigrationState) -> tuple[bool, str, str]:
    """Return (failed, label, colour), mirroring the CLI repair exit code exactly.

    The CLI is ``1 if (state.failed_messages or declined) else 0`` where declined
    is ``outcome.declined`` filtered to ``UNREPAIRED_WARNING_TYPES`` (cli.py, the
    repair command). Sourced from the returned outcome, not the never-cleared
    ``state.warnings``, per the #308 fix.
    """
    declined = [w for w in outcome.declined if w.get("type") in UNREPAIRED_WARNING_TYPES]
    if state.failed_messages or declined:
        return True, "Repair left unfixable problems.", "text-red-600"
    return False, "Repair complete.", "text-green-600"


def render_check_report(report: CheckReport) -> None:
    """Render a CheckReport as a summary line and a per-result table.

    The report is a return value, not an event stream, so this renders from the
    object directly. The repair page renders its own RepairOutcome instead (that
    object does not carry a check), see render_repair_outcome.
    """
    counts = report.counts()
    ui.label(
        f"{counts['ok']} ok, {counts['warn']} warn, "
        f"{counts['fail']} fail, {counts['unverifiable']} unverifiable"
    ).classes("text-sm text-gray-600")
    columns = [
        {"name": "name", "label": "Entity", "field": "name", "align": "left"},
        {"name": "status", "label": "Status", "field": "status", "align": "left"},
        {"name": "detail", "label": "Detail", "field": "detail", "align": "left"},
    ]
    ui.table(columns=columns, rows=_check_rows(report)).classes("w-full mt-2")


def render_repair_outcome(outcome: RepairOutcome) -> None:
    """Render what a repair did, and a table of what it could not fix.

    RepairOutcome does not store a post-repair check (the engine leaves that to
    the shell), so this renders the outcome's own fields rather than reusing the
    check table. The declined text is server-controlled, so it is sanitized.
    """
    dlq = outcome.dead_letter
    ui.label(
        f"Recreated {len(outcome.recreated_channels)} channels, "
        f"{len(outcome.recreated_roles)} roles, "
        f"{len(outcome.recreated_categories)} categories. "
        f"Restored {len(outcome.restored_tails)} tails. "
        f"Dead-letter drained {dlq.get('drained', 0)}, {dlq.get('remaining', 0)} remaining."
    ).classes("text-sm text-gray-600")
    if outcome.declined:
        ui.label("Could not fix:").classes("text-sm font-bold mt-2 text-red-600")
        columns = [
            {"name": "type", "label": "Kind", "field": "type", "align": "left"},
            {"name": "detail", "label": "Detail", "field": "detail", "align": "left"},
        ]
        rows = [
            {
                "type": sanitize_secrets(str(w.get("type", ""))),
                "detail": sanitize_secrets(str(w.get("detail", w.get("name", "")))),
            }
            for w in outcome.declined
        ]
        ui.table(columns=columns, rows=rows).classes("w-full mt-1")


def _probe_rows(report: ProbeReport) -> list[dict[str, str]]:
    """One table row per probe check, carrying its badge colour.

    ``ProbeCheck.detail`` is instance-controlled prose (limits, header dumps,
    exception messages from a Stoat we do not run), and the table bypasses the
    logging formatter, so the detail and name are sanitized here, the same reason
    ``_check_rows`` sanitizes the check table. The status is an internal enum.
    """
    return [
        {
            "name": sanitize_secrets(c.name),
            "status": c.status,
            "detail": sanitize_secrets(c.detail),
            "colour": _status_colour(c.status),
        }
        for c in report.checks
    ]


def _probe_counts(report: ProbeReport) -> dict[str, int]:
    """Count the probe checks by status (ok/warn/fail).

    ProbeReport carries no ``counts()`` of its own (unlike CheckReport), so the
    summary line computes them here.
    """
    counts = {"ok": 0, "warn": 0, "fail": 0}
    for c in report.checks:
        if c.status in counts:
            counts[c.status] += 1
    return counts


def render_probe_report(report: ProbeReport) -> None:
    """Render a ProbeReport as a summary line and a per-check table.

    Probe is a return value, not an event stream (``run_probe`` never calls
    ``on_event``), so this renders from the object directly, like
    ``render_check_report``.
    """
    counts = _probe_counts(report)
    ui.label(f"{counts['ok']} ok, {counts['warn']} warn, {counts['fail']} fail").classes(
        "text-sm text-gray-600"
    )
    columns = [
        {"name": "name", "label": "Check", "field": "name", "align": "left"},
        {"name": "status", "label": "Status", "field": "status", "align": "left"},
        {"name": "detail", "label": "Detail", "field": "detail", "align": "left"},
    ]
    ui.table(columns=columns, rows=_probe_rows(report)).classes("w-full mt-2")


def _semaphore_is_set() -> bool:
    """True when the module-level rate limiter already exists."""
    return _api._request_semaphore is not None


def _safe_push(push: Callable[[str], None], line: str) -> None:
    """Sanitize before writing to the on-screen log widget.

    The redacting formatter only covers Python logging; ``log_display.push``
    writes straight to the ``ui.log`` widget, which the formatter never sees, and
    ``run_check`` / ``run_probe`` do no redaction of their own. So the runner
    redacts at the push site. Proven by ``scratchpad/proto_redaction.py``.
    """
    push(sanitize_secrets(line))


def run_tool(
    client: Any,
    log_push: Callable[[str], None],
    coro_factory: Callable[[], Awaitable[T]],
    on_done: Callable[[T | None], None],
) -> None:
    """Run one tool call in a client-scoped background task with one error path.

    ``with client:`` re-enters the page's slot stack so ``ui.notify`` and friends
    reach the right client from the background task (issue #123). Any exception is
    sanitized and pushed to the log, and the page callback is handed ``None``.
    """

    async def _run() -> None:
        with client:
            try:
                result = await coro_factory()
                on_done(result)
            except Exception as exc:  # noqa: BLE001 -- one uniform error surface
                _safe_push(log_push, f"[ERROR] {exc}")
                on_done(None)

    background_tasks.create(_run())


def prepare_tool_call(token: str) -> list[str]:
    """Register the token, ensure the rate limiter exists, return proxy notices.

    Order matters: register the secret before anything that could log or persist,
    because the redactor returns text unchanged while nothing is registered. Init
    the semaphore only when it is unset, so a read-only tool page cannot swap the
    module-level object mid-flight and double a concurrent migration's cap (an
    ``init_request_semaphore`` call builds a new object, and a coroutine already
    holding the old one keeps running).
    """
    register_secret("stoat", token)
    if not _semaphore_is_set():
        _api.init_request_semaphore()
    return format_proxy_notices()


#: The tool pages, listed on the /tools landing screen. Every route is present
#: from the start; each page comes online as its plan chunk lands.
_TOOLS: list[tuple[str, str, str]] = [
    ("Check", "/tools/check", "Verify a finished migration"),
    ("Repair", "/tools/repair", "Recreate what is missing and resend"),
    ("Retry", "/tools/retry", "Resend failed messages"),
    ("Probe", "/tools/probe", "Check API limits before migrating"),
    ("Blueprint export", "/tools/blueprint-export", "Turn an export into a reusable blueprint"),
    ("Build", "/tools/build", "Build a server from a template or blueprint"),
    ("Validate", "/tools/validate", "Check an export before migrating"),
    ("Stats", "/tools/stats", "Summarise a past migration"),
    ("TLS check", "/tools/tls-check", "Inspect trust and proxy state"),
]


@ui.page("/tools")
def tools_page() -> None:
    """Landing screen listing every tool page."""
    with ui.column().classes("w-full items-center min-h-screen bg-gray-50 py-10"):
        ui.label("Tools").classes("text-2xl font-bold mb-4")
        for name, route, desc in _TOOLS:
            with ui.card().classes("w-full max-w-xl"):
                ui.link(name, route).classes("text-lg font-bold")
                ui.label(desc).classes("text-sm text-gray-500")


def _tab_token() -> str | None:
    """The Stoat token from the memory-only tab store, or None when absent."""
    token = app.storage.tab.get("token")
    return token if isinstance(token, str) and token else None


@ui.page("/tools/check")
def check_page() -> None:
    """Verify a finished migration by loading its state and running run_check."""
    client = ui.context.client
    stoat_url = str(app.storage.user.get("stoat_url", ""))
    token = _tab_token()

    with ui.column().classes("w-full items-center min-h-screen bg-gray-50 py-10"):
        ui.label("Verify a finished migration").classes("text-2xl font-bold mb-4")
        if token is None:
            ui.label("Session expired. Re-enter your token on the setup page.").classes(
                "text-red-600"
            )
            return

        default_dir = str(app.storage.user.get("output_dir", "./ferry-output"))
        dir_input = ui.input("Output directory", value=default_dir).classes("w-96")
        log = ui.log(max_lines=200).classes("w-full max-w-2xl h-48 font-mono text-xs mt-2")
        results = ui.column().classes("w-full max-w-2xl")

        def on_event(event: MigrationEvent) -> None:
            _safe_push(log.push, f"[{event.phase}] {event.message}")

        def on_done(result: CheckReport | str | None) -> None:
            results.clear()
            with results:
                if result is None:
                    ui.label("Check failed. See the log above.").classes("text-red-600")
                    return
                if isinstance(result, str):
                    ui.label(result).classes("text-amber-700")
                    return
                label, colour = _check_verdict(result)
                ui.label(label).classes(f"text-lg font-bold {colour}")
                render_check_report(result)

        def _run() -> None:
            results.clear()
            output_dir = Path(dir_input.value)
            for line in prepare_tool_call(token):
                _safe_push(log.push, line)

            async def _do_check() -> CheckReport | str:
                try:
                    state = load_state(output_dir)
                except StateError as exc:
                    return f"Cannot load this migration: {sanitize_secrets(str(exc))}"
                try:
                    return await run_check(stoat_url, token, state, on_event)
                except (CheckError, MigrationError) as exc:
                    # MigrationError covers a rate-limit or circuit-breaker failure
                    # from the API client; the CLI catches both here too.
                    return f"Cannot check this migration: {sanitize_secrets(str(exc))}"

            run_tool(client, log.push, _do_check, on_done)

        ui.button("Run check", on_click=_run).classes("mt-2")


@ui.page("/tools/repair")
def repair_page() -> None:
    """Verify a migration, then recreate what is missing and resend."""
    client = ui.context.client
    stoat_url = str(app.storage.user.get("stoat_url", ""))
    token = _tab_token()

    with ui.column().classes("w-full items-center min-h-screen bg-gray-50 py-10"):
        ui.label("Verify and fix a migration").classes("text-2xl font-bold mb-4")
        if token is None:
            ui.label("Session expired. Re-enter your token on the setup page.").classes(
                "text-red-600"
            )
            return

        default_dir = str(app.storage.user.get("output_dir", "./ferry-output"))
        dir_input = ui.input("Output directory", value=default_dir).classes("w-96")
        export_input = ui.input("Export directory (the original DCE export)").classes("w-96")
        dry_run_cb = ui.checkbox("Dry run (report only, change nothing)", value=True)
        log = ui.log(max_lines=300).classes("w-full max-w-2xl h-48 font-mono text-xs mt-2")
        results = ui.column().classes("w-full max-w-2xl")

        errors: list[str] = []
        state_ref: list[MigrationState] = []

        def on_event(event: MigrationEvent) -> None:
            if event.status == "error":
                errors.append(event.message)
            _safe_push(log.push, f"[{event.phase}] {event.message}")

        def on_done(outcome: RepairOutcome | None) -> None:
            results.clear()
            with results:
                if outcome is None:
                    ui.label("Repair failed. See the log above.").classes("text-red-600")
                    return
                if errors:
                    # A refusal (a rolled-back state) is not a repair failure: the
                    # engine correctly declined, and the CLI exits 0 for it. Show a
                    # neutral refusal, neither a green success nor a red failure, so
                    # neither shell claims the repair failed.
                    ui.label(
                        f"Repair refused, nothing changed: {sanitize_secrets(errors[-1])}"
                    ).classes("text-amber-700")
                    return
                _, label, colour = _repair_verdict(outcome, state_ref[0])
                ui.label(label).classes(f"text-lg font-bold {colour}")
                render_repair_outcome(outcome)

        def _run() -> None:
            results.clear()
            errors.clear()
            state_ref.clear()
            export_dir = export_input.value.strip()
            if not export_dir or not Path(export_dir).is_dir():
                ui.notify(
                    "Enter a valid export directory. Repair needs the original export.",
                    type="warning",
                )
                return
            output_dir = Path(dir_input.value)
            for line in prepare_tool_call(token):
                _safe_push(log.push, line)

            async def _do_repair() -> RepairOutcome:
                exports = parse_export_directory(Path(export_dir))
                state = load_state(output_dir)
                state_ref.append(state)
                config = FerryConfig(
                    export_dir=Path(export_dir),
                    stoat_url=stoat_url,
                    token=token,
                    output_dir=output_dir,
                    server_id=state.stoat_server_id or None,
                    skip_export=True,
                    dry_run=dry_run_cb.value,
                )
                return await run_repair(config, state, exports, on_event)

            run_tool(client, log.push, _do_repair, on_done)

        ui.button("Run repair", on_click=_run).classes("mt-2")


@ui.page("/tools/retry")
def retry_page() -> None:
    """Resend the messages that failed on a prior migration."""
    client = ui.context.client
    stoat_url = str(app.storage.user.get("stoat_url", ""))
    token = _tab_token()

    with ui.column().classes("w-full items-center min-h-screen bg-gray-50 py-10"):
        ui.label("Resend failed messages").classes("text-2xl font-bold mb-4")
        if token is None:
            ui.label("Session expired. Re-enter your token on the setup page.").classes(
                "text-red-600"
            )
            return

        default_dir = str(app.storage.user.get("output_dir", "./ferry-output"))
        dir_input = ui.input("Output directory", value=default_dir).classes("w-96")
        export_input = ui.input("Export directory (the original DCE export)").classes("w-96")
        log = ui.log(max_lines=300).classes("w-full max-w-2xl h-48 font-mono text-xs mt-2")
        results = ui.column().classes("w-full max-w-2xl")

        def on_event(event: MigrationEvent) -> None:
            _safe_push(log.push, f"[{event.phase}] {event.message}")

        def on_done(counts: tuple[int, int] | None) -> None:
            results.clear()
            with results:
                if counts is None:
                    ui.label("Retry failed. See the log above.").classes("text-red-600")
                    return
                succeeded, still_failed = counts
                colour = "text-red-600" if still_failed else "text-green-600"
                ui.label(f"{succeeded} succeeded, {still_failed} still failed.").classes(
                    f"text-lg font-bold {colour}"
                )

        def _run() -> None:
            results.clear()
            export_dir = export_input.value.strip()
            if not export_dir or not Path(export_dir).is_dir():
                ui.notify(
                    "Enter a valid export directory. Retry needs the original export.",
                    type="warning",
                )
                return
            output_dir = Path(dir_input.value)
            for line in prepare_tool_call(token):
                _safe_push(log.push, line)

            async def _do_retry() -> tuple[int, int]:
                exports = parse_export_directory(Path(export_dir))
                if not exports:
                    # Never hand run_retry_failed an empty list: it would resolve
                    # nothing and report success (a silent no-op). Surface it.
                    raise ValueError("No export files found in that directory.")
                state = load_state(output_dir)
                before = len(state.failed_messages)
                config = FerryConfig(
                    export_dir=Path(export_dir),
                    stoat_url=stoat_url,
                    token=token,
                    output_dir=output_dir,
                    server_id=state.stoat_server_id or None,
                    skip_export=True,
                )
                await run_retry_failed(config, state, exports, on_event)
                after = len(state.failed_messages)
                return before - after, after

            run_tool(client, log.push, _do_retry, on_done)

        ui.button("Run retry", on_click=_run).classes("mt-2")


@ui.page("/tools/probe")
def probe_page() -> None:
    """Preflight a live Stoat instance for Autumn limits, rate window, voice, webhooks.

    A self-contained preflight: it collects its own URL and token rather than a
    prior migration's session (design line 83), so it works before any migration
    exists. No session-expired guard, for the same reason.
    """
    client = ui.context.client
    default_url = str(app.storage.user.get("stoat_url", ""))
    default_token = _tab_token() or ""

    with ui.column().classes("w-full items-center min-h-screen bg-gray-50 py-10"):
        ui.label("Preflight a Stoat instance").classes("text-2xl font-bold mb-4")

        url_input = ui.input("Stoat URL", value=default_url).classes("w-96")
        token_input = ui.input("Stoat token", value=default_token, password=True).classes("w-96")
        server_input = ui.input("Throwaway test server ID").classes("w-96")
        deep_cb = ui.checkbox("Deep probe (upload test files at each Autumn size boundary)")
        deep_warning = ui.label(
            "Deep probe uploads test files to Autumn and cannot delete them, so it "
            "leaves orphaned files in storage. Tick the box below to confirm."
        ).classes("text-amber-700 text-sm max-w-2xl")
        deep_warning.bind_visibility_from(deep_cb, "value")
        deep_confirm = ui.checkbox("I understand the deep probe leaves orphaned files in Autumn")
        deep_confirm.bind_visibility_from(deep_cb, "value")

        log = ui.log(max_lines=200).classes("w-full max-w-2xl h-48 font-mono text-xs mt-2")
        results = ui.column().classes("w-full max-w-2xl")

        def on_done(report: ProbeReport | None) -> None:
            results.clear()
            with results:
                if report is None:
                    ui.label("Probe failed. See the log above.").classes("text-red-600")
                    return
                render_probe_report(report)

        def _run() -> None:
            results.clear()
            stoat_url = url_input.value.strip()
            token = token_input.value.strip()
            server_id = server_input.value.strip()
            if not stoat_url or not token:
                ui.notify("Enter a Stoat URL and token.", type="warning")
                return
            if not server_id:
                ui.notify(
                    "Enter a throwaway test server ID. Probe creates and deletes entities in it.",
                    type="warning",
                )
                return
            deep = deep_cb.value
            if deep and not deep_confirm.value:
                ui.notify(
                    "Confirm the deep-probe warning before running a deep probe.",
                    type="warning",
                )
                return
            for line in prepare_tool_call(token):
                _safe_push(log.push, line)

            async def _do_probe() -> ProbeReport:
                # run_probe never calls its on_event (it returns a report), so a
                # no-op callback matches the CLI's `lambda _e: None`.
                return await run_probe(stoat_url, token, server_id, lambda _e: None, deep=deep)

            run_tool(client, log.push, _do_probe, on_done)

        ui.button("Run probe", on_click=_run).classes("mt-2")


@ui.page("/tools/blueprint-export")
def blueprint_export_page() -> None:
    """Turn a DCE export directory into a reusable server blueprint.

    Offline: it reads local export files and writes a JSON blueprint, making no
    API call, so there is no token and no runner. Unlike the CLI, which
    overwrites the output silently, this asks before replacing an existing file.
    """
    with ui.column().classes("w-full items-center min-h-screen bg-gray-50 py-10"):
        ui.label("Export a blueprint").classes("text-2xl font-bold mb-4")
        from_input = ui.input("Export directory (the DCE export)").classes("w-96")
        output_input = ui.input("Output path (blueprint JSON)", value="blueprint.json").classes(
            "w-96"
        )
        name_input = ui.input("Server name (optional, overrides the export's)").classes("w-96")
        overwrite_cb = ui.checkbox("Overwrite the output file if it already exists")
        results = ui.column().classes("w-full max-w-2xl")

        def _write(bp: ServerBlueprint, output_path: Path) -> None:
            export_blueprint(bp, output_path)
            channels = sum(len(c.channels) for c in bp.categories) + len(bp.uncategorized_channels)
            results.clear()
            with results:
                # Counts are integers and the path is user-supplied, so neither is
                # server-controlled text; no sanitizing needed here.
                ui.label(
                    f"Blueprint written to {output_path} "
                    f"({len(bp.categories)} categories, {channels} channels)."
                ).classes("text-green-600")

        def _run() -> None:
            results.clear()
            from_dir = from_input.value.strip()
            output = output_input.value.strip()
            if not from_dir or not Path(from_dir).is_dir():
                ui.notify("Enter a valid export directory.", type="warning")
                return
            if not output:
                ui.notify("Enter an output path for the blueprint.", type="warning")
                return
            exports = parse_export_directory(Path(from_dir))
            if not exports:
                with results:
                    ui.label("No valid DCE JSON files found in that directory.").classes(
                        "text-red-600"
                    )
                return
            output_path = Path(output)
            if output_path.exists() and not overwrite_cb.value:
                # The CLI overwrites silently; here the write waits on an explicit
                # confirmation so a user cannot lose an existing blueprint by accident.
                with results:
                    ui.label(
                        f"{output_path} already exists. Tick 'Overwrite' to replace it."
                    ).classes("text-amber-700")
                return
            bp = blueprint_from_exports(exports, name_input.value.strip() or None)
            _write(bp, output_path)

        ui.button("Export blueprint", on_click=_run).classes("mt-2")
