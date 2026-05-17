"""Discord REST helpers for test-server provisioning (Bot auth).

This module deliberately does NOT inherit its exceptions from FerryError.
The location (tests/provisioning/) excludes it from built wheels via the
[tool.hatch.build.targets.wheel] stanza in pyproject.toml; the separate
exception hierarchy reinforces that isolation in the type system.
"""

from __future__ import annotations


class ProvisioningError(Exception):
    """Base for all provisioning failures."""


class ProvisioningAuthError(ProvisioningError):
    """Bot token invalid, expired, or missing required scopes (401)."""


class ProvisioningPermissionError(ProvisioningError):
    """Bot lacks Discord permission for the operation (403)."""


class ProvisioningRateLimitError(ProvisioningError):
    """Rate limited after exhausting retries (429)."""
