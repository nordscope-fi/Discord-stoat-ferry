"""Discord REST helpers for test-server provisioning (Bot auth).

This module deliberately does NOT inherit its exceptions from FerryError.
The location (tests/provisioning/) excludes it from built wheels via the
[tool.hatch.build.targets.wheel] stanza in pyproject.toml; the separate
exception hierarchy reinforces that isolation in the type system.
"""

from __future__ import annotations

import logging


class ProvisioningError(Exception):
    """Base for all provisioning failures."""


class ProvisioningAuthError(ProvisioningError):
    """Bot token invalid, expired, or missing required scopes (401)."""


class ProvisioningPermissionError(ProvisioningError):
    """Bot lacks Discord permission for the operation (403)."""


class ProvisioningRateLimitError(ProvisioningError):
    """Rate limited after exhausting retries (429)."""


class TokenRedactingFilter(logging.Filter):
    """Scrubs the bot token from every log record's resolved message.

    Operates on record.getMessage() (which uniformly handles %-style, {}-style,
    and Mapping-args formatting) rather than scanning record.msg and record.args
    separately. If the token is found, replaces record.msg with the redacted
    version and clears record.args so re-formatting doesn't reintroduce the
    token.
    """

    REDACTED = "<TOKEN>"

    def __init__(self, token: str) -> None:
        super().__init__()
        self._token = token

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if self._token in message:
            record.msg = message.replace(self._token, self.REDACTED)
            record.args = None
        return True
