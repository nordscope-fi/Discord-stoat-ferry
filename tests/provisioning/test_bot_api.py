"""Tests for tests/provisioning/_bot_api.py."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from tests.provisioning._bot_api import (
    ProvisioningAuthError,
    ProvisioningError,
    ProvisioningPermissionError,
    ProvisioningRateLimitError,
    TokenRedactingFilter,
)

if TYPE_CHECKING:
    import pytest


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


def test_token_redacting_filter_scrubs_token_from_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Filter must replace literal token with <TOKEN> in record.getMessage()."""
    token = "secret-token-MTM0NTY3.ABCdef"
    fltr = TokenRedactingFilter(token)
    logger = logging.getLogger("test_token_filter")
    logger.addFilter(fltr)
    logger.setLevel(logging.DEBUG)

    with caplog.at_level(logging.DEBUG, logger="test_token_filter"):
        logger.info("about to call: token=%s", token)

    assert token not in caplog.text
    assert "<TOKEN>" in caplog.text


def test_token_redacting_filter_handles_no_token_in_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Filter must not modify records that don't contain the token."""
    token = "secret-token-xyz"
    fltr = TokenRedactingFilter(token)
    logger = logging.getLogger("test_token_filter_clean")
    logger.addFilter(fltr)
    logger.setLevel(logging.DEBUG)

    with caplog.at_level(logging.DEBUG, logger="test_token_filter_clean"):
        logger.info("normal log line with no secrets")

    assert "normal log line with no secrets" in caplog.text
