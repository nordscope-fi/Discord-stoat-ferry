"""Always-on file logging and token redaction.

Ferry's GUI binary is built with ``console=False``, so ``sys.stderr`` is ``None``
and ``logging.lastResort`` silently discards everything. That is why issue #123
was undiagnosable for eleven releases. These tests cover the replacement.

The redaction tests matter more than they look: this log file is the artifact we
will ask users to attach to bug reports, so a leak here publishes tokens.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from discord_ferry.core import logging_setup
from discord_ferry.core.security import register_secret, reset_secret_registry

if TYPE_CHECKING:
    from pathlib import Path

STOAT_TOKEN = "SEKRET-stoat-token-abcd"  # noqa: S105 - test fixture, not a real credential
# Assembled at runtime, never stored as a literal: a Discord-shaped string in a
# committed file trips GitHub secret-scanning push protection (it did, on the
# first push of this branch). The joined value still exercises the redaction
# regex exactly as a real token would.
DISCORD_TOKEN = ".".join(("MTIzNDU2Nzg5MDEyMzQ1Njc4", "GhIjKl", "abcdefghijklmnopqrstuvwxyz123"))


@pytest.fixture
def log_file(tmp_path: Path) -> Path:
    """A configured log file. The autouse conftest fixture handles teardown."""
    path = logging_setup.configure_logging(path=tmp_path / "ferry.log")
    assert path is not None
    return path


def _read(path: Path) -> str:
    for handler in logging.getLogger().handlers:
        handler.flush()
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Handler wiring
# ---------------------------------------------------------------------------


def test_configure_logging_creates_the_file(log_file: Path) -> None:
    """SC-123-14."""
    logging.getLogger("discord_ferry.test").info("hello")
    assert "hello" in _read(log_file)


def test_configure_logging_adds_only_a_file_handler(tmp_path: Path) -> None:
    """SC-123-15: Rich and click.echo own the console.

    A StreamHandler here would double every line the CLI prints. Measured as a
    DELTA -- pytest attaches its own stream handlers to the root logger, so an
    absolute check would fail for reasons that have nothing to do with Ferry.
    """
    root = logging.getLogger()
    before = list(root.handlers)
    logging_setup.configure_logging(path=tmp_path / "ferry.log")
    added = [h for h in root.handlers if h not in before]

    assert len(added) == 1, f"expected exactly one new handler, got {added}"
    assert isinstance(added[0], logging.FileHandler)


def test_configure_logging_is_idempotent(tmp_path: Path) -> None:
    """SC-123-23: repeated calls must not stack handlers.

    `cli.main()` is a Click GROUP callback, so it runs on every
    `CliRunner().invoke(main, ...)` — dozens of times per session.
    """
    logging_setup.configure_logging(path=tmp_path / "ferry.log")
    before = len(logging.getLogger().handlers)
    logging_setup.configure_logging(path=tmp_path / "ferry.log")
    assert len(logging.getLogger().handlers) == before


def test_unwritable_path_degrades_gracefully(tmp_path: Path) -> None:
    """SC-123-22: logging is a diagnostic aid, never a startup dependency."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    assert logging_setup.configure_logging(path=blocker / "sub" / "ferry.log") is None


def test_reset_detaches_the_handler(tmp_path: Path) -> None:
    """SC-123-24: mirrors api._reset_circuit_state."""
    logging_setup.configure_logging(path=tmp_path / "ferry.log")
    logging_setup.reset_logging()
    assert not [h for h in logging.getLogger().handlers if h.get_name() == "ferry-file"]


def test_debug_records_do_not_reach_the_file(log_file: Path) -> None:
    """SC-123-26: messages.py logs per message at DEBUG.

    Letting those through would bury the signal and burn through rotation on a
    large migration.
    """
    logging.getLogger("discord_ferry.migrator.messages").debug("per-message noise")
    assert "per-message noise" not in _read(log_file)


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def test_registered_token_is_masked(log_file: Path) -> None:
    """SC-123-17: the registry layer."""
    register_secret("stoat", STOAT_TOKEN)
    logging.getLogger("discord_ferry.test").warning("connecting with %s" % STOAT_TOKEN)  # noqa: UP031
    body = _read(log_file)
    assert STOAT_TOKEN not in body
    assert "****abcd" in body


def test_token_in_deferred_arg_is_masked(log_file: Path) -> None:
    """SC-123-18: the round-1 Critical.

    A `logging.Filter` sees `record.msg` (the template) and `record.args`
    separately — the final string is only built later by `record.getMessage()`.
    A filter that sanitises `record.msg` alone leaks anything passed as `%s`.

    This is not hypothetical: `exporter/runner.py` does
    `logger.debug("DCE: %s", line)` with raw DCE stdout, and DCE is invoked with
    the Discord token on its command line.
    """
    register_secret("stoat", STOAT_TOKEN)
    logging.getLogger("discord_ferry.test").warning("DCE: %s", STOAT_TOKEN)

    body = _read(log_file)
    assert STOAT_TOKEN not in body, "token leaked via record.args — filter used record.msg only"
    assert "****abcd" in body


def test_unregistered_discord_token_is_caught_by_the_pattern(log_file: Path) -> None:
    """SC-123-19: the regex floor covers records emitted before registration.

    The registry cannot help before `register_secret` runs, and there IS such a
    window — `configure_logging()` is called in `main()`, long before any token
    exists.
    """
    reset_secret_registry()
    logging.getLogger("discord_ferry.test").warning("auth header: %s", DISCORD_TOKEN)
    assert DISCORD_TOKEN not in _read(log_file)


def test_traceback_is_redacted(log_file: Path) -> None:
    """A token inside an exception message must not survive into the file.

    NiceGUI routes uncaught background-task exceptions to logging, so this is
    exactly the path #123-class failures now travel — and exception messages
    routinely quote the token that caused the failure.

    `propagate = False` is load-bearing. `record.exc_text` is None while filters
    run and is only populated by `Formatter.format()`. Under pytest, the logging
    plugin's own handler formats the record FIRST, pre-populating that field —
    so a filter-based redactor passes this test while leaking in production.
    Isolating the logger means only Ferry's handler ever sees the record, which
    is what production looks like.
    """
    register_secret("stoat", STOAT_TOKEN)
    logger = logging.getLogger("discord_ferry.isolated_traceback_probe")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    for handler in logging.getLogger().handlers:
        if handler.get_name() == "ferry-file":
            logger.addHandler(handler)

    try:
        try:
            raise ValueError(f"bad token {STOAT_TOKEN} in message")
        except ValueError:
            logger.exception("boom")
        body = _read(log_file)
    finally:
        logger.handlers.clear()
        logger.propagate = True

    assert "Traceback" in body, "no traceback was written — the test proves nothing"
    assert STOAT_TOKEN not in body, "raw token leaked via the traceback"
    assert "****abcd" in body


def test_registry_reset_clears_secrets(log_file: Path) -> None:
    """Registry state must not leak between tests."""
    register_secret("stoat", STOAT_TOKEN)
    reset_secret_registry()
    logging.getLogger("discord_ferry.test").warning("value is %s", STOAT_TOKEN)
    # Nothing registered and not Discord-shaped, so it passes through unmasked —
    # this asserts the reset actually happened, not that leaking is acceptable.
    assert STOAT_TOKEN in _read(log_file)


# ---------------------------------------------------------------------------
# Entry-point wiring
# ---------------------------------------------------------------------------


def test_probe_command_registers_its_token() -> None:
    """SC-123-20: `run_probe` builds no FerryConfig and no SecureTokenStore.

    Without an explicit hook in `probe_cmd`, the Stoat token passed to
    `ferry probe` has ZERO redaction coverage — and the regex floor deliberately
    cannot match Stoat tokens (opaque base64url).
    """
    import inspect

    from discord_ferry import cli

    source = inspect.getsource(cli.probe_cmd.callback)
    assert "register_secret" in source, "probe_cmd must register its token"


def test_cli_group_configures_logging() -> None:
    """SC-123-25 companion: the wiring exists at the group callback."""
    import inspect

    from discord_ferry import cli

    assert "configure_logging()" in inspect.getsource(cli.main.callback)
