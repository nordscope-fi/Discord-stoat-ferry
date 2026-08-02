from __future__ import annotations

import aiohttp
import pytest
from aioresponses import aioresponses

from discord_ferry.migrator.probe import ProbeReport, _check_autumn, run_probe
from discord_ferry.uploader.autumn import TAG_SIZE_LIMITS

BASE_URL = "https://api.test"
AUTUMN_URL = "https://cdn.test"
TOKEN = "test-token"


def _root_payload(limits: dict[str, object] | None) -> dict[str, object]:
    """A Stoat API root payload, optionally carrying a `features.limits` block."""
    features: dict[str, object] = {"autumn": {"url": AUTUMN_URL}}
    if limits is not None:
        features["limits"] = limits
    return {"revolt": "0.14.3", "features": features}


def _check(report: ProbeReport, name: str) -> str:
    """Return "status: detail" for a named check, so assertions read as one string."""
    for c in report.checks:
        if c.name == name:
            return f"{c.status}: {c.detail}"
    raise AssertionError(f"no check named {name!r} in {[c.name for c in report.checks]}")


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


# ---------------------------------------------------------------------------
# Autumn limits drift detector
# ---------------------------------------------------------------------------


async def test_autumn_limits_mismatch_warns(mock_aiohttp: aioresponses) -> None:
    """A deliberately mismatched advertised limit must produce `warn` — proving it can fail.

    Against the pre-fix code this asserts nothing: `_check_autumn` read `tags` from the
    Autumn root, which no longer returns that key, so the diff list was always empty and
    the check reported "matches assumptions" unconditionally.
    """
    advertised = dict(TAG_SIZE_LIMITS)
    advertised["icons"] = 1_000_000  # against our assumed 2_500_000
    mock_aiohttp.get(
        f"{BASE_URL}/",
        payload=_root_payload({"default": {"file_upload_size_limits": advertised}}),
    )
    mock_aiohttp.get(f"{AUTUMN_URL}/", payload={"autumn": "hi", "version": "0.14.3"})

    report = ProbeReport()
    async with aiohttp.ClientSession() as session:
        await _check_autumn(session, BASE_URL, report)

    result = _check(report, "autumn_limits")
    assert result.startswith("warn:")
    assert "icons" in result
    assert "1000000" in result.replace(",", "").replace("_", "")
    assert "2500000" in result.replace(",", "").replace("_", "")
    assert "default" in result


async def test_autumn_limits_matching_reports_ok(mock_aiohttp: aioresponses) -> None:
    """Limits equal to our assumptions report `ok`, naming the tier(s) actually compared.

    The payload is built FROM TAG_SIZE_LIMITS so this cannot rot when those values change.
    """
    mock_aiohttp.get(
        f"{BASE_URL}/",
        payload=_root_payload(
            {
                "new_user": {"file_upload_size_limits": dict(TAG_SIZE_LIMITS)},
                "default": {"file_upload_size_limits": dict(TAG_SIZE_LIMITS)},
            }
        ),
    )
    mock_aiohttp.get(f"{AUTUMN_URL}/", payload={"autumn": "hi", "version": "0.14.3"})

    report = ProbeReport()
    async with aiohttp.ClientSession() as session:
        await _check_autumn(session, BASE_URL, report)

    result = _check(report, "autumn_limits")
    assert result.startswith("ok:")
    assert "new_user" in result and "default" in result


async def test_autumn_limits_absent_is_reported_not_silently_passed(
    mock_aiohttp: aioresponses,
) -> None:
    """A root without `features.limits` must say so — never "matches assumptions".

    This is the exact failure the old implementation had: nothing to compare, reported
    as a clean pass.
    """
    mock_aiohttp.get(f"{BASE_URL}/", payload=_root_payload(None))
    mock_aiohttp.get(f"{AUTUMN_URL}/", payload={"autumn": "hi", "version": "0.14.3"})

    report = ProbeReport()
    async with aiohttp.ClientSession() as session:
        await _check_autumn(session, BASE_URL, report)

    result = _check(report, "autumn_limits")
    assert result.startswith("warn:")
    assert "could not read" in result
    assert "matches" not in result
    assert "assumptions" not in result


async def test_unreachable_autumn_root_does_not_mask_the_limits_result(
    mock_aiohttp: aioresponses,
) -> None:
    """Autumn being down fails only its own check; the limits diff still lands.

    The two are reported separately precisely so one cannot hide the other.
    """
    mock_aiohttp.get(
        f"{BASE_URL}/",
        payload=_root_payload({"default": {"file_upload_size_limits": dict(TAG_SIZE_LIMITS)}}),
    )
    mock_aiohttp.get(f"{AUTUMN_URL}/", status=503)

    report = ProbeReport()
    async with aiohttp.ClientSession() as session:
        await _check_autumn(session, BASE_URL, report)

    assert _check(report, "autumn_limits").startswith("ok:")
    reachable = _check(report, "autumn_reachable")
    assert reachable.startswith("fail:")
    assert "503" in reachable


async def test_malformed_limits_block_does_not_abort_the_probe(
    mock_aiohttp: aioresponses,
) -> None:
    """A non-dict where a dict is expected must not raise out of the check.

    `run_probe` wraps its four checks in try/finally with NO except, so an AttributeError
    escaping here would abort every remaining check and propagate to the caller. Truthy
    non-dicts are the dangerous shape: `x or {}` does not save them.
    """
    mock_aiohttp.get(
        f"{BASE_URL}/",
        payload={"features": {"autumn": "not-a-dict", "limits": {"default": "not-a-dict"}}},
    )

    report = ProbeReport()
    async with aiohttp.ClientSession() as session:
        await _check_autumn(session, BASE_URL, report)  # must not raise

    assert _check(report, "autumn_limits").startswith("warn:")
    assert _check(report, "autumn_reachable").startswith("warn:")


async def test_non_dict_root_does_not_abort_the_probe(mock_aiohttp: aioresponses) -> None:
    """A 200 whose JSON body is a list, not an object, is survivable."""
    mock_aiohttp.get(f"{BASE_URL}/", payload=["not", "an", "object"])

    report = ProbeReport()
    async with aiohttp.ClientSession() as session:
        await _check_autumn(session, BASE_URL, report)  # must not raise

    assert _check(report, "autumn_limits").startswith("warn:")


async def test_tag_we_assume_but_server_never_advertises_is_not_a_pass(
    mock_aiohttp: aioresponses,
) -> None:
    """An unverified assumption must warn, not report "matches".

    Reporting a tag we never actually compared as passing is exactly the failure this
    whole check is being fixed for.
    """
    partial = {"attachments": TAG_SIZE_LIMITS["attachments"]}  # only 1 of our 6
    mock_aiohttp.get(
        f"{BASE_URL}/", payload=_root_payload({"default": {"file_upload_size_limits": partial}})
    )
    mock_aiohttp.get(f"{AUTUMN_URL}/", payload={"autumn": "hi", "version": "0.14.3"})

    report = ProbeReport()
    async with aiohttp.ClientSession() as session:
        await _check_autumn(session, BASE_URL, report)

    result = _check(report, "autumn_limits")
    assert result.startswith("warn:")
    assert "not advertised" in result
    assert "emojis" in result


async def test_server_bucket_we_have_no_limit_for_is_reported(mock_aiohttp: aioresponses) -> None:
    """A bucket the instance advertises that we know nothing about is drift worth seeing."""
    advertised = dict(TAG_SIZE_LIMITS)
    advertised["stickers"] = 1_000_000
    mock_aiohttp.get(
        f"{BASE_URL}/", payload=_root_payload({"default": {"file_upload_size_limits": advertised}})
    )
    mock_aiohttp.get(f"{AUTUMN_URL}/", payload={"autumn": "hi", "version": "0.14.3"})

    report = ProbeReport()
    async with aiohttp.ClientSession() as session:
        await _check_autumn(session, BASE_URL, report)

    result = _check(report, "autumn_limits")
    assert result.startswith("warn:")
    assert "stickers" in result
