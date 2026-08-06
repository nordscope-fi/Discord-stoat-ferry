"""Always-on file logging with token redaction.

Ferry ships its GUI as a windowed PyInstaller binary (``ferry.spec`` sets
``console=False``), which means ``sys.stdout`` and ``sys.stderr`` are ``None``.
Nothing in ``src/`` ever attached a logging handler, so the 13 module loggers --
and, worse, NiceGUI's own uncaught-background-task handler -- wrote to
``logging.lastResort``, whose ``emit`` no-ops when ``sys.stderr`` is ``None``.

That is why issue #123 was undiagnosable: a ``RuntimeError`` killed the export
task on every run for eleven releases and the traceback went nowhere. This
module exists so the next failure of that class leaves evidence.
"""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from discord_ferry.core.security import sanitize_secrets

__all__ = ["configure_logging", "reset_logging"]

_LOG_DIR_NAME = ".discord-ferry"
_LOG_FILE_NAME = "ferry.log"
_MAX_BYTES = 2 * 1024 * 1024
_BACKUP_COUNT = 3
_HANDLER_NAME = "ferry-file"

# Discord tokens have a recognisable three-part shape. This is a *floor*, not the
# main defence: it covers the window between process start and the first
# register_secret() call. It deliberately does NOT try to match Stoat tokens --
# those are opaque base64url and any pattern wide enough to catch one would also
# redact ordinary Ulids and channel IDs.
_DISCORD_TOKEN_RE = re.compile(r"\b(?:mfa\.[\w-]{20,}|[\w-]{23,28}\.[\w-]{6,7}\.[\w-]{27,})\b")


def _redact(text: str) -> str:
    """Mask registered secrets, then Discord-shaped tokens, in *text*."""
    return _DISCORD_TOKEN_RE.sub("****", sanitize_secrets(text))


class _RedactingFormatter(logging.Formatter):
    """Sanitise the fully-formatted record, tracebacks included.

    Redaction MUST happen here rather than in a ``logging.Filter``, for two
    reasons that a filter-based implementation gets wrong:

    1. **Tracebacks.** ``record.exc_text`` is ``None`` while filters run -- it is
       populated by ``Formatter.format()`` afterwards. A filter that sanitises
       ``exc_text`` is therefore a no-op on the first handler to see the record,
       and the raw traceback lands in the file. (This bites hard here: exception
       *messages* routinely carry the token that caused the failure. It also
       hides under pytest, whose logging plugin formats the record first and
       pre-populates the field -- so a filter-based version passes its own test
       while leaking in production.)
    2. **Shared records.** A filter has to mutate ``record.msg`` / ``record.args``
       to catch deferred ``%s`` arguments, and that mutation is visible to every
       other handler on the logger. Formatting is per-handler and mutates
       nothing.

    ``format()`` renders message, args, ``exc_text`` and ``stack_info`` into one
    string, so sanitising its return value covers all of them at once.
    """

    def format(self, record: logging.LogRecord) -> str:
        return _redact(super().format(record))


def _log_path() -> Path:
    """Return the log file path. Sits under the same root as the DCE cache."""
    return Path.home() / _LOG_DIR_NAME / "logs" / _LOG_FILE_NAME


def _existing_handler() -> logging.Handler | None:
    return next(
        (h for h in logging.getLogger().handlers if h.get_name() == _HANDLER_NAME),
        None,
    )


def configure_logging(*, path: Path | None = None) -> Path | None:
    """Attach a rotating file handler to the root logger.

    Idempotent: a second call returns the existing path rather than stacking a
    handler. That matters because ``cli.main()`` is a Click *group* callback, so
    it runs on every ``CliRunner().invoke(main, ...)``.

    Deliberately attaches no ``StreamHandler``: Rich and ``click.echo`` own the
    console, and a second stream would double every line the CLI prints.

    Args:
        path: Override the log location. Tests pass a tmp path; production
            never does.

    Returns:
        The resolved log path, or ``None`` when the file could not be opened.
        Logging is a diagnostic aid, never a startup dependency -- an
        unwritable home directory must not stop Ferry from running.
    """
    existing = _existing_handler()
    if existing is not None:
        return Path(existing.baseFilename) if isinstance(existing, RotatingFileHandler) else None

    target = path or _log_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            target,
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
    except OSError:
        return None

    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(_RedactingFormatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s"))

    root = logging.getLogger()
    root.addHandler(handler)
    # INFO, not DEBUG: migrator/messages.py logs per message, which would bury
    # the signal and blow through rotation on a large migration.
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)

    return target


def reset_logging() -> None:
    """Detach the file handler. For tests only.

    Mirrors ``migrator.api._reset_circuit_state``. Without this, the handler
    installed by one test stays attached for the whole pytest session.
    """
    handler = _existing_handler()
    if handler is not None:
        logging.getLogger().removeHandler(handler)
        handler.close()
