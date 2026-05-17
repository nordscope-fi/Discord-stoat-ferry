"""Tests for tests/provisioning/_bot_api.py."""

from __future__ import annotations

from tests.provisioning._bot_api import (
    ProvisioningAuthError,
    ProvisioningError,
    ProvisioningPermissionError,
    ProvisioningRateLimitError,
)


def test_exception_hierarchy_is_self_contained() -> None:
    """ProvisioningError hierarchy must NOT inherit from FerryError.

    The import firewall is structural (tests/ not in wheels) and the type
    hierarchy reinforces it: code in src/discord_ferry/ that catches
    FerryError will not accidentally catch ProvisioningError.
    """
    assert issubclass(ProvisioningAuthError, ProvisioningError)
    assert issubclass(ProvisioningPermissionError, ProvisioningError)
    assert issubclass(ProvisioningRateLimitError, ProvisioningError)
    assert issubclass(ProvisioningError, Exception)
    # The firewall in types:
    from discord_ferry.errors import FerryError

    assert not issubclass(ProvisioningError, FerryError)
