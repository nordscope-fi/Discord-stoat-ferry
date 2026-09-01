"""HTTP application for the isolated public feedback intake service."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from ipaddress import IPv6Address, ip_address
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

import aiohttp
from aiohttp import web
from aiohttp.abc import AbstractAccessLogger

from discord_ferry.core.http import new_session
from discord_ferry.feedback import (
    DestinationKind,
    FeedbackErrorCode,
    FeedbackKind,
    FeedbackRequest,
    FeedbackValidationError,
    feedback_content_hash,
)
from discord_ferry.feedback_service.challenge import (
    ChallengeVerificationError,
    create_challenge,
    verify_challenge,
)
from discord_ferry.feedback_service.config import ServiceConfig
from discord_ferry.feedback_service.github import (
    FeedbackGitHub,
    GitHubAuthenticationError,
    GitHubDeliveryError,
    GitHubDeliveryUncertainError,
    GitHubReadinessError,
    ReconciledDestination,
    ReconciliationRequiredError,
)
from discord_ferry.feedback_service.store import ClaimOutcome, FeedbackStore, ReceiptRecord

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable


class ReadinessGitHub(Protocol):
    def invalidate_readiness(self) -> None: ...

    async def check_readiness(self) -> None: ...

    async def create_issue(self, request: FeedbackRequest) -> str: ...

    async def create_discussion(self, request: FeedbackRequest) -> str: ...

    async def reconcile_pending(self, receipt: ReceiptRecord) -> ReconciledDestination: ...


_HEALTH_PROBE_ID = UUID(int=0)
_READINESS_CACHE = timedelta(seconds=30)
logger = logging.getLogger(__name__)


class ServiceJSONFormatter(logging.Formatter):
    """Render only the bounded metadata allowed in service logs."""

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "event": getattr(record, "event", "service"),
                "receipt": getattr(record, "receipt", None),
                "state": getattr(record, "state", None),
                "destination_kind": getattr(record, "destination_kind", None),
                "status_class": getattr(record, "status_class", None),
                "duration_ms": getattr(record, "duration_ms", None),
            },
            separators=(",", ":"),
        )


class FeedbackAccessLogger(AbstractAccessLogger):
    """Record request timing without source, path, query, or headers."""

    def log(self, request: object, response: object, duration: float) -> None:
        status = getattr(response, "status", 500)
        status_class = f"{status // 100}xx" if isinstance(status, int) else None
        self.logger.info(
            "http_access",
            extra={
                "event": "http_access",
                "receipt": None,
                "state": None,
                "destination_kind": None,
                "status_class": status_class,
                "duration_ms": round(duration * 1000),
            },
        )


def install_service_logging() -> None:
    """Install the same metadata-only formatter for service and access events."""

    handler = logging.StreamHandler()
    handler.setFormatter(ServiceJSONFormatter())
    for target in (logger, logging.getLogger("aiohttp.access")):
        target.handlers = [handler]
        target.setLevel(logging.INFO)
        target.propagate = False


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


@dataclass
class _ReadinessState:
    now: Callable[[], datetime]
    expires_at: datetime | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


CONFIG_KEY = web.AppKey("config", ServiceConfig)
STORE_KEY = web.AppKey("store", FeedbackStore)
SESSION_KEY = web.AppKey("session", aiohttp.ClientSession)
GITHUB_KEY = web.AppKey("github", ReadinessGitHub)
READINESS_KEY = web.AppKey("readiness", _ReadinessState)


async def _health(request: web.Request) -> web.Response:
    """Report whether the service can query its local durable store."""

    try:
        await request.app[STORE_KEY].get_receipt(_HEALTH_PROBE_ID)
    except (OSError, sqlite3.Error):
        return web.json_response({"status": "unhealthy"}, status=503)
    return web.json_response({"status": "ok"})


async def _ready(request: web.Request) -> web.Response:
    """Report whether every external GitHub route is available."""

    if await _github_is_ready(request.app):
        return web.json_response({"status": "ready"})
    return web.json_response({"status": "unready"}, status=503)


async def _github_is_ready(app: web.Application) -> bool:
    state = app[READINESS_KEY]
    async with state.lock:
        now = state.now().astimezone(UTC)
        if state.expires_at is not None and now < state.expires_at:
            return True
        try:
            await app[GITHUB_KEY].check_readiness()
        except (GitHubAuthenticationError, GitHubReadinessError):
            state.expires_at = None
            return False
        state.expires_at = now + _READINESS_CACHE
    return True


def _error_response(
    code: str,
    message: str,
    *,
    status: int,
    retry_at: datetime | None = None,
) -> web.Response:
    body: dict[str, object] = {"code": code, "message": message}
    if retry_at is not None:
        body["retry_at"] = retry_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return web.json_response(body, status=status)


def _request_source(request: web.Request) -> str:
    peername = None if request.transport is None else request.transport.get_extra_info("peername")
    if not isinstance(peername, tuple) or not peername or not isinstance(peername[0], str):
        raise RuntimeError("request peer is unavailable")
    peer = ip_address(peername[0])
    source = peer
    config = request.app[CONFIG_KEY]
    if any(peer in network for network in config.trusted_proxy_networks):
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            try:
                forwarded_chain = [ip_address(value.strip()) for value in forwarded.split(",")]
            except ValueError:
                source = peer
            else:
                for candidate in reversed(forwarded_chain):
                    source = candidate
                    if not any(candidate in network for network in config.trusted_proxy_networks):
                        break
    if isinstance(source, IPv6Address) and source.ipv4_mapped is not None:
        return source.ipv4_mapped.compressed
    return source.compressed


async def _challenge(request: web.Request) -> web.Response:
    """Issue one signed proof challenge after validation and source quota."""

    if request.content_type != "application/json":
        return _error_response(
            "invalid_input",
            "Request content must be JSON.",
            status=415,
        )
    try:
        data = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _error_response("invalid_input", "Request JSON is invalid.", status=400)
    if not isinstance(data, dict) or set(data) != {"contract_version", "request_id"}:
        return _error_response("invalid_input", "Request body is invalid.", status=400)
    if isinstance(data["contract_version"], bool) or data["contract_version"] != 1:
        return _error_response("invalid_input", "Contract version is invalid.", status=400)
    request_id_value = data["request_id"]
    if not isinstance(request_id_value, str):
        return _error_response("invalid_input", "Request ID is invalid.", status=400)
    try:
        request_id = UUID(request_id_value)
        source = _request_source(request)
    except ValueError:
        return _error_response("invalid_input", "Request ID is invalid.", status=400)
    except RuntimeError:
        return _error_response(
            "temporary_failure",
            "Feedback service is temporarily unavailable.",
            status=503,
        )

    store = request.app[STORE_KEY]
    source_hash = store.source_digest(source)
    now = request.app[READINESS_KEY].now().astimezone(UTC)
    quota = await store.check_challenge_quota_hash(source_hash, now=now)
    if not quota.allowed:
        return _error_response(
            "throttled",
            "Too many challenge requests. Try again later.",
            status=429,
            retry_at=quota.retry_at,
        )
    challenge = create_challenge(
        request_id,
        source_hash,
        now,
        request.app[CONFIG_KEY].challenge_key,
    )
    return web.json_response(challenge.response_mapping())


def _receipt_response(
    request_id: UUID,
    destination_kind: DestinationKind,
    destination_url: str,
) -> web.Response:
    return web.json_response(
        {
            "receipt": str(request_id),
            "destination_kind": destination_kind.value,
            "url": destination_url,
        }
    )


def _log_feedback(
    request_id: UUID,
    state: str,
    destination_kind: DestinationKind,
    status: int,
    started: float,
) -> None:
    logger.info(
        "feedback_delivery",
        extra={
            "event": "feedback_delivery",
            "receipt": str(request_id),
            "state": state,
            "destination_kind": destination_kind.value,
            "status_class": f"{status // 100}xx",
            "duration_ms": round((time.monotonic() - started) * 1000),
        },
    )


async def _feedback(request: web.Request) -> web.Response:
    """Validate, claim, deliver, and receipt one reviewed feedback request."""

    started = time.monotonic()
    if request.content_type != "application/json":
        return _error_response(
            FeedbackErrorCode.INVALID_INPUT.value,
            "Request content must be JSON.",
            status=415,
        )
    try:
        data = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _error_response(
            FeedbackErrorCode.INVALID_INPUT.value,
            "Request JSON is invalid.",
            status=400,
        )
    try:
        feedback = FeedbackRequest.from_mapping(data)
        source = _request_source(request)
    except FeedbackValidationError:
        return _error_response(
            FeedbackErrorCode.INVALID_INPUT.value,
            "Feedback request is invalid.",
            status=400,
        )
    except RuntimeError:
        return _error_response(
            FeedbackErrorCode.TEMPORARY_FAILURE.value,
            "Feedback service is temporarily unavailable.",
            status=503,
        )

    store = request.app[STORE_KEY]
    source_hash = store.source_digest(source)
    now = request.app[READINESS_KEY].now().astimezone(UTC)
    try:
        verify_challenge(
            feedback.challenge,
            request_id=feedback.request_id,
            source_hash=source_hash,
            now=now,
            key=request.app[CONFIG_KEY].challenge_key,
        )
    except ChallengeVerificationError as exc:
        return _error_response(
            exc.code.value,
            "Feedback challenge is invalid.",
            status=400,
        )

    cleaned = feedback.cleaned_for_send()
    destination_kind = (
        DestinationKind.ISSUE if cleaned.kind is FeedbackKind.BUG else DestinationKind.DISCUSSION
    )
    content_hash = feedback_content_hash(cleaned)
    existing = await store.get_receipt(cleaned.request_id)
    if existing is None:
        quota = await store.claim_report_quota_hash(source_hash, now=now)
        if not quota.allowed:
            _log_feedback(cleaned.request_id, "throttled", destination_kind, 429, started)
            return _error_response(
                FeedbackErrorCode.THROTTLED.value,
                "Too many feedback requests. Try again later.",
                status=429,
                retry_at=quota.retry_at,
            )

    claim = await store.claim_receipt(
        cleaned.request_id,
        content_hash,
        destination_kind,
        now=now,
    )
    if claim.outcome is ClaimOutcome.CONFLICT:
        _log_feedback(cleaned.request_id, "conflict", destination_kind, 409, started)
        return _error_response(
            FeedbackErrorCode.DUPLICATE_ID_CONFLICT.value,
            "This request ID already belongs to different feedback.",
            status=409,
        )
    if claim.outcome is ClaimOutcome.DELIVERED:
        destination_url = claim.record.destination_url
        if destination_url is None:
            return _error_response(
                FeedbackErrorCode.TEMPORARY_FAILURE.value,
                "Feedback receipt is temporarily unavailable.",
                status=503,
            )
        _log_feedback(cleaned.request_id, "delivered", destination_kind, 200, started)
        return _receipt_response(cleaned.request_id, destination_kind, destination_url)

    github = request.app[GITHUB_KEY]
    if claim.outcome is ClaimOutcome.PENDING:
        try:
            reconciled = await github.reconcile_pending(claim.record)
        except ReconciliationRequiredError:
            _log_feedback(cleaned.request_id, "pending", destination_kind, 503, started)
            return _error_response(
                FeedbackErrorCode.RECONCILIATION_REQUIRED.value,
                "Feedback delivery requires maintainer review.",
                status=503,
            )
        if reconciled.kind is not destination_kind:
            _log_feedback(cleaned.request_id, "pending", destination_kind, 503, started)
            return _error_response(
                FeedbackErrorCode.RECONCILIATION_REQUIRED.value,
                "Feedback delivery requires maintainer review.",
                status=503,
            )
        destination_url = reconciled.url
    else:
        if not await _github_is_ready(request.app):
            await store.mark_failed(cleaned.request_id, now=now)
            _log_feedback(cleaned.request_id, "failed", destination_kind, 503, started)
            return _error_response(
                FeedbackErrorCode.GITHUB_FAILURE.value,
                "GitHub feedback routing is unavailable.",
                status=503,
            )
        try:
            destination_url = (
                await github.create_issue(cleaned)
                if destination_kind is DestinationKind.ISSUE
                else await github.create_discussion(cleaned)
            )
        except GitHubDeliveryUncertainError:
            invalidate_readiness(request.app)
            _log_feedback(cleaned.request_id, "pending", destination_kind, 503, started)
            return _error_response(
                FeedbackErrorCode.RECONCILIATION_REQUIRED.value,
                "Feedback delivery requires maintainer review.",
                status=503,
            )
        except (
            GitHubAuthenticationError,
            GitHubDeliveryError,
            GitHubReadinessError,
        ):
            invalidate_readiness(request.app)
            await store.mark_failed(cleaned.request_id, now=now)
            _log_feedback(cleaned.request_id, "failed", destination_kind, 502, started)
            return _error_response(
                FeedbackErrorCode.GITHUB_FAILURE.value,
                "GitHub rejected feedback delivery.",
                status=502,
            )

    await store.store_contact(cleaned.request_id, cleaned.contact_email, now=now)
    await store.mark_delivered(cleaned.request_id, destination_url, now=now)
    _log_feedback(cleaned.request_id, "delivered", destination_kind, 200, started)
    return _receipt_response(cleaned.request_id, destination_kind, destination_url)


def invalidate_readiness(app: web.Application) -> None:
    """Require the next readiness request to check GitHub again."""

    app[READINESS_KEY].expires_at = None
    app[GITHUB_KEY].invalidate_readiness()


def create_app(
    config: ServiceConfig,
    *,
    store: FeedbackStore | None = None,
    session: aiohttp.ClientSession | None = None,
    github: ReadinessGitHub | None = None,
    now: Callable[[], datetime] = _utc_now,
) -> web.Application:
    """Build the feedback service without starting a network listener."""

    app = web.Application(client_max_size=config.max_request_bytes)
    app[CONFIG_KEY] = config
    app[READINESS_KEY] = _ReadinessState(now=now)
    app[STORE_KEY] = store or FeedbackStore(
        config.database_path,
        contact_key=config.contact_key,
        source_hash_key=config.source_hash_key,
    )

    async def lifecycle(application: web.Application) -> AsyncIterator[None]:
        await application[STORE_KEY].initialize()
        owned_session = session is None
        active_session = session or new_session(
            timeout=aiohttp.ClientTimeout(total=config.github_timeout_seconds)
        )
        application[SESSION_KEY] = active_session
        application[GITHUB_KEY] = github or FeedbackGitHub(config, session=active_session)
        try:
            yield
        finally:
            if owned_session:
                await active_session.close()

    app.cleanup_ctx.append(lifecycle)
    app.router.add_get("/health", _health, allow_head=False)
    app.router.add_get("/ready", _ready, allow_head=False)
    app.router.add_post("/v1/challenge", _challenge)
    app.router.add_post("/v1/feedback", _feedback)
    return app


__all__ = [
    "CONFIG_KEY",
    "GITHUB_KEY",
    "READINESS_KEY",
    "SESSION_KEY",
    "STORE_KEY",
    "create_app",
    "invalidate_readiness",
]
