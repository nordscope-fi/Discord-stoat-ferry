from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from aiohttp import ClientTimeout
from aioresponses import aioresponses
from yarl import URL

from discord_ferry import feedback as feedback_module
from discord_ferry.core.engine import PHASE_ORDER
from discord_ferry.core.security import register_secret
from discord_ferry.feedback import (
    Architecture,
    FeedbackClient,
    FeedbackDiagnostics,
    FeedbackDraft,
    FeedbackKind,
    FeedbackRequest,
    FeedbackServiceError,
    FeedbackStage,
    FeedbackValidationError,
    OperatingSystem,
    RuntimeContext,
    build_diagnostics,
    canonical_json,
    challenge_proof_input,
    clean_diagnostics_for_send,
    feedback_content_hash,
    normalize_text,
    render_diagnostics,
    solve_challenge,
)

REPO = Path(__file__).resolve().parent.parent


def test_adr_030_records_feedback_service_defaults() -> None:
    if not (REPO / "AGENTS.md").exists():
        pytest.skip("restored instruction layer is absent")

    adr = REPO / "docs/architecture/adr/030-feedback-service-defaults.md"
    index = REPO / "docs/architecture/adr/README.md"

    assert adr.exists()
    text = " ".join(adr.read_text(encoding="utf-8").split()).lower()
    for expected in (
        "**status:** accepted",
        "https://feedback.nordscope.fi",
        "contract version 1",
        "32 kibibytes",
        "18 zero bits",
        "15 minutes",
        "three accepted reports per rolling hour",
        "ten per rolling day",
        "30 challenges per hour",
        "5 seconds",
        "20 seconds",
        "7 days",
        "24 hours",
        "30 days",
    ):
        assert expected in text

    assert "030-feedback-service-defaults.md" in index.read_text(encoding="utf-8")


def _valid_diagnostics() -> dict[str, object]:
    return {
        "ferry_version": "2.37.2",
        "operating_system": "macos",
        "architecture": "arm64",
        "interface": "cli",
        "stage": "setup",
        "last_error": None,
        "log_excerpt": None,
    }


def _valid_challenge() -> dict[str, object]:
    return {
        "challenge_version": 1,
        "challenge_id": "018f4c8c-3f52-7a89-a901-0123456789ab",
        "request_id": "018f4c8c-3f52-7a89-a901-0123456789ac",
        "nonce": "YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXowMTIzNDU",
        "expires_at": "2026-08-31T12:00:00Z",
        "work_factor": 18,
        "signature": "YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXowMTIzNDU",
        "counter": 42,
    }


def _valid_request() -> dict[str, Any]:
    return {
        "contract_version": 1,
        "request_id": "018f4c8c-3f52-7a89-a901-0123456789ac",
        "kind": "bug",
        "description": "Ferry stopped on the review screen.",
        "expected": None,
        "reproduction": None,
        "diagnostics": _valid_diagnostics(),
        "contact_email": None,
        "public_acknowledged": True,
        "diagnostics_acknowledged": True,
        "challenge": _valid_challenge(),
    }


def test_feedback_kinds_are_closed() -> None:
    assert {kind.value for kind in FeedbackKind} == {"bug", "idea", "general"}
    with pytest.raises(ValueError):
        FeedbackKind("support")


def test_diagnostic_enums_match_the_released_contract() -> None:
    assert {value.value for value in OperatingSystem} == {
        "windows",
        "macos",
        "linux",
        "other",
    }
    assert {value.value for value in Architecture} == {"x86_64", "arm64", "other"}
    assert {value.value for value in FeedbackStage} == {
        "setup",
        "discord_export",
        "review",
        "export",
        "validate",
        "connect",
        "server",
        "roles",
        "categories",
        "channels",
        "emoji",
        "avatars",
        "messages",
        "reactions",
        "pins",
        "report",
        "validate_migration",
        "rollback",
        "check",
        "repair",
        "retry",
        "complete",
        "unknown",
    }
    feedback_only_stages = {
        "setup",
        "discord_export",
        "review",
        "validate_migration",
        "rollback",
        "check",
        "repair",
        "retry",
        "complete",
        "unknown",
    }
    assert {value.value for value in FeedbackStage} == set(PHASE_ORDER) | feedback_only_stages


@pytest.mark.parametrize(
    ("member", "value"),
    [
        ("attachment", "export.json"),
        ("token", "not-allowed"),
        ("output_dir", "/tmp/ferry-output"),
        ("state", {"current_phase": "messages"}),
    ],
)
def test_request_rejects_unknown_members(member: str, value: object) -> None:
    data = _valid_request()
    data[member] = value

    with pytest.raises(FeedbackValidationError, match=f"unknown member: {member}"):
        FeedbackRequest.from_mapping(data)


def test_diagnostics_reject_unknown_members() -> None:
    data = _valid_diagnostics()
    data["export_path"] = "/private/export"

    with pytest.raises(FeedbackValidationError, match="unknown member: export_path"):
        FeedbackDiagnostics.from_mapping(data)


@pytest.mark.parametrize(
    ("text", "limit", "expected"),
    [
        ("line one\r\nline two\rline three", 64, "line one\nline two\nline three"),
        ("å" * 4, 8, "å" * 4),
    ],
)
def test_normalize_text_uses_line_feeds_and_utf8_bytes(
    text: str, limit: int, expected: str
) -> None:
    assert normalize_text("description", text, minimum=1, maximum=limit) == expected


def test_normalize_text_rejects_one_utf8_byte_over_the_limit() -> None:
    with pytest.raises(FeedbackValidationError, match="description exceeds 8 UTF-8 bytes"):
        normalize_text("description", "å" * 4 + "x", minimum=1, maximum=8)


@pytest.mark.parametrize("text", ["hello\x00world", "hello\x07world", "   "])
def test_normalize_text_rejects_controls_and_blank_text(text: str) -> None:
    with pytest.raises(FeedbackValidationError):
        normalize_text("description", text, minimum=1, maximum=100)


@pytest.mark.parametrize(
    "email",
    ["two@@example.com", "missing-at", "a b@example.com", "å@example.com", "@example.com"],
)
def test_request_rejects_invalid_contact_email(email: str) -> None:
    data = _valid_request()
    data["contact_email"] = email

    with pytest.raises(FeedbackValidationError, match="contact_email"):
        FeedbackRequest.from_mapping(data)


def test_request_accepts_exact_field_boundaries() -> None:
    data = _valid_request()
    data["description"] = "d" * 8_000
    data["expected"] = "é" * 2_000
    data["reproduction"] = "r" * 4_000
    diagnostics = _valid_diagnostics()
    diagnostics["last_error"] = "e" * 2_000
    diagnostics["log_excerpt"] = "l" * 12_000
    data["diagnostics"] = diagnostics

    request = FeedbackRequest.from_mapping(data)

    assert len(request.description.encode()) == 8_000
    assert request.diagnostics is not None
    assert len(request.diagnostics.log_excerpt.encode()) == 12_000


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("description", "d" * 8_001, "description exceeds 8000 UTF-8 bytes"),
        ("expected", "e" * 4_001, "expected exceeds 4000 UTF-8 bytes"),
        ("reproduction", "r" * 4_001, "reproduction exceeds 4000 UTF-8 bytes"),
    ],
    ids=["description", "expected", "reproduction"],
)
def test_request_rejects_public_fields_over_the_limit(field: str, value: str, message: str) -> None:
    data = _valid_request()
    data[field] = value

    with pytest.raises(FeedbackValidationError, match=message):
        FeedbackRequest.from_mapping(data)


def test_request_requires_diagnostic_acknowledgement() -> None:
    data = _valid_request()
    data["diagnostics_acknowledged"] = False

    with pytest.raises(FeedbackValidationError, match="diagnostics_acknowledged"):
        FeedbackRequest.from_mapping(data)


def test_canonical_json_sorts_compacts_and_preserves_utf8() -> None:
    assert canonical_json({"z": "Häme", "a": [1, True, None]}) == (
        b'{"a":[1,true,null],"z":"H\xc3\xa4me"}'
    )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_canonical_json_rejects_non_finite_numbers(value: float) -> None:
    with pytest.raises(ValueError, match="JSON compliant"):
        canonical_json({"value": value})


def test_content_hash_uses_normalized_line_endings() -> None:
    windows_data = _valid_request()
    windows_data["description"] = "one\r\ntwo\rthree"
    unix_data = _valid_request()
    unix_data["description"] = "one\ntwo\nthree"

    windows_request = FeedbackRequest.from_mapping(windows_data)
    unix_request = FeedbackRequest.from_mapping(unix_data)

    assert windows_request.description == "one\ntwo\nthree"
    assert feedback_content_hash(windows_request) == feedback_content_hash(unix_request)


def test_content_hash_excludes_request_identity_and_challenge() -> None:
    request = FeedbackRequest.from_mapping(_valid_request())
    changed_challenge = replace(
        request.challenge,
        challenge_id=UUID("018f4c8c-3f52-7a89-a901-1123456789ab"),
        counter=999,
    )
    changed_identity = replace(
        request,
        request_id=UUID("018f4c8c-3f52-7a89-a901-1123456789ac"),
        challenge=replace(
            changed_challenge,
            request_id=UUID("018f4c8c-3f52-7a89-a901-1123456789ac"),
        ),
    )

    assert feedback_content_hash(request) == feedback_content_hash(changed_identity)


def test_content_hash_includes_public_and_private_content() -> None:
    request = FeedbackRequest.from_mapping(_valid_request())

    assert feedback_content_hash(request) != feedback_content_hash(
        replace(request, description="Different public report")
    )
    assert feedback_content_hash(request) != feedback_content_hash(
        replace(request, contact_email="owner@example.com")
    )


def test_content_hash_is_stable_across_processes() -> None:
    request_data = _valid_request()
    expected = feedback_content_hash(FeedbackRequest.from_mapping(request_data))
    script = """
import json
import sys
from discord_ferry.feedback import FeedbackRequest, feedback_content_hash

request = FeedbackRequest.from_mapping(json.load(sys.stdin))
print(feedback_content_hash(request))
"""

    actual = (
        subprocess.check_output(
            [sys.executable, "-c", script],
            input=json.dumps(request_data).encode(),
        )
        .decode()
        .strip()
    )

    assert actual == expected


@pytest.mark.parametrize(
    ("system", "machine", "expected_system", "expected_machine"),
    [
        ("win32", "AMD64", OperatingSystem.WINDOWS, Architecture.X86_64),
        ("darwin", "aarch64", OperatingSystem.MACOS, Architecture.ARM64),
        ("linux", "x86_64", OperatingSystem.LINUX, Architecture.X86_64),
        ("plan9", "sparc", OperatingSystem.OTHER, Architecture.OTHER),
    ],
)
def test_build_diagnostics_normalizes_platform_aliases(
    system: str,
    machine: str,
    expected_system: OperatingSystem,
    expected_machine: Architecture,
) -> None:
    diagnostics = build_diagnostics(
        RuntimeContext(
            ferry_version="2.37.2",
            operating_system=system,
            architecture=machine,
            interface="gui",
            stage="messages",
            last_error="The migration stopped",
        ),
        include_logs=False,
        log_path=None,
    )

    assert diagnostics.operating_system is expected_system
    assert diagnostics.architecture is expected_machine
    assert diagnostics.interface.value == "gui"
    assert diagnostics.stage is FeedbackStage.MESSAGES
    assert diagnostics.last_error == "The migration stopped"
    assert diagnostics.log_excerpt is None


def test_build_diagnostics_maps_an_unsupported_stage_to_unknown() -> None:
    diagnostics = build_diagnostics(
        RuntimeContext("2.37.2", "linux", "x86_64", "cli", "unreleased_phase", None),
        include_logs=False,
        log_path=None,
    )

    assert diagnostics.stage is FeedbackStage.UNKNOWN


def test_build_diagnostics_reads_only_a_bounded_log_tail(tmp_path: Path) -> None:
    log_path = tmp_path / "ferry.log"
    log_path.write_text("\n".join(f"line {number:03}: " + "x" * 200 for number in range(150)))
    context = RuntimeContext("2.37.2", "linux", "x86_64", "cli", "messages", None)

    without_logs = build_diagnostics(
        context,
        include_logs=False,
        log_path=log_path,
    )
    with_logs = build_diagnostics(
        context,
        include_logs=True,
        log_path=log_path,
    )

    assert without_logs.log_excerpt is None
    assert with_logs.log_excerpt is not None
    assert 1 <= len(with_logs.log_excerpt.splitlines()) <= 100
    assert len(with_logs.log_excerpt.encode()) <= 12_000
    assert "line 149" in with_logs.log_excerpt
    assert "line 000" not in with_logs.log_excerpt


def test_render_diagnostics_returns_the_exact_review_text() -> None:
    diagnostics = FeedbackDiagnostics.from_mapping(_valid_diagnostics())

    assert (
        render_diagnostics(diagnostics)
        == """Ferry version: 2.37.2
Operating system: macos
Architecture: arm64
Interface: cli
Stage: setup"""
    )


def test_clean_diagnostics_for_send_removes_credential_shapes() -> None:
    registered = "stoat-example-value-1234"
    register_secret("stoat", registered)
    discord_token = ".".join(
        ("MTIzNDU2Nzg5MDEyMzQ1Njc4", "GhIjKl", "abcdefghijklmnopqrstuvwxyz123")
    )
    diagnostics = FeedbackDiagnostics(
        ferry_version=registered,
        operating_system=OperatingSystem.MACOS,
        architecture=Architecture.ARM64,
        interface=feedback_module.FeedbackInterface.CLI,
        stage=FeedbackStage.SETUP,
        last_error=(
            "Authorization: Bearer private-value\n"
            "Proxy-Authorization: Basic cHJpdmF0ZQ==\n"
            "https://person:password@example.com/path ****1234"
        ),
        log_excerpt=f"Discord rejected {discord_token}",
    )

    cleaned = clean_diagnostics_for_send(diagnostics)
    encoded = json.dumps(cleaned.to_mapping())

    for forbidden in (
        registered,
        "private-value",
        "cHJpdmF0ZQ==",
        "person:password@",
        discord_token,
        "****1234",
    ):
        assert forbidden not in encoded
    assert cleaned.ferry_version == "****"
    assert "Authorization: ****" in (cleaned.last_error or "")


@pytest.mark.parametrize(
    "label",
    ["PRIVATE KEY", "RSA PRIVATE KEY", "EC PRIVATE KEY", "OPENSSH PRIVATE KEY"],
)
def test_clean_diagnostics_for_send_removes_pem_private_keys(label: str) -> None:
    marker = "private-key-material"
    diagnostics = FeedbackDiagnostics.from_mapping(
        {
            **_valid_diagnostics(),
            "log_excerpt": (
                f"before\n-----BEGIN {label}-----\n{marker}\n-----END {label}-----\nafter"
            ),
        }
    )

    cleaned = clean_diagnostics_for_send(diagnostics)

    assert marker not in (cleaned.log_excerpt or "")
    assert "PRIVATE KEY-----" not in (cleaned.log_excerpt or "")
    assert cleaned.log_excerpt == "before\n****\nafter"


def test_clean_diagnostics_for_send_removes_a_truncated_pem_private_key() -> None:
    diagnostics = FeedbackDiagnostics.from_mapping(
        {
            **_valid_diagnostics(),
            "log_excerpt": "before\n-----BEGIN PRIVATE KEY-----\ntruncated-key-material",
        }
    )

    cleaned = clean_diagnostics_for_send(diagnostics)

    assert cleaned.log_excerpt == "before\n****"


def test_clean_diagnostics_rejects_a_registered_value_left_after_cleaning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register_secret("stoat", "registered-value-1234")
    diagnostics = FeedbackDiagnostics.from_mapping(
        {**_valid_diagnostics(), "last_error": "registered-value-1234"}
    )
    monkeypatch.setattr(
        feedback_module,
        "sanitize_secrets_for_public_output",
        lambda text: text,
    )

    with pytest.raises(FeedbackValidationError, match="registered credential"):
        clean_diagnostics_for_send(diagnostics)


def _challenge_response() -> dict[str, object]:
    data = _valid_challenge()
    data.pop("counter")
    return data


def test_challenge_proof_input_matches_the_released_vector() -> None:
    challenge = feedback_module.Challenge.from_response_mapping(_challenge_response())
    expected_members = _challenge_response()

    assert challenge_proof_input(challenge, 42) == (canonical_json(expected_members) + b"\n42")
    with pytest.raises(FeedbackValidationError, match="counter"):
        challenge_proof_input(challenge, -1)
    with pytest.raises(FeedbackValidationError, match="counter"):
        challenge_proof_input(challenge, feedback_module.MAX_COUNTER + 1)


def test_solve_challenge_finds_an_18_bit_proof() -> None:
    challenge = feedback_module.Challenge.from_response_mapping(_challenge_response())

    solved = solve_challenge(challenge)
    digest = hashlib.sha256(challenge_proof_input(solved, solved.counter)).digest()

    assert digest[0:2] == b"\x00\x00"
    assert digest[2] & 0b1100_0000 == 0


@pytest.mark.asyncio
async def test_feedback_client_parses_challenge_and_sends_closed_request() -> None:
    url = "https://feedback.example/v1/challenge"
    with aioresponses() as mocked:
        mocked.post(url, payload=_challenge_response())
        async with FeedbackClient("https://feedback.example") as client:
            challenge = await client.create_challenge(UUID("018f4c8c-3f52-7a89-a901-0123456789ac"))

    assert challenge.counter == 0
    request = mocked.requests[("POST", URL(url))][0]
    assert request.kwargs["json"] == {
        "contract_version": 1,
        "request_id": "018f4c8c-3f52-7a89-a901-0123456789ac",
    }


@pytest.mark.asyncio
async def test_feedback_client_rejects_invalid_utf8_challenge_json() -> None:
    url = "https://feedback.example/v1/challenge"
    with aioresponses() as mocked:
        mocked.post(url, body=b"\xff", content_type="application/json")
        async with FeedbackClient("https://feedback.example") as client:
            with pytest.raises(
                FeedbackValidationError,
                match="feedback service returned invalid JSON",
            ):
                await client.create_challenge(UUID("018f4c8c-3f52-7a89-a901-0123456789ac"))


@pytest.mark.asyncio
async def test_feedback_client_uses_released_timeouts_and_owns_its_session() -> None:
    client = FeedbackClient("https://feedback.example")

    async with client:
        session = client.session
        assert isinstance(session.timeout, ClientTimeout)
        assert session.timeout.connect == 5
        assert session.timeout.total == 20
        assert session.closed is False

    assert session.closed is True


@pytest.mark.asyncio
async def test_feedback_client_leaves_a_supplied_session_open() -> None:
    async with feedback_module.new_session() as session:
        async with FeedbackClient("https://feedback.example", session=session) as client:
            assert client.session is session
        assert session.closed is False


@pytest.mark.asyncio
async def test_feedback_client_raises_typed_service_errors() -> None:
    url = "https://feedback.example/v1/challenge"
    with aioresponses() as mocked:
        mocked.post(
            url,
            status=429,
            payload={
                "code": "throttled",
                "message": "Try later",
                "retry_at": "2026-08-31T13:00:00Z",
            },
        )
        async with FeedbackClient("https://feedback.example") as client:
            with pytest.raises(FeedbackServiceError) as raised:
                await client.create_challenge(UUID("018f4c8c-3f52-7a89-a901-0123456789ac"))

    assert raised.value.error.code is feedback_module.FeedbackErrorCode.THROTTLED
    assert raised.value.status == 429


class _ClientDraft:
    def __init__(self) -> None:
        self.request_id = UUID("018f4c8c-3f52-7a89-a901-0123456789ac")

    def to_request(self, challenge: feedback_module.Challenge) -> FeedbackRequest:
        data = _valid_request()
        data["challenge"] = challenge.to_mapping()
        return FeedbackRequest.from_mapping(data)


@pytest.mark.asyncio
async def test_feedback_client_returns_receipt_with_one_write_and_no_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    challenge_url = "https://feedback.example/v1/challenge"
    feedback_url = "https://feedback.example/v1/feedback"
    monkeypatch.setattr(
        feedback_module,
        "solve_challenge",
        lambda challenge: replace(challenge, counter=42),
    )
    with aioresponses() as mocked:
        mocked.post(challenge_url, payload=_challenge_response())
        mocked.post(
            feedback_url,
            payload={
                "receipt": "018f4c8c-3f52-7a89-a901-2123456789ac",
                "destination_kind": "issue",
                "url": "https://github.com/nordscope-fi/Discord-stoat-ferry/issues/999",
            },
        )
        async with FeedbackClient("https://feedback.example") as client:
            receipt = await client.submit(_ClientDraft())

    assert receipt.destination_kind.value == "issue"
    assert receipt.url.endswith("/issues/999")
    assert len(mocked.requests[("POST", URL(feedback_url))]) == 1


@pytest.mark.asyncio
async def test_feedback_client_rejects_invalid_utf8_receipt_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    challenge_url = "https://feedback.example/v1/challenge"
    feedback_url = "https://feedback.example/v1/feedback"
    monkeypatch.setattr(
        feedback_module,
        "solve_challenge",
        lambda challenge: replace(challenge, counter=42),
    )
    with aioresponses() as mocked:
        mocked.post(challenge_url, payload=_challenge_response())
        mocked.post(feedback_url, body=b"\xff", content_type="application/json")
        async with FeedbackClient("https://feedback.example") as client:
            with pytest.raises(
                FeedbackValidationError,
                match="feedback service returned invalid JSON",
            ):
                await client.submit(_ClientDraft())


@pytest.mark.asyncio
async def test_feedback_client_does_not_retry_a_failed_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    challenge_url = "https://feedback.example/v1/challenge"
    feedback_url = "https://feedback.example/v1/feedback"
    monkeypatch.setattr(
        feedback_module,
        "solve_challenge",
        lambda challenge: replace(challenge, counter=42),
    )
    with aioresponses() as mocked:
        mocked.post(challenge_url, payload=_challenge_response())
        mocked.post(
            feedback_url,
            status=503,
            payload={"code": "temporary_failure", "message": "Unavailable"},
        )
        async with FeedbackClient("https://feedback.example") as client:
            with pytest.raises(FeedbackServiceError):
                await client.submit(_ClientDraft())

    assert len(mocked.requests[("POST", URL(feedback_url))]) == 1


def _draft_challenge(draft: FeedbackDraft) -> feedback_module.Challenge:
    return replace(
        feedback_module.Challenge.from_response_mapping(_challenge_response()),
        request_id=draft.request_id,
        counter=42,
    )


def test_feedback_draft_builds_the_shortest_anonymous_request() -> None:
    draft = FeedbackDraft(FeedbackKind.BUG, "Ferry stopped")
    draft.acknowledge_public()

    request = draft.to_request(_draft_challenge(draft))
    encoded = request.to_mapping()

    assert request.request_id == draft.request_id
    assert encoded["kind"] == "bug"
    assert encoded["description"] == "Ferry stopped"
    assert encoded["diagnostics_acknowledged"] is False
    for absent in ("expected", "reproduction", "diagnostics", "contact_email", "name"):
        assert absent not in encoded


def test_feedback_draft_renders_the_exact_public_preview_and_copy() -> None:
    diagnostics = FeedbackDiagnostics.from_mapping(_valid_diagnostics())
    draft = FeedbackDraft(
        FeedbackKind.IDEA,
        "Add resumable uploads",
        expected="Uploads continue after a restart",
        reproduction="Stop Ferry during an upload",
        diagnostics=diagnostics,
        contact_email="private@example.com",
        request_id=UUID("018f4c8c-3f52-7a89-a901-0123456789ac"),
    )
    expected = """## Report
Add resumable uploads

## Expected result
Uploads continue after a restart

## Reproduction steps
Stop Ferry during an upload

## Diagnostics
Ferry version: 2.37.2
Operating system: macos
Architecture: arm64
Interface: cli
Stage: setup

## Ferry context
Submitted through Discord Ferry.
Ferry version: 2.37.2
Interface: cli

## Receipt
<!-- ferry-feedback:018f4c8c-3f52-7a89-a901-0123456789ac -->
Receipt: `018f4c8c-3f52-7a89-a901-0123456789ac`
Private contact is available to maintainers under this receipt."""

    assert draft.render_public_body() == expected
    assert draft.copy_text() == expected
    assert "private@example.com" not in expected


def test_feedback_draft_edits_invalidate_the_relevant_acknowledgements() -> None:
    draft = FeedbackDraft(
        FeedbackKind.BUG,
        "Original report",
        diagnostics=FeedbackDiagnostics.from_mapping(_valid_diagnostics()),
    )
    draft.acknowledge_public()
    draft.acknowledge_diagnostics()

    draft.edit_description("Edited report")

    assert draft.public_acknowledged is False
    assert draft.diagnostics_acknowledged is True
    draft.acknowledge_public()

    draft.edit_diagnostics(
        FeedbackDiagnostics.from_mapping({**_valid_diagnostics(), "last_error": "Edited error"})
    )

    assert draft.public_acknowledged is False
    assert draft.diagnostics_acknowledged is False
    assert "Original report" not in draft.render_public_body()
    assert "Edited report" in draft.render_public_body()
    assert "Edited error" in draft.render_public_body()


def test_feedback_draft_requires_review_acknowledgements() -> None:
    draft = FeedbackDraft(
        FeedbackKind.BUG,
        "Report",
        diagnostics=FeedbackDiagnostics.from_mapping(_valid_diagnostics()),
    )

    with pytest.raises(FeedbackValidationError, match="public_acknowledged"):
        draft.to_request(_draft_challenge(draft))
    draft.acknowledge_public()
    with pytest.raises(FeedbackValidationError, match="diagnostics_acknowledged"):
        draft.to_request(_draft_challenge(draft))


def test_feedback_draft_retry_keeps_the_request_id_and_replaces_the_challenge() -> None:
    draft = FeedbackDraft(FeedbackKind.GENERAL, "A general note")
    draft.acknowledge_public()
    first = _draft_challenge(draft)
    second = replace(
        first,
        challenge_id=UUID("018f4c8c-3f52-7a89-a901-3123456789ac"),
        counter=99,
    )

    first_request = draft.to_request(first)
    second_request = draft.to_request(second)

    assert first_request.request_id == second_request.request_id == draft.request_id
    assert first_request.challenge.challenge_id != second_request.challenge.challenge_id


def test_feedback_draft_save_is_explicit_owner_only_and_excludes_contact(
    tmp_path: Path,
    windows_filesystem: None,
) -> None:
    draft = FeedbackDraft(
        FeedbackKind.BUG,
        "Saved report",
        contact_email="private@example.com",
    )
    path = tmp_path / "feedback.md"

    draft.save(path)
    assert path.read_text() == draft.render_public_body()
    assert "private@example.com" not in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600

    draft.edit_description("Saved again")
    draft.save(path, include_contact=True)
    saved = path.read_text()
    assert "Saved again" in saved
    assert "private@example.com" in saved
    assert path.stat().st_mode & 0o777 == 0o600
