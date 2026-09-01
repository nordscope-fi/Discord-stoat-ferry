from __future__ import annotations

import base64
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

import aiohttp
import jwt
import pytest
from aioresponses import aioresponses
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from yarl import URL

from discord_ferry.feedback import (
    Architecture,
    Challenge,
    DestinationKind,
    FeedbackDiagnostics,
    FeedbackDraft,
    FeedbackInterface,
    FeedbackKind,
    FeedbackRequest,
    FeedbackStage,
    OperatingSystem,
)
from discord_ferry.feedback_service.config import ServiceConfig
from discord_ferry.feedback_service.github import (
    DiscussionCategories,
    FeedbackGitHub,
    GitHubAuthenticationError,
    GitHubDeliveryError,
    GitHubDeliveryUncertainError,
    GitHubReadinessError,
    ReconciledDestination,
    ReconciliationRequiredError,
    render_github_body,
    render_title,
)
from discord_ferry.feedback_service.store import ReceiptRecord, ReceiptState

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
APP_ID = 4_773_301
INSTALLATION_ID = 157_795_120
INSTALLATION_URL = f"https://api.github.com/app/installations/{INSTALLATION_ID}/access_tokens"
INSTALLATION_DETAILS_URL = (
    "https://api.github.com/repos/nordscope-fi/Discord-stoat-ferry/installation"
)
INSTALLATION_TOKEN = "github-installation-token-marker"
REQUEST_ID = UUID("12345678-1234-4abc-9def-123456789abc")
ISSUES_URL = "https://api.github.com/repos/nordscope-fi/Discord-stoat-ferry/issues"
ISSUE_URL = "https://github.com/nordscope-fi/Discord-stoat-ferry/issues/987"
GRAPHQL_URL = "https://api.github.com/graphql"
REPOSITORY_URL = "https://api.github.com/repos/nordscope-fi/Discord-stoat-ferry"
REPOSITORY_ID = "R_ferry_repository"
IDEAS_ID = "DIC_ideas"
GENERAL_ID = "DIC_general"
DISCUSSION_URL = "https://github.com/nordscope-fi/Discord-stoat-ferry/discussions/654"
ISSUES_LIST_URL = f"{ISSUES_URL}?state=all&sort=created&direction=desc&per_page=100&page=1"


@pytest.fixture(scope="module")
def github_keys() -> tuple[str, str]:
    private_key = rsa.generate_private_key(public_exponent=65_537, key_size=2_048)
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private_key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


@pytest.fixture
def service_config(tmp_path: Path, github_keys: tuple[str, str]) -> ServiceConfig:
    return ServiceConfig(
        repository="nordscope-fi/Discord-stoat-ferry",
        github_app_id=APP_ID,
        github_installation_id=INSTALLATION_ID,
        github_private_key=github_keys[0],
        database_path=tmp_path / "feedback.sqlite3",
        challenge_key=b"C" * 32,
        source_hash_key=b"S" * 32,
        contact_key=b"E" * 32,
        trusted_proxy_networks=(),
    )


@pytest.fixture
def mock_github() -> Iterator[aioresponses]:
    with aioresponses() as mocked:
        yield mocked


def _credential_response(*, expires_at: datetime) -> dict[str, str]:
    return {
        "token": INSTALLATION_TOKEN,
        "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
    }


def _request(
    *,
    kind: FeedbackKind = FeedbackKind.BUG,
    description: str = "Ferry stops during channel import",
    expected: str | None = "The import should continue",
    reproduction: str | None = "Start a migration with forum channels",
    diagnostics: FeedbackDiagnostics | None = None,
    contact_email: str | None = "private-contact@example.com",
) -> FeedbackRequest:
    challenge = Challenge(
        challenge_version=1,
        challenge_id=UUID("aaaaaaaa-1234-4abc-9def-123456789abc"),
        request_id=REQUEST_ID,
        nonce="A" * 43,
        expires_at=NOW + timedelta(minutes=15),
        work_factor=18,
        signature="B" * 43,
        counter=1,
    )
    return FeedbackRequest(
        contract_version=1,
        request_id=REQUEST_ID,
        kind=kind,
        description=description,
        expected=expected,
        reproduction=reproduction,
        diagnostics=diagnostics,
        contact_email=contact_email,
        public_acknowledged=True,
        diagnostics_acknowledged=diagnostics is not None,
        challenge=challenge,
    )


def _diagnostics() -> FeedbackDiagnostics:
    return FeedbackDiagnostics(
        ferry_version="2.38.0",
        operating_system=OperatingSystem.MACOS,
        architecture=Architecture.ARM64,
        interface=FeedbackInterface.GUI,
        stage=FeedbackStage.CHANNELS,
        last_error="channel marker failed",
        log_excerpt=None,
    )


def _mock_installation(mocked: aioresponses) -> None:
    mocked.post(
        INSTALLATION_URL,
        payload=_credential_response(expires_at=NOW + timedelta(hours=1)),
    )


def _category_page(
    categories: list[tuple[str, str]],
    *,
    has_next_page: bool = False,
    end_cursor: str | None = None,
    repository: str = "nordscope-fi/Discord-stoat-ferry",
) -> dict[str, object]:
    return {
        "data": {
            "repository": {
                "id": REPOSITORY_ID,
                "nameWithOwner": repository,
                "discussionCategories": {
                    "nodes": [{"id": identifier, "name": name} for identifier, name in categories],
                    "pageInfo": {
                        "hasNextPage": has_next_page,
                        "endCursor": end_cursor,
                    },
                },
            }
        }
    }


def _mock_categories(mocked: aioresponses) -> None:
    mocked.post(
        GRAPHQL_URL,
        payload=_category_page([(IDEAS_ID, "Ideas"), (GENERAL_ID, "General")]),
    )


def _mock_readiness(
    mocked: aioresponses,
    *,
    permissions: dict[str, str] | None = None,
    repository: str = "nordscope-fi/Discord-stoat-ferry",
    labels: tuple[str, str] = ("bug", "triage"),
) -> None:
    mocked.get(
        INSTALLATION_DETAILS_URL,
        payload={
            "id": INSTALLATION_ID,
            "permissions": permissions
            or {"metadata": "read", "issues": "write", "discussions": "write"},
        },
    )
    _mock_installation(mocked)
    mocked.get(REPOSITORY_URL, payload={"full_name": repository})
    for expected, actual in zip(("bug", "triage"), labels, strict=True):
        mocked.get(f"{REPOSITORY_URL}/labels/{expected}", payload={"name": actual})
    _mock_categories(mocked)


def _receipt(destination_kind: DestinationKind) -> ReceiptRecord:
    return ReceiptRecord(
        request_id=REQUEST_ID,
        content_hash="a" * 64,
        state=ReceiptState.PENDING,
        destination_kind=destination_kind,
        destination_url=None,
        created_at=NOW,
        updated_at=NOW,
        expires_at=NOW + timedelta(days=7),
        audit_at=None,
    )


def _github_item(
    url: str,
    *,
    created_at: datetime,
    marker: str | None = None,
) -> dict[str, str]:
    body = "Unrelated feedback" if marker is None else f"Report\n\n{marker}"
    return {
        "html_url": url,
        "url": url,
        "body": body,
        "created_at": created_at.isoformat().replace("+00:00", "Z"),
        "createdAt": created_at.isoformat().replace("+00:00", "Z"),
    }


def _discussion_page(
    items: list[dict[str, str]],
    *,
    has_next_page: bool = False,
    end_cursor: str | None = None,
) -> dict[str, object]:
    return {
        "data": {
            "repository": {
                "nameWithOwner": "nordscope-fi/Discord-stoat-ferry",
                "discussions": {
                    "nodes": items,
                    "pageInfo": {
                        "hasNextPage": has_next_page,
                        "endCursor": end_cursor,
                    },
                },
            }
        }
    }


def _method_call_count(mocked: aioresponses, method: str) -> int:
    return sum(
        len(calls)
        for (request_method, _url), calls in mocked.requests.items()
        if request_method == method
    )


async def test_readiness_proves_installation_repository_labels_and_categories(
    service_config: ServiceConfig,
    mock_github: aioresponses,
) -> None:
    _mock_readiness(mock_github)

    async with FeedbackGitHub(service_config, now=lambda: NOW) as github:
        await github.check_readiness()

    installation_call = mock_github.requests[("GET", URL(INSTALLATION_DETAILS_URL))][0]
    assert installation_call.kwargs["headers"]["Authorization"].startswith("Bearer ey")
    assert mock_github.requests[("GET", URL(REPOSITORY_URL))]
    assert mock_github.requests[("GET", URL(f"{REPOSITORY_URL}/labels/bug"))]
    assert mock_github.requests[("GET", URL(f"{REPOSITORY_URL}/labels/triage"))]


@pytest.mark.parametrize(
    "permissions",
    [
        {"issues": "write", "discussions": "write"},
        {"metadata": "read", "issues": "read", "discussions": "write"},
        {"metadata": "read", "issues": "write", "discussions": "read"},
    ],
    ids=["metadata", "issues", "discussions"],
)
async def test_readiness_rejects_missing_app_permissions(
    service_config: ServiceConfig,
    mock_github: aioresponses,
    permissions: dict[str, str],
) -> None:
    mock_github.get(
        INSTALLATION_DETAILS_URL,
        payload={"id": INSTALLATION_ID, "permissions": permissions},
    )

    async with FeedbackGitHub(service_config, now=lambda: NOW) as github:
        with pytest.raises(GitHubReadinessError, match="permission"):
            await github.check_readiness()


async def test_readiness_rejects_wrong_repository(
    service_config: ServiceConfig,
    mock_github: aioresponses,
) -> None:
    _mock_readiness(mock_github, repository="nordscope-fi/not-ferry")

    async with FeedbackGitHub(service_config, now=lambda: NOW) as github:
        with pytest.raises(GitHubReadinessError, match="repository"):
            await github.check_readiness()


@pytest.mark.parametrize(
    "labels",
    [("Bug", "triage"), ("bug", "needs-triage")],
    ids=["bug", "triage"],
)
async def test_readiness_requires_exact_labels(
    service_config: ServiceConfig,
    mock_github: aioresponses,
    labels: tuple[str, str],
) -> None:
    _mock_readiness(mock_github, labels=labels)

    async with FeedbackGitHub(service_config, now=lambda: NOW) as github:
        with pytest.raises(GitHubReadinessError, match="label"):
            await github.check_readiness()


async def test_readiness_maps_timeout_without_credential_details(
    service_config: ServiceConfig,
    mock_github: aioresponses,
) -> None:
    mock_github.get(INSTALLATION_DETAILS_URL, exception=TimeoutError("private marker"))

    async with FeedbackGitHub(service_config, now=lambda: NOW) as github:
        with pytest.raises(GitHubReadinessError) as raised:
            await github.check_readiness()

    assert "private marker" not in str(raised.value)
    assert INSTALLATION_TOKEN not in str(raised.value)


async def test_auth_mints_rs256_app_jwt_and_uses_installation_identifiers(
    service_config: ServiceConfig,
    github_keys: tuple[str, str],
    mock_github: aioresponses,
) -> None:
    mock_github.post(
        INSTALLATION_URL,
        payload=_credential_response(expires_at=NOW + timedelta(hours=1)),
    )

    async with FeedbackGitHub(service_config, now=lambda: NOW) as github:
        token = await github.installation_token()

    assert token == INSTALLATION_TOKEN
    request = mock_github.requests[("POST", URL(INSTALLATION_URL))][0]
    headers = request.kwargs["headers"]
    assert headers["Accept"] == "application/vnd.github+json"
    assert headers["X-GitHub-Api-Version"] == "2022-11-28"
    scheme, encoded = headers["Authorization"].split(" ", 1)
    assert scheme == "Bearer"
    claims = jwt.decode(
        encoded,
        github_keys[1],
        algorithms=["RS256"],
        options={"verify_exp": False, "verify_iat": False},
    )
    assert claims["iss"] == str(APP_ID)
    assert NOW - timedelta(seconds=60) <= datetime.fromtimestamp(claims["iat"], tz=UTC) <= NOW
    assert datetime.fromtimestamp(claims["exp"], tz=UTC) <= NOW + timedelta(minutes=10)


async def test_auth_caches_installation_token_until_refresh_window(
    service_config: ServiceConfig,
    mock_github: aioresponses,
) -> None:
    current = NOW
    mock_github.post(
        INSTALLATION_URL,
        payload=_credential_response(expires_at=NOW + timedelta(minutes=5)),
    )

    async with FeedbackGitHub(service_config, now=lambda: current) as github:
        first = await github.installation_token()
        current = NOW + timedelta(minutes=3, seconds=59)
        second = await github.installation_token()

    assert first == second == INSTALLATION_TOKEN
    assert len(mock_github.requests[("POST", URL(INSTALLATION_URL))]) == 1


async def test_auth_refreshes_one_minute_before_expiry(
    service_config: ServiceConfig,
    mock_github: aioresponses,
) -> None:
    current = NOW
    mock_github.post(
        INSTALLATION_URL,
        payload=_credential_response(expires_at=NOW + timedelta(minutes=5)),
    )
    mock_github.post(
        INSTALLATION_URL,
        payload={
            "token": "refreshed-installation-token-marker",
            "expires_at": (NOW + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        },
    )

    async with FeedbackGitHub(service_config, now=lambda: current) as github:
        assert await github.installation_token() == INSTALLATION_TOKEN
        current = NOW + timedelta(minutes=4)
        assert await github.installation_token() == "refreshed-installation-token-marker"

    assert len(mock_github.requests[("POST", URL(INSTALLATION_URL))]) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 500, 503])
async def test_auth_rejects_github_errors_without_exposing_credentials(
    service_config: ServiceConfig,
    mock_github: aioresponses,
    caplog: pytest.LogCaptureFixture,
    status: int,
) -> None:
    mock_github.post(INSTALLATION_URL, status=status, body=INSTALLATION_TOKEN)

    async with FeedbackGitHub(service_config, now=lambda: NOW) as github:
        with pytest.raises(GitHubAuthenticationError) as raised:
            await github.installation_token()

    output = f"{raised.value!r}\n{raised.value}\n{caplog.text}"
    assert str(status) in output
    assert INSTALLATION_TOKEN not in output
    assert service_config.github_private_key not in output


async def test_auth_timeout_is_redacted_and_not_retried(
    service_config: ServiceConfig,
    mock_github: aioresponses,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mock_github.post(INSTALLATION_URL, exception=TimeoutError(INSTALLATION_TOKEN))

    async with FeedbackGitHub(service_config, now=lambda: NOW) as github:
        with pytest.raises(GitHubAuthenticationError) as raised:
            await github.installation_token()

    output = f"{raised.value!r}\n{raised.value}\n{caplog.text}"
    assert "unavailable" in output
    assert INSTALLATION_TOKEN not in output
    assert len(mock_github.requests[("POST", URL(INSTALLATION_URL))]) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"token": "", "expires_at": "2026-08-31T13:00:00Z"},
        {"token": INSTALLATION_TOKEN, "expires_at": "not-a-time"},
        {"token": INSTALLATION_TOKEN, "expires_at": "2026-08-31T11:59:00Z"},
    ],
)
async def test_auth_rejects_invalid_credential_responses(
    service_config: ServiceConfig,
    mock_github: aioresponses,
    payload: object,
) -> None:
    mock_github.post(INSTALLATION_URL, payload=payload)

    async with FeedbackGitHub(service_config, now=lambda: NOW) as github:
        with pytest.raises(GitHubAuthenticationError, match="invalid credential response"):
            await github.installation_token()


async def test_auth_caller_owned_session_stays_open(
    service_config: ServiceConfig,
) -> None:
    async with aiohttp.ClientSession() as session:
        async with FeedbackGitHub(service_config, session=session, now=lambda: NOW):
            pass
        assert not session.closed


async def test_auth_invalid_signing_key_is_classified_without_key_content(
    service_config: ServiceConfig,
) -> None:
    marker = "private-signing-key-marker"
    invalid = replace(
        service_config,
        github_private_key=(f"-----BEGIN PRIVATE KEY-----\n{marker}\n-----END PRIVATE KEY-----"),
    )

    async with FeedbackGitHub(invalid, now=lambda: NOW) as github:
        with pytest.raises(GitHubAuthenticationError, match="signing failed") as raised:
            await github.installation_token()

    assert marker not in str(raised.value)


def test_issue_title_uses_first_nonempty_normalized_line_and_is_bounded() -> None:
    title = render_title("\n  Ferry\t stops   here  \nSecond line")
    bounded = render_title("x" * 500)

    assert title == "Ferry stops here"
    assert len(bounded) == 200
    assert bounded.endswith("…")


def test_issue_body_has_fixed_public_sections_provenance_and_no_contact() -> None:
    request = _request(diagnostics=_diagnostics())

    body = render_github_body(request)

    assert body.startswith("## Report\nFerry stops during channel import")
    assert "## Expected result\nThe import should continue" in body
    assert "## Reproduction steps\nStart a migration with forum channels" in body
    assert "## Diagnostics\nFerry version: 2.38.0" in body
    assert "## Ferry context\nSubmitted through Discord Ferry." in body
    assert "Ferry version: 2.38.0" in body
    assert "Interface: gui" in body
    assert f"<!-- ferry-feedback:{REQUEST_ID} -->" in body
    assert f"Receipt: `{REQUEST_ID}`" in body
    assert "Private contact is available to maintainers under this receipt." in body
    assert request.contact_email not in body


def test_draft_preview_equals_the_cleaned_github_body() -> None:
    private_value = "example-private-authorization-value"
    diagnostics = replace(
        _diagnostics(),
        last_error=f"Authorization: Bearer {private_value}",
    )
    draft = FeedbackDraft(
        FeedbackKind.BUG,
        "Ferry stops during channel import",
        expected="The import should continue",
        reproduction="Start a migration with forum channels",
        diagnostics=diagnostics,
        contact_email="private-contact@example.com",
        request_id=REQUEST_ID,
    )
    draft.acknowledge_diagnostics()
    draft.acknowledge_public()
    request = draft.to_request(_request().challenge).cleaned_for_send()

    assert draft.render_public_body() == render_github_body(request)
    assert private_value not in draft.render_public_body()


def test_issue_body_omits_optional_sections_cleanly() -> None:
    body = render_github_body(
        _request(expected=None, reproduction=None, diagnostics=None, contact_email=None)
    )

    assert "Expected result" not in body
    assert "Reproduction steps" not in body
    assert "Diagnostics" not in body
    assert "Ferry version:" not in body
    assert "Interface:" not in body
    assert "Private contact" not in body
    assert "\n\n\n" not in body


def test_issue_body_escapes_receipt_markers_from_user_content() -> None:
    foreign_id = "ffffffff-1234-4abc-9def-123456789abc"
    foreign_marker = f"<!-- ferry-feedback:{foreign_id} -->"
    body = render_github_body(_request(description=f"Report\n{foreign_marker}"))

    assert foreign_marker not in body
    assert f"&lt;!-- ferry-feedback:{foreign_id} -->" in body
    assert body.count(f"<!-- ferry-feedback:{REQUEST_ID} -->") == 1


async def test_issue_creation_targets_ferry_repo_labels_and_returns_url(
    service_config: ServiceConfig,
    mock_github: aioresponses,
) -> None:
    _mock_installation(mock_github)
    mock_github.post(ISSUES_URL, payload={"html_url": ISSUE_URL}, status=201)
    request = _request(diagnostics=_diagnostics())

    async with FeedbackGitHub(service_config, now=lambda: NOW) as github:
        result = await github.create_issue(request)

    assert result == ISSUE_URL
    issue_call = mock_github.requests[("POST", URL(ISSUES_URL))][0]
    assert issue_call.kwargs["json"] == {
        "title": "Ferry stops during channel import",
        "body": render_github_body(request),
        "labels": ["bug", "triage"],
    }
    headers = issue_call.kwargs["headers"]
    assert headers["Authorization"] == f"Bearer {INSTALLATION_TOKEN}"
    assert len(mock_github.requests[("POST", URL(ISSUES_URL))]) == 1


async def test_issue_creation_rejects_non_bug_without_network_write(
    service_config: ServiceConfig,
) -> None:
    async with FeedbackGitHub(service_config, now=lambda: NOW) as github:
        with pytest.raises(GitHubDeliveryError, match="Bug"):
            await github.create_issue(_request(kind=FeedbackKind.IDEA))


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_issue_creation_classifies_clear_rejections(
    service_config: ServiceConfig,
    mock_github: aioresponses,
    status: int,
) -> None:
    _mock_installation(mock_github)
    mock_github.post(ISSUES_URL, status=status, body="private-contact@example.com")

    async with FeedbackGitHub(service_config, now=lambda: NOW) as github:
        with pytest.raises(GitHubDeliveryError, match=str(status)) as raised:
            await github.create_issue(_request())

    assert "private-contact@example.com" not in str(raised.value)
    assert len(mock_github.requests[("POST", URL(ISSUES_URL))]) == 1


@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_issue_creation_classifies_server_responses_as_uncertain(
    service_config: ServiceConfig,
    mock_github: aioresponses,
    status: int,
) -> None:
    _mock_installation(mock_github)
    mock_github.post(ISSUES_URL, status=status)

    async with FeedbackGitHub(service_config, now=lambda: NOW) as github:
        with pytest.raises(GitHubDeliveryUncertainError, match=str(status)):
            await github.create_issue(_request())

    assert len(mock_github.requests[("POST", URL(ISSUES_URL))]) == 1


async def test_issue_creation_transport_loss_is_uncertain_and_not_retried(
    service_config: ServiceConfig,
    mock_github: aioresponses,
) -> None:
    _mock_installation(mock_github)
    mock_github.post(ISSUES_URL, exception=TimeoutError("private-contact@example.com"))

    async with FeedbackGitHub(service_config, now=lambda: NOW) as github:
        with pytest.raises(GitHubDeliveryUncertainError, match="uncertain") as raised:
            await github.create_issue(_request())

    assert "private-contact@example.com" not in str(raised.value)
    assert len(mock_github.requests[("POST", URL(ISSUES_URL))]) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"html_url": "http://github.com/nordscope-fi/Discord-stoat-ferry/issues/987"},
        {"html_url": "https://example.com/nordscope-fi/Discord-stoat-ferry/issues/987"},
        {"html_url": "https://github.com/nordscope-fi/another-repo/issues/987"},
    ],
)
async def test_issue_creation_treats_invalid_success_response_as_uncertain(
    service_config: ServiceConfig,
    mock_github: aioresponses,
    payload: object,
) -> None:
    _mock_installation(mock_github)
    mock_github.post(ISSUES_URL, payload=payload, status=201)

    async with FeedbackGitHub(service_config, now=lambda: NOW) as github:
        with pytest.raises(GitHubDeliveryUncertainError, match="invalid Issue response"):
            await github.create_issue(_request())


async def test_category_resolution_follows_pages_and_matches_exact_names(
    service_config: ServiceConfig,
    mock_github: aioresponses,
) -> None:
    _mock_installation(mock_github)
    mock_github.post(
        GRAPHQL_URL,
        payload=_category_page(
            [(IDEAS_ID, "Ideas"), ("DIC_lower", "general")],
            has_next_page=True,
            end_cursor="page-2",
        ),
    )
    mock_github.post(
        GRAPHQL_URL,
        payload=_category_page([(GENERAL_ID, "General"), ("DIC_other", "Announcements")]),
    )

    async with FeedbackGitHub(service_config, now=lambda: NOW) as github:
        categories = await github.resolve_discussion_categories()

    assert categories == DiscussionCategories(ideas_id=IDEAS_ID, general_id=GENERAL_ID)
    calls = mock_github.requests[("POST", URL(GRAPHQL_URL))]
    assert len(calls) == 2
    assert calls[0].kwargs["json"]["variables"] == {
        "owner": "nordscope-fi",
        "name": "Discord-stoat-ferry",
        "cursor": None,
    }
    assert calls[1].kwargs["json"]["variables"]["cursor"] == "page-2"


async def test_category_resolution_caches_only_successful_result(
    service_config: ServiceConfig,
    mock_github: aioresponses,
) -> None:
    _mock_installation(mock_github)
    mock_github.post(
        GRAPHQL_URL,
        payload=_category_page([(IDEAS_ID, "Ideas"), (GENERAL_ID, "General")]),
    )

    async with FeedbackGitHub(service_config, now=lambda: NOW) as github:
        first = await github.resolve_discussion_categories()
        second = await github.resolve_discussion_categories()

    assert first is second
    assert len(mock_github.requests[("POST", URL(GRAPHQL_URL))]) == 1


@pytest.mark.parametrize(
    "categories",
    [
        [(GENERAL_ID, "General")],
        [(IDEAS_ID, "Ideas")],
        [(IDEAS_ID, "ideas"), (GENERAL_ID, "General")],
        [(IDEAS_ID, "Ideas"), (GENERAL_ID, "general")],
        [(IDEAS_ID, "Ideas"), ("DIC_duplicate", "Ideas"), (GENERAL_ID, "General")],
        [(IDEAS_ID, "Ideas"), (GENERAL_ID, "General"), ("DIC_duplicate", "General")],
    ],
)
async def test_category_resolution_rejects_missing_renamed_or_duplicate_names(
    service_config: ServiceConfig,
    mock_github: aioresponses,
    categories: list[tuple[str, str]],
) -> None:
    _mock_installation(mock_github)
    mock_github.post(GRAPHQL_URL, payload=_category_page(categories))

    async with FeedbackGitHub(service_config, now=lambda: NOW) as github:
        with pytest.raises(GitHubReadinessError, match="category"):
            await github.resolve_discussion_categories()


async def test_category_resolution_rejects_repository_mismatch(
    service_config: ServiceConfig,
    mock_github: aioresponses,
) -> None:
    _mock_installation(mock_github)
    mock_github.post(
        GRAPHQL_URL,
        payload=_category_page(
            [(IDEAS_ID, "Ideas"), (GENERAL_ID, "General")],
            repository="nordscope-fi/another-repo",
        ),
    )

    async with FeedbackGitHub(service_config, now=lambda: NOW) as github:
        with pytest.raises(GitHubReadinessError, match="repository"):
            await github.resolve_discussion_categories()


@pytest.mark.parametrize(
    "failure",
    [
        {"errors": [{"message": "private-contact@example.com"}]},
        {"data": {"repository": None}},
        {"data": None},
    ],
)
async def test_category_resolution_rejects_graphql_page_failures_without_details(
    service_config: ServiceConfig,
    mock_github: aioresponses,
    failure: object,
) -> None:
    _mock_installation(mock_github)
    mock_github.post(GRAPHQL_URL, payload=failure)

    async with FeedbackGitHub(service_config, now=lambda: NOW) as github:
        with pytest.raises(GitHubReadinessError) as raised:
            await github.resolve_discussion_categories()

    assert "private-contact@example.com" not in str(raised.value)


async def test_category_resolution_invalidates_cached_ids_before_failed_refresh(
    service_config: ServiceConfig,
    mock_github: aioresponses,
) -> None:
    _mock_installation(mock_github)
    valid = _category_page([(IDEAS_ID, "Ideas"), (GENERAL_ID, "General")])
    mock_github.post(GRAPHQL_URL, payload=valid)
    mock_github.post(GRAPHQL_URL, status=503)
    mock_github.post(GRAPHQL_URL, payload=valid)

    async with FeedbackGitHub(service_config, now=lambda: NOW) as github:
        assert await github.resolve_discussion_categories() == DiscussionCategories(
            ideas_id=IDEAS_ID,
            general_id=GENERAL_ID,
        )
        with pytest.raises(GitHubReadinessError):
            await github.resolve_discussion_categories(refresh=True)
        assert await github.resolve_discussion_categories() == DiscussionCategories(
            ideas_id=IDEAS_ID,
            general_id=GENERAL_ID,
        )

    assert len(mock_github.requests[("POST", URL(GRAPHQL_URL))]) == 3


@pytest.mark.parametrize(
    ("kind", "category_id"),
    [(FeedbackKind.IDEA, IDEAS_ID), (FeedbackKind.GENERAL, GENERAL_ID)],
)
async def test_discussion_creation_uses_resolved_category_and_returns_url(
    service_config: ServiceConfig,
    mock_github: aioresponses,
    kind: FeedbackKind,
    category_id: str,
) -> None:
    _mock_installation(mock_github)
    _mock_categories(mock_github)
    mock_github.post(
        GRAPHQL_URL,
        payload={
            "data": {
                "createDiscussion": {
                    "discussion": {"url": DISCUSSION_URL},
                }
            }
        },
    )
    request = _request(kind=kind, diagnostics=_diagnostics())

    async with FeedbackGitHub(service_config, now=lambda: NOW) as github:
        await github.resolve_discussion_categories()
        result = await github.create_discussion(request)

    assert result == DISCUSSION_URL
    mutation = mock_github.requests[("POST", URL(GRAPHQL_URL))][1]
    variables = mutation.kwargs["json"]["variables"]
    assert variables == {
        "repositoryId": REPOSITORY_ID,
        "categoryId": category_id,
        "title": "Ferry stops during channel import",
        "body": render_github_body(request),
    }
    assert mutation.kwargs["headers"]["Authorization"] == f"Bearer {INSTALLATION_TOKEN}"
    assert len(mock_github.requests[("POST", URL(GRAPHQL_URL))]) == 2


async def test_discussion_creation_requires_readiness_categories(
    service_config: ServiceConfig,
) -> None:
    async with FeedbackGitHub(service_config, now=lambda: NOW) as github:
        with pytest.raises(GitHubReadinessError, match="categories"):
            await github.create_discussion(_request(kind=FeedbackKind.IDEA))


async def test_discussion_creation_rejects_bug_without_mutation(
    service_config: ServiceConfig,
    mock_github: aioresponses,
) -> None:
    _mock_installation(mock_github)
    _mock_categories(mock_github)

    async with FeedbackGitHub(service_config, now=lambda: NOW) as github:
        await github.resolve_discussion_categories()
        with pytest.raises(GitHubDeliveryError, match="Idea or General"):
            await github.create_discussion(_request(kind=FeedbackKind.BUG))

    assert len(mock_github.requests[("POST", URL(GRAPHQL_URL))]) == 1


async def test_discussion_creation_graphql_errors_are_clear_and_redacted(
    service_config: ServiceConfig,
    mock_github: aioresponses,
) -> None:
    _mock_installation(mock_github)
    _mock_categories(mock_github)
    mock_github.post(
        GRAPHQL_URL,
        payload={"errors": [{"message": "private-contact@example.com"}]},
    )

    async with FeedbackGitHub(service_config, now=lambda: NOW) as github:
        await github.resolve_discussion_categories()
        with pytest.raises(GitHubDeliveryError, match="GraphQL errors") as raised:
            await github.create_discussion(_request(kind=FeedbackKind.IDEA))

    assert "private-contact@example.com" not in str(raised.value)
    assert len(mock_github.requests[("POST", URL(GRAPHQL_URL))]) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_discussion_creation_http_rejections_are_clear(
    service_config: ServiceConfig,
    mock_github: aioresponses,
    status: int,
) -> None:
    _mock_installation(mock_github)
    _mock_categories(mock_github)
    mock_github.post(GRAPHQL_URL, status=status)

    async with FeedbackGitHub(service_config, now=lambda: NOW) as github:
        await github.resolve_discussion_categories()
        with pytest.raises(GitHubDeliveryError, match=str(status)):
            await github.create_discussion(_request(kind=FeedbackKind.GENERAL))


@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_discussion_creation_server_responses_are_uncertain(
    service_config: ServiceConfig,
    mock_github: aioresponses,
    status: int,
) -> None:
    _mock_installation(mock_github)
    _mock_categories(mock_github)
    mock_github.post(GRAPHQL_URL, status=status)

    async with FeedbackGitHub(service_config, now=lambda: NOW) as github:
        await github.resolve_discussion_categories()
        with pytest.raises(GitHubDeliveryUncertainError, match=str(status)):
            await github.create_discussion(_request(kind=FeedbackKind.GENERAL))


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": {"createDiscussion": {"discussion": None}}},
        {"data": {"createDiscussion": {"discussion": {"url": None}}}},
        {
            "data": {
                "createDiscussion": {
                    "discussion": {
                        "url": "https://github.com/nordscope-fi/another-repo/discussions/654"
                    }
                }
            }
        },
    ],
)
async def test_discussion_creation_missing_or_invalid_url_is_uncertain(
    service_config: ServiceConfig,
    mock_github: aioresponses,
    payload: object,
) -> None:
    _mock_installation(mock_github)
    _mock_categories(mock_github)
    mock_github.post(GRAPHQL_URL, payload=payload)

    async with FeedbackGitHub(service_config, now=lambda: NOW) as github:
        await github.resolve_discussion_categories()
        with pytest.raises(GitHubDeliveryUncertainError, match="invalid response"):
            await github.create_discussion(_request(kind=FeedbackKind.IDEA))


async def test_discussion_creation_timeout_is_uncertain_and_not_retried(
    service_config: ServiceConfig,
    mock_github: aioresponses,
) -> None:
    _mock_installation(mock_github)
    _mock_categories(mock_github)
    mock_github.post(GRAPHQL_URL, exception=TimeoutError("private-contact@example.com"))

    async with FeedbackGitHub(service_config, now=lambda: NOW) as github:
        await github.resolve_discussion_categories()
        with pytest.raises(GitHubDeliveryUncertainError, match="uncertain") as raised:
            await github.create_discussion(_request(kind=FeedbackKind.IDEA))

    assert "private-contact@example.com" not in str(raised.value)
    assert len(mock_github.requests[("POST", URL(GRAPHQL_URL))]) == 2


async def test_reconcile_issue_follows_pages_until_exact_marker(
    service_config: ServiceConfig,
    mock_github: aioresponses,
) -> None:
    _mock_installation(mock_github)
    marker = f"<!-- ferry-feedback:{REQUEST_ID} -->"
    page_two = f"{ISSUES_URL}?state=all&sort=created&direction=desc&per_page=100&page=2"
    mock_github.get(
        ISSUES_LIST_URL,
        payload=[_github_item(ISSUE_URL, created_at=NOW + timedelta(minutes=1))],
        headers={"Link": f'<{page_two}>; rel="next"'},
    )
    mock_github.get(
        page_two,
        payload=[
            _github_item(ISSUE_URL, created_at=NOW, marker=marker),
            _github_item(
                "https://github.com/nordscope-fi/Discord-stoat-ferry/issues/900",
                created_at=NOW - timedelta(minutes=3),
                marker=marker,
            ),
        ],
    )

    async with FeedbackGitHub(service_config, now=lambda: NOW) as github:
        result = await github.reconcile_pending(_receipt(DestinationKind.ISSUE))

    assert result == ReconciledDestination(DestinationKind.ISSUE, ISSUE_URL)
    assert _method_call_count(mock_github, "GET") == 2


async def test_reconcile_issue_ignores_pull_requests_in_issue_listing(
    service_config: ServiceConfig,
    mock_github: aioresponses,
) -> None:
    _mock_installation(mock_github)
    marker = f"<!-- ferry-feedback:{REQUEST_ID} -->"
    pull_request: dict[str, object] = {
        **_github_item(
            "https://github.com/nordscope-fi/Discord-stoat-ferry/pull/999",
            created_at=NOW + timedelta(minutes=1),
        ),
        "pull_request": {"url": "https://api.github.com/repos/example/pulls/999"},
    }
    mock_github.get(
        ISSUES_LIST_URL,
        payload=[
            pull_request,
            _github_item(ISSUE_URL, created_at=NOW, marker=marker),
        ],
    )

    async with FeedbackGitHub(service_config, now=lambda: NOW) as github:
        result = await github.reconcile_pending(_receipt(DestinationKind.ISSUE))

    assert result.url == ISSUE_URL


async def test_reconcile_issue_stops_after_cutoff_without_following_next_page(
    service_config: ServiceConfig,
    mock_github: aioresponses,
) -> None:
    _mock_installation(mock_github)
    unused = f"{ISSUES_URL}?state=all&sort=created&direction=desc&per_page=100&page=2"
    mock_github.get(
        ISSUES_LIST_URL,
        payload=[
            _github_item(ISSUE_URL, created_at=NOW - timedelta(minutes=3)),
        ],
        headers={"Link": f'<{unused}>; rel="next"'},
    )

    async with FeedbackGitHub(service_config, now=lambda: NOW) as github:
        with pytest.raises(ReconciliationRequiredError, match="operator review"):
            await github.reconcile_pending(_receipt(DestinationKind.ISSUE))

    assert _method_call_count(mock_github, "GET") == 1


async def test_reconcile_discussion_scans_both_categories_and_returns_one_match(
    service_config: ServiceConfig,
    mock_github: aioresponses,
) -> None:
    _mock_installation(mock_github)
    _mock_categories(mock_github)
    marker = f"<!-- ferry-feedback:{REQUEST_ID} -->"
    mock_github.post(
        GRAPHQL_URL,
        payload=_discussion_page(
            [_github_item(DISCUSSION_URL, created_at=NOW + timedelta(minutes=1))],
            has_next_page=True,
            end_cursor="ideas-page-2",
        ),
    )
    mock_github.post(
        GRAPHQL_URL,
        payload=_discussion_page([_github_item(DISCUSSION_URL, created_at=NOW)]),
    )
    mock_github.post(
        GRAPHQL_URL,
        payload=_discussion_page([_github_item(DISCUSSION_URL, created_at=NOW, marker=marker)]),
    )

    async with FeedbackGitHub(service_config, now=lambda: NOW) as github:
        await github.resolve_discussion_categories()
        result = await github.reconcile_pending(_receipt(DestinationKind.DISCUSSION))

    assert result == ReconciledDestination(DestinationKind.DISCUSSION, DISCUSSION_URL)
    calls = mock_github.requests[("POST", URL(GRAPHQL_URL))]
    assert calls[1].kwargs["json"]["variables"]["categoryId"] == IDEAS_ID
    assert calls[2].kwargs["json"]["variables"]["cursor"] == "ideas-page-2"
    assert calls[3].kwargs["json"]["variables"]["categoryId"] == GENERAL_ID
    assert all("createDiscussion" not in call.kwargs["json"]["query"] for call in calls[1:])


async def test_reconcile_discussion_rejects_matches_across_categories(
    service_config: ServiceConfig,
    mock_github: aioresponses,
) -> None:
    _mock_installation(mock_github)
    _mock_categories(mock_github)
    marker = f"<!-- ferry-feedback:{REQUEST_ID} -->"
    for number in (654, 655):
        mock_github.post(
            GRAPHQL_URL,
            payload=_discussion_page(
                [
                    _github_item(
                        f"https://github.com/nordscope-fi/Discord-stoat-ferry/discussions/{number}",
                        created_at=NOW,
                        marker=marker,
                    )
                ]
            ),
        )

    async with FeedbackGitHub(service_config, now=lambda: NOW) as github:
        await github.resolve_discussion_categories()
        with pytest.raises(ReconciliationRequiredError, match="operator review"):
            await github.reconcile_pending(_receipt(DestinationKind.DISCUSSION))


async def test_reconcile_discussion_failed_page_requires_operator_review(
    service_config: ServiceConfig,
    mock_github: aioresponses,
) -> None:
    _mock_installation(mock_github)
    _mock_categories(mock_github)
    mock_github.post(GRAPHQL_URL, payload={"errors": [{"message": "private detail"}]})

    async with FeedbackGitHub(service_config, now=lambda: NOW) as github:
        await github.resolve_discussion_categories()
        with pytest.raises(ReconciliationRequiredError, match="operator review") as raised:
            await github.reconcile_pending(_receipt(DestinationKind.DISCUSSION))

    assert "private detail" not in str(raised.value)


@pytest.mark.parametrize("matches", [0, 2])
async def test_reconcile_rejects_zero_or_multiple_issue_matches(
    service_config: ServiceConfig,
    mock_github: aioresponses,
    matches: int,
) -> None:
    _mock_installation(mock_github)
    marker = f"<!-- ferry-feedback:{REQUEST_ID} -->"
    mock_github.get(
        ISSUES_LIST_URL,
        payload=[
            _github_item(
                f"https://github.com/nordscope-fi/Discord-stoat-ferry/issues/{index + 1}",
                created_at=NOW,
                marker=marker if index < matches else None,
            )
            for index in range(max(1, matches))
        ],
    )

    async with FeedbackGitHub(service_config, now=lambda: NOW) as github:
        with pytest.raises(ReconciliationRequiredError, match="operator review"):
            await github.reconcile_pending(_receipt(DestinationKind.ISSUE))


@pytest.mark.parametrize("status", [401, 403, 500, 503])
async def test_reconcile_failed_page_requires_operator_review_and_redacts_content(
    service_config: ServiceConfig,
    mock_github: aioresponses,
    status: int,
) -> None:
    _mock_installation(mock_github)
    mock_github.get(ISSUES_LIST_URL, status=status, body="private-contact@example.com")

    async with FeedbackGitHub(service_config, now=lambda: NOW) as github:
        with pytest.raises(ReconciliationRequiredError, match="operator review") as raised:
            await github.reconcile_pending(_receipt(DestinationKind.ISSUE))

    assert "private-contact@example.com" not in str(raised.value)


async def test_reconcile_late_completion_can_be_found_on_later_retry_without_create(
    service_config: ServiceConfig,
    mock_github: aioresponses,
) -> None:
    _mock_installation(mock_github)
    marker = f"<!-- ferry-feedback:{REQUEST_ID} -->"
    mock_github.get(ISSUES_LIST_URL, payload=[])
    mock_github.get(
        ISSUES_LIST_URL,
        payload=[_github_item(ISSUE_URL, created_at=NOW, marker=marker)],
    )

    async with FeedbackGitHub(service_config, now=lambda: NOW) as github:
        with pytest.raises(ReconciliationRequiredError):
            await github.reconcile_pending(_receipt(DestinationKind.ISSUE))
        result = await github.reconcile_pending(_receipt(DestinationKind.ISSUE))

    assert result.url == ISSUE_URL
    assert ("POST", URL(ISSUES_URL)) not in mock_github.requests
    assert _method_call_count(mock_github, "GET") == 2


async def test_reconcile_requires_pending_receipt_without_network_call(
    service_config: ServiceConfig,
) -> None:
    receipt = _receipt(DestinationKind.ISSUE)
    delivered = ReceiptRecord(
        **{
            **receipt.__dict__,
            "state": ReceiptState.DELIVERED,
            "destination_url": ISSUE_URL,
        }
    )

    async with FeedbackGitHub(service_config, now=lambda: NOW) as github:
        with pytest.raises(ReconciliationRequiredError, match="pending"):
            await github.reconcile_pending(delivered)


async def test_github_payloads_errors_and_logs_exclude_private_markers(
    service_config: ServiceConfig,
    mock_github: aioresponses,
    caplog: pytest.LogCaptureFixture,
) -> None:
    source_marker = "198.51.100.219"
    _mock_installation(mock_github)
    mock_github.post(ISSUES_URL, payload={"html_url": ISSUE_URL}, status=201)
    _mock_categories(mock_github)
    mock_github.post(
        GRAPHQL_URL,
        payload={"data": {"createDiscussion": {"discussion": {"url": DISCUSSION_URL}}}},
    )
    mock_github.post(ISSUES_URL, status=422, body="private-contact@example.com")

    errors: list[str] = []
    async with FeedbackGitHub(service_config, now=lambda: NOW) as github:
        await github.create_issue(_request())
        await github.resolve_discussion_categories()
        await github.create_discussion(_request(kind=FeedbackKind.IDEA))
        with pytest.raises(GitHubDeliveryError) as raised:
            await github.create_issue(_request())
        errors.append(str(raised.value))

    payloads = [
        call.kwargs.get("json")
        for calls in mock_github.requests.values()
        for call in calls
        if call.kwargs.get("json") is not None
    ]
    audited = "\n".join((json.dumps(payloads), *errors, caplog.text))
    markers = (
        "private-contact@example.com",
        service_config.github_private_key,
        INSTALLATION_TOKEN,
        source_marker,
        base64.urlsafe_b64encode(service_config.challenge_key).decode(),
        base64.urlsafe_b64encode(service_config.source_hash_key).decode(),
        base64.urlsafe_b64encode(service_config.contact_key).decode(),
    )
    assert "contact_email" not in audited
    for marker in markers:
        assert marker not in audited
