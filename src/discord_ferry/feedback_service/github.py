"""GitHub App authentication and public feedback delivery."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast
from urllib.parse import urlparse

import aiohttp
import jwt

from discord_ferry.core.http import new_session
from discord_ferry.core.security import register_secret
from discord_ferry.feedback import DestinationKind, FeedbackKind, render_public_feedback_body
from discord_ferry.feedback_service.store import ReceiptState

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from discord_ferry.feedback import FeedbackRequest
    from discord_ferry.feedback_service.config import ServiceConfig
    from discord_ferry.feedback_service.store import ReceiptRecord

_API_ROOT = "https://api.github.com"
_GRAPHQL_URL = f"{_API_ROOT}/graphql"
_API_VERSION = "2022-11-28"
_APP_JWT_LIFETIME = timedelta(minutes=10)
_APP_JWT_CLOCK_SKEW = timedelta(minutes=1)
_INSTALLATION_REFRESH = timedelta(minutes=1)
_TITLE_LIMIT = 200
_SPACE = re.compile(r"\s+")


class GitHubAuthenticationError(RuntimeError):
    """Raised when installation authentication cannot be completed safely."""


class GitHubDeliveryError(RuntimeError):
    """Raised when GitHub clearly refuses or invalidates a delivery request."""


class GitHubDeliveryUncertainError(RuntimeError):
    """Raised when GitHub may have accepted a write whose result was lost."""


class GitHubReadinessError(RuntimeError):
    """Raised when GitHub routing cannot be proven ready."""


@dataclass(frozen=True)
class _InstallationCredential:
    token: str
    expires_at: datetime


@dataclass(frozen=True)
class DiscussionCategories:
    """Strictly resolved Discussion destination identifiers."""

    ideas_id: str
    general_id: str


@dataclass(frozen=True)
class ReconciledDestination:
    """One existing GitHub item matched to a pending receipt."""

    kind: DestinationKind
    url: str


class ReconciliationRequiredError(RuntimeError):
    """Raised when a pending write cannot be resolved without an operator."""


_CATEGORY_QUERY = """
query FerryFeedbackCategories($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    id
    nameWithOwner
    discussionCategories(first: 100, after: $cursor) {
      nodes { id name }
      pageInfo { hasNextPage endCursor }
    }
  }
}
""".strip()

_CREATE_DISCUSSION_MUTATION = """
mutation FerryCreateDiscussion(
  $repositoryId: ID!,
  $categoryId: ID!,
  $title: String!,
  $body: String!
) {
  createDiscussion(input: {
    repositoryId: $repositoryId,
    categoryId: $categoryId,
    title: $title,
    body: $body
  }) {
    discussion { url }
  }
}
""".strip()

_LIST_DISCUSSIONS_QUERY = """
query FerryFeedbackDiscussions(
  $owner: String!,
  $name: String!,
  $categoryId: ID!,
  $cursor: String
) {
  repository(owner: $owner, name: $name) {
    nameWithOwner
    discussions(
      first: 100,
      after: $cursor,
      categoryId: $categoryId,
      orderBy: {field: CREATED_AT, direction: DESC}
    ) {
      nodes { url body createdAt }
      pageInfo { hasNextPage endCursor }
    }
  }
}
""".strip()


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError
    source = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(source)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError
    return parsed.astimezone(UTC)


def render_title(description: str) -> str:
    """Build a bounded GitHub title from the first nonempty report line."""

    title = next(
        (_SPACE.sub(" ", line).strip() for line in description.splitlines() if line.strip()),
        "Feedback submitted through Ferry",
    )
    if len(title) <= _TITLE_LIMIT:
        return title
    return f"{title[: _TITLE_LIMIT - 1].rstrip()}…"


def render_github_body(request: FeedbackRequest) -> str:
    """Render fixed public sections without including private contact data."""

    return render_public_feedback_body(
        request_id=request.request_id,
        description=request.description,
        expected=request.expected,
        reproduction=request.reproduction,
        diagnostics=request.diagnostics,
        contact_available=request.contact_email is not None,
    )


class FeedbackGitHub:
    """Authenticate as the dedicated GitHub App and deliver public feedback."""

    def __init__(
        self,
        config: ServiceConfig,
        *,
        session: aiohttp.ClientSession | None = None,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._config = config
        self._session = session
        self._owns_session = session is None
        self._now = now
        self._credential: _InstallationCredential | None = None
        self._credential_lock = asyncio.Lock()
        self._categories: DiscussionCategories | None = None
        self._repository_id: str | None = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("FeedbackGitHub must be entered before use")
        return self._session

    async def __aenter__(self) -> FeedbackGitHub:
        if self._session is None:
            self._session = new_session(
                timeout=aiohttp.ClientTimeout(total=self._config.github_timeout_seconds)
            )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()

    @staticmethod
    def _authorization(credential: str) -> Mapping[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {credential}",
            "X-GitHub-Api-Version": _API_VERSION,
        }

    def _app_jwt(self, now: datetime) -> str:
        issued_at = now - _APP_JWT_CLOCK_SKEW
        try:
            encoded = jwt.encode(
                {
                    "iat": int(issued_at.timestamp()),
                    "exp": int((issued_at + _APP_JWT_LIFETIME).timestamp()),
                    "iss": str(self._config.github_app_id),
                },
                self._config.github_private_key,
                algorithm="RS256",
            )
        except (jwt.PyJWTError, TypeError, ValueError):
            raise GitHubAuthenticationError("GitHub App signing failed") from None
        register_secret("github_app_jwt", encoded, fully_mask=True)
        return encoded

    @staticmethod
    async def _json_object(response: aiohttp.ClientResponse) -> dict[str, object]:
        try:
            value = await response.json(content_type=None)
        except (aiohttp.ContentTypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise GitHubAuthenticationError(
                "GitHub returned an invalid credential response"
            ) from exc
        if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
            raise GitHubAuthenticationError("GitHub returned an invalid credential response")
        return cast("dict[str, object]", value)

    async def installation_token(self) -> str:
        """Return an in-memory installation token, refreshing before expiry."""

        async with self._credential_lock:
            now = self._now().astimezone(UTC)
            cached = self._credential
            if cached is not None and now < cached.expires_at - _INSTALLATION_REFRESH:
                return cached.token
            credential = await self._mint_installation_credential(now)
            self._credential = credential
            return credential.token

    def invalidate_readiness(self) -> None:
        """Discard cached authentication and Discussion routing after a failure."""

        self._credential = None
        self._categories = None
        self._repository_id = None

    async def check_readiness(self) -> None:
        """Prove the configured installation, permissions, labels, and categories."""

        self.invalidate_readiness()
        try:
            await self._check_installation_permissions()
            token = await self.installation_token()
            await self._check_repository(token)
            for label in self._config.issue_labels:
                await self._check_label(token, label)
            await self.resolve_discussion_categories(refresh=True)
        except GitHubReadinessError:
            self.invalidate_readiness()
            raise
        except GitHubAuthenticationError:
            self.invalidate_readiness()
            raise GitHubReadinessError("GitHub readiness authentication failed") from None

    async def _readiness_json(
        self,
        url: str,
        *,
        authorization: str,
        subject: str,
    ) -> dict[str, object]:
        try:
            async with self.session.get(
                url,
                headers=self._authorization(authorization),
                allow_redirects=False,
            ) as response:
                if not 200 <= response.status < 300:
                    raise GitHubReadinessError(f"GitHub {subject} check failed")
                try:
                    return await self._json_object(response)
                except GitHubAuthenticationError:
                    raise GitHubReadinessError(
                        f"GitHub {subject} check returned an invalid response"
                    ) from None
        except GitHubReadinessError:
            raise
        except (aiohttp.ClientError, TimeoutError):
            raise GitHubReadinessError(f"GitHub {subject} check is unavailable") from None

    async def _check_installation_permissions(self) -> None:
        url = f"{_API_ROOT}/repos/{self._config.repository}/installation"
        data = await self._readiness_json(
            url,
            authorization=self._app_jwt(self._now().astimezone(UTC)),
            subject="installation permission",
        )
        permissions = data.get("permissions")
        if data.get("id") != self._config.github_installation_id or not isinstance(
            permissions, dict
        ):
            raise GitHubReadinessError("GitHub installation permission check is invalid")
        required = {"metadata": "read", "issues": "write", "discussions": "write"}
        if any(permissions.get(name) != level for name, level in required.items()):
            raise GitHubReadinessError("GitHub installation permission check failed")

    async def _check_repository(self, token: str) -> None:
        data = await self._readiness_json(
            f"{_API_ROOT}/repos/{self._config.repository}",
            authorization=token,
            subject="repository",
        )
        if data.get("full_name") != self._config.repository:
            raise GitHubReadinessError("GitHub returned the wrong repository")

    async def _check_label(self, token: str, label: str) -> None:
        data = await self._readiness_json(
            f"{_API_ROOT}/repos/{self._config.repository}/labels/{label}",
            authorization=token,
            subject="label",
        )
        if data.get("name") != label:
            raise GitHubReadinessError("GitHub label routing is invalid")

    async def _mint_installation_credential(
        self,
        now: datetime,
    ) -> _InstallationCredential:
        url = f"{_API_ROOT}/app/installations/{self._config.github_installation_id}/access_tokens"
        try:
            async with self.session.post(
                url,
                headers=self._authorization(self._app_jwt(now)),
            ) as response:
                if not 200 <= response.status < 300:
                    raise GitHubAuthenticationError(
                        f"GitHub installation authentication returned HTTP {response.status}"
                    )
                data = await self._json_object(response)
        except GitHubAuthenticationError:
            raise
        except (aiohttp.ClientError, TimeoutError):
            raise GitHubAuthenticationError(
                "GitHub installation authentication is unavailable"
            ) from None

        token = data.get("token")
        try:
            expires_at = _timestamp(data.get("expires_at"))
        except (TypeError, ValueError):
            raise GitHubAuthenticationError(
                "GitHub returned an invalid credential response"
            ) from None
        if not isinstance(token, str) or not token or expires_at <= now:
            raise GitHubAuthenticationError("GitHub returned an invalid credential response")
        register_secret("github_installation_token", token, fully_mask=True)
        return _InstallationCredential(token=token, expires_at=expires_at)

    async def create_issue(self, request: FeedbackRequest) -> str:
        """Create one labelled Bug Issue and return its verified public URL."""

        if request.kind is not FeedbackKind.BUG:
            raise GitHubDeliveryError("only Bug feedback can create a GitHub Issue")
        token = await self.installation_token()
        url = f"{_API_ROOT}/repos/{self._config.repository}/issues"
        try:
            async with self.session.post(
                url,
                headers=self._authorization(token),
                json={
                    "title": render_title(request.description),
                    "body": render_github_body(request),
                    "labels": list(self._config.issue_labels),
                },
                allow_redirects=False,
            ) as response:
                if 400 <= response.status < 500:
                    raise GitHubDeliveryError(
                        f"GitHub rejected Issue creation with HTTP {response.status}"
                    )
                if not 200 <= response.status < 300:
                    raise GitHubDeliveryUncertainError(
                        f"GitHub Issue delivery is uncertain after HTTP {response.status}"
                    )
                try:
                    data = await self._json_object(response)
                except GitHubAuthenticationError:
                    raise GitHubDeliveryUncertainError(
                        "GitHub Issue delivery is uncertain after an invalid response"
                    ) from None
        except (GitHubDeliveryError, GitHubDeliveryUncertainError):
            raise
        except (aiohttp.ClientError, TimeoutError):
            raise GitHubDeliveryUncertainError("GitHub Issue delivery is uncertain") from None

        destination = data.get("html_url")
        if not self._valid_destination_url(destination, "issues"):
            raise GitHubDeliveryUncertainError(
                "GitHub Issue delivery is uncertain after an invalid Issue response"
            )
        return cast("str", destination)

    def _valid_destination_url(self, value: object, destination: str) -> bool:
        if not isinstance(value, str):
            return False
        parsed = urlparse(value)
        prefix = f"/{self._config.repository}/{destination}/"
        return (
            parsed.scheme == "https"
            and parsed.hostname == "github.com"
            and parsed.path.startswith(prefix)
            and parsed.params == ""
            and parsed.query == ""
            and parsed.fragment == ""
        )

    async def resolve_discussion_categories(
        self,
        *,
        refresh: bool = False,
    ) -> DiscussionCategories:
        """Resolve exact Ideas and General category identifiers across all pages."""

        if self._categories is not None and not refresh:
            return self._categories
        self._categories = None
        self._repository_id = None
        try:
            categories, repository_id = await self._fetch_discussion_categories()
        except GitHubReadinessError:
            raise
        except GitHubAuthenticationError:
            raise GitHubReadinessError("GitHub category authentication failed") from None
        self._categories = categories
        self._repository_id = repository_id
        return categories

    async def _fetch_discussion_categories(self) -> tuple[DiscussionCategories, str]:
        owner, name = self._config.repository.split("/", 1)
        token = await self.installation_token()
        cursor: str | None = None
        seen_cursors: set[str] = set()
        repository_id: str | None = None
        matches: dict[str, list[str]] = {
            self._config.idea_category: [],
            self._config.general_category: [],
        }
        while True:
            page = await self._category_page(token, owner=owner, name=name, cursor=cursor)
            page_repository_id, nodes, has_next, next_cursor = self._parse_category_page(page)
            if repository_id is None:
                repository_id = page_repository_id
            elif repository_id != page_repository_id:
                raise GitHubReadinessError("GitHub repository changed during category lookup")
            for node in nodes:
                category_name = cast("str", node["name"])
                if category_name in matches:
                    matches[category_name].append(cast("str", node["id"]))
            if not has_next:
                break
            assert next_cursor is not None
            if next_cursor in seen_cursors:
                raise GitHubReadinessError("GitHub category pagination repeated a cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        if any(len(identifiers) != 1 for identifiers in matches.values()):
            raise GitHubReadinessError("GitHub Discussion category routing is invalid")
        assert repository_id is not None
        return (
            DiscussionCategories(
                ideas_id=matches[self._config.idea_category][0],
                general_id=matches[self._config.general_category][0],
            ),
            repository_id,
        )

    async def _category_page(
        self,
        token: str,
        *,
        owner: str,
        name: str,
        cursor: str | None,
    ) -> dict[str, object]:
        try:
            async with self.session.post(
                _GRAPHQL_URL,
                headers=self._authorization(token),
                json={
                    "query": _CATEGORY_QUERY,
                    "variables": {"owner": owner, "name": name, "cursor": cursor},
                },
                allow_redirects=False,
            ) as response:
                if not 200 <= response.status < 300:
                    raise GitHubReadinessError(
                        f"GitHub category lookup returned HTTP {response.status}"
                    )
                try:
                    page = await self._json_object(response)
                except GitHubAuthenticationError:
                    raise GitHubReadinessError(
                        "GitHub category lookup returned an invalid response"
                    ) from None
        except GitHubReadinessError:
            raise
        except (aiohttp.ClientError, TimeoutError):
            raise GitHubReadinessError("GitHub category lookup is unavailable") from None
        if page.get("errors"):
            raise GitHubReadinessError("GitHub category lookup returned GraphQL errors")
        return page

    def _parse_category_page(
        self,
        page: dict[str, object],
    ) -> tuple[str, list[dict[str, object]], bool, str | None]:
        try:
            data = page["data"]
            if not isinstance(data, dict):
                raise TypeError
            repository = data["repository"]
            if not isinstance(repository, dict):
                raise TypeError
            if repository["nameWithOwner"] != self._config.repository:
                raise GitHubReadinessError("GitHub returned the wrong repository")
            repository_id = repository["id"]
            connection = repository["discussionCategories"]
            if not isinstance(repository_id, str) or not repository_id:
                raise TypeError
            if not isinstance(connection, dict):
                raise TypeError
            raw_nodes = connection["nodes"]
            page_info = connection["pageInfo"]
            if not isinstance(raw_nodes, list) or not isinstance(page_info, dict):
                raise TypeError
            nodes: list[dict[str, object]] = []
            for raw_node in raw_nodes:
                if not isinstance(raw_node, dict):
                    raise TypeError
                identifier = raw_node.get("id")
                name = raw_node.get("name")
                if not isinstance(identifier, str) or not identifier or not isinstance(name, str):
                    raise TypeError
                nodes.append(cast("dict[str, object]", raw_node))
            has_next = page_info["hasNextPage"]
            end_cursor = page_info.get("endCursor")
            if not isinstance(has_next, bool):
                raise TypeError
            if has_next and (not isinstance(end_cursor, str) or not end_cursor):
                raise TypeError
            if not has_next and end_cursor is not None and not isinstance(end_cursor, str):
                raise TypeError
        except GitHubReadinessError:
            raise
        except (KeyError, TypeError):
            raise GitHubReadinessError(
                "GitHub category lookup returned an invalid response"
            ) from None
        return repository_id, nodes, has_next, cast("str | None", end_cursor)

    async def create_discussion(self, request: FeedbackRequest) -> str:
        """Create one Idea or General Discussion using readiness-proven routing."""

        if request.kind is FeedbackKind.BUG:
            raise GitHubDeliveryError(
                "only Idea or General feedback can create a GitHub Discussion"
            )
        categories = self._categories
        repository_id = self._repository_id
        if categories is None or repository_id is None:
            raise GitHubReadinessError("GitHub Discussion categories are not ready")
        category_id = (
            categories.ideas_id if request.kind is FeedbackKind.IDEA else categories.general_id
        )
        token = await self.installation_token()
        try:
            async with self.session.post(
                _GRAPHQL_URL,
                headers=self._authorization(token),
                json={
                    "query": _CREATE_DISCUSSION_MUTATION,
                    "variables": {
                        "repositoryId": repository_id,
                        "categoryId": category_id,
                        "title": render_title(request.description),
                        "body": render_github_body(request),
                    },
                },
                allow_redirects=False,
            ) as response:
                if 400 <= response.status < 500:
                    raise GitHubDeliveryError(
                        f"GitHub rejected Discussion creation with HTTP {response.status}"
                    )
                if not 200 <= response.status < 300:
                    raise GitHubDeliveryUncertainError(
                        f"GitHub Discussion delivery is uncertain after HTTP {response.status}"
                    )
                try:
                    data = await self._json_object(response)
                except GitHubAuthenticationError:
                    raise GitHubDeliveryUncertainError(
                        "GitHub Discussion delivery is uncertain after an invalid response"
                    ) from None
        except (GitHubDeliveryError, GitHubDeliveryUncertainError):
            raise
        except (aiohttp.ClientError, TimeoutError):
            raise GitHubDeliveryUncertainError("GitHub Discussion delivery is uncertain") from None

        if data.get("errors"):
            raise GitHubDeliveryError("GitHub Discussion creation returned GraphQL errors")
        destination = self._discussion_url(data)
        if not self._valid_destination_url(destination, "discussions"):
            raise GitHubDeliveryUncertainError(
                "GitHub Discussion delivery is uncertain after an invalid response"
            )
        return cast("str", destination)

    @staticmethod
    def _discussion_url(data: dict[str, object]) -> object:
        root = data.get("data")
        if not isinstance(root, dict):
            return None
        result = root.get("createDiscussion")
        if not isinstance(result, dict):
            return None
        discussion = result.get("discussion")
        if not isinstance(discussion, dict):
            return None
        return discussion.get("url")

    async def reconcile_pending(self, receipt: ReceiptRecord) -> ReconciledDestination:
        """Find one existing item for a pending receipt without creating another."""

        if receipt.state is not ReceiptState.PENDING:
            raise ReconciliationRequiredError("only a pending receipt can be reconciled")
        cutoff = receipt.created_at - timedelta(minutes=2)
        marker = f"<!-- ferry-feedback:{receipt.request_id} -->"
        try:
            if receipt.destination_kind is DestinationKind.ISSUE:
                matches = await self._scan_issues(marker=marker, cutoff=cutoff)
            else:
                matches = await self._scan_discussions(marker=marker, cutoff=cutoff)
        except ReconciliationRequiredError:
            raise
        except GitHubAuthenticationError:
            raise ReconciliationRequiredError(
                "GitHub reconciliation requires operator review"
            ) from None
        if len(matches) != 1:
            raise ReconciliationRequiredError("GitHub reconciliation requires operator review")
        return ReconciledDestination(receipt.destination_kind, matches[0])

    async def _scan_issues(self, *, marker: str, cutoff: datetime) -> list[str]:
        token = await self.installation_token()
        url: str | None = f"{_API_ROOT}/repos/{self._config.repository}/issues"
        params: Mapping[str, str] | None = {
            "state": "all",
            "sort": "created",
            "direction": "desc",
            "per_page": "100",
            "page": "1",
        }
        matches: list[str] = []
        seen_pages: set[str] = set()
        while url is not None:
            if url in seen_pages:
                raise ReconciliationRequiredError("GitHub reconciliation requires operator review")
            seen_pages.add(url)
            items, next_url = await self._issue_page(token, url=url, params=params)
            params = None
            reached_cutoff = self._collect_matches(
                items,
                marker=marker,
                cutoff=cutoff,
                date_member="created_at",
                url_member="html_url",
                destination="issues",
                matches=matches,
            )
            if reached_cutoff:
                break
            url = next_url
        return matches

    async def _issue_page(
        self,
        token: str,
        *,
        url: str,
        params: Mapping[str, str] | None,
    ) -> tuple[list[dict[str, object]], str | None]:
        try:
            async with self.session.get(
                url,
                params=params,
                headers=self._authorization(token),
                allow_redirects=False,
            ) as response:
                if not 200 <= response.status < 300:
                    raise ReconciliationRequiredError(
                        "GitHub reconciliation requires operator review"
                    )
                try:
                    raw_items = await response.json(content_type=None)
                except (aiohttp.ContentTypeError, json.JSONDecodeError, UnicodeDecodeError):
                    raise ReconciliationRequiredError(
                        "GitHub reconciliation requires operator review"
                    ) from None
                if not isinstance(raw_items, list):
                    raise ReconciliationRequiredError(
                        "GitHub reconciliation requires operator review"
                    )
                items: list[dict[str, object]] = []
                for item in raw_items:
                    if not isinstance(item, dict):
                        raise ReconciliationRequiredError(
                            "GitHub reconciliation requires operator review"
                        )
                    items.append(cast("dict[str, object]", item))
                next_link = response.links.get("next")
                next_url = None if next_link is None else str(next_link["url"])
        except ReconciliationRequiredError:
            raise
        except (aiohttp.ClientError, TimeoutError, KeyError):
            raise ReconciliationRequiredError(
                "GitHub reconciliation requires operator review"
            ) from None
        if next_url is not None and not self._valid_issue_page_url(next_url):
            raise ReconciliationRequiredError("GitHub reconciliation requires operator review")
        return items, next_url

    def _valid_issue_page_url(self, value: str) -> bool:
        parsed = urlparse(value)
        return (
            parsed.scheme == "https"
            and parsed.hostname == "api.github.com"
            and parsed.path == f"/repos/{self._config.repository}/issues"
            and parsed.params == ""
            and parsed.fragment == ""
            and parsed.username is None
            and parsed.password is None
        )

    async def _scan_discussions(self, *, marker: str, cutoff: datetime) -> list[str]:
        categories = self._categories
        if categories is None or self._repository_id is None:
            raise ReconciliationRequiredError("GitHub reconciliation requires operator review")
        token = await self.installation_token()
        matches: list[str] = []
        for category_id in (categories.ideas_id, categories.general_id):
            cursor: str | None = None
            seen_cursors: set[str] = set()
            while True:
                items, has_next, next_cursor = await self._discussion_scan_page(
                    token,
                    category_id=category_id,
                    cursor=cursor,
                )
                reached_cutoff = self._collect_matches(
                    items,
                    marker=marker,
                    cutoff=cutoff,
                    date_member="createdAt",
                    url_member="url",
                    destination="discussions",
                    matches=matches,
                )
                if reached_cutoff or not has_next:
                    break
                assert next_cursor is not None
                if next_cursor in seen_cursors:
                    raise ReconciliationRequiredError(
                        "GitHub reconciliation requires operator review"
                    )
                seen_cursors.add(next_cursor)
                cursor = next_cursor
        return matches

    async def _discussion_scan_page(
        self,
        token: str,
        *,
        category_id: str,
        cursor: str | None,
    ) -> tuple[list[dict[str, object]], bool, str | None]:
        owner, name = self._config.repository.split("/", 1)
        try:
            async with self.session.post(
                _GRAPHQL_URL,
                headers=self._authorization(token),
                json={
                    "query": _LIST_DISCUSSIONS_QUERY,
                    "variables": {
                        "owner": owner,
                        "name": name,
                        "categoryId": category_id,
                        "cursor": cursor,
                    },
                },
                allow_redirects=False,
            ) as response:
                if not 200 <= response.status < 300:
                    raise ReconciliationRequiredError(
                        "GitHub reconciliation requires operator review"
                    )
                try:
                    page = await self._json_object(response)
                except GitHubAuthenticationError:
                    raise ReconciliationRequiredError(
                        "GitHub reconciliation requires operator review"
                    ) from None
        except ReconciliationRequiredError:
            raise
        except (aiohttp.ClientError, TimeoutError):
            raise ReconciliationRequiredError(
                "GitHub reconciliation requires operator review"
            ) from None
        if page.get("errors"):
            raise ReconciliationRequiredError("GitHub reconciliation requires operator review")
        return self._parse_discussion_scan_page(page)

    def _parse_discussion_scan_page(
        self,
        page: dict[str, object],
    ) -> tuple[list[dict[str, object]], bool, str | None]:
        try:
            data = page["data"]
            if not isinstance(data, dict):
                raise TypeError
            repository = data["repository"]
            if not isinstance(repository, dict):
                raise TypeError
            if repository["nameWithOwner"] != self._config.repository:
                raise TypeError
            connection = repository["discussions"]
            if not isinstance(connection, dict):
                raise TypeError
            raw_nodes = connection["nodes"]
            page_info = connection["pageInfo"]
            if not isinstance(raw_nodes, list) or not isinstance(page_info, dict):
                raise TypeError
            nodes: list[dict[str, object]] = []
            for node in raw_nodes:
                if not isinstance(node, dict):
                    raise TypeError
                nodes.append(cast("dict[str, object]", node))
            has_next = page_info["hasNextPage"]
            next_cursor = page_info.get("endCursor")
            if not isinstance(has_next, bool):
                raise TypeError
            if has_next and (not isinstance(next_cursor, str) or not next_cursor):
                raise TypeError
        except (KeyError, TypeError):
            raise ReconciliationRequiredError(
                "GitHub reconciliation requires operator review"
            ) from None
        return nodes, has_next, cast("str | None", next_cursor)

    def _collect_matches(
        self,
        items: list[dict[str, object]],
        *,
        marker: str,
        cutoff: datetime,
        date_member: str,
        url_member: str,
        destination: str,
        matches: list[str],
    ) -> bool:
        for item in items:
            try:
                created_at = _timestamp(item.get(date_member))
            except (TypeError, ValueError):
                raise ReconciliationRequiredError(
                    "GitHub reconciliation requires operator review"
                ) from None
            if created_at < cutoff:
                return True
            if destination == "issues" and "pull_request" in item:
                continue
            body = item.get("body")
            url = item.get(url_member)
            if not isinstance(body, str) or not self._valid_destination_url(url, destination):
                raise ReconciliationRequiredError("GitHub reconciliation requires operator review")
            if marker in body:
                matches.append(cast("str", url))
        return False


__all__ = [
    "FeedbackGitHub",
    "DiscussionCategories",
    "GitHubAuthenticationError",
    "GitHubDeliveryError",
    "GitHubDeliveryUncertainError",
    "GitHubReadinessError",
    "ReconciledDestination",
    "ReconciliationRequiredError",
    "render_github_body",
    "render_title",
]
