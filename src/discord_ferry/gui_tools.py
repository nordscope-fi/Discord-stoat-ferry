"""GUI tool pages and their shared safety runner (issue #484 and children).

These pages let a GUI user reach the CLI's post-migration and preflight commands
(check, repair, retry, probe, blueprint, build, and the diagnostic three) without
a terminal. Every page that calls the Stoat API goes through :func:`prepare_tool_call`
first, so the token is registered for redaction and the rate limiter exists before
any request.
"""

from __future__ import annotations

import discord_ferry.migrator.api as _api
from discord_ferry.core.http import format_proxy_notices
from discord_ferry.core.security import register_secret


def _semaphore_is_set() -> bool:
    """True when the module-level rate limiter already exists."""
    return _api._request_semaphore is not None


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
