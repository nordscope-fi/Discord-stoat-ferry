"""Tests for the isolated public feedback intake service."""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import logging
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from ipaddress import ip_network
from types import SimpleNamespace
from uuid import UUID

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from discord_ferry.feedback import (
    Architecture,
    Challenge,
    DestinationKind,
    FeedbackDiagnostics,
    FeedbackError,
    FeedbackErrorCode,
    FeedbackInterface,
    FeedbackKind,
    FeedbackRequest,
    FeedbackStage,
    OperatingSystem,
    canonical_json,
    solve_challenge,
)
from discord_ferry.feedback_service.challenge import (
    ChallengeVerificationError,
    challenge_signature_input,
    create_challenge,
    verify_challenge,
)
from discord_ferry.feedback_service.config import ConfigError, ServiceConfig
from discord_ferry.feedback_service.github import (
    GitHubDeliveryError,
    GitHubDeliveryUncertainError,
    GitHubReadinessError,
    ReconciledDestination,
    ReconciliationRequiredError,
)
from discord_ferry.feedback_service.store import FeedbackStore, ReceiptState

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
REQUEST_ID = UUID("018f4c8c-3f52-7a89-a901-0123456789ac")
SOURCE_HASH = "a" * 64
OTHER_SOURCE_HASH = "b" * 64
CHALLENGE_KEY = bytes(range(32))


def _key(fill: int) -> str:
    return base64.urlsafe_b64encode(bytes([fill]) * 32).decode()


def _valid_config_env() -> dict[str, str]:
    return {
        "FERRY_FEEDBACK_REPOSITORY": "nordscope-fi/Discord-stoat-ferry",
        "FERRY_FEEDBACK_GITHUB_APP_ID": "4773301",
        "FERRY_FEEDBACK_GITHUB_INSTALLATION_ID": "157795120",
        "FERRY_FEEDBACK_GITHUB_PRIVATE_KEY": (
            "-----BEGIN PRIVATE KEY-----\nfixture\n-----END PRIVATE KEY-----"
        ),
        "FERRY_FEEDBACK_DATABASE_PATH": "/data/feedback.sqlite3",
        "FERRY_FEEDBACK_CHALLENGE_KEY": _key(1),
        "FERRY_FEEDBACK_SOURCE_HASH_KEY": _key(2),
        "FERRY_FEEDBACK_CONTACT_KEY": _key(3),
        "FERRY_FEEDBACK_TRUSTED_PROXY_NETWORKS": "10.0.0.0/8,fd00::/8",
    }


def test_service_config_parses_the_closed_runtime_values() -> None:
    config = ServiceConfig.from_env(_valid_config_env())

    assert config.repository == "nordscope-fi/Discord-stoat-ferry"
    assert config.github_app_id == 4_773_301
    assert config.github_installation_id == 157_795_120
    assert str(config.database_path) == "/data/feedback.sqlite3"
    assert [str(network) for network in config.trusted_proxy_networks] == [
        "10.0.0.0/8",
        "fd00::/8",
    ]
    assert config.issue_labels == ("bug", "triage")
    assert config.idea_category == "Ideas"
    assert config.general_category == "General"


def test_service_config_accepts_github_rsa_private_key() -> None:
    env = _valid_config_env()
    env["FERRY_FEEDBACK_GITHUB_PRIVATE_KEY"] = (
        "-----BEGIN RSA PRIVATE KEY-----\nfixture\n-----END RSA PRIVATE KEY-----\n"
    )

    config = ServiceConfig.from_env(env)

    assert config.github_private_key == env["FERRY_FEEDBACK_GITHUB_PRIVATE_KEY"]


@pytest.mark.parametrize(
    "private_key",
    [
        "-----BEGIN EC PRIVATE KEY-----\nfixture\n-----END RSA PRIVATE KEY-----",
        "-----BEGIN RSA PRIVATE KEY-----\nfixture\n-----END PRIVATE KEY-----",
    ],
)
def test_service_config_rejects_mismatched_private_key_envelopes(private_key: str) -> None:
    env = _valid_config_env()
    env["FERRY_FEEDBACK_GITHUB_PRIVATE_KEY"] = private_key

    with pytest.raises(ConfigError, match="FERRY_FEEDBACK_GITHUB_PRIVATE_KEY"):
        ServiceConfig.from_env(env)


@pytest.mark.parametrize("missing", sorted(_valid_config_env()))
def test_service_config_names_each_missing_variable_without_a_value(missing: str) -> None:
    env = _valid_config_env()
    removed = env.pop(missing)

    with pytest.raises(ConfigError) as raised:
        ServiceConfig.from_env(env)

    assert missing in str(raised.value)
    assert removed not in str(raised.value)


@pytest.mark.parametrize("value", ["not base64!", _key(1)[:-4]])
def test_service_config_rejects_invalid_base64_keys(value: str) -> None:
    env = _valid_config_env()
    env["FERRY_FEEDBACK_CHALLENGE_KEY"] = value

    with pytest.raises(ConfigError, match="FERRY_FEEDBACK_CHALLENGE_KEY"):
        ServiceConfig.from_env(env)


@pytest.mark.parametrize(
    "path",
    ["feedback.sqlite3", ":memory:", "/feedback.sqlite3", "/tmp/feedback.sqlite3"],
)
def test_service_config_rejects_database_paths_outside_the_private_volume(path: str) -> None:
    env = _valid_config_env()
    env["FERRY_FEEDBACK_DATABASE_PATH"] = path

    with pytest.raises(ConfigError, match="FERRY_FEEDBACK_DATABASE_PATH"):
        ServiceConfig.from_env(env)


def test_service_config_rejects_another_repository() -> None:
    env = _valid_config_env()
    env["FERRY_FEEDBACK_REPOSITORY"] = "nordscope-fi/portalpilot"

    with pytest.raises(ConfigError, match="FERRY_FEEDBACK_REPOSITORY"):
        ServiceConfig.from_env(env)


def test_service_config_requires_three_independent_keys() -> None:
    env = _valid_config_env()
    env["FERRY_FEEDBACK_CONTACT_KEY"] = env["FERRY_FEEDBACK_CHALLENGE_KEY"]

    with pytest.raises(ConfigError, match="independent"):
        ServiceConfig.from_env(env)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("FERRY_FEEDBACK_GITHUB_APP_ID", "0"),
        ("FERRY_FEEDBACK_GITHUB_APP_ID", "not-an-id"),
        ("FERRY_FEEDBACK_GITHUB_INSTALLATION_ID", "-1"),
    ],
)
def test_service_config_requires_positive_github_ids(name: str, value: str) -> None:
    env = _valid_config_env()
    env[name] = value

    with pytest.raises(ConfigError, match=name):
        ServiceConfig.from_env(env)


@pytest.mark.parametrize("networks", ["", "not-a-network", "10.0.0.1/24"])
def test_service_config_rejects_invalid_trusted_proxy_networks(networks: str) -> None:
    env = _valid_config_env()
    env["FERRY_FEEDBACK_TRUSTED_PROXY_NETWORKS"] = networks

    with pytest.raises(ConfigError, match="FERRY_FEEDBACK_TRUSTED_PROXY_NETWORKS"):
        ServiceConfig.from_env(env)


def test_service_config_defaults_are_positive_and_match_adr_030() -> None:
    config = ServiceConfig.from_env(_valid_config_env())

    assert config.max_request_bytes == 32 * 1024
    assert config.challenge_expiry_seconds == 15 * 60
    assert config.challenge_work_factor == 18
    assert config.challenge_limit_per_hour == 30
    assert config.report_limit_per_hour == 3
    assert config.report_limit_per_day == 10
    assert config.total_report_limit_per_hour == 60
    assert config.receipt_retention_seconds == 7 * 24 * 60 * 60
    assert config.rate_retention_seconds == 24 * 60 * 60
    assert config.contact_retention_seconds == 30 * 24 * 60 * 60
    assert config.github_timeout_seconds > 0


def test_service_config_repr_redacts_every_secret() -> None:
    env = _valid_config_env()
    rendered = repr(ServiceConfig.from_env(env))

    for secret_name in (
        "FERRY_FEEDBACK_GITHUB_PRIVATE_KEY",
        "FERRY_FEEDBACK_CHALLENGE_KEY",
        "FERRY_FEEDBACK_SOURCE_HASH_KEY",
        "FERRY_FEEDBACK_CONTACT_KEY",
    ):
        assert env[secret_name] not in rendered
    assert "fixture" not in rendered


@lru_cache(maxsize=1)
def _solved_challenge() -> Challenge:
    return solve_challenge(create_challenge(REQUEST_ID, SOURCE_HASH, NOW, CHALLENGE_KEY))


def test_service_challenge_has_a_32_byte_nonce_and_whole_second_expiry() -> None:
    challenge = create_challenge(REQUEST_ID, SOURCE_HASH, NOW, CHALLENGE_KEY)

    decoded_nonce = base64.urlsafe_b64decode(f"{challenge.nonce}==")
    assert len(decoded_nonce) == 32
    assert challenge.expires_at == NOW + timedelta(minutes=15)
    assert challenge.expires_at.microsecond == 0
    assert challenge.request_id == REQUEST_ID
    assert challenge.work_factor == 18


def test_challenge_signature_input_is_canonical_and_source_bound() -> None:
    challenge = create_challenge(REQUEST_ID, SOURCE_HASH, NOW, CHALLENGE_KEY)
    unsigned = challenge.response_mapping()
    unsigned.pop("signature")
    unsigned["source_hash"] = SOURCE_HASH

    assert challenge_signature_input(challenge, SOURCE_HASH) == canonical_json(unsigned)
    assert challenge_signature_input(challenge, SOURCE_HASH) != challenge_signature_input(
        challenge, OTHER_SOURCE_HASH
    )


def test_service_verifies_the_exact_client_challenge_solution() -> None:
    challenge = _solved_challenge()

    verify_challenge(
        challenge,
        request_id=REQUEST_ID,
        source_hash=SOURCE_HASH,
        now=NOW + timedelta(minutes=1),
        key=CHALLENGE_KEY,
    )


def test_service_rejects_an_expired_challenge() -> None:
    challenge = _solved_challenge()

    with pytest.raises(ChallengeVerificationError) as raised:
        verify_challenge(
            challenge,
            request_id=REQUEST_ID,
            source_hash=SOURCE_HASH,
            now=NOW + timedelta(minutes=15),
            key=CHALLENGE_KEY,
        )

    assert raised.value.code is FeedbackErrorCode.EXPIRED_CHALLENGE


@pytest.mark.parametrize(
    ("request_id", "source_hash"),
    [
        (UUID("018f4c8c-3f52-7a89-a901-1123456789ac"), SOURCE_HASH),
        (REQUEST_ID, OTHER_SOURCE_HASH),
    ],
    ids=["request", "source"],
)
def test_service_challenge_is_bound_to_request_and_source(
    request_id: UUID,
    source_hash: str,
) -> None:
    challenge = _solved_challenge()

    with pytest.raises(ChallengeVerificationError) as raised:
        verify_challenge(
            challenge,
            request_id=request_id,
            source_hash=source_hash,
            now=NOW + timedelta(minutes=1),
            key=CHALLENGE_KEY,
        )

    assert raised.value.code is FeedbackErrorCode.INVALID_CHALLENGE


@pytest.mark.parametrize("counter", [-1, 9_007_199_254_740_992])
def test_service_rejects_challenge_counter_outside_the_contract(counter: int) -> None:
    challenge = replace(_solved_challenge(), counter=counter)

    with pytest.raises(ChallengeVerificationError, match="counter"):
        verify_challenge(
            challenge,
            request_id=REQUEST_ID,
            source_hash=SOURCE_HASH,
            now=NOW,
            key=CHALLENGE_KEY,
        )


@pytest.mark.parametrize(
    "changed",
    [
        {"nonce": "YmJiYmJiYmJiYmJiYmJiYmJiYmJiYmJiYmJiYmJiYmI"},
        {"expires_at": NOW + timedelta(minutes=14)},
        {"work_factor": 17},
        {"signature": "YmJiYmJiYmJiYmJiYmJiYmJiYmJiYmJiYmJiYmJiYmI"},
    ],
    ids=["nonce", "expiry", "work-factor", "signature"],
)
def test_service_rejects_each_altered_signed_challenge_member(
    changed: dict[str, object],
) -> None:
    challenge = replace(_solved_challenge(), **changed)

    with pytest.raises(ChallengeVerificationError):
        verify_challenge(
            challenge,
            request_id=REQUEST_ID,
            source_hash=SOURCE_HASH,
            now=NOW,
            key=CHALLENGE_KEY,
        )


def test_service_challenge_replay_reaches_idempotency_without_local_replay_state() -> None:
    challenge = _solved_challenge()

    for _ in range(2):
        verify_challenge(
            challenge,
            request_id=REQUEST_ID,
            source_hash=SOURCE_HASH,
            now=NOW + timedelta(minutes=1),
            key=CHALLENGE_KEY,
        )


def _service_config(tmp_path: object) -> ServiceConfig:
    from pathlib import Path

    path = Path(str(tmp_path))
    return ServiceConfig(
        repository="nordscope-fi/Discord-stoat-ferry",
        github_app_id=4_773_301,
        github_installation_id=157_795_120,
        github_private_key=("-----BEGIN PRIVATE KEY-----\nfixture\n-----END PRIVATE KEY-----"),
        database_path=path / "feedback.sqlite3",
        challenge_key=b"C" * 32,
        source_hash_key=b"S" * 32,
        contact_key=b"E" * 32,
        trusted_proxy_networks=(),
    )


class _GitHubMustNotRun:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"health called GitHub member {name}")


async def test_health_returns_small_json_after_sqlite_query(tmp_path: object) -> None:
    from discord_ferry.feedback_service.app import create_app

    config = _service_config(tmp_path)
    client = TestClient(TestServer(create_app(config, github=_GitHubMustNotRun())))
    await client.start_server()
    try:
        response = await client.get("/health")
        assert response.status == 200
        assert response.content_type == "application/json"
        assert await response.json() == {"status": "ok"}
        assert config.database_path.is_file()
    finally:
        await client.close()


async def test_health_returns_503_when_sqlite_query_fails(
    tmp_path: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from discord_ferry.feedback_service.app import create_app

    config = _service_config(tmp_path)
    store = FeedbackStore(config.database_path)

    async def broken_receipt(_request_id: UUID) -> None:
        raise sqlite3.OperationalError("database marker must stay private")

    monkeypatch.setattr(store, "get_receipt", broken_receipt)
    client = TestClient(TestServer(create_app(config, store=store)))
    await client.start_server()
    try:
        response = await client.get("/health")
        assert response.status == 503
        assert await response.json() == {"status": "unhealthy"}
        assert "database marker" not in await response.text()
    finally:
        await client.close()


@pytest.mark.parametrize("method", ["head", "post", "put", "patch", "delete", "options"])
async def test_health_is_get_only(tmp_path: object, method: str) -> None:
    from discord_ferry.feedback_service.app import create_app

    client = TestClient(TestServer(create_app(_service_config(tmp_path))))
    await client.start_server()
    try:
        response = await client.request(method, "/health")
        assert response.status == 405
    finally:
        await client.close()


async def test_health_does_not_log_request_body(
    tmp_path: object,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from discord_ferry.feedback_service.app import create_app

    marker = "health-private-body-marker"
    client = TestClient(TestServer(create_app(_service_config(tmp_path))))
    await client.start_server()
    try:
        response = await client.request("GET", "/health", data=marker)
        assert response.status == 200
    finally:
        await client.close()
    assert marker not in caplog.text


async def test_health_cleanup_closes_owned_session(tmp_path: object) -> None:
    from discord_ferry.feedback_service.app import SESSION_KEY, create_app

    app = create_app(_service_config(tmp_path))
    client = TestClient(TestServer(app))
    await client.start_server()
    session = app[SESSION_KEY]
    assert not session.closed

    await client.close()

    assert session.closed


async def test_health_cleanup_keeps_caller_session_open(tmp_path: object) -> None:
    from discord_ferry.feedback_service.app import create_app

    async with aiohttp.ClientSession() as session:
        client = TestClient(TestServer(create_app(_service_config(tmp_path), session=session)))
        await client.start_server()
        await client.close()
        assert not session.closed


class _ReadyGitHub:
    def __init__(self, *results: Exception | None) -> None:
        self.results = list(results)
        self.calls = 0
        self.invalidations = 0

    def invalidate_readiness(self) -> None:
        self.invalidations += 1

    async def check_readiness(self) -> None:
        self.calls += 1
        if self.results:
            result = self.results.pop(0)
            if result is not None:
                raise result


async def test_ready_returns_small_json_and_caches_success(tmp_path: object) -> None:
    from discord_ferry.feedback_service.app import create_app

    github = _ReadyGitHub()
    client = TestClient(TestServer(create_app(_service_config(tmp_path), github=github)))
    await client.start_server()
    try:
        first = await client.get("/ready")
        second = await client.get("/ready")
        assert first.status == second.status == 200
        assert await first.json() == await second.json() == {"status": "ready"}
        assert github.calls == 1
    finally:
        await client.close()


@pytest.mark.parametrize(
    "reason",
    [
        "missing metadata permission",
        "missing Issues permission",
        "missing Discussions permission",
        "wrong repository",
        "absent bug label",
        "absent triage label",
        "missing Discussion category",
        "GitHub timeout",
    ],
)
async def test_ready_maps_every_dependency_failure_to_private_503(
    tmp_path: object,
    reason: str,
) -> None:
    from discord_ferry.feedback_service.app import create_app

    private_marker = f"{reason}: private-key-marker credential-marker"
    github = _ReadyGitHub(GitHubReadinessError(private_marker))
    client = TestClient(TestServer(create_app(_service_config(tmp_path), github=github)))
    await client.start_server()
    try:
        response = await client.get("/ready")
        body = await response.text()
        assert response.status == 503
        assert await response.json() == {"status": "unready"}
        assert reason not in body
        assert "private-key-marker" not in body
        assert "credential-marker" not in body
    finally:
        await client.close()


async def test_ready_does_not_cache_failure(tmp_path: object) -> None:
    from discord_ferry.feedback_service.app import create_app

    github = _ReadyGitHub(GitHubReadinessError("temporary"), None)
    client = TestClient(TestServer(create_app(_service_config(tmp_path), github=github)))
    await client.start_server()
    try:
        failed = await client.get("/ready")
        recovered = await client.get("/ready")
        assert failed.status == 503
        assert recovered.status == 200
        assert github.calls == 2
    finally:
        await client.close()


async def test_ready_refreshes_expired_success(tmp_path: object) -> None:
    from discord_ferry.feedback_service.app import create_app

    current = [NOW]
    github = _ReadyGitHub()
    client = TestClient(
        TestServer(
            create_app(
                _service_config(tmp_path),
                github=github,
                now=lambda: current[0],
            )
        )
    )
    await client.start_server()
    try:
        assert (await client.get("/ready")).status == 200
        current[0] += timedelta(seconds=31)
        assert (await client.get("/ready")).status == 200
        assert github.calls == 2
    finally:
        await client.close()


async def test_ready_cache_can_be_invalidated_after_adapter_failure(
    tmp_path: object,
) -> None:
    from discord_ferry.feedback_service.app import create_app, invalidate_readiness

    github = _ReadyGitHub()
    app = create_app(_service_config(tmp_path), github=github)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        assert (await client.get("/ready")).status == 200
        invalidate_readiness(app)
        assert (await client.get("/ready")).status == 200
        assert github.calls == 2
    finally:
        await client.close()


def _source_digest(source: str) -> str:
    return hmac.new(b"S" * 32, source.encode("ascii"), hashlib.sha256).hexdigest()


def test_request_source_uses_first_untrusted_proxy_hop_from_the_right(
    tmp_path: object,
) -> None:
    from discord_ferry.feedback_service.app import CONFIG_KEY, _request_source

    config = replace(
        _service_config(tmp_path),
        trusted_proxy_networks=(
            ip_network("127.0.0.0/8"),
            ip_network("10.0.0.0/8"),
        ),
    )
    request = SimpleNamespace(
        transport=SimpleNamespace(get_extra_info=lambda _name: ("127.0.0.1", 1234)),
        headers={"X-Forwarded-For": "203.0.113.99, 198.51.100.21, 10.0.0.8"},
        app={CONFIG_KEY: config},
    )

    assert _request_source(request) == "198.51.100.21"


async def test_challenge_route_returns_signed_json_for_direct_source(
    tmp_path: object,
) -> None:
    from discord_ferry.feedback_service.app import create_app

    client = TestClient(TestServer(create_app(_service_config(tmp_path), now=lambda: NOW)))
    await client.start_server()
    try:
        response = await client.post(
            "/v1/challenge",
            json={"contract_version": 1, "request_id": str(REQUEST_ID)},
        )
        assert response.status == 200
        assert response.content_type == "application/json"
        challenge = Challenge.from_response_mapping(await response.json())
        assert challenge.request_id == REQUEST_ID
        verify_challenge(
            solve_challenge(challenge),
            request_id=REQUEST_ID,
            source_hash=_source_digest("127.0.0.1"),
            now=NOW,
            key=b"C" * 32,
        )
    finally:
        await client.close()


async def test_challenge_route_trusts_forwarded_source_only_from_proxy(
    tmp_path: object,
) -> None:
    from discord_ferry.feedback_service.app import create_app

    config = replace(
        _service_config(tmp_path),
        trusted_proxy_networks=(
            ip_network("127.0.0.0/8"),
            ip_network("10.0.0.0/8"),
        ),
    )
    client = TestClient(TestServer(create_app(config, now=lambda: NOW)))
    await client.start_server()
    try:
        response = await client.post(
            "/v1/challenge",
            headers={"X-Forwarded-For": "203.0.113.99, 198.51.100.21, 10.0.0.8"},
            json={"contract_version": 1, "request_id": str(REQUEST_ID)},
        )
        challenge = Challenge.from_response_mapping(await response.json())
        verify_challenge(
            solve_challenge(challenge),
            request_id=REQUEST_ID,
            source_hash=_source_digest("198.51.100.21"),
            now=NOW,
            key=b"C" * 32,
        )
    finally:
        await client.close()


async def test_challenge_route_ignores_spoofed_forwarding_from_untrusted_peer(
    tmp_path: object,
) -> None:
    from discord_ferry.feedback_service.app import create_app

    client = TestClient(TestServer(create_app(_service_config(tmp_path), now=lambda: NOW)))
    await client.start_server()
    try:
        response = await client.post(
            "/v1/challenge",
            headers={"X-Forwarded-For": "198.51.100.99"},
            json={"contract_version": 1, "request_id": str(REQUEST_ID)},
        )
        challenge = Challenge.from_response_mapping(await response.json())
        verify_challenge(
            solve_challenge(challenge),
            request_id=REQUEST_ID,
            source_hash=_source_digest("127.0.0.1"),
            now=NOW,
            key=b"C" * 32,
        )
    finally:
        await client.close()


@pytest.mark.parametrize(
    "body",
    [
        {"request_id": str(REQUEST_ID)},
        {"contract_version": 2, "request_id": str(REQUEST_ID)},
        {"contract_version": 1, "request_id": "not-a-uuid"},
        {"contract_version": 1, "request_id": str(REQUEST_ID), "extra": "private"},
        [1, str(REQUEST_ID)],
    ],
    ids=["missing-version", "version", "uuid", "extra", "not-object"],
)
async def test_challenge_route_requires_closed_valid_body(
    tmp_path: object,
    body: object,
) -> None:
    from discord_ferry.feedback_service.app import create_app

    client = TestClient(TestServer(create_app(_service_config(tmp_path))))
    await client.start_server()
    try:
        response = await client.post("/v1/challenge", json=body)
        assert response.status == 400
        error = FeedbackError.from_mapping(await response.json())
        assert error.code is FeedbackErrorCode.INVALID_INPUT
    finally:
        await client.close()


async def test_challenge_route_requires_json_content_type(tmp_path: object) -> None:
    from discord_ferry.feedback_service.app import create_app

    client = TestClient(TestServer(create_app(_service_config(tmp_path))))
    await client.start_server()
    try:
        response = await client.post(
            "/v1/challenge",
            data='{"contract_version":1}',
            headers={"Content-Type": "text/plain"},
        )
        assert response.status == 415
        assert (
            FeedbackError.from_mapping(await response.json()).code
            is FeedbackErrorCode.INVALID_INPUT
        )
    finally:
        await client.close()


async def test_challenge_route_enforces_hourly_quota(tmp_path: object) -> None:
    from discord_ferry.feedback_service.app import create_app

    client = TestClient(TestServer(create_app(_service_config(tmp_path), now=lambda: NOW)))
    await client.start_server()
    try:
        for number in range(30):
            response = await client.post(
                "/v1/challenge",
                json={
                    "contract_version": 1,
                    "request_id": str(UUID(int=number + 1)),
                },
            )
            assert response.status == 200
        denied = await client.post(
            "/v1/challenge",
            json={"contract_version": 1, "request_id": str(UUID(int=31))},
        )
        error = FeedbackError.from_mapping(await denied.json())
        assert denied.status == 429
        assert error.code is FeedbackErrorCode.THROTTLED
        assert error.retry_at == NOW + timedelta(hours=1)
    finally:
        await client.close()


@pytest.mark.parametrize("method", ["get", "put", "patch", "delete", "head"])
async def test_challenge_route_rejects_unsupported_methods(
    tmp_path: object,
    method: str,
) -> None:
    from discord_ferry.feedback_service.app import create_app

    client = TestClient(TestServer(create_app(_service_config(tmp_path))))
    await client.start_server()
    try:
        assert (await client.request(method, "/v1/challenge")).status == 405
    finally:
        await client.close()


async def test_challenge_route_rejects_browser_preflight_and_omits_cors(
    tmp_path: object,
) -> None:
    from discord_ferry.feedback_service.app import create_app

    client = TestClient(TestServer(create_app(_service_config(tmp_path))))
    await client.start_server()
    try:
        response = await client.options(
            "/v1/challenge",
            headers={
                "Origin": "https://attacker.example",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert response.status == 405
        assert "Access-Control-Allow-Origin" not in response.headers
    finally:
        await client.close()


async def test_challenge_route_does_not_log_body_or_source(
    tmp_path: object,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from discord_ferry.feedback_service.app import create_app

    source = "198.51.100.188"
    body_marker = "challenge-private-body-marker"
    client = TestClient(TestServer(create_app(_service_config(tmp_path))))
    await client.start_server()
    try:
        await client.post(
            "/v1/challenge",
            headers={"X-Forwarded-For": source},
            json={
                "contract_version": 1,
                "request_id": str(REQUEST_ID),
                "unknown": body_marker,
            },
        )
    finally:
        await client.close()
    assert source not in caplog.text
    assert body_marker not in caplog.text


def _feedback_request(
    *,
    kind: FeedbackKind = FeedbackKind.BUG,
    description: str = "Ferry stopped during channel creation",
    contact_email: str | None = "private-feedback@example.com",
    source: str = "127.0.0.1",
    diagnostics: FeedbackDiagnostics | None = None,
) -> FeedbackRequest:
    challenge = solve_challenge(
        create_challenge(
            REQUEST_ID,
            _source_digest(source),
            NOW,
            b"C" * 32,
        )
    )
    return FeedbackRequest(
        contract_version=1,
        request_id=REQUEST_ID,
        kind=kind,
        description=description,
        expected="The migration should continue",
        reproduction="Start a migration with forum channels",
        diagnostics=diagnostics,
        contact_email=contact_email,
        public_acknowledged=True,
        diagnostics_acknowledged=diagnostics is not None,
        challenge=challenge,
    )


class _FeedbackGitHub(_ReadyGitHub):
    def __init__(
        self,
        *create_results: str | Exception,
        reconcile_result: ReconciledDestination | Exception | None = None,
    ) -> None:
        super().__init__()
        self.create_results = list(create_results)
        self.reconcile_result = reconcile_result
        self.issue_requests: list[FeedbackRequest] = []
        self.discussion_requests: list[FeedbackRequest] = []
        self.reconcile_calls = 0

    def _create_result(self, default: str) -> str:
        result = self.create_results.pop(0) if self.create_results else default
        if isinstance(result, Exception):
            raise result
        return result

    async def create_issue(self, request: FeedbackRequest) -> str:
        self.issue_requests.append(request)
        return self._create_result("https://github.com/nordscope-fi/Discord-stoat-ferry/issues/901")

    async def create_discussion(self, request: FeedbackRequest) -> str:
        self.discussion_requests.append(request)
        return self._create_result(
            "https://github.com/nordscope-fi/Discord-stoat-ferry/discussions/902"
        )

    async def reconcile_pending(self, receipt: object) -> ReconciledDestination:
        self.reconcile_calls += 1
        result = self.reconcile_result
        if isinstance(result, Exception):
            raise result
        if result is None:
            raise ReconciliationRequiredError("no match")
        return result


@pytest.mark.parametrize(
    ("kind", "destination_kind", "url_part"),
    [
        (FeedbackKind.BUG, DestinationKind.ISSUE, "/issues/"),
        (FeedbackKind.IDEA, DestinationKind.DISCUSSION, "/discussions/"),
        (FeedbackKind.GENERAL, DestinationKind.DISCUSSION, "/discussions/"),
    ],
)
async def test_feedback_route_delivers_each_kind_and_stores_private_contact(
    tmp_path: object,
    kind: FeedbackKind,
    destination_kind: DestinationKind,
    url_part: str,
) -> None:
    from discord_ferry.feedback_service.app import STORE_KEY, create_app

    github = _FeedbackGitHub()
    app = create_app(_service_config(tmp_path), github=github, now=lambda: NOW)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post(
            "/v1/feedback",
            json=_feedback_request(kind=kind).to_mapping(),
        )
        assert response.status == 200
        receipt = await response.json()
        assert receipt["receipt"] == str(REQUEST_ID)
        assert receipt["destination_kind"] == destination_kind.value
        assert url_part in receipt["url"]
        record = await app[STORE_KEY].get_receipt(REQUEST_ID)
        assert record is not None
        assert record.state is ReceiptState.DELIVERED
        assert record.destination_url == receipt["url"]
        assert (
            await app[STORE_KEY].get_contact(REQUEST_ID, now=NOW) == "private-feedback@example.com"
        )
    finally:
        await client.close()

    if kind is FeedbackKind.BUG:
        assert len(github.issue_requests) == 1
        assert not github.discussion_requests
    else:
        assert len(github.discussion_requests) == 1
        assert not github.issue_requests


@pytest.mark.parametrize(
    "mutation",
    [
        lambda body: {**body, "attachment": "forbidden"},
        lambda body: {**body, "contract_version": 2},
        lambda body: {**body, "public_acknowledged": False},
        lambda body: {
            **body,
            "challenge": {**body["challenge"], "signature": "A" * 43},
        },
    ],
    ids=["attachment", "version", "acknowledgement", "challenge"],
)
async def test_feedback_route_rejects_before_receipt_or_github(
    tmp_path: object,
    mutation: object,
) -> None:
    from discord_ferry.feedback_service.app import STORE_KEY, create_app

    github = _FeedbackGitHub()
    app = create_app(_service_config(tmp_path), github=github, now=lambda: NOW)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        body = _feedback_request().to_mapping()
        response = await client.post("/v1/feedback", json=mutation(body))
        assert response.status == 400
        error = FeedbackError.from_mapping(await response.json())
        assert error.code in {
            FeedbackErrorCode.INVALID_INPUT,
            FeedbackErrorCode.INVALID_CHALLENGE,
        }
        assert await app[STORE_KEY].get_receipt(REQUEST_ID) is None
    finally:
        await client.close()
    assert not github.issue_requests
    assert not github.discussion_requests


async def test_feedback_route_rejects_wrong_content_type_and_oversize(
    tmp_path: object,
) -> None:
    from discord_ferry.feedback_service.app import create_app

    github = _FeedbackGitHub()
    client = TestClient(TestServer(create_app(_service_config(tmp_path), github=github)))
    await client.start_server()
    try:
        wrong_type = await client.post(
            "/v1/feedback",
            data="{}",
            headers={"Content-Type": "text/plain"},
        )
        oversized = await client.post(
            "/v1/feedback",
            data=b"{" + b"x" * (32 * 1024),
            headers={"Content-Type": "application/json"},
        )
        assert wrong_type.status == 415
        assert oversized.status == 413
    finally:
        await client.close()
    assert not github.issue_requests


async def test_feedback_route_throttles_before_receipt_and_github(tmp_path: object) -> None:
    from discord_ferry.feedback_service.app import STORE_KEY, create_app

    github = _FeedbackGitHub()
    app = create_app(_service_config(tmp_path), github=github, now=lambda: NOW)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        for offset in range(3):
            assert (
                await app[STORE_KEY].claim_report_quota(
                    "127.0.0.1",
                    now=NOW + timedelta(seconds=offset),
                )
            ).allowed
        response = await client.post(
            "/v1/feedback",
            json=_feedback_request().to_mapping(),
        )
        assert response.status == 429
        assert FeedbackError.from_mapping(await response.json()).code is FeedbackErrorCode.THROTTLED
        assert await app[STORE_KEY].get_receipt(REQUEST_ID) is None
    finally:
        await client.close()
    assert not github.issue_requests


async def test_feedback_route_replays_delivered_receipt_without_github(
    tmp_path: object,
) -> None:
    from discord_ferry.feedback_service.app import create_app

    github = _FeedbackGitHub()
    client = TestClient(
        TestServer(create_app(_service_config(tmp_path), github=github, now=lambda: NOW))
    )
    await client.start_server()
    try:
        body = _feedback_request().to_mapping()
        first = await client.post("/v1/feedback", json=body)
        second = await client.post("/v1/feedback", json=body)
        assert first.status == second.status == 200
        assert await first.json() == await second.json()
        assert len(github.issue_requests) == 1
    finally:
        await client.close()


async def test_feedback_route_rejects_changed_content_for_same_id(
    tmp_path: object,
) -> None:
    from discord_ferry.feedback_service.app import create_app

    github = _FeedbackGitHub()
    client = TestClient(
        TestServer(create_app(_service_config(tmp_path), github=github, now=lambda: NOW))
    )
    await client.start_server()
    try:
        assert (
            await client.post("/v1/feedback", json=_feedback_request().to_mapping())
        ).status == 200
        conflict = await client.post(
            "/v1/feedback",
            json=_feedback_request(description="Changed report").to_mapping(),
        )
        assert conflict.status == 409
        assert (
            FeedbackError.from_mapping(await conflict.json()).code
            is FeedbackErrorCode.DUPLICATE_ID_CONFLICT
        )
        assert len(github.issue_requests) == 1
    finally:
        await client.close()


async def test_feedback_route_marks_clear_github_failure_retryable(
    tmp_path: object,
) -> None:
    from discord_ferry.feedback_service.app import STORE_KEY, create_app

    github = _FeedbackGitHub(GitHubDeliveryError("private GitHub marker"))
    app = create_app(_service_config(tmp_path), github=github, now=lambda: NOW)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post(
            "/v1/feedback",
            json=_feedback_request().to_mapping(),
        )
        assert response.status == 502
        assert "private GitHub marker" not in await response.text()
        record = await app[STORE_KEY].get_receipt(REQUEST_ID)
        assert record is not None and record.state is ReceiptState.FAILED
    finally:
        await client.close()


async def test_feedback_route_reconciles_uncertain_write_without_second_create(
    tmp_path: object,
) -> None:
    from discord_ferry.feedback_service.app import STORE_KEY, create_app

    destination = "https://github.com/nordscope-fi/Discord-stoat-ferry/issues/944"
    github = _FeedbackGitHub(
        GitHubDeliveryUncertainError("lost response"),
        reconcile_result=ReconciledDestination(DestinationKind.ISSUE, destination),
    )
    app = create_app(_service_config(tmp_path), github=github, now=lambda: NOW)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        body = _feedback_request().to_mapping()
        uncertain = await client.post("/v1/feedback", json=body)
        repaired = await client.post("/v1/feedback", json=body)
        assert uncertain.status == 503
        assert (
            FeedbackError.from_mapping(await uncertain.json()).code
            is FeedbackErrorCode.RECONCILIATION_REQUIRED
        )
        assert repaired.status == 200
        assert (await repaired.json())["url"] == destination
        assert len(github.issue_requests) == 1
        assert github.reconcile_calls == 1
        record = await app[STORE_KEY].get_receipt(REQUEST_ID)
        assert record is not None and record.state is ReceiptState.DELIVERED
    finally:
        await client.close()


async def test_feedback_route_keeps_unresolved_pending_without_create(
    tmp_path: object,
) -> None:
    from discord_ferry.feedback_service.app import STORE_KEY, create_app

    github = _FeedbackGitHub(
        GitHubDeliveryUncertainError("lost response"),
        reconcile_result=ReconciliationRequiredError("zero matches"),
    )
    app = create_app(_service_config(tmp_path), github=github, now=lambda: NOW)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        body = _feedback_request().to_mapping()
        assert (await client.post("/v1/feedback", json=body)).status == 503
        retry = await client.post("/v1/feedback", json=body)
        assert retry.status == 503
        assert len(github.issue_requests) == 1
        record = await app[STORE_KEY].get_receipt(REQUEST_ID)
        assert record is not None and record.state is ReceiptState.PENDING
    finally:
        await client.close()


async def test_feedback_route_logs_metadata_without_content(
    tmp_path: object,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from discord_ferry.feedback_service.app import ServiceJSONFormatter, create_app

    description = "feedback-route-public-marker"
    contact = "feedback-route-private@example.com"
    github = _FeedbackGitHub()
    client = TestClient(
        TestServer(create_app(_service_config(tmp_path), github=github, now=lambda: NOW))
    )
    await client.start_server()
    try:
        with caplog.at_level("INFO"):
            response = await client.post(
                "/v1/feedback",
                json=_feedback_request(
                    description=description,
                    contact_email=contact,
                ).to_mapping(),
            )
        assert response.status == 200
    finally:
        await client.close()
    assert description not in caplog.text
    assert contact not in caplog.text
    delivery = next(
        record for record in caplog.records if getattr(record, "event", None) == "feedback_delivery"
    )
    rendered = json.loads(ServiceJSONFormatter().format(delivery))
    assert rendered["receipt"] == str(REQUEST_ID)
    assert rendered["state"] == "delivered"
    assert rendered["destination_kind"] == "issue"
    assert rendered["status_class"] == "2xx"
    assert isinstance(rendered["duration_ms"], int)


async def test_feedback_route_cleans_diagnostics_again_before_github(
    tmp_path: object,
) -> None:
    from discord_ferry.feedback_service.app import create_app

    credential = "mfa.abcdefghijklmnopqrstuvwxyz123456"
    diagnostics = FeedbackDiagnostics(
        ferry_version="2.38.0",
        operating_system=OperatingSystem.MACOS,
        architecture=Architecture.ARM64,
        interface=FeedbackInterface.GUI,
        stage=FeedbackStage.CHANNELS,
        last_error=f"Authorization: Bearer {credential}",
        log_excerpt=f"request token={credential}",
    )
    github = _FeedbackGitHub()
    client = TestClient(
        TestServer(create_app(_service_config(tmp_path), github=github, now=lambda: NOW))
    )
    await client.start_server()
    try:
        response = await client.post(
            "/v1/feedback",
            json=_feedback_request(diagnostics=diagnostics).to_mapping(),
        )
        assert response.status == 200
    finally:
        await client.close()

    sent = github.issue_requests[0]
    assert sent.diagnostics is not None
    assert credential not in str(sent.diagnostics.to_mapping())


def test_startup_serve_loads_config_and_runs_only_public_routes(tmp_path: object) -> None:
    from discord_ferry.feedback_service.__main__ import serve

    env = _valid_config_env()
    env["FERRY_FEEDBACK_DATABASE_PATH"] = f"/data/{tmp_path!s}/feedback.sqlite3"
    captured: dict[str, object] = {}

    def runner(app: object, **kwargs: object) -> None:
        captured["app"] = app
        captured.update(kwargs)

    assert (
        serve(
            ["serve", "--host", "127.0.0.1", "--port", "8123"],
            environ=env,
            runner=runner,
        )
        == 0
    )
    app = captured["app"]
    assert isinstance(app, aiohttp.web.Application)
    assert {route.resource.canonical for route in app.router.routes()} == {
        "/health",
        "/ready",
        "/v1/challenge",
        "/v1/feedback",
    }
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8123
    assert captured["handle_signals"] is True


@pytest.mark.parametrize(
    "arguments",
    [
        ["serve", "--host", "not-an-address"],
        ["serve", "--port", "0"],
        ["serve", "--port", "65536"],
        ["serve", "--port", "not-a-port"],
    ],
)
def test_startup_rejects_invalid_host_or_port(arguments: list[str]) -> None:
    from discord_ferry.feedback_service.__main__ import serve

    with pytest.raises(SystemExit):
        serve(arguments, environ=_valid_config_env(), runner=lambda *_args, **_kwargs: None)


def test_startup_refuses_missing_secret_before_runner(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from discord_ferry.feedback_service.__main__ import serve

    env = _valid_config_env()
    private_value = env.pop("FERRY_FEEDBACK_GITHUB_PRIVATE_KEY")
    called = False

    def runner(_app: object, **_kwargs: object) -> None:
        nonlocal called
        called = True

    assert serve(["serve"], environ=env, runner=runner) == 1
    assert called is False
    error = capsys.readouterr().err
    assert "FERRY_FEEDBACK_GITHUB_PRIVATE_KEY" in error
    assert private_value not in error


async def test_startup_initializes_schema_and_expires_old_receipts(
    tmp_path: object,
) -> None:
    from discord_ferry.feedback_service.app import STORE_KEY, create_app

    config = _service_config(tmp_path)
    store = FeedbackStore(
        config.database_path,
        contact_key=config.contact_key,
        source_hash_key=config.source_hash_key,
    )
    await store.initialize(now=NOW - timedelta(days=20))
    await store.claim_receipt(
        REQUEST_ID,
        "a" * 64,
        DestinationKind.ISSUE,
        now=NOW - timedelta(days=20),
    )
    app = create_app(config, store=store)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        assert await app[STORE_KEY].get_receipt(REQUEST_ID) is None
        assert config.database_path.is_file()
    finally:
        await client.close()


def test_log_privacy_formatter_emits_only_bounded_metadata() -> None:
    from discord_ferry.feedback_service.app import FeedbackAccessLogger, ServiceJSONFormatter

    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(ServiceJSONFormatter())
    access_logger = logging.getLogger("feedback-access-test")
    access_logger.handlers = [handler]
    access_logger.propagate = False
    access_logger.setLevel(logging.INFO)
    adapter = FeedbackAccessLogger(access_logger, "%a %r")

    adapter.log(
        SimpleNamespace(
            remote="198.51.100.200",
            path_qs="/v1/feedback?private=query-marker",
        ),
        SimpleNamespace(status=204),
        0.0123,
    )

    event = json.loads(output.getvalue())
    assert event == {
        "event": "http_access",
        "receipt": None,
        "state": None,
        "destination_kind": None,
        "status_class": "2xx",
        "duration_ms": 12,
    }
    assert "198.51.100.200" not in output.getvalue()
    assert "query-marker" not in output.getvalue()
