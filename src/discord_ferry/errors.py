"""Custom exceptions for Discord Ferry."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from discord_ferry.core.permissions import PermissionOutcome


class FerryError(Exception):
    """Base exception for all ferry errors."""


class ValidationError(FerryError):
    """Export validation failed."""


class BlueprintValidationError(FerryError, ValueError):
    """Invalid blueprint cap or explicitly selected metadata."""


class CheckError(FerryError):
    """A finished migration cannot be checked against a live server.

    Distinct from :class:`ValidationError`, which is about a DiscordChatExporter
    export being unusable as INPUT. This is about a recorded migration being
    unusable as a SUBJECT: a dry-run state, or one that never reached the
    structure phase and so records no server.
    """


class StoatConnectionError(FerryError):
    """Stoat API connection failed."""


class AutumnUploadError(FerryError):
    """File upload to Autumn failed."""


class MigrationError(FerryError):
    """Error during migration phase."""


class PermissionSafetyError(MigrationError):
    """Preserve the permission outcome without exposing diagnostic text."""

    def __init__(self, outcome: PermissionOutcome) -> None:
        self.outcome = outcome
        super().__init__(
            f"Permission safety stop: {outcome.reason} during {outcome.stage}; "
            f"destination effects: {outcome.effects}"
        )


class DuplicateSendError(MigrationError):
    """Stoat rejected a send because its Idempotency-Key was still cached.

    Cached consumption does not prove message creation. Stoat does not return a
    message identifier with this response. Subclasses MigrationError so existing
    callers retain their exception handling.
    """


class MessageSendFailure(MigrationError):  # noqa: N818 - Approved structured-failure contract.
    """Safe transport provenance for an opted-in message send."""

    def __init__(
        self,
        message: str,
        *,
        disposition: Literal["confirmed_rejected", "uncertain"],
        http_status: int | None = None,
        error_type: str | None = None,
    ) -> None:
        super().__init__(message)
        self.disposition = disposition
        self.http_status = http_status
        self.error_type = error_type


class DeliveryUnresolvedError(MigrationError):
    """An uncertain delivery remains preserved rather than being replayed."""


class StateError(FerryError):
    """State file read/write error."""


class ExportError(MigrationError):
    """Error during DCE export phase."""


class DCENotFoundError(ExportError):
    """DCE binary not found and download failed."""


class DotNetMissingError(ExportError):
    """Required .NET runtime not detected."""


class DiscordAuthError(ExportError):
    """Discord token validation failed."""
