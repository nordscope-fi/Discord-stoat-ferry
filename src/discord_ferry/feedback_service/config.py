"""Closed startup configuration for the public feedback service."""

from __future__ import annotations

import base64
import binascii
import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, TypeAlias

if TYPE_CHECKING:
    from collections.abc import Mapping

_REPOSITORY = "nordscope-fi/Discord-stoat-ferry"
_DATA_ROOT = Path("/data")
_REQUIRED_ENV = (
    "FERRY_FEEDBACK_REPOSITORY",
    "FERRY_FEEDBACK_GITHUB_APP_ID",
    "FERRY_FEEDBACK_GITHUB_INSTALLATION_ID",
    "FERRY_FEEDBACK_GITHUB_PRIVATE_KEY",
    "FERRY_FEEDBACK_DATABASE_PATH",
    "FERRY_FEEDBACK_CHALLENGE_KEY",
    "FERRY_FEEDBACK_SOURCE_HASH_KEY",
    "FERRY_FEEDBACK_CONTACT_KEY",
    "FERRY_FEEDBACK_TRUSTED_PROXY_NETWORKS",
)
Network: TypeAlias = ipaddress.IPv4Network | ipaddress.IPv6Network


class ConfigError(ValueError):
    """Raised when startup configuration is absent or unsafe."""


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name)
    if value is None or not value.strip():
        raise ConfigError(f"missing required environment variable: {name}")
    return value


def _positive_id(env: Mapping[str, str], name: str) -> int:
    value = _required(env, name)
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a positive integer") from exc
    if parsed <= 0:
        raise ConfigError(f"{name} must be a positive integer")
    return parsed


def _decoded_key(env: Mapping[str, str], name: str) -> bytes:
    value = _required(env, name)
    try:
        decoded = base64.b64decode(value, altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ConfigError(f"{name} must be base64 for exactly 32 bytes") from exc
    if len(decoded) != 32:
        raise ConfigError(f"{name} must be base64 for exactly 32 bytes")
    return decoded


def _database_path(env: Mapping[str, str]) -> Path:
    name = "FERRY_FEEDBACK_DATABASE_PATH"
    value = _required(env, name)
    path = Path(value)
    try:
        resolved = path.resolve(strict=False)
        resolved.relative_to(_DATA_ROOT.resolve(strict=False))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{name} must name a file inside /data") from exc
    if resolved == _DATA_ROOT.resolve(strict=False):
        raise ConfigError(f"{name} must name a file inside /data")
    return resolved


def _trusted_networks(env: Mapping[str, str]) -> tuple[Network, ...]:
    name = "FERRY_FEEDBACK_TRUSTED_PROXY_NETWORKS"
    value = _required(env, name)
    networks: list[Network] = []
    try:
        for member in value.split(","):
            if not member.strip():
                raise ValueError
            networks.append(ipaddress.ip_network(member.strip(), strict=True))
    except ValueError as exc:
        raise ConfigError(f"{name} must contain canonical IP networks") from exc
    if not networks:
        raise ConfigError(f"{name} must contain at least one IP network")
    return tuple(networks)


@dataclass(frozen=True)
class ServiceConfig:
    """Validated values needed before the public listener can start."""

    repository: str
    github_app_id: int
    github_installation_id: int
    github_private_key: str = field(repr=False)
    database_path: Path
    challenge_key: bytes = field(repr=False)
    source_hash_key: bytes = field(repr=False)
    contact_key: bytes = field(repr=False)
    trusted_proxy_networks: tuple[Network, ...]
    issue_labels: tuple[str, str] = field(default=("bug", "triage"), init=False)
    idea_category: str = field(default="Ideas", init=False)
    general_category: str = field(default="General", init=False)
    max_request_bytes: int = field(default=32 * 1024, init=False)
    challenge_expiry_seconds: int = field(default=15 * 60, init=False)
    challenge_work_factor: int = field(default=18, init=False)
    challenge_limit_per_hour: int = field(default=30, init=False)
    report_limit_per_hour: int = field(default=3, init=False)
    report_limit_per_day: int = field(default=10, init=False)
    total_report_limit_per_hour: int = field(default=60, init=False)
    receipt_retention_seconds: int = field(default=7 * 24 * 60 * 60, init=False)
    rate_retention_seconds: int = field(default=24 * 60 * 60, init=False)
    contact_retention_seconds: int = field(default=30 * 24 * 60 * 60, init=False)
    github_timeout_seconds: int = field(default=20, init=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ServiceConfig:
        source = os.environ if env is None else env
        for name in _REQUIRED_ENV:
            _required(source, name)

        repository = _required(source, "FERRY_FEEDBACK_REPOSITORY")
        if repository != _REPOSITORY:
            raise ConfigError("FERRY_FEEDBACK_REPOSITORY must name the Ferry repository")

        private_key = _required(source, "FERRY_FEEDBACK_GITHUB_PRIVATE_KEY")
        private_key_envelopes = (
            ("-----BEGIN PRIVATE KEY-----", "-----END PRIVATE KEY-----"),
            ("-----BEGIN RSA PRIVATE KEY-----", "-----END RSA PRIVATE KEY-----"),
        )
        normalized_private_key = private_key.rstrip()
        if not any(
            normalized_private_key.startswith(header) and normalized_private_key.endswith(footer)
            for header, footer in private_key_envelopes
        ):
            raise ConfigError("FERRY_FEEDBACK_GITHUB_PRIVATE_KEY must contain a private PEM key")

        challenge_key = _decoded_key(source, "FERRY_FEEDBACK_CHALLENGE_KEY")
        source_hash_key = _decoded_key(source, "FERRY_FEEDBACK_SOURCE_HASH_KEY")
        contact_key = _decoded_key(source, "FERRY_FEEDBACK_CONTACT_KEY")
        if len({challenge_key, source_hash_key, contact_key}) != 3:
            raise ConfigError("feedback service keys must be independent")

        return cls(
            repository=repository,
            github_app_id=_positive_id(source, "FERRY_FEEDBACK_GITHUB_APP_ID"),
            github_installation_id=_positive_id(source, "FERRY_FEEDBACK_GITHUB_INSTALLATION_ID"),
            github_private_key=private_key,
            database_path=_database_path(source),
            challenge_key=challenge_key,
            source_hash_key=source_hash_key,
            contact_key=contact_key,
            trusted_proxy_networks=_trusted_networks(source),
        )


__all__ = ["ConfigError", "ServiceConfig"]
