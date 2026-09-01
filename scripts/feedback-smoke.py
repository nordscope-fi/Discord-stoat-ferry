#!/usr/bin/env python3
"""Create three bounded feedback items after explicit production confirmation."""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlparse
from uuid import uuid4

from discord_ferry.feedback import (
    DestinationKind,
    FeedbackClient,
    FeedbackDraft,
    FeedbackKind,
    FeedbackReceipt,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

_ROUTES = (
    (FeedbackKind.BUG, DestinationKind.ISSUE),
    (FeedbackKind.IDEA, DestinationKind.DISCUSSION),
    (FeedbackKind.GENERAL, DestinationKind.DISCUSSION),
)


class _ClientContext(Protocol):
    async def __aenter__(self) -> _ClientContext: ...

    async def __aexit__(self, *args: object) -> None: ...

    async def submit(self, draft: FeedbackDraft) -> FeedbackReceipt: ...


@dataclass(frozen=True)
class SmokeResult:
    """One validated destination safe to print for later cleanup."""

    marker: str
    kind: str
    destination_kind: DestinationKind
    url: str


class SmokeRunError(RuntimeError):
    """A failed smoke run with any destinations created before the failure."""

    def __init__(self, completed: Sequence[SmokeResult]) -> None:
        super().__init__("production smoke failed")
        self.completed = tuple(completed)


def _https_base_url(value: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise argparse.ArgumentTypeError("base URL must be an HTTPS origin")
    return value.rstrip("/")


def _marker() -> str:
    timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"ferry-feedback-smoke-{timestamp}-{uuid4().hex[:8]}"


def _validated_result(
    marker: str,
    kind: FeedbackKind,
    expected: DestinationKind,
    receipt: FeedbackReceipt,
) -> SmokeResult:
    parsed = urlparse(receipt.url)
    route = "issues" if expected is DestinationKind.ISSUE else "discussions"
    prefix = f"/nordscope-fi/Discord-stoat-ferry/{route}/"
    number = parsed.path.removeprefix(prefix)
    if receipt.destination_kind is not expected:
        raise ValueError("feedback destination kind does not match the smoke route")
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith(prefix)
        or not number.isdigit()
    ):
        raise ValueError("feedback destination is not a matching GitHub URL")
    return SmokeResult(marker, kind.value, receipt.destination_kind, receipt.url)


async def run_smoke(
    base_url: str,
    *,
    client_factory: Callable[[str], _ClientContext] = FeedbackClient,
    marker: str | None = None,
) -> list[SmokeResult]:
    """Submit one minimal report per public route and validate every receipt."""

    active_marker = _marker() if marker is None else marker
    completed: list[SmokeResult] = []
    try:
        async with client_factory(base_url) as client:
            for kind, destination in _ROUTES:
                draft = FeedbackDraft(
                    kind=kind,
                    description=f"{active_marker} {kind.value} route",
                )
                draft.acknowledge_public()
                receipt = await client.submit(draft)
                completed.append(_validated_result(active_marker, kind, destination, receipt))
    except Exception as exc:
        raise SmokeRunError(completed) from exc
    return completed


def _print_results(results: Sequence[SmokeResult]) -> None:
    for result in results:
        print(f"{result.marker}\t{result.kind}\t{result.url}")


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: Callable[[str], Awaitable[list[SmokeResult]]] = run_smoke,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, type=_https_base_url)
    parser.add_argument("--confirm-production-smoke", action="store_true")
    arguments = parser.parse_args(argv)
    if not arguments.confirm_production_smoke:
        parser.error("--confirm-production-smoke is required")

    try:
        results = asyncio.run(runner(arguments.base_url))
    except SmokeRunError as exc:
        _print_results(exc.completed)
        print("Production smoke failed.", file=sys.stderr)
        return 1
    except Exception:
        print("Production smoke failed.", file=sys.stderr)
        return 1
    _print_results(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
