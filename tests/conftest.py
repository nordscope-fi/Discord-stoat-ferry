"""Shared test fixtures for Discord Ferry."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from aiohttp.client_reqrep import ClientResponse

# --- aioresponses / aiohttp 3.14 compatibility shim ---------------------------
# aiohttp 3.14 made ``stream_writer`` a *required* keyword-only argument of
# ``ClientResponse.__init__``. aioresponses 0.7.8 builds its mock responses by
# calling that constructor directly and does not pass it, so every mocked HTTP
# call raises ``TypeError: ... missing 1 required keyword-only argument:
# 'stream_writer'``. This mirrors the upstream fix (aioresponses#288) by
# supplying a lightweight default; ``stream_writer`` is only consulted for its
# ``output_size`` attribute. The signature guard makes this a no-op on
# aiohttp < 3.14, and ``setdefault`` keeps it inert once a fixed aioresponses
# (or a caller that passes ``stream_writer``) is in play.
#
# REMOVE THIS once aioresponses ships a release containing
# https://github.com/pnuckowski/aioresponses/pull/288 and the dev dependency is
# bumped past it.
if "stream_writer" in inspect.signature(ClientResponse.__init__).parameters:
    _orig_client_response_init = ClientResponse.__init__

    def _patched_client_response_init(self: ClientResponse, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("stream_writer", Mock(output_size=0))
        _orig_client_response_init(self, *args, **kwargs)

    # Cannot statically reassign a method; this is a deliberate test-only patch.
    ClientResponse.__init__ = _patched_client_response_init  # type: ignore[method-assign]
# -----------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    """Return the path to the test fixtures directory."""
    return FIXTURES_DIR
