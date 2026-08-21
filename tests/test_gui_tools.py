"""Tests for the GUI tool pages and their shared runner (issue #484 and children)."""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

if TYPE_CHECKING:
    from nicegui.testing import User

# The User-fixture tests below need the routes re-registered inside the
# simulation; nicegui_app.py reloads both gui and gui_tools for that.
pytestmark = pytest.mark.nicegui_main_file("tests/nicegui_app.py")

# A realistic-length opaque Stoat token, so the redaction test can actually fail
# (a token too short to trigger the mask would pass a broken implementation).
_TOKEN = "01F8MECHZX3TBDFGH4JKLMNPQR_opaque_base64url_like_secret_value"


def test_repair_failure_set_is_shared_and_exact() -> None:
    """SC-1.8: the repair pass-or-fail set is one shared constant, exact."""
    from discord_ferry.migrator.verify import UNREPAIRED_WARNING_TYPES

    assert (
        frozenset(
            {
                "no_recorded_name",
                "not_in_export",
                "forum_index_not_repairable",
                # #307 emoji repair: could-not-recreate and failed-rewrite both
                # leave something failing. emoji_in_split_tail is excluded (partial
                # restore of a recreated emoji), matching the CLI comment.
                "emoji_missing_media",
                "emoji_rewrite_failed",
            }
        )
        == UNREPAIRED_WARNING_TYPES
    )


def test_prepare_registers_secret_before_semaphore(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """SC-1.2: the token is registered before anything else the helper does."""
    from discord_ferry import gui_tools

    calls: list[str] = []
    api = gui_tools._api
    monkeypatch.setattr(gui_tools, "register_secret", lambda *a, **k: calls.append("secret"))
    monkeypatch.setattr(api, "init_request_semaphore", lambda *a, **k: calls.append("sem"))
    monkeypatch.setattr(gui_tools, "_semaphore_is_set", lambda: False)
    monkeypatch.setattr(gui_tools, "format_proxy_notices", lambda: calls.append("proxy") or [])

    gui_tools.prepare_tool_call("tok")

    assert calls[0] == "secret"


def test_prepare_inits_semaphore_only_when_unset(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """SC-1.4: a read-only page must not swap the semaphore mid-flight."""
    from discord_ferry import gui_tools

    monkeypatch.setattr(gui_tools, "register_secret", lambda *a, **k: None)
    monkeypatch.setattr(gui_tools, "format_proxy_notices", lambda: [])

    inited: list[int] = []
    api = gui_tools._api
    monkeypatch.setattr(api, "init_request_semaphore", lambda *a, **k: inited.append(1))

    monkeypatch.setattr(gui_tools, "_semaphore_is_set", lambda: True)
    gui_tools.prepare_tool_call("tok")
    assert inited == []  # already set: not re-inited

    monkeypatch.setattr(gui_tools, "_semaphore_is_set", lambda: False)
    gui_tools.prepare_tool_call("tok")
    assert inited == [1]  # unset: inited once


def test_prepare_returns_proxy_notices(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """SC-1.5: the helper surfaces proxy notices for the page to render."""
    from discord_ferry import gui_tools

    monkeypatch.setattr(gui_tools, "register_secret", lambda *a, **k: None)
    monkeypatch.setattr(gui_tools, "_semaphore_is_set", lambda: True)
    monkeypatch.setattr(gui_tools, "format_proxy_notices", lambda: ["proxy: on"])

    assert gui_tools.prepare_tool_call("tok") == ["proxy: on"]


def test_safe_push_masks_token() -> None:
    """SC-1.3: a token in a log-widget line is masked at the push site.

    register_secret alone protects the Python-logging sink; the ui.log widget is
    a separate sink the formatter never sees, and check/probe do no redaction of
    their own. Proven by scratchpad/proto_redaction.py.
    """
    from discord_ferry import gui_tools
    from discord_ferry.core.security import register_secret, reset_secret_registry

    reset_secret_registry()
    register_secret("stoat", _TOKEN)
    pushed: list[str] = []

    gui_tools._safe_push(pushed.append, f"[ERROR] boom: {_TOKEN}")

    assert _TOKEN not in pushed[0]  # masked at the push site
    assert _TOKEN in f"[ERROR] boom: {_TOKEN}"  # control: an unsanitized push would leak it
    reset_secret_registry()


def test_run_tool_error_path_sanitizes_and_reports_none(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """run_tool routes an exception to a sanitized log line and hands the callback None."""
    from discord_ferry import gui_tools
    from discord_ferry.core.security import register_secret, reset_secret_registry

    reset_secret_registry()
    register_secret("stoat", _TOKEN)

    # Run the background coroutine synchronously instead of scheduling it.
    monkeypatch.setattr(gui_tools.background_tasks, "create", lambda coro: asyncio.run(coro))

    pushed: list[str] = []
    done: list[object] = []

    async def _boom() -> None:
        raise RuntimeError(f"failed with {_TOKEN}")

    gui_tools.run_tool(contextlib.nullcontext(), pushed.append, _boom, done.append)

    assert done == [None]  # callback got None on error
    assert pushed and _TOKEN not in pushed[0]  # error line was sanitized
    reset_secret_registry()


async def test_tools_landing_lists_tools(user: User) -> None:
    """SC-1.1: /tools is reachable and lists every tool."""
    await user.open("/tools")
    for name in (
        "Check",
        "Repair",
        "Retry",
        "Probe",
        "Blueprint export",
        "Build",
        "Validate",
        "Stats",
        "TLS check",
    ):
        await user.should_see(name)


def test_status_colour_covers_every_check_status() -> None:
    """SC-1.7: every CheckReport status maps to a colour, none falls through."""
    from discord_ferry import gui_tools
    from discord_ferry.migrator.verify import STATUSES

    for status in STATUSES:
        assert status in gui_tools._STATUS_COLOURS
    # probe uses ok/warn/fail, a subset already covered above
    for status in ("ok", "warn", "fail"):
        assert gui_tools._status_colour(status)
    # an unknown status resolves to a defined default, never a crash
    assert gui_tools._status_colour("nonsense")


def test_check_rows_one_per_result_with_status_and_detail() -> None:
    """SC-2.1: the check table has a row per result carrying status and detail."""
    from discord_ferry import gui_tools
    from discord_ferry.migrator.verify import CheckReport

    report = CheckReport()
    report.add(name="general", status="ok", kind="channel_present", detail="present")
    report.add(name="mods", status="fail", kind="channel_missing", detail="missing on target")

    rows = gui_tools._check_rows(report)

    assert len(rows) == 2
    assert rows[0]["name"] == "general"
    assert rows[0]["status"] == "ok"
    assert rows[0]["detail"] == "present"
    assert rows[1]["colour"] == gui_tools._status_colour("fail")
    assert report.counts()["fail"] == 1


async def test_check_page_bounces_without_token(
    user: User,
    user_store: dict[str, object],
    tab_store: dict[str, object],
) -> None:
    """SC-1.6: the session-expired guard fires from the page builder."""
    user_store["stoat_url"] = "https://example.invalid"
    tab_store.pop("token", None)

    await user.open("/tools/check")
    await user.should_see("Session expired")
    await user.should_not_see("Run check")


async def test_check_page_streams_banners_and_renders_table(
    user: User,
    user_store: dict[str, object],
    tab_store: dict[str, object],
    tmp_path,  # type: ignore[no-untyped-def]
) -> None:
    """SC-2.2: the two banners reach the log, then the table renders."""
    from discord_ferry.core.events import MigrationEvent
    from discord_ferry.migrator.verify import CheckReport

    user_store["stoat_url"] = "https://example.invalid"
    user_store["output_dir"] = str(tmp_path)
    tab_store["token"] = _TOKEN

    report = CheckReport()
    report.add(name="general", status="ok", kind="channel_present", detail="present")

    async def fake_check(stoat_url, token, state, on_event, *, session=None):  # type: ignore[no-untyped-def]
        on_event(
            MigrationEvent(phase="check", status="started", message="Checking server structure...")
        )
        on_event(
            MigrationEvent(phase="check", status="started", message="Checking each channel...")
        )
        return report

    with (
        patch("discord_ferry.gui_tools.load_state", return_value=object()),
        patch("discord_ferry.gui_tools.run_check", new=fake_check),
    ):
        await user.open("/tools/check")
        user.find("Run check").click()
        await user.should_see("Checking server structure")  # first banner streamed
        await user.should_see("1 ok")  # the report's counts line rendered after the run


def test_check_verdict_matches_has_failures() -> None:
    """SC-2.4 / SC-I1: the GUI verdict tracks report.has_failures, same as the CLI."""
    from discord_ferry import gui_tools
    from discord_ferry.migrator.verify import CheckReport

    clean = CheckReport()
    clean.add(name="general", status="ok", kind="channel_present", detail="present")
    failing = CheckReport()
    failing.add(name="mods", status="fail", kind="channel_missing", detail="missing")

    clean_label, _ = gui_tools._check_verdict(clean)
    fail_label, _ = gui_tools._check_verdict(failing)

    assert clean.has_failures is False
    assert failing.has_failures is True
    assert clean_label != fail_label
    # parity: the verdict is a pure function of has_failures
    assert ("problem" in fail_label.lower()) and ("passed" in clean_label.lower())


async def test_check_page_shows_cannot_check_on_checkerror(
    user: User,
    user_store: dict[str, object],
    tab_store: dict[str, object],
    tmp_path,  # type: ignore[no-untyped-def]
) -> None:
    """SC-2.3: a CheckError (dry-run or server-less state) shows a clear message."""
    from discord_ferry.errors import CheckError

    user_store["stoat_url"] = "https://example.invalid"
    user_store["output_dir"] = str(tmp_path)
    tab_store["token"] = _TOKEN

    async def raises_check_error(*a, **k):  # type: ignore[no-untyped-def]
        raise CheckError("state is a dry run, nothing was migrated")

    with (
        patch("discord_ferry.gui_tools.load_state", return_value=object()),
        patch("discord_ferry.gui_tools.run_check", new=raises_check_error),
    ):
        await user.open("/tools/check")
        user.find("Run check").click()
        await user.should_see("Cannot check this migration")


def test_check_rows_sanitizes_server_controlled_text() -> None:
    """Chunk-2 review: the table is a rendered sink, so name/detail are sanitized.

    A token in a server-controlled field would otherwise reach the table, which
    the logging formatter does not cover (same class as the log-widget sink).
    """
    from discord_ferry import gui_tools
    from discord_ferry.core.security import register_secret, reset_secret_registry
    from discord_ferry.migrator.verify import CheckReport

    reset_secret_registry()
    register_secret("stoat", _TOKEN)
    report = CheckReport()
    report.add(
        name=f"chan-{_TOKEN}",
        status="fail",
        kind="channel_missing",
        detail=f"detail with {_TOKEN} in it",
    )

    rows = gui_tools._check_rows(report)

    assert _TOKEN not in rows[0]["name"]
    assert _TOKEN not in rows[0]["detail"]
    # control: the token really was in the source fields
    assert _TOKEN in report.results[0].detail
    reset_secret_registry()


def test_probe_rows_one_per_check_with_status_and_detail() -> None:
    """Chunk 6 Task 13: the probe table has a row per check carrying status and detail."""
    from discord_ferry import gui_tools
    from discord_ferry.migrator.probe import ProbeReport

    report = ProbeReport()
    report.add("autumn_reachable", "ok", "https://autumn (version 1)")
    report.add("voice_channel", "warn", "voice unsupported on this instance")

    rows = gui_tools._probe_rows(report)

    assert len(rows) == 2
    assert rows[0]["name"] == "autumn_reachable"
    assert rows[0]["status"] == "ok"
    assert rows[0]["detail"] == "https://autumn (version 1)"
    assert rows[1]["colour"] == gui_tools._status_colour("warn")


def test_probe_rows_sanitizes_server_controlled_text() -> None:
    """The probe detail is instance-controlled text, so it is sanitized at the sink."""
    from discord_ferry import gui_tools
    from discord_ferry.core.security import register_secret, reset_secret_registry
    from discord_ferry.migrator.probe import ProbeReport

    reset_secret_registry()
    register_secret("stoat", _TOKEN)
    report = ProbeReport()
    report.add("rate_limit", "ok", f"headers leaked {_TOKEN}")

    rows = gui_tools._probe_rows(report)

    assert _TOKEN not in rows[0]["detail"]
    assert _TOKEN in report.checks[0].detail  # control: the token really was in the source
    reset_secret_registry()


def test_probe_report_summary_counts_by_status() -> None:
    """render_probe_report's summary line counts ok/warn/fail from the checks."""
    from discord_ferry import gui_tools
    from discord_ferry.migrator.probe import ProbeReport

    report = ProbeReport()
    report.add("a", "ok", "x")
    report.add("b", "warn", "y")
    report.add("c", "fail", "z")

    assert gui_tools._probe_counts(report) == {"ok": 1, "warn": 1, "fail": 1}


async def test_probe_page_bounces_without_token(
    user: User,
    user_store: dict[str, object],
    tab_store: dict[str, object],
) -> None:
    """Chunk 6 Task 14: probe shows the session-expired guard when the tab store is empty."""
    user_store["stoat_url"] = "https://example.invalid"
    tab_store.pop("token", None)

    await user.open("/tools/probe")
    await user.should_see("Session expired")
    await user.should_not_see("Run probe")


async def test_probe_page_requires_test_server_id(
    user: User,
    user_store: dict[str, object],
    tab_store: dict[str, object],
) -> None:
    """Chunk 6 Task 14: probe refuses to run with no test server id; run_probe not called."""
    from unittest.mock import AsyncMock

    user_store["stoat_url"] = "https://example.invalid"
    tab_store["token"] = _TOKEN

    with patch("discord_ferry.gui_tools.run_probe", new=AsyncMock()) as probe_mock:
        await user.open("/tools/probe")
        # URL and token defaulted from storage; test-server-id left blank.
        user.find("Run probe").click()
        await user.should_see("throwaway test server ID")
        assert probe_mock.await_count == 0


async def test_probe_page_deep_requires_confirmation(
    user: User,
    user_store: dict[str, object],
    tab_store: dict[str, object],
) -> None:
    """Chunk 6 Task 14: deep shows the orphan warning and blocks the call until confirmed."""
    from unittest.mock import AsyncMock

    user_store["stoat_url"] = "https://example.invalid"
    tab_store["token"] = _TOKEN

    with patch("discord_ferry.gui_tools.run_probe", new=AsyncMock()) as probe_mock:
        await user.open("/tools/probe")
        user.find("Throwaway test server ID").elements.pop().set_value("srv-throwaway")
        user.find("Deep probe").elements.pop().set_value(True)
        await user.should_see("leaves orphaned files")  # warning shown before any call
        user.find("Run probe").click()
        await user.should_see("Confirm the deep-probe warning")
        assert probe_mock.await_count == 0  # blocked until confirmed


async def test_probe_page_deep_confirm_resets_when_toggled_off(
    user: User,
    user_store: dict[str, object],
    tab_store: dict[str, object],
) -> None:
    """Whole-branch review: turning deep off then on again must not carry a stale confirm.

    Binding only the confirm box's visibility would hide it while keeping value True,
    so a re-tick of deep could run a deep probe without a fresh acknowledgement.
    """
    from unittest.mock import AsyncMock

    user_store["stoat_url"] = "https://example.invalid"
    tab_store["token"] = _TOKEN

    with patch("discord_ferry.gui_tools.run_probe", new=AsyncMock()) as probe_mock:
        await user.open("/tools/probe")
        user.find("Throwaway test server ID").elements.pop().set_value("srv-throwaway")
        deep = user.find("Deep probe").elements.pop()
        deep.set_value(True)  # confirm box is now visible, so it can be found
        confirm = user.find("I understand the deep probe").elements.pop()
        confirm.set_value(True)
        deep.set_value(False)  # toggling deep off resets the confirmation
        assert confirm.value is False
        deep.set_value(True)  # re-enabling deep still shows an unconfirmed box
        assert confirm.value is False
        user.find("Run probe").click()
        await user.should_see("Confirm the deep-probe warning")
        assert probe_mock.await_count == 0  # blocked: the stale tick did not carry


async def test_probe_page_registers_token_and_renders(
    user: User,
    user_store: dict[str, object],
    tab_store: dict[str, object],
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    """Chunk 6 Task 14: a valid run registers the token via the runner and renders the table."""
    from discord_ferry import gui_tools
    from discord_ferry.migrator.probe import ProbeReport

    user_store["stoat_url"] = "https://example.invalid"
    tab_store["token"] = _TOKEN

    registered: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gui_tools, "register_secret", lambda name, value: registered.append((name, value))
    )

    report = ProbeReport()
    report.add("autumn_reachable", "ok", "https://autumn (version 1)")

    async def fake_probe(stoat_url, token, server_id, on_event, *, deep=False, session=None):  # type: ignore[no-untyped-def]
        return report

    with patch("discord_ferry.gui_tools.run_probe", new=fake_probe):
        await user.open("/tools/probe")
        user.find("Throwaway test server ID").elements.pop().set_value("srv-throwaway")
        user.find("Run probe").click()
        await user.should_see("1 ok, 0 warn, 0 fail")  # the table's summary rendered

    assert ("stoat", _TOKEN) in registered  # the runner registered the token before the call


def _one_export():  # type: ignore[no-untyped-def]
    """A single parsed export with one text channel, for the blueprint page."""
    from discord_ferry.parser.models import DCEChannel, DCEExport, DCEGuild

    return DCEExport(
        guild=DCEGuild(id="g1", name="My Guild"),
        channel=DCEChannel(id="general", type=0, name="general", category="Text"),
    )


async def test_blueprint_export_page_writes_file(
    user: User,
    tmp_path,  # type: ignore[no-untyped-def]
) -> None:
    """Chunk 7 Task 15b: a valid run writes the blueprint file and reports it."""
    output = tmp_path / "bp.json"

    with patch("discord_ferry.gui_tools.parse_export_directory", return_value=[_one_export()]):
        await user.open("/tools/blueprint-export")
        user.find("Export directory").elements.pop().set_value(str(tmp_path))
        user.find("Output path").elements.pop().set_value(str(output))
        user.find("Export blueprint").click()
        await user.should_see("Blueprint written")

    assert output.exists()


async def test_blueprint_export_page_no_dce_json_error(
    user: User,
    tmp_path,  # type: ignore[no-untyped-def]
) -> None:
    """Chunk 7 Task 15b: an empty parse shows a clear error, not a crash."""
    with patch("discord_ferry.gui_tools.parse_export_directory", return_value=[]):
        await user.open("/tools/blueprint-export")
        user.find("Export directory").elements.pop().set_value(str(tmp_path))
        user.find("Export blueprint").click()
        await user.should_see("No valid DCE JSON files found")


async def test_blueprint_export_page_confirms_before_overwrite(
    user: User,
    tmp_path,  # type: ignore[no-untyped-def]
) -> None:
    """Chunk 7 Task 15b: an existing output is not overwritten until confirmed."""
    output = tmp_path / "bp.json"
    output.write_text("OLD", encoding="utf-8")

    with patch("discord_ferry.gui_tools.parse_export_directory", return_value=[_one_export()]):
        await user.open("/tools/blueprint-export")
        user.find("Export directory").elements.pop().set_value(str(tmp_path))
        user.find("Output path").elements.pop().set_value(str(output))
        user.find("Export blueprint").click()
        await user.should_see("already exists")
        assert output.read_text(encoding="utf-8") == "OLD"  # not overwritten without confirmation

        user.find("Overwrite the output file").elements.pop().set_value(True)
        user.find("Export blueprint").click()
        await user.should_see("Blueprint written")

    assert output.read_text(encoding="utf-8") != "OLD"  # overwritten after ticking Overwrite


async def test_build_page_bounces_without_token(
    user: User,
    user_store: dict[str, object],
    tab_store: dict[str, object],
) -> None:
    """Chunk 8 Task 16: the build page shows the session-expired guard with no token."""
    user_store["stoat_url"] = "https://example.invalid"
    tab_store.pop("token", None)

    await user.open("/tools/build")
    await user.should_see("Session expired")
    await user.should_not_see("Build server")


async def test_build_page_shows_no_rollback_notice(
    user: User,
    user_store: dict[str, object],
    tab_store: dict[str, object],
) -> None:
    """Chunk 8 Task 16: the page states the built server cannot be rolled back."""
    user_store["stoat_url"] = "https://example.invalid"
    tab_store["token"] = _TOKEN

    await user.open("/tools/build")
    await user.should_see("rollback tool cannot undo it")


async def test_build_page_requires_exactly_one_source(
    user: User,
    user_store: dict[str, object],
    tab_store: dict[str, object],
    tmp_path,  # type: ignore[no-untyped-def]
) -> None:
    """Chunk 8 Task 16: neither and both are rejected; run_build is not called."""
    from unittest.mock import AsyncMock

    user_store["stoat_url"] = "https://example.invalid"
    tab_store["token"] = _TOKEN

    with patch("discord_ferry.gui_tools.run_build", new=AsyncMock()) as build_mock:
        await user.open("/tools/build")
        # Neither a template nor a blueprint file.
        user.find("Build server").click()
        await user.should_see("exactly one source")

        # Both a template and a blueprint file.
        user.find("Template").elements.pop().set_value("gaming")
        user.find("Blueprint file").elements.pop().set_value(str(tmp_path / "bp.json"))
        user.find("Build server").click()
        await user.should_see("exactly one source")

        assert build_mock.await_count == 0


async def test_build_page_runs_and_soft_role_warning_still_succeeds(
    user: User,
    user_store: dict[str, object],
    tab_store: dict[str, object],
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    """Chunk 8 Task 17: a build with a role-ordering warning still reports success.

    The role-ordering failure is a warning event inside run_build, not a raise,
    so the page renders the success label rather than the error path. The token is
    registered via the runner before the call.
    """
    from discord_ferry import gui_tools
    from discord_ferry.core.events import MigrationEvent

    user_store["stoat_url"] = "https://example.invalid"
    tab_store["token"] = _TOKEN

    registered: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gui_tools, "register_secret", lambda name, value: registered.append((name, value))
    )

    async def fake_build(stoat_url, token, bp, on_event, *, session=None):  # type: ignore[no-untyped-def]
        on_event(MigrationEvent(phase="build", status="warning", message="Role ordering failed: x"))
        return "srv-123"

    with patch("discord_ferry.gui_tools.run_build", new=fake_build):
        await user.open("/tools/build")
        user.find("Template").elements.pop().set_value("gaming")
        user.find("Build server").click()
        await user.should_see("Server built (srv-123)")
        await user.should_not_see("Build failed")

    assert ("stoat", _TOKEN) in registered  # token registered before the build


class _StubState:
    """Minimal stand-in for MigrationState for the repair verdict and page."""

    stoat_server_id = "srv"

    def __init__(self, failed_messages: list) -> None:  # type: ignore[type-arg]
        self.failed_messages = failed_messages


def test_repair_verdict_matches_cli_failure_set() -> None:
    """SC-3.4 / SC-I2: verdict uses state.failed_messages + declined in the shared set."""
    from discord_ferry import gui_tools
    from discord_ferry.migrator.verify import RepairOutcome

    # An unrepaired type -> fail.
    failed, _, _ = gui_tools._repair_verdict(
        RepairOutcome(declined=[{"type": "no_recorded_name"}]), _StubState([])
    )
    assert failed is True

    # An excluded type (partial merge restore) -> pass, matching the CLI.
    ok, _, _ = gui_tools._repair_verdict(
        RepairOutcome(declined=[{"type": "merge_thread_content_not_restored"}]), _StubState([])
    )
    assert ok is False

    # Leftover failed messages -> fail.
    failed2, _, _ = gui_tools._repair_verdict(RepairOutcome(), _StubState([{"id": "1"}]))
    assert failed2 is True

    # Nothing wrong -> pass.
    clean, _, _ = gui_tools._repair_verdict(RepairOutcome(), _StubState([]))
    assert clean is False


async def test_repair_page_dry_run_defaults_on(
    user: User,
    user_store: dict[str, object],
    tab_store: dict[str, object],
) -> None:
    """SC-3.2: the dry-run toggle defaults on."""
    user_store["stoat_url"] = "https://example.invalid"
    tab_store["token"] = _TOKEN

    await user.open("/tools/repair")
    await user.should_see("Dry run")
    checkbox = user.find("Dry run").elements.pop()
    assert checkbox.value is True


async def test_repair_page_refuses_without_export_dir(
    user: User,
    user_store: dict[str, object],
    tab_store: dict[str, object],
) -> None:
    """SC-3.1: repair refuses to run with no export directory; run_repair not called."""
    user_store["stoat_url"] = "https://example.invalid"
    tab_store["token"] = _TOKEN

    from unittest.mock import AsyncMock

    with patch("discord_ferry.gui_tools.run_repair", new=AsyncMock()) as run_repair_mock:
        await user.open("/tools/repair")
        # export dir left blank
        user.find("Run repair").click()
        await user.should_see("valid export directory")
        assert run_repair_mock.await_count == 0


async def test_repair_page_surfaces_rolled_back_refusal(
    user: User,
    user_store: dict[str, object],
    tab_store: dict[str, object],
    tmp_path,  # type: ignore[no-untyped-def]
) -> None:
    """SC-3.3: a rolled-back-state refusal is shown and no success is reported."""
    from discord_ferry.core.events import MigrationEvent
    from discord_ferry.migrator.verify import RepairOutcome

    export_dir = tmp_path / "export"
    export_dir.mkdir()
    user_store["stoat_url"] = "https://example.invalid"
    user_store["output_dir"] = str(tmp_path)
    tab_store["token"] = _TOKEN

    async def refusing_repair(config, state, exports, on_event, **k):  # type: ignore[no-untyped-def]
        on_event(
            MigrationEvent(phase="repair", status="error", message="records a rollback. Refusing.")
        )
        return RepairOutcome()

    with (
        patch("discord_ferry.gui_tools.parse_export_directory", return_value=[]),
        patch("discord_ferry.gui_tools.load_state", return_value=_StubState([])),
        patch("discord_ferry.gui_tools.run_repair", new=refusing_repair),
    ):
        await user.open("/tools/repair")
        user.find("Export directory").elements.pop().set_value(str(export_dir))
        user.find("Run repair").click()
        await user.should_see("Refusing")
        await user.should_see("Repair refused")
        await user.should_not_see("Repair complete.")


async def test_retry_page_refuses_without_export_dir(
    user: User,
    user_store: dict[str, object],
    tab_store: dict[str, object],
) -> None:
    """SC-4.1: retry refuses without a valid export dir; run_retry_failed not called."""
    from unittest.mock import AsyncMock

    user_store["stoat_url"] = "https://example.invalid"
    tab_store["token"] = _TOKEN

    with patch("discord_ferry.gui_tools.run_retry_failed", new=AsyncMock()) as retry_mock:
        await user.open("/tools/retry")
        user.find("Run retry").click()
        await user.should_see("valid export directory")
        assert retry_mock.await_count == 0


async def test_retry_page_reports_succeeded_and_failed(
    user: User,
    user_store: dict[str, object],
    tab_store: dict[str, object],
    tmp_path,  # type: ignore[no-untyped-def]
) -> None:
    """SC-4.2: the page shows N succeeded, M still failed from state.failed_messages."""
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    user_store["stoat_url"] = "https://example.invalid"
    user_store["output_dir"] = str(tmp_path)
    tab_store["token"] = _TOKEN

    async def fake_retry(config, state, exports, on_event, **k):  # type: ignore[no-untyped-def]
        state.failed_messages.clear()  # two were pending; both now succeed

    with (
        patch("discord_ferry.gui_tools.parse_export_directory", return_value=[object()]),
        patch(
            "discord_ferry.gui_tools.load_state",
            return_value=_StubState([{"id": "1"}, {"id": "2"}]),
        ),
        patch("discord_ferry.gui_tools.run_retry_failed", new=fake_retry),
    ):
        await user.open("/tools/retry")
        user.find("Export directory").elements.pop().set_value(str(export_dir))
        user.find("Run retry").click()
        await user.should_see("2 succeeded, 0 still failed")


async def test_retry_page_handles_no_failures(
    user: User,
    user_store: dict[str, object],
    tab_store: dict[str, object],
    tmp_path,  # type: ignore[no-untyped-def]
) -> None:
    """SC-4.3: with nothing failed, the page reports 0 and 0 without erroring."""
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    user_store["stoat_url"] = "https://example.invalid"
    user_store["output_dir"] = str(tmp_path)
    tab_store["token"] = _TOKEN

    async def fake_retry(config, state, exports, on_event, **k):  # type: ignore[no-untyped-def]
        return None  # early return: nothing to do

    with (
        patch("discord_ferry.gui_tools.parse_export_directory", return_value=[object()]),
        patch("discord_ferry.gui_tools.load_state", return_value=_StubState([])),
        patch("discord_ferry.gui_tools.run_retry_failed", new=fake_retry),
    ):
        await user.open("/tools/retry")
        user.find("Export directory").elements.pop().set_value(str(export_dir))
        user.find("Run retry").click()
        await user.should_see("0 succeeded, 0 still failed")


async def test_no_tool_page_renders_a_raw_token(
    user: User,
    user_store: dict[str, object],
    tab_store: dict[str, object],
    tmp_path,  # type: ignore[no-untyped-def]
) -> None:
    """SC-I4: a token in an engine exception never reaches a tool page's rendered output.

    The page registers the token (prepare_tool_call) before the run, and run_tool
    sanitizes the error at the log-widget push site. Each page is exercised; the
    error label is the positive control (proving the flow ran) so should_not_see
    is not vacuous for the rendered label/table surface.
    """
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    user_store["stoat_url"] = "https://example.invalid"
    user_store["output_dir"] = str(tmp_path)
    tab_store["token"] = _TOKEN

    async def boom(*a, **k):  # type: ignore[no-untyped-def]
        raise RuntimeError(f"upstream said {_TOKEN}")

    # Check page: run_check raises with the token in its message.
    with (
        patch("discord_ferry.gui_tools.load_state", return_value=_StubState([])),
        patch("discord_ferry.gui_tools.run_check", new=boom),
    ):
        await user.open("/tools/check")
        user.find("Run check").click()
        await user.should_see("Check failed")  # positive control: the error path ran
        await user.should_not_see(_TOKEN)

    # Repair and retry pages: same, with the export dir filled in.
    for route, button, target, label in (
        ("/tools/repair", "Run repair", "run_repair", "Repair failed"),
        ("/tools/retry", "Run retry", "run_retry_failed", "Retry failed"),
    ):
        with (
            patch("discord_ferry.gui_tools.parse_export_directory", return_value=[object()]),
            patch("discord_ferry.gui_tools.load_state", return_value=_StubState([])),
            patch(f"discord_ferry.gui_tools.{target}", new=boom),
        ):
            await user.open(route)
            user.find("Export directory").elements.pop().set_value(str(export_dir))
            user.find(button).click()
            await user.should_see(label)  # positive control: the error path ran
            await user.should_not_see(_TOKEN)


async def test_check_page_shows_cannot_check_on_migration_error(
    user: User,
    user_store: dict[str, object],
    tab_store: dict[str, object],
    tmp_path,  # type: ignore[no-untyped-def]
) -> None:
    """Whole-branch review: a MigrationError (rate limit / breaker) is caught like the CLI."""
    from discord_ferry.errors import MigrationError

    user_store["stoat_url"] = "https://example.invalid"
    user_store["output_dir"] = str(tmp_path)
    tab_store["token"] = _TOKEN

    async def raises_migration_error(*a, **k):  # type: ignore[no-untyped-def]
        raise MigrationError("circuit breaker open")

    with (
        patch("discord_ferry.gui_tools.load_state", return_value=_StubState([])),
        patch("discord_ferry.gui_tools.run_check", new=raises_migration_error),
    ):
        await user.open("/tools/check")
        user.find("Run check").click()
        await user.should_see("Cannot check this migration")
        await user.should_not_see("Check failed")  # not the generic error path


# ---------------------------------------------------------------------------
# Phase 3: validate (#492), stats (#493), tls-check (#494)
# ---------------------------------------------------------------------------


async def test_validate_page_no_dce_json_error(
    user: User,
    tmp_path,  # type: ignore[no-untyped-def]
) -> None:
    """#492: an empty parse shows a clear error, not a crash."""
    with patch("discord_ferry.gui_tools.parse_export_directory", return_value=[]):
        await user.open("/tools/validate")
        user.find("Export directory").elements.pop().set_value(str(tmp_path))
        user.find("Validate export").click()
        await user.should_see("No valid DCE JSON files found")


async def test_validate_page_renders_warnings_and_ack_reason(
    user: User,
    tmp_path,  # type: ignore[no-untyped-def]
) -> None:
    """#492: warnings render, and the plain-text-mentions acknowledgement reason shows."""
    warnings = [
        {"type": "rendered_markdown", "message": "3 mentions written as plain text", "count": "3"}
    ]

    with (
        patch("discord_ferry.gui_tools.parse_export_directory", return_value=[object()]),
        patch("discord_ferry.gui_tools.validate_export", return_value=warnings),
        patch(
            "discord_ferry.gui_tools.acknowledgement_required",
            return_value="3 message(s) have mentions written as plain text.",
        ),
    ):
        await user.open("/tools/validate")
        user.find("Export directory").elements.pop().set_value(str(tmp_path))
        user.find("Validate export").click()
        await user.should_see("mentions written as plain text")
        await user.should_see("1 warning(s)")


async def test_validate_page_clean_export(
    user: User,
    tmp_path,  # type: ignore[no-untyped-def]
) -> None:
    """#492: a clean export reports no warnings."""
    with (
        patch("discord_ferry.gui_tools.parse_export_directory", return_value=[object()]),
        patch("discord_ferry.gui_tools.validate_export", return_value=[]),
    ):
        await user.open("/tools/validate")
        user.find("Export directory").elements.pop().set_value(str(tmp_path))
        user.find("Validate export").click()
        await user.should_see("Export looks good")


def _fake_summary(last_error: str | None = None):  # type: ignore[no-untyped-def]
    """A StateSummary-shaped stub for the stats row-builder test."""
    from types import SimpleNamespace

    fidelity = SimpleNamespace(
        overall=98.5, messages=99.0, attachments=100.0, embeds=None, replies=50.0, reactions=None
    )
    return SimpleNamespace(
        channels=3,
        roles=2,
        categories=1,
        emojis=0,
        messages=1234,
        attachments_uploaded=10,
        attachments_skipped=1,
        pins_applied=2,
        reactions_applied=5,
        replies_linked=4,
        replies_total=8,
        embeds_total=0,
        embeds_dropped=0,
        failed_messages=0,
        prior_messages_total=0,
        error_count=1 if last_error else 0,
        warning_count=0,
        last_error=last_error,
        last_warning=None,
        fidelity=fidelity,
        rollback=None,
        channel_breakdown={},
        is_dry_run=False,
        stoat_server_id="01SRV",
        duration_seconds=12.0,
        duration_state="complete",
        current_phase="report",
    )


def test_stats_rows_maps_summary_and_sanitizes_last_error() -> None:
    """#493: the stats rows carry the counters and mask a token in last_error."""
    from discord_ferry import gui_tools
    from discord_ferry.core.security import register_secret, reset_secret_registry

    reset_secret_registry()
    register_secret("stoat", _TOKEN)
    rows = gui_tools._stats_rows(_fake_summary(last_error=f"boom {_TOKEN}"))

    by_item = {r["item"]: r["value"] for r in rows}
    assert by_item["Channels"] == "3"
    assert by_item["Messages migrated"] == "1,234"
    assert by_item["Fidelity embeds"] == "n/a"  # None denominator, not 0%
    errors_row = next(r["value"] for r in rows if r["item"] == "Errors")
    assert _TOKEN not in errors_row  # the last-error preview was sanitized
    reset_secret_registry()


async def test_stats_page_renders_summary(
    user: User,
    user_store: dict[str, object],
    tmp_path,  # type: ignore[no-untyped-def]
) -> None:
    """#493: a valid state renders the summary (the server-id label is the positive control)."""
    user_store["output_dir"] = str(tmp_path)

    with (
        patch("discord_ferry.gui_tools.load_state", return_value=object()),
        patch("discord_ferry.gui_tools.summarize_state", return_value=_fake_summary()),
    ):
        await user.open("/tools/stats")
        user.find("Show stats").click()
        await user.should_see("Stoat server 01SRV")  # label, not a table cell


async def test_stats_page_missing_state_error(
    user: User,
    user_store: dict[str, object],
    tmp_path,  # type: ignore[no-untyped-def]
) -> None:
    """#493: a missing or invalid state file shows a clear error."""
    from discord_ferry.errors import StateError

    user_store["output_dir"] = str(tmp_path)

    with patch("discord_ferry.gui_tools.load_state", side_effect=StateError("no state.json found")):
        await user.open("/tools/stats")
        user.find("Show stats").click()
        await user.should_see("Cannot read this migration")


async def test_tls_check_page_renders_both_groups(
    user: User,
) -> None:
    """#494: the TLS trust and proxy groups both render from the describe functions."""
    with (
        patch(
            "discord_ferry.gui_tools.describe_trust",
            return_value={"trust-source": "certifi+system"},
        ),
        patch("discord_ferry.gui_tools.describe_proxy", return_value={"proxy-source": "none"}),
    ):
        await user.open("/tools/tls-check")
        await user.should_see("TLS trust")
        await user.should_see("certifi+system")
        await user.should_see("Proxy")
        await user.should_see("proxy-source")
