"""GUI tool pages and their shared safety runner (issue #484 and children).

These pages let a GUI user reach the CLI's post-migration and preflight commands
(check, repair, retry, probe, blueprint, build, and the diagnostic three) without
a terminal. Every page that calls the Stoat API goes through :func:`prepare_tool_call`
first, so the token is registered for redaction and the rate limiter exists before
any request.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

from nicegui import background_tasks

import discord_ferry.migrator.api as _api
from discord_ferry.core.http import format_proxy_notices
from discord_ferry.core.security import register_secret, sanitize_secrets

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

T = TypeVar("T")


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
