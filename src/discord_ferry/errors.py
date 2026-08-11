"""Custom exceptions for Discord Ferry."""


class FerryError(Exception):
    """Base exception for all ferry errors."""


class ValidationError(FerryError):
    """Export validation failed."""


class StoatConnectionError(FerryError):
    """Stoat API connection failed."""


class AutumnUploadError(FerryError):
    """File upload to Autumn failed."""


class MigrationError(FerryError):
    """Error during migration phase."""


class DuplicateSendError(MigrationError):
    """Stoat rejected a send because its Idempotency-Key was still cached.

    The message IS on the server. Stoat does not return it, so the caller has no
    Stoat message id. Subclasses MigrationError so a call site that does not catch
    this behaves exactly as it did before the class existed.
    """


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
