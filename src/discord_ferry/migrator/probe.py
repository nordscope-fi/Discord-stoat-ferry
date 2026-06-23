"""Live validation probe for a Stoat instance (read-mostly diagnostics)."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import aiohttp

from discord_ferry.migrator.api import (
    api_create_channel,
    api_create_webhook,
    api_delete_channel,
    api_delete_webhook,
    api_execute_webhook,
    api_fetch_channel,
)
from discord_ferry.migrator.connect import _discover_autumn_url
from discord_ferry.uploader.autumn import TAG_SIZE_LIMITS

if TYPE_CHECKING:
    from collections.abc import Callable


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
    sess = session or aiohttp.ClientSession()
    try:
        await _check_autumn(sess, stoat_url, report)
        await _check_voice_bug(sess, stoat_url, token, test_server_id, report)
        await _check_webhook(sess, stoat_url, token, test_server_id, report)
        await _check_rate_limit(sess, stoat_url, token, test_server_id, report)
        if deep:
            report.add("deep_probe", "warn", "deep boundary-upload probe not yet implemented")
    finally:
        if own_session:
            await sess.close()
    return report


async def _check_autumn(sess: aiohttp.ClientSession, stoat_url: str, report: ProbeReport) -> None:
    try:
        autumn_url = await _discover_autumn_url(sess, stoat_url)
        # Best-effort: GET the autumn root for advertised per-tag limits.
        async with sess.get(f"{autumn_url.rstrip('/')}/") as resp:
            data: dict[str, Any] = await resp.json() if resp.status == 200 else {}
        advertised = data.get("tags") or {}
        diffs = [
            f"{tag}: advertised {advertised.get(tag)} vs assumed {assumed}"
            for tag, assumed in TAG_SIZE_LIMITS.items()
            if tag in advertised and advertised.get(tag) != assumed
        ]
        report.add(
            "autumn_limits",
            "warn" if diffs else "ok",
            "; ".join(diffs) or "matches assumptions",
        )
    except Exception as exc:  # noqa: BLE001 — probe must continue
        report.add("autumn_limits", "fail", f"{type(exc).__name__}: {exc}")


async def _check_voice_bug(
    sess: aiohttp.ClientSession, stoat_url: str, token: str, server_id: str, report: ProbeReport
) -> None:
    # SOURCE-VERIFIED (stoatchat/stoatchat core/models/v0/channels.rs): this fork has
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
