"""Live validation probe for a Stoat instance (read-mostly diagnostics)."""

from __future__ import annotations

import contextlib
import struct
import zlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import aiohttp  # noqa: TCH002

from discord_ferry.core.http import new_session
from discord_ferry.migrator.api import (
    api_create_channel,
    api_create_webhook,
    api_delete_channel,
    api_delete_webhook,
    api_execute_webhook,
    api_fetch_channel,
)
from discord_ferry.uploader.autumn import TAG_SIZE_LIMITS

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

# Tiers on the Stoat root that may carry upload limits. `global` carries none today; it
# is listed so that it would be compared rather than missed if it ever gained them.
_LIMIT_TIERS = ("global", "new_user", "default")


def _make_test_file(directory: Path, tag: str, size: int) -> Path:
    """Generate a test file of exactly *size* bytes for an Autumn upload probe.

    Non-``attachments`` tags reject non-images, so those get a minimal valid PNG
    padded with a ``tEXt`` chunk. ``attachments`` gets raw zero bytes.
    """
    path = directory / f"probe_{tag}_{size}.{'png' if tag != 'attachments' else 'bin'}"
    if tag == "attachments":
        path.write_bytes(b"\x00" * size)
        return path

    sig = b"\x89PNG\r\n\x1a\n"

    def _chunk(name: bytes, data: bytes) -> bytes:
        raw = name + data
        return struct.pack(">I", len(data)) + raw + struct.pack(">I", zlib.crc32(raw) & 0xFFFFFFFF)

    ihdr_data = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    ihdr = _chunk(b"IHDR", ihdr_data)
    raw_pixel = zlib.compress(b"\x00\x00\x00\x00")
    idat = _chunk(b"IDAT", raw_pixel)
    iend = _chunk(b"IEND", b"")
    core = sig + ihdr + idat + iend
    core_size = len(core)

    if size <= core_size:
        path.write_bytes(core)
        return path

    keyword = b"probe"
    text_overhead = 4 + 4 + len(keyword) + 1 + 4  # length + "tEXt" + keyword + null + CRC
    pad_data_len = size - core_size - text_overhead
    text_chunk = _chunk(b"tEXt", keyword + b"\x00" + b"x" * pad_data_len)
    content = sig + ihdr + idat + text_chunk + iend
    path.write_bytes(content)
    return path


async def _raw_autumn_upload(
    session: aiohttp.ClientSession,
    autumn_url: str,
    tag: str,
    file_path: Path,
    token: str,
) -> int:
    """Upload a file to Autumn and return the HTTP status code.

    No client-side size check, no retry. Used by the deep probe to test
    whether the server enforces its advertised limit.
    """
    url = f"{autumn_url.rstrip('/')}/{tag}"
    headers = {"x-session-token": token}
    form = aiohttp.FormData()
    form.add_field("file", file_path.open("rb"), filename=file_path.name)
    async with session.post(url, data=form, headers=headers) as resp:
        return resp.status


def _sub_dict(obj: Any, key: str) -> dict[str, Any]:
    """``obj[key]`` when both it and ``obj`` are dicts, else ``{}``.

    This module parses JSON from an instance we do not control, and ``run_probe`` wraps
    its four checks in ``try/finally`` with **no** ``except`` -- so an ``AttributeError``
    from something like ``limits["new_user"]`` being a string would abort every remaining
    check and propagate to the caller. Guarding each hop makes the parse total, which is
    safer than catching afterwards.
    """
    value = obj.get(key) if isinstance(obj, dict) else None
    return value if isinstance(value, dict) else {}


@dataclass
class ProbeCheck:
    name: str
    status: str  # "ok" | "warn" | "fail"
    detail: str


@dataclass
class ProbeReport:
    checks: list[ProbeCheck] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str) -> None:
        self.checks.append(ProbeCheck(name=name, status=status, detail=detail))


async def run_probe(
    stoat_url: str,
    token: str,
    test_server_id: str,
    on_event: Callable[[object], None],
    *,
    deep: bool = False,
    session: aiohttp.ClientSession | None = None,
) -> ProbeReport:
    """Run four independent read-mostly checks against a live Stoat instance.

    Never constructs or writes a MigrationState/state.json. Every entity created
    under ``test_server_id`` is torn down in a ``finally`` block.
    """
    report = ProbeReport()
    own_session = session is None
    sess = session or new_session()
    try:
        autumn_url = await _check_autumn(sess, stoat_url, report)
        await _check_voice_bug(sess, stoat_url, token, test_server_id, report)
        await _check_webhook(sess, stoat_url, token, test_server_id, report)
        await _check_rate_limit(sess, stoat_url, token, test_server_id, report)
        if deep:
            await _check_deep_uploads(
                sess, stoat_url, autumn_url, token, test_server_id, report
            )
    finally:
        if own_session:
            await sess.close()
    return report


async def _check_autumn(
    sess: aiohttp.ClientSession, stoat_url: str, report: ProbeReport
) -> str | None:
    """Diff our assumed upload limits against the ones this instance advertises.

    The limits live on the **Stoat** root under
    ``features.limits.<tier>.file_upload_size_limits`` -- not on the Autumn root, which
    returns only ``{"autumn": ..., "version": ...}``. Until v2.8.5 this read a ``tags``
    key from the Autumn root; that key does not exist, so the diff list was always empty
    and the check reported "matches assumptions" unconditionally. That is worse than no
    check, because it asserted the very thing it never tested -- and it hid five wrong
    values in ``TAG_SIZE_LIMITS`` for months.

    One GET serves both halves: the limits, and the Autumn URL for the reachability
    check. Reported as two independent checks so an unreachable Autumn cannot mask the
    limits result.
    """
    try:
        async with sess.get(f"{stoat_url.rstrip('/')}/") as resp:
            root: Any = await resp.json() if resp.status == 200 else {}
    except Exception as exc:  # noqa: BLE001 — probe must continue
        detail = f"{type(exc).__name__}: {exc}"
        report.add("autumn_limits", "fail", detail)
        report.add("autumn_reachable", "fail", f"Stoat root unreachable: {detail}")
        return None

    features = _sub_dict(root, "features")
    _report_autumn_limits(features, report)
    await _report_autumn_reachable(sess, features, report)

    autumn_url = _sub_dict(features, "autumn").get("url")
    return autumn_url if isinstance(autumn_url, str) and autumn_url else None


def _report_autumn_limits(features: dict[str, Any], report: ProbeReport) -> None:
    """Compare ``TAG_SIZE_LIMITS`` per tier, or say plainly that we could not read them."""
    limits = _sub_dict(features, "limits")
    tiers_read: list[str] = []
    diffs: list[str] = []
    for tier in _LIMIT_TIERS:
        advertised = _sub_dict(limits, tier).get("file_upload_size_limits")
        if not isinstance(advertised, dict) or not advertised:
            continue
        tiers_read.append(tier)
        diffs += [
            f"{tier}/{tag}: advertised {advertised[tag]} vs assumed {assumed}"
            for tag, assumed in TAG_SIZE_LIMITS.items()
            if tag in advertised and advertised[tag] != assumed
        ]
        # A tag we assume but the instance never advertises is UNVERIFIED, not matching.
        # Counting it as a pass would repeat this check's original sin.
        diffs += [
            f"{tier}/{tag}: assumed {assumed} but not advertised"
            for tag, assumed in TAG_SIZE_LIMITS.items()
            if tag not in advertised
        ]
        # A bucket we have no limit for is drift too -- it may be one we should support.
        diffs += [
            f"{tier}/{tag}: advertised {value}, we assume nothing"
            for tag, value in sorted(advertised.items())
            if tag not in TAG_SIZE_LIMITS
        ]

    if not tiers_read:
        # Never "matches assumptions" here -- we compared nothing.
        report.add(
            "autumn_limits",
            "warn",
            "could not read advertised limits (features.limits."
            f"<{'|'.join(_LIMIT_TIERS)}>.file_upload_size_limits absent)",
        )
        return

    report.add(
        "autumn_limits",
        "warn" if diffs else "ok",
        "; ".join(diffs) or f"matches assumptions for tier(s): {', '.join(tiers_read)}",
    )


async def _report_autumn_reachable(
    sess: aiohttp.ClientSession, features: dict[str, Any], report: ProbeReport
) -> None:
    """Reachability/version signal only -- the Autumn root advertises no limits."""
    autumn_url = _sub_dict(features, "autumn").get("url") or ""
    if not isinstance(autumn_url, str) or not autumn_url:
        report.add("autumn_reachable", "warn", "features.autumn.url absent from the Stoat root")
        return
    try:
        async with sess.get(f"{autumn_url.rstrip('/')}/") as resp:
            if resp.status != 200:
                # Reporting a non-200 as "ok" would repeat this check's original sin:
                # asserting health it never established.
                report.add("autumn_reachable", "fail", f"{autumn_url}: HTTP {resp.status}")
                return
            body: dict[str, Any] = await resp.json(content_type=None)
        report.add("autumn_reachable", "ok", f"{autumn_url} (version {body.get('version', '?')})")
    except Exception as exc:  # noqa: BLE001 — probe must continue
        report.add("autumn_reachable", "fail", f"{autumn_url}: {type(exc).__name__}: {exc}")


async def _check_voice_bug(
    sess: aiohttp.ClientSession, stoat_url: str, token: str, server_id: str, report: ProbeReport
) -> None:
    # SOURCE-VERIFIED (stoatchat/stoatchat core/models/v0/channels.rs): Stoat has
    # NO VoiceChannel variant — every server channel serializes as
    # "channel_type":"TextChannel". A Voice request maps to TextChannel{ voice: Some(..) }.
    # So the real signal for "voice works on this instance" is the PRESENCE of a non-null
    # `voice` field on the returned channel — NOT the discriminator. On stock self-hosted,
    # voice is unsupported (#194/#176) and `voice` comes back absent (plain text).
    channel_id: str | None = None
    try:
        created = await api_create_channel(
            sess, stoat_url, token, server_id, name="ferry-probe-voice", channel_type="Voice"
        )
        channel_id = created.get("_id")
        fetched = await api_fetch_channel(sess, stoat_url, token, channel_id) if channel_id else {}
        discriminator = fetched.get("channel_type", "?")
        has_voice = fetched.get("voice") is not None
        if has_voice:
            report.add(
                "voice_channel",
                "ok",
                f"voice supported (channel_type={discriminator}, voice present)",
            )
        else:
            report.add(
                "voice_channel",
                "warn",
                f"requested Voice but `voice` field absent (channel_type={discriminator}) — "
                "voice unsupported on this instance / Bug #194; Discord voice channels become text",
            )
    except Exception as exc:  # noqa: BLE001
        report.add("voice_channel", "fail", f"{type(exc).__name__}: {exc}")
    finally:
        if channel_id:
            # best-effort teardown
            with contextlib.suppress(Exception):
                await api_delete_channel(sess, stoat_url, token, channel_id)


async def _check_webhook(
    sess: aiohttp.ClientSession, stoat_url: str, token: str, server_id: str, report: ProbeReport
) -> None:
    channel_id: str | None = None
    webhook_id: str | None = None
    try:
        created = await api_create_channel(
            sess, stoat_url, token, server_id, name="ferry-probe-webhook", channel_type="Text"
        )
        channel_id = created.get("_id")
        if not channel_id:
            report.add("webhook", "fail", "could not create probe channel")
            return
        # SOURCE-VERIFIED (stoatchat routes/mod.rs): `POST /channels/{id}/webhooks` (create) is
        # ALWAYS mounted, but the `/webhooks/*` EXECUTE group is mounted only when
        # `features.webhooks_enabled` is true — default FALSE in stock Revolt.toml. So create may
        # 2xx while execute 404s. Judge availability on EXECUTE, not create.
        wh = await api_create_webhook(
            sess, stoat_url, token, channel_id, name="Discord Ferry Probe"
        )
        webhook_id = wh.get("id")  # webhook id is `id`, not `_id` (verified)
        if not webhook_id:
            # Malformed create response — don't execute against /webhooks//{token}
            # (which would mis-report as "webhooks DISABLED").
            report.add("webhook", "fail", "webhook create returned no id")
            return
        await api_execute_webhook(sess, stoat_url, webhook_id, wh.get("token", ""), content="probe")
        report.add(
            "webhook",
            "ok",
            "webhooks ENABLED on this instance (execute succeeded). "
            "NOTE: default webhook perms lack UploadFiles, so attachment "
            "sends would fail.",
        )
    except Exception as exc:  # noqa: BLE001
        report.add(
            "webhook",
            "warn",
            "webhooks DISABLED on this instance (features.webhooks_enabled=false by default) — "
            f"execute unavailable: {type(exc).__name__}: {exc}. This is config, not a bug.",
        )
    finally:
        if webhook_id:
            with contextlib.suppress(Exception):
                await api_delete_webhook(sess, stoat_url, token, webhook_id)
        if channel_id:
            with contextlib.suppress(Exception):
                await api_delete_channel(sess, stoat_url, token, channel_id)


async def _check_rate_limit(
    sess: aiohttp.ClientSession, stoat_url: str, token: str, server_id: str, report: ProbeReport
) -> None:
    try:
        from discord_ferry.migrator.api import _headers

        url = f"{stoat_url.rstrip('/')}/servers/{server_id}"
        async with sess.get(url, headers=_headers(token)) as resp:
            rl = {k: v for k, v in resp.headers.items() if k.lower().startswith("x-ratelimit")}
        report.add("rate_limit", "ok", f"headers={rl or 'none observed'}")
    except Exception as exc:  # noqa: BLE001
        report.add("rate_limit", "fail", f"{type(exc).__name__}: {exc}")


async def _check_deep_uploads(
    sess: aiohttp.ClientSession,
    stoat_url: str,
    autumn_url: str | None,
    token: str,
    server_id: str,
    report: ProbeReport,
) -> None:
    """Upload test files at each TAG_SIZE_LIMITS boundary and report enforcement.

    Stub: the full implementation lands in the next task.
    """
    if not autumn_url:
        report.add("deep_probe", "fail", "cannot run deep probe without Autumn URL")
        return
    report.add("deep_probe", "warn", "deep boundary-upload probe not yet implemented")
