"""Shared models and client-side support for voluntary Ferry feedback."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import os
import re
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, Protocol, TypeVar, cast
from urllib.parse import urlparse
from uuid import UUID, uuid4

import aiohttp

from discord_ferry.core.http import new_session
from discord_ferry.core.security import (
    contains_registered_secret,
    sanitize_secrets_for_public_output,
)

if TYPE_CHECKING:
    from pathlib import Path

MAX_COUNTER = 9_007_199_254_740_991
_BASE64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_DISCORD_TOKEN = re.compile(r"\b(?:mfa\.[\w-]{20,}|[\w-]{23,28}\.[\w-]{6,7}\.[\w-]{27,})\b")
_AUTHORIZATION_HEADER = re.compile(
    r"(?im)^(?P<prefix>[ \t]*(?:proxy-)?authorization[ \t]*:[ \t]*)[^\r\n]*$"
)
_CREDENTIAL_URL = re.compile(r"\b(?P<scheme>https?://)[^/@\s]+@", re.IGNORECASE)
_PEM_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?P<label>(?:[A-Z0-9]+ )*PRIVATE KEY)-----"
    r".*?(?:-----END (?P=label)-----|\Z)",
    re.DOTALL,
)
EnumT = TypeVar("EnumT", bound=StrEnum)


class FeedbackValidationError(ValueError):
    """Raised when feedback data does not match the released closed contract."""


class FeedbackKind(StrEnum):
    """Public destination selected by the person submitting feedback."""

    BUG = "bug"
    IDEA = "idea"
    GENERAL = "general"


class OperatingSystem(StrEnum):
    """Normalized operating systems accepted in diagnostics."""

    WINDOWS = "windows"
    MACOS = "macos"
    LINUX = "linux"
    OTHER = "other"


class Architecture(StrEnum):
    """Normalized processor architectures accepted in diagnostics."""

    X86_64 = "x86_64"
    ARM64 = "arm64"
    OTHER = "other"


class FeedbackInterface(StrEnum):
    """Ferry interface that collected the report."""

    GUI = "gui"
    CLI = "cli"


class FeedbackStage(StrEnum):
    """Closed migration and interface stages used in diagnostics."""

    SETUP = "setup"
    DISCORD_EXPORT = "discord_export"
    REVIEW = "review"
    EXPORT = "export"
    VALIDATE = "validate"
    CONNECT = "connect"
    SERVER = "server"
    ROLES = "roles"
    CATEGORIES = "categories"
    CHANNELS = "channels"
    EMOJI = "emoji"
    AVATARS = "avatars"
    MESSAGES = "messages"
    REACTIONS = "reactions"
    PINS = "pins"
    REPORT = "report"
    VALIDATE_MIGRATION = "validate_migration"
    ROLLBACK = "rollback"
    CHECK = "check"
    REPAIR = "repair"
    RETRY = "retry"
    COMPLETE = "complete"
    UNKNOWN = "unknown"


class DestinationKind(StrEnum):
    """GitHub item created by the feedback service."""

    ISSUE = "issue"
    DISCUSSION = "discussion"


class FeedbackErrorCode(StrEnum):
    """Stable service error codes understood by Ferry clients."""

    INVALID_INPUT = "invalid_input"
    INVALID_CHALLENGE = "invalid_challenge"
    EXPIRED_CHALLENGE = "expired_challenge"
    THROTTLED = "throttled"
    DUPLICATE_ID_CONFLICT = "duplicate_id_conflict"
    TEMPORARY_FAILURE = "temporary_failure"
    GITHUB_FAILURE = "github_failure"
    RECONCILIATION_REQUIRED = "reconciliation_required"


def _closed_mapping(
    name: str,
    value: object,
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise FeedbackValidationError(f"{name} must be an object")
    data = cast("dict[str, object]", value)
    unknown = sorted(set(data) - required - optional)
    if unknown:
        raise FeedbackValidationError(f"unknown member: {unknown[0]}")
    missing = sorted(required - set(data))
    if missing:
        raise FeedbackValidationError(f"missing member: {missing[0]}")
    return data


def _enum_value(name: str, enum_type: type[EnumT], value: object) -> EnumT:
    if not isinstance(value, str):
        raise FeedbackValidationError(f"{name} must be a string")
    try:
        return enum_type(value)
    except ValueError as exc:
        raise FeedbackValidationError(f"{name} has an unsupported value") from exc


def _uuid_value(name: str, value: object) -> UUID:
    if not isinstance(value, str):
        raise FeedbackValidationError(f"{name} must be a UUID string")
    try:
        return UUID(value)
    except ValueError as exc:
        raise FeedbackValidationError(f"{name} must be a UUID string") from exc


def _datetime_value(name: str, value: object) -> datetime:
    if not isinstance(value, str):
        raise FeedbackValidationError(f"{name} must be a timestamp string")
    source = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        result = datetime.fromisoformat(source)
    except ValueError as exc:
        raise FeedbackValidationError(f"{name} must be an ISO timestamp") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise FeedbackValidationError(f"{name} must include a timezone")
    if result.microsecond:
        raise FeedbackValidationError(f"{name} must use whole seconds")
    return result


def _base64url_value(name: str, value: object, *, decoded_bytes: int) -> str:
    if not isinstance(value, str) or not value or "=" in value or not _BASE64URL.fullmatch(value):
        raise FeedbackValidationError(f"{name} must be unpadded base64url")
    try:
        decoded = base64.urlsafe_b64decode(f"{value}{'=' * (-len(value) % 4)}")
    except (ValueError, binascii.Error) as exc:
        raise FeedbackValidationError(f"{name} must be unpadded base64url") from exc
    if len(decoded) != decoded_bytes:
        raise FeedbackValidationError(f"{name} must encode {decoded_bytes} bytes")
    return value


def normalize_text(name: str, value: object, *, minimum: int, maximum: int) -> str:
    """Normalize line endings and enforce a UTF-8 byte boundary."""

    if not isinstance(value, str):
        raise FeedbackValidationError(f"{name} must be a string")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    for character in normalized:
        if character in {"\n", "\t"}:
            continue
        if unicodedata.category(character) == "Cc":
            raise FeedbackValidationError(f"{name} contains an unsupported control character")
    length = len(normalized.encode("utf-8"))
    if length < minimum:
        raise FeedbackValidationError(f"{name} must contain at least {minimum} UTF-8 bytes")
    if not normalized.strip():
        raise FeedbackValidationError(f"{name} must not be blank")
    if length > maximum:
        raise FeedbackValidationError(f"{name} exceeds {maximum} UTF-8 bytes")
    return normalized


def _optional_text(name: str, value: object, *, maximum: int) -> str | None:
    if value is None or value == "":
        return None
    return normalize_text(name, value, minimum=1, maximum=maximum)


def _contact_email(value: object) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise FeedbackValidationError("contact_email must be a string")
    if len(value) > 254 or not value.isascii():
        raise FeedbackValidationError("contact_email is invalid")
    if value.count("@") != 1 or value.startswith("@") or value.endswith("@"):
        raise FeedbackValidationError("contact_email is invalid")
    if any(character.isspace() or unicodedata.category(character) == "Cc" for character in value):
        raise FeedbackValidationError("contact_email is invalid")
    return value


@dataclass(frozen=True)
class FeedbackDiagnostics:
    """Optional public runtime context reviewed by the user."""

    ferry_version: str
    operating_system: OperatingSystem
    architecture: Architecture
    interface: FeedbackInterface
    stage: FeedbackStage
    last_error: str | None = None
    log_excerpt: str | None = None

    @classmethod
    def from_mapping(cls, value: object) -> FeedbackDiagnostics:
        data = _closed_mapping(
            "diagnostics",
            value,
            required=frozenset(
                {"ferry_version", "operating_system", "architecture", "interface", "stage"}
            ),
            optional=frozenset({"last_error", "log_excerpt"}),
        )
        ferry_version = normalize_text(
            "ferry_version", data["ferry_version"], minimum=1, maximum=32
        )
        last_error = _optional_text("last_error", data.get("last_error"), maximum=2_000)
        log_excerpt = _optional_text("log_excerpt", data.get("log_excerpt"), maximum=12_000)
        if log_excerpt is not None and len(log_excerpt.splitlines()) > 100:
            raise FeedbackValidationError("log_excerpt exceeds 100 lines")
        return cls(
            ferry_version=ferry_version,
            operating_system=_enum_value(
                "operating_system", OperatingSystem, data["operating_system"]
            ),
            architecture=_enum_value("architecture", Architecture, data["architecture"]),
            interface=_enum_value("interface", FeedbackInterface, data["interface"]),
            stage=_enum_value("stage", FeedbackStage, data["stage"]),
            last_error=last_error,
            log_excerpt=log_excerpt,
        )

    def to_mapping(self) -> dict[str, object]:
        result: dict[str, object] = {
            "ferry_version": self.ferry_version,
            "operating_system": self.operating_system.value,
            "architecture": self.architecture.value,
            "interface": self.interface.value,
            "stage": self.stage.value,
        }
        if self.last_error is not None:
            result["last_error"] = self.last_error
        if self.log_excerpt is not None:
            result["log_excerpt"] = self.log_excerpt
        return result


@dataclass(frozen=True)
class RuntimeContext:
    """Raw local runtime values used to prepare an optional diagnostic preview."""

    ferry_version: str
    operating_system: str
    architecture: str
    interface: str
    stage: str
    last_error: str | None


def _operating_system(value: str) -> OperatingSystem:
    normalized = value.casefold()
    if normalized in {"win32", "windows", "cygwin", "msys"}:
        return OperatingSystem.WINDOWS
    if normalized in {"darwin", "macos", "mac", "osx"}:
        return OperatingSystem.MACOS
    if normalized.startswith("linux"):
        return OperatingSystem.LINUX
    return OperatingSystem.OTHER


def _architecture(value: str) -> Architecture:
    normalized = value.casefold()
    if normalized in {"amd64", "x86_64", "x64"}:
        return Architecture.X86_64
    if normalized in {"arm64", "aarch64"}:
        return Architecture.ARM64
    return Architecture.OTHER


def _feedback_stage(value: str) -> FeedbackStage:
    try:
        return FeedbackStage(value)
    except ValueError:
        return FeedbackStage.UNKNOWN


def _log_tail(path: Path) -> str | None:
    try:
        with path.open("rb") as log_file:
            log_file.seek(0, 2)
            size = log_file.tell()
            log_file.seek(max(0, size - 48 * 1024))
            source = log_file.read()
    except OSError:
        return None

    text = source.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(text.splitlines()[-100:])
    encoded = text.encode("utf-8")
    if len(encoded) > 12_000:
        text = encoded[-12_000:].decode("utf-8", errors="ignore")
    return text or None


def build_diagnostics(
    context: RuntimeContext,
    *,
    include_logs: bool,
    log_path: Path | None,
) -> FeedbackDiagnostics:
    """Build the closed diagnostic preview from basic context and one log tail."""

    return FeedbackDiagnostics.from_mapping(
        {
            "ferry_version": context.ferry_version,
            "operating_system": _operating_system(context.operating_system).value,
            "architecture": _architecture(context.architecture).value,
            "interface": _enum_value("interface", FeedbackInterface, context.interface).value,
            "stage": _feedback_stage(context.stage).value,
            "last_error": context.last_error,
            "log_excerpt": (_log_tail(log_path) if include_logs and log_path is not None else None),
        }
    )


def render_diagnostics(diagnostics: FeedbackDiagnostics) -> str:
    """Render the exact diagnostic text shown before public submission."""

    lines = [
        f"Ferry version: {diagnostics.ferry_version}",
        f"Operating system: {diagnostics.operating_system.value}",
        f"Architecture: {diagnostics.architecture.value}",
        f"Interface: {diagnostics.interface.value}",
        f"Stage: {diagnostics.stage.value}",
    ]
    if diagnostics.last_error is not None:
        lines.extend(("", "Last error:", diagnostics.last_error))
    if diagnostics.log_excerpt is not None:
        lines.extend(("", "Recent log:", diagnostics.log_excerpt))
    return "\n".join(lines)


def _clean_diagnostic_text(text: str) -> str:
    cleaned = sanitize_secrets_for_public_output(text)
    cleaned = _DISCORD_TOKEN.sub("****", cleaned)
    cleaned = _AUTHORIZATION_HEADER.sub(lambda match: f"{match.group('prefix')}****", cleaned)
    cleaned = _CREDENTIAL_URL.sub(lambda match: match.group("scheme"), cleaned)
    cleaned = _PEM_PRIVATE_KEY.sub("****", cleaned)
    if contains_registered_secret(cleaned):
        raise FeedbackValidationError("diagnostics contain a registered credential")
    if _DISCORD_TOKEN.search(cleaned) or _CREDENTIAL_URL.search(cleaned):
        raise FeedbackValidationError("diagnostics contain a credential-shaped value")
    return cleaned


def clean_diagnostics_for_send(diagnostics: FeedbackDiagnostics) -> FeedbackDiagnostics:
    """Apply the final credential cleaner to every editable diagnostic string."""

    return FeedbackDiagnostics.from_mapping(
        {
            **diagnostics.to_mapping(),
            "ferry_version": _clean_diagnostic_text(diagnostics.ferry_version),
            "last_error": (
                None
                if diagnostics.last_error is None
                else _clean_diagnostic_text(diagnostics.last_error)
            ),
            "log_excerpt": (
                None
                if diagnostics.log_excerpt is None
                else _clean_diagnostic_text(diagnostics.log_excerpt)
            ),
        }
    )


def _safe_public_text(value: str) -> str:
    return value.replace("<!-- ferry-feedback:", "&lt;!-- ferry-feedback:")


def render_public_feedback_body(
    *,
    request_id: UUID,
    description: str,
    expected: str | None,
    reproduction: str | None,
    diagnostics: FeedbackDiagnostics | None,
    contact_available: bool,
) -> str:
    """Render the exact cleaned body reviewed locally and published on GitHub."""

    cleaned_diagnostics = None if diagnostics is None else clean_diagnostics_for_send(diagnostics)
    sections = [("Report", _safe_public_text(description))]
    if expected is not None:
        sections.append(("Expected result", _safe_public_text(expected)))
    if reproduction is not None:
        sections.append(("Reproduction steps", _safe_public_text(reproduction)))
    if cleaned_diagnostics is not None:
        sections.append(("Diagnostics", _safe_public_text(render_diagnostics(cleaned_diagnostics))))

    context = ["Submitted through Discord Ferry."]
    if cleaned_diagnostics is not None:
        context.extend(
            (
                f"Ferry version: {cleaned_diagnostics.ferry_version}",
                f"Interface: {cleaned_diagnostics.interface.value}",
            )
        )
    sections.append(("Ferry context", "\n".join(context)))

    receipt = [
        f"<!-- ferry-feedback:{request_id} -->",
        f"Receipt: `{request_id}`",
    ]
    if contact_available:
        receipt.append("Private contact is available to maintainers under this receipt.")
    sections.append(("Receipt", "\n".join(receipt)))
    return "\n\n".join(f"## {heading}\n{body}" for heading, body in sections)


@dataclass(frozen=True)
class Challenge:
    """Signed proof-of-work challenge carried by a feedback request."""

    challenge_version: Literal[1]
    challenge_id: UUID
    request_id: UUID
    nonce: str
    expires_at: datetime
    work_factor: Literal[18]
    signature: str
    counter: int

    @classmethod
    def from_mapping(cls, value: object) -> Challenge:
        data = _closed_mapping(
            "challenge",
            value,
            required=frozenset(
                {
                    "challenge_version",
                    "challenge_id",
                    "request_id",
                    "nonce",
                    "expires_at",
                    "work_factor",
                    "signature",
                    "counter",
                }
            ),
        )
        if data["challenge_version"] != 1:
            raise FeedbackValidationError("challenge_version must be 1")
        if data["work_factor"] != 18:
            raise FeedbackValidationError("work_factor must be 18")
        counter = data["counter"]
        if (
            isinstance(counter, bool)
            or not isinstance(counter, int)
            or not 0 <= counter <= MAX_COUNTER
        ):
            raise FeedbackValidationError("counter is outside the supported range")
        return cls(
            challenge_version=1,
            challenge_id=_uuid_value("challenge_id", data["challenge_id"]),
            request_id=_uuid_value("challenge.request_id", data["request_id"]),
            nonce=_base64url_value("nonce", data["nonce"], decoded_bytes=32),
            expires_at=_datetime_value("expires_at", data["expires_at"]),
            work_factor=18,
            signature=_base64url_value("signature", data["signature"], decoded_bytes=32),
            counter=counter,
        )

    @classmethod
    def from_response_mapping(cls, value: object) -> Challenge:
        """Parse the closed server response before a counter has been solved."""

        data = _closed_mapping(
            "challenge response",
            value,
            required=frozenset(
                {
                    "challenge_version",
                    "challenge_id",
                    "request_id",
                    "nonce",
                    "expires_at",
                    "work_factor",
                    "signature",
                }
            ),
        )
        if data["challenge_version"] != 1:
            raise FeedbackValidationError("challenge_version must be 1")
        if data["work_factor"] != 18:
            raise FeedbackValidationError("work_factor must be 18")
        return cls(
            challenge_version=1,
            challenge_id=_uuid_value("challenge_id", data["challenge_id"]),
            request_id=_uuid_value("challenge.request_id", data["request_id"]),
            nonce=_base64url_value("nonce", data["nonce"], decoded_bytes=32),
            expires_at=_datetime_value("expires_at", data["expires_at"]),
            work_factor=18,
            signature=_base64url_value("signature", data["signature"], decoded_bytes=32),
            counter=0,
        )

    def response_mapping(self) -> dict[str, object]:
        """Return the challenge members signed by the service."""

        return {
            "challenge_version": self.challenge_version,
            "challenge_id": str(self.challenge_id),
            "request_id": str(self.request_id),
            "nonce": self.nonce,
            "expires_at": self.expires_at.isoformat().replace("+00:00", "Z"),
            "work_factor": self.work_factor,
            "signature": self.signature,
        }

    def to_mapping(self) -> dict[str, object]:
        return {
            "challenge_version": self.challenge_version,
            "challenge_id": str(self.challenge_id),
            "request_id": str(self.request_id),
            "nonce": self.nonce,
            "expires_at": self.expires_at.isoformat().replace("+00:00", "Z"),
            "work_factor": self.work_factor,
            "signature": self.signature,
            "counter": self.counter,
        }


@dataclass(frozen=True)
class FeedbackRequest:
    """Closed version 1 request accepted by the feedback service."""

    contract_version: Literal[1]
    request_id: UUID
    kind: FeedbackKind
    description: str
    expected: str | None
    reproduction: str | None
    diagnostics: FeedbackDiagnostics | None
    contact_email: str | None
    public_acknowledged: Literal[True]
    diagnostics_acknowledged: bool
    challenge: Challenge

    @classmethod
    def from_mapping(cls, value: object) -> FeedbackRequest:
        data = _closed_mapping(
            "feedback request",
            value,
            required=frozenset(
                {
                    "contract_version",
                    "request_id",
                    "kind",
                    "description",
                    "public_acknowledged",
                    "diagnostics_acknowledged",
                    "challenge",
                }
            ),
            optional=frozenset({"expected", "reproduction", "diagnostics", "contact_email"}),
        )
        if data["contract_version"] != 1:
            raise FeedbackValidationError("contract_version must be 1")
        if data["public_acknowledged"] is not True:
            raise FeedbackValidationError("public_acknowledged must be true")
        diagnostics_value = data.get("diagnostics")
        diagnostics = (
            None
            if diagnostics_value is None
            else FeedbackDiagnostics.from_mapping(diagnostics_value)
        )
        diagnostics_acknowledged = data["diagnostics_acknowledged"]
        if not isinstance(diagnostics_acknowledged, bool):
            raise FeedbackValidationError("diagnostics_acknowledged must be a boolean")
        if diagnostics is not None and not diagnostics_acknowledged:
            raise FeedbackValidationError(
                "diagnostics_acknowledged must be true when diagnostics are included"
            )
        request_id = _uuid_value("request_id", data["request_id"])
        challenge = Challenge.from_mapping(data["challenge"])
        if challenge.request_id != request_id:
            raise FeedbackValidationError("challenge request_id does not match request_id")
        return cls(
            contract_version=1,
            request_id=request_id,
            kind=_enum_value("kind", FeedbackKind, data["kind"]),
            description=normalize_text(
                "description", data["description"], minimum=1, maximum=8_000
            ),
            expected=_optional_text("expected", data.get("expected"), maximum=4_000),
            reproduction=_optional_text("reproduction", data.get("reproduction"), maximum=4_000),
            diagnostics=diagnostics,
            contact_email=_contact_email(data.get("contact_email")),
            public_acknowledged=True,
            diagnostics_acknowledged=diagnostics_acknowledged,
            challenge=challenge,
        )

    def to_mapping(self) -> dict[str, object]:
        result: dict[str, object] = {
            "contract_version": self.contract_version,
            "request_id": str(self.request_id),
            "kind": self.kind.value,
            "description": self.description,
            "public_acknowledged": self.public_acknowledged,
            "diagnostics_acknowledged": self.diagnostics_acknowledged,
            "challenge": self.challenge.to_mapping(),
        }
        if self.expected is not None:
            result["expected"] = self.expected
        if self.reproduction is not None:
            result["reproduction"] = self.reproduction
        if self.diagnostics is not None:
            result["diagnostics"] = self.diagnostics.to_mapping()
        if self.contact_email is not None:
            result["contact_email"] = self.contact_email
        return result

    def content_for_hash(self) -> dict[str, object]:
        """Return parsed public and private content without replaceable identity data."""

        return {
            "contract_version": self.contract_version,
            "kind": self.kind.value,
            "description": self.description,
            "expected": self.expected,
            "reproduction": self.reproduction,
            "diagnostics": None if self.diagnostics is None else self.diagnostics.to_mapping(),
            "contact_email": self.contact_email,
        }

    def cleaned_for_send(self) -> FeedbackRequest:
        """Run the final diagnostic cleaner immediately before encoding."""

        if self.diagnostics is None:
            return self
        return replace(self, diagnostics=clean_diagnostics_for_send(self.diagnostics))


def canonical_json(value: object) -> bytes:
    """Encode a value as deterministic compact UTF-8 JSON."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def feedback_content_hash(request: FeedbackRequest) -> str:
    """Hash the parsed report content used for duplicate protection."""

    return hashlib.sha256(canonical_json(request.content_for_hash())).hexdigest()


@dataclass(frozen=True)
class FeedbackReceipt:
    """Accepted feedback destination returned to Ferry."""

    receipt: UUID
    destination_kind: DestinationKind
    url: str

    @classmethod
    def from_mapping(cls, value: object) -> FeedbackReceipt:
        data = _closed_mapping(
            "feedback receipt",
            value,
            required=frozenset({"receipt", "destination_kind", "url"}),
        )
        url = data["url"]
        if not isinstance(url, str):
            raise FeedbackValidationError("url must be a string")
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != "github.com":
            raise FeedbackValidationError("url must be an HTTPS GitHub URL")
        return cls(
            receipt=_uuid_value("receipt", data["receipt"]),
            destination_kind=_enum_value(
                "destination_kind", DestinationKind, data["destination_kind"]
            ),
            url=url,
        )


@dataclass(frozen=True)
class FeedbackError:
    """Typed service error returned without feedback or credential content."""

    code: FeedbackErrorCode
    message: str
    retry_at: datetime | None = None

    @classmethod
    def from_mapping(cls, value: object) -> FeedbackError:
        data = _closed_mapping(
            "feedback error",
            value,
            required=frozenset({"code", "message"}),
            optional=frozenset({"retry_at"}),
        )
        retry_at_value = data.get("retry_at")
        return cls(
            code=_enum_value("code", FeedbackErrorCode, data["code"]),
            message=normalize_text("message", data["message"], minimum=1, maximum=500),
            retry_at=(
                None if retry_at_value is None else _datetime_value("retry_at", retry_at_value)
            ),
        )


class FeedbackServiceError(RuntimeError):
    """Typed error response returned by the feedback service."""

    def __init__(self, status: int, error: FeedbackError) -> None:
        super().__init__(error.message)
        self.status = status
        self.error = error


class _DraftForSubmission(Protocol):
    request_id: UUID

    def to_request(self, challenge: Challenge) -> FeedbackRequest: ...


@dataclass
class FeedbackDraft:
    """Editable in-memory report retained across an explicit retry."""

    kind: FeedbackKind
    description: str
    expected: str | None = None
    reproduction: str | None = None
    diagnostics: FeedbackDiagnostics | None = None
    contact_email: str | None = None
    request_id: UUID = field(default_factory=uuid4)
    public_acknowledged: bool = field(default=False, init=False)
    diagnostics_acknowledged: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self.description = normalize_text("description", self.description, minimum=1, maximum=8_000)
        self.expected = _optional_text("expected", self.expected, maximum=4_000)
        self.reproduction = _optional_text("reproduction", self.reproduction, maximum=4_000)
        self.contact_email = _contact_email(self.contact_email)

    def acknowledge_public(self) -> None:
        self.public_acknowledged = True

    def acknowledge_diagnostics(self) -> None:
        if self.diagnostics is None:
            raise FeedbackValidationError("no diagnostics are available to acknowledge")
        self.diagnostics_acknowledged = True

    def edit_kind(self, kind: FeedbackKind) -> None:
        self.kind = kind
        self.public_acknowledged = False

    def edit_description(self, description: str) -> None:
        self.description = normalize_text("description", description, minimum=1, maximum=8_000)
        self.public_acknowledged = False

    def edit_expected(self, expected: str | None) -> None:
        self.expected = _optional_text("expected", expected, maximum=4_000)
        self.public_acknowledged = False

    def edit_reproduction(self, reproduction: str | None) -> None:
        self.reproduction = _optional_text("reproduction", reproduction, maximum=4_000)
        self.public_acknowledged = False

    def edit_diagnostics(self, diagnostics: FeedbackDiagnostics | None) -> None:
        self.diagnostics = diagnostics
        self.public_acknowledged = False
        self.diagnostics_acknowledged = False

    def edit_contact_email(self, contact_email: str | None) -> None:
        self.contact_email = _contact_email(contact_email)

    def render_public_body(self) -> str:
        return render_public_feedback_body(
            request_id=self.request_id,
            description=self.description,
            expected=self.expected,
            reproduction=self.reproduction,
            diagnostics=self.diagnostics,
            contact_available=self.contact_email is not None,
        )

    def copy_text(self) -> str:
        return self.render_public_body()

    def to_request(self, challenge: Challenge) -> FeedbackRequest:
        data: dict[str, object] = {
            "contract_version": 1,
            "request_id": str(self.request_id),
            "kind": self.kind.value,
            "description": self.description,
            "public_acknowledged": self.public_acknowledged,
            "diagnostics_acknowledged": self.diagnostics_acknowledged,
            "challenge": challenge.to_mapping(),
        }
        if self.expected is not None:
            data["expected"] = self.expected
        if self.reproduction is not None:
            data["reproduction"] = self.reproduction
        if self.diagnostics is not None:
            data["diagnostics"] = self.diagnostics.to_mapping()
        if self.contact_email is not None:
            data["contact_email"] = self.contact_email
        return FeedbackRequest.from_mapping(data)

    def save(self, path: Path, *, include_contact: bool = False) -> None:
        text = self.render_public_body()
        if include_contact and self.contact_email is not None:
            text = f"{text}\n\n## Private contact\n{self.contact_email}"
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as draft_file:
                draft_file.write(text)
                draft_file.flush()
                os.fsync(draft_file.fileno())
            temporary.chmod(0o600)
            temporary.replace(path)
            path.chmod(0o600)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise


def challenge_proof_input(challenge: Challenge, counter: int) -> bytes:
    """Build the exact bytes covered by a proof-of-work counter."""

    if isinstance(counter, bool) or not isinstance(counter, int) or not 0 <= counter <= MAX_COUNTER:
        raise FeedbackValidationError("counter is outside the supported range")
    return canonical_json(challenge.response_mapping()) + b"\n" + str(counter).encode("ascii")


def solve_challenge(challenge: Challenge) -> Challenge:
    """Find the first counter whose SHA-256 digest starts with 18 zero bits."""

    prefix = canonical_json(challenge.response_mapping()) + b"\n"
    for counter in range(MAX_COUNTER + 1):
        digest = hashlib.sha256(prefix + str(counter).encode("ascii")).digest()
        if digest[0:2] == b"\x00\x00" and digest[2] & 0b1100_0000 == 0:
            return replace(challenge, counter=counter)
    raise FeedbackValidationError("no supported challenge counter was found")


class FeedbackClient:
    """Account-free feedback service client with one non-retried write."""

    def __init__(
        self,
        base_url: str = "https://feedback.nordscope.fi",
        *,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or parsed.hostname is None:
            raise ValueError("feedback service URL must use HTTPS")
        self._base_url = base_url.rstrip("/")
        self._session = session
        self._owns_session = session is None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("FeedbackClient must be entered before use")
        return self._session

    async def __aenter__(self) -> FeedbackClient:
        if self._session is None:
            self._session = new_session(timeout=aiohttp.ClientTimeout(connect=5, total=20))
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()

    async def _response_data(self, response: aiohttp.ClientResponse) -> object:
        try:
            return await response.json(content_type=None)
        except (aiohttp.ContentTypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise FeedbackValidationError("feedback service returned invalid JSON") from exc

    async def _raise_service_error(self, response: aiohttp.ClientResponse) -> None:
        try:
            error = FeedbackError.from_mapping(await self._response_data(response))
        except FeedbackValidationError:
            error = FeedbackError(
                code=FeedbackErrorCode.TEMPORARY_FAILURE,
                message=f"Feedback service returned HTTP {response.status}",
            )
        raise FeedbackServiceError(response.status, error)

    async def create_challenge(self, request_id: UUID) -> Challenge:
        async with self.session.post(
            f"{self._base_url}/v1/challenge",
            json={"contract_version": 1, "request_id": str(request_id)},
        ) as response:
            if not 200 <= response.status < 300:
                await self._raise_service_error(response)
            challenge = Challenge.from_response_mapping(await self._response_data(response))
        if challenge.request_id != request_id:
            raise FeedbackValidationError("challenge request_id does not match request_id")
        return challenge

    async def _post_once(self, request: FeedbackRequest) -> FeedbackReceipt:
        async with self.session.post(
            f"{self._base_url}/v1/feedback",
            json=request.to_mapping(),
        ) as response:
            if not 200 <= response.status < 300:
                await self._raise_service_error(response)
            return FeedbackReceipt.from_mapping(await self._response_data(response))

    async def submit(self, draft: _DraftForSubmission) -> FeedbackReceipt:
        challenge = await self.create_challenge(draft.request_id)
        solved = await asyncio.to_thread(solve_challenge, challenge)
        request = draft.to_request(solved).cleaned_for_send()
        return await self._post_once(request)


__all__ = [
    "Architecture",
    "Challenge",
    "DestinationKind",
    "FeedbackDiagnostics",
    "FeedbackDraft",
    "FeedbackError",
    "FeedbackErrorCode",
    "FeedbackClient",
    "FeedbackInterface",
    "FeedbackKind",
    "FeedbackReceipt",
    "FeedbackRequest",
    "FeedbackStage",
    "FeedbackValidationError",
    "FeedbackServiceError",
    "OperatingSystem",
    "RuntimeContext",
    "build_diagnostics",
    "canonical_json",
    "challenge_proof_input",
    "clean_diagnostics_for_send",
    "feedback_content_hash",
    "normalize_text",
    "render_diagnostics",
    "render_public_feedback_body",
    "solve_challenge",
]
