"""Stateless signed proof-of-work challenges bound to one network source."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from discord_ferry.feedback import (
    MAX_COUNTER,
    Challenge,
    FeedbackErrorCode,
    canonical_json,
    challenge_proof_input,
)

_SOURCE_HASH = re.compile(r"^[0-9a-f]{64}$")
_CHALLENGE_LIFETIME = timedelta(minutes=15)


class ChallengeVerificationError(ValueError):
    """Stable service error for an invalid or expired challenge."""

    def __init__(self, code: FeedbackErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _validate_inputs(source_hash: str, key: bytes) -> None:
    if not _SOURCE_HASH.fullmatch(source_hash):
        raise ValueError("source_hash must be a lowercase SHA-256 digest")
    if len(key) != 32:
        raise ValueError("challenge key must contain exactly 32 bytes")


def challenge_signature_input(challenge: Challenge, source_hash: str) -> bytes:
    """Return canonical members authenticated by the service signature."""

    if not _SOURCE_HASH.fullmatch(source_hash):
        raise ValueError("source_hash must be a lowercase SHA-256 digest")
    members = challenge.response_mapping()
    members.pop("signature")
    members["source_hash"] = source_hash
    return canonical_json(members)


def create_challenge(
    request_id: UUID,
    source_hash: str,
    now: datetime,
    key: bytes,
) -> Challenge:
    """Create a signed challenge that expires after the released 15-minute window."""

    _validate_inputs(source_hash, key)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("challenge time must include a timezone")
    issued_at = now.astimezone(UTC).replace(microsecond=0)
    unsigned = Challenge(
        challenge_version=1,
        challenge_id=uuid4(),
        request_id=request_id,
        nonce=_base64url(secrets.token_bytes(32)),
        expires_at=issued_at + _CHALLENGE_LIFETIME,
        work_factor=18,
        signature=_base64url(bytes(32)),
        counter=0,
    )
    signature = hmac.new(
        key,
        challenge_signature_input(unsigned, source_hash),
        hashlib.sha256,
    ).digest()
    return Challenge(
        challenge_version=unsigned.challenge_version,
        challenge_id=unsigned.challenge_id,
        request_id=unsigned.request_id,
        nonce=unsigned.nonce,
        expires_at=unsigned.expires_at,
        work_factor=unsigned.work_factor,
        signature=_base64url(signature),
        counter=0,
    )


def verify_challenge(
    challenge: Challenge,
    *,
    request_id: UUID,
    source_hash: str,
    now: datetime,
    key: bytes,
) -> None:
    """Verify signature, binding, expiry, counter, and the 18-bit proof."""

    try:
        _validate_inputs(source_hash, key)
        signature_input = challenge_signature_input(challenge, source_hash)
    except ValueError as exc:
        raise ChallengeVerificationError(
            FeedbackErrorCode.INVALID_CHALLENGE,
            "challenge configuration is invalid",
        ) from exc
    expected = _base64url(hmac.new(key, signature_input, hashlib.sha256).digest())
    if not hmac.compare_digest(challenge.signature, expected):
        raise ChallengeVerificationError(
            FeedbackErrorCode.INVALID_CHALLENGE,
            "challenge signature is invalid",
        )
    if challenge.request_id != request_id:
        raise ChallengeVerificationError(
            FeedbackErrorCode.INVALID_CHALLENGE,
            "challenge request binding is invalid",
        )
    if now.tzinfo is None or now.utcoffset() is None:
        raise ChallengeVerificationError(
            FeedbackErrorCode.INVALID_CHALLENGE,
            "challenge verification time is invalid",
        )
    if challenge.expires_at.microsecond != 0 or challenge.expires_at.tzinfo is None:
        raise ChallengeVerificationError(
            FeedbackErrorCode.INVALID_CHALLENGE,
            "challenge expiry is invalid",
        )
    if now.astimezone(UTC) >= challenge.expires_at.astimezone(UTC):
        raise ChallengeVerificationError(
            FeedbackErrorCode.EXPIRED_CHALLENGE,
            "challenge has expired",
        )
    if (
        isinstance(challenge.counter, bool)
        or not isinstance(challenge.counter, int)
        or not 0 <= challenge.counter <= MAX_COUNTER
    ):
        raise ChallengeVerificationError(
            FeedbackErrorCode.INVALID_CHALLENGE,
            "challenge counter is outside the supported range",
        )
    digest = hashlib.sha256(challenge_proof_input(challenge, challenge.counter)).digest()
    if digest[0:2] != b"\x00\x00" or digest[2] & 0b1100_0000:
        raise ChallengeVerificationError(
            FeedbackErrorCode.INVALID_CHALLENGE,
            "challenge proof is invalid",
        )


__all__ = [
    "ChallengeVerificationError",
    "challenge_signature_input",
    "create_challenge",
    "verify_challenge",
]
