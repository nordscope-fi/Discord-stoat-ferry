"""Controlled production-smoke command for the public feedback routes."""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import aiohttp
import pytest

from discord_ferry.feedback import DestinationKind, FeedbackReceipt

if TYPE_CHECKING:
    from types import ModuleType

SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "feedback-smoke.py"
MARKER = "ferry-feedback-smoke-20260831T120000Z-deadbeef"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("feedback_smoke_script", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FakeClient:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.drafts: list[dict[str, object]] = []

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def submit(self, draft: object) -> object:
        self.drafts.append(asdict(draft))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _receipt(kind: DestinationKind, number: int) -> FeedbackReceipt:
    route = "issues" if kind is DestinationKind.ISSUE else "discussions"
    return FeedbackReceipt(
        receipt=uuid4(),
        destination_kind=kind,
        url=f"https://github.com/nordscope-fi/Discord-stoat-ferry/{route}/{number}",
    )


async def test_smoke_submits_three_bounded_routes_and_validates_destinations() -> None:
    module = _load_script()
    client = _FakeClient(
        [
            _receipt(DestinationKind.ISSUE, 901),
            _receipt(DestinationKind.DISCUSSION, 902),
            _receipt(DestinationKind.DISCUSSION, 903),
        ]
    )

    results = await module.run_smoke(
        "https://feedback.example",
        client_factory=lambda _url: client,
        marker=MARKER,
    )

    assert [result.kind for result in results] == ["bug", "idea", "general"]
    assert [result.destination_kind for result in results] == [
        DestinationKind.ISSUE,
        DestinationKind.DISCUSSION,
        DestinationKind.DISCUSSION,
    ]
    descriptions = [str(draft["description"]) for draft in client.drafts]
    assert len(set(descriptions)) == 3
    assert all(MARKER in description for description in descriptions)
    for draft in client.drafts:
        assert draft["expected"] is None
        assert draft["reproduction"] is None
        assert draft["diagnostics"] is None
        assert draft["contact_email"] is None
        assert draft["public_acknowledged"] is True
        assert draft["diagnostics_acknowledged"] is False


def test_smoke_requires_explicit_confirmation_before_calling_service() -> None:
    module = _load_script()
    calls: list[str] = []

    async def runner(base_url: str) -> list[object]:
        calls.append(base_url)
        return []

    with pytest.raises(SystemExit, match="2"):
        module.main(["--base-url", "https://feedback.example"], runner=runner)
    assert calls == []


def test_smoke_prints_only_marker_kind_and_github_url(capsys: pytest.CaptureFixture[str]) -> None:
    module = _load_script()
    client = _FakeClient(
        [
            _receipt(DestinationKind.ISSUE, 911),
            _receipt(DestinationKind.DISCUSSION, 912),
            _receipt(DestinationKind.DISCUSSION, 913),
        ]
    )

    async def runner(base_url: str) -> list[object]:
        return await module.run_smoke(
            base_url,
            client_factory=lambda _url: client,
            marker=MARKER,
        )

    exit_code = module.main(
        [
            "--base-url",
            "https://feedback.example",
            "--confirm-production-smoke",
        ],
        runner=runner,
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.splitlines() == [
        f"{MARKER}\tbug\thttps://github.com/nordscope-fi/Discord-stoat-ferry/issues/911",
        f"{MARKER}\tidea\thttps://github.com/nordscope-fi/Discord-stoat-ferry/discussions/912",
        f"{MARKER}\tgeneral\thttps://github.com/nordscope-fi/Discord-stoat-ferry/discussions/913",
    ]
    for forbidden in ("challenge", "signature", "credential", "contact", "token"):
        assert forbidden not in captured.out.casefold()


@pytest.mark.parametrize("failed_route", [0, 1, 2])
def test_smoke_returns_nonzero_when_any_route_fails(
    failed_route: int,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_script()
    outcomes: list[object] = [
        _receipt(DestinationKind.ISSUE, 921),
        _receipt(DestinationKind.DISCUSSION, 922),
        _receipt(DestinationKind.DISCUSSION, 923),
    ]
    outcomes[failed_route] = aiohttp.ClientConnectionError("credential-marker challenge-marker")
    client = _FakeClient(outcomes)

    async def runner(base_url: str) -> list[object]:
        return await module.run_smoke(
            base_url,
            client_factory=lambda _url: client,
            marker=MARKER,
        )

    exit_code = module.main(
        [
            "--base-url",
            "https://feedback.example",
            "--confirm-production-smoke",
        ],
        runner=runner,
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    completed_lines = [
        f"{MARKER}\tbug\thttps://github.com/nordscope-fi/Discord-stoat-ferry/issues/921",
        f"{MARKER}\tidea\thttps://github.com/nordscope-fi/Discord-stoat-ferry/discussions/922",
    ]
    assert captured.out.splitlines() == completed_lines[:failed_route]
    assert captured.err == "Production smoke failed.\n"
    for forbidden in ("credential-marker", "challenge-marker"):
        assert forbidden not in captured.out
        assert forbidden not in captured.err


@pytest.mark.parametrize(
    ("receipt", "reason"),
    [
        (_receipt(DestinationKind.DISCUSSION, 931), "wrong kind"),
        (
            FeedbackReceipt(
                receipt=uuid4(),
                destination_kind=DestinationKind.ISSUE,
                url="https://example.invalid/issues/931",
            ),
            "wrong host",
        ),
    ],
)
def test_smoke_rejects_wrong_kind_or_destination(
    receipt: FeedbackReceipt,
    reason: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_script()
    client = _FakeClient([receipt])

    async def runner(base_url: str) -> list[object]:
        return await module.run_smoke(
            base_url,
            client_factory=lambda _url: client,
            marker=MARKER,
        )

    assert reason
    exit_code = module.main(
        [
            "--base-url",
            "https://feedback.example",
            "--confirm-production-smoke",
        ],
        runner=runner,
    )
    assert exit_code == 1
    assert capsys.readouterr().err == "Production smoke failed.\n"
