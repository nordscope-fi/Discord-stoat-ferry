from __future__ import annotations

import aiohttp
import pytest
from aioresponses import aioresponses

from discord_ferry.migrator.probe import ProbeReport, run_probe

BASE_URL = "https://api.test"
TOKEN = "test-token"


def _noop_event(_evt: object) -> None:
    pass


@pytest.fixture
def mock_aiohttp() -> aioresponses:
    with aioresponses() as m:
        yield m


async def test_probe_voice_teardown_fires_on_getback_failure(mock_aiohttp: aioresponses) -> None:
    """If the GET-back fails, the created channel is still deleted (I2)."""
    # Root (autumn discovery) — minimal.
    mock_aiohttp.get(
        f"{BASE_URL}/", payload={"features": {"autumn": {"url": f"{BASE_URL}/autumn"}}}
    )
    # Voice check: create succeeds (returns TextChannel — this fork has no VoiceChannel variant),
    # GET-back 500s on every retry, DELETE must still fire.
    mock_aiohttp.post(
        f"{BASE_URL}/servers/srv1/channels",
        payload={"_id": "ch_tmp", "channel_type": "TextChannel"},
    )
    mock_aiohttp.get(f"{BASE_URL}/channels/ch_tmp", status=500, repeat=True)
    delete_called = {"n": 0}
    mock_aiohttp.delete(
        f"{BASE_URL}/channels/ch_tmp",
        callback=lambda u, **k: delete_called.__setitem__("n", 1),
        status=204,
    )
    # Webhook check: create 4xx (disabled) — keep it simple.
    mock_aiohttp.post(f"{BASE_URL}/channels/ch_tmp/webhooks", status=403, repeat=True)

    async with aiohttp.ClientSession() as session:
        report = await run_probe(BASE_URL, TOKEN, "srv1", _noop_event, session=session)

    assert delete_called["n"] == 1  # teardown fired despite GET-back failure
    assert isinstance(report, ProbeReport)
