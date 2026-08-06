"""Secure token storage and sanitization utilities."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SecureTokenStore:
    """Wraps tokens in a memory-only container.

    Token values are never included in repr output.  Use ``get()`` to
    retrieve a raw value for API calls and ``masked()`` for display.
    """

    _tokens: dict[str, str] = field(repr=False, default_factory=dict)

    def __init__(self, tokens: dict[str, str]) -> None:
        # Store a copy so the caller cannot mutate our internal state.
        object.__setattr__(self, "_tokens", dict(tokens))

    def get(self, name: str) -> str:
        """Return the raw token value for *name*.

        Raises ``KeyError`` if the name is not found.
        """
        return self._tokens[name]

    def register(self, name: str, value: str) -> None:
        """Add or replace a token value.

        Used by the process-wide registry below, which is populated as tokens
        arrive rather than all at once in ``__init__``.
        """
        self._tokens[name] = value

    def clear(self) -> None:
        """Drop every stored token."""
        self._tokens.clear()

    def masked(self, name: str) -> str:
        """Return a masked representation of the token for display.

        Returns ``"****{last4}"`` when the token is 5+ characters, or
        ``"****"`` for shorter tokens.
        """
        value = self._tokens[name]
        if len(value) >= 5:
            return f"****{value[-4:]}"
        return "****"

    def sanitize(self, text: str) -> str:
        """Strip all known token values from *text*, replacing with masked versions.

        Empty-string tokens are skipped to avoid replacing every empty
        substring in the text.
        """
        result = text
        for name, value in self._tokens.items():
            if not value:
                continue
            masked = self.masked(name)
            result = result.replace(value, masked)
        return result

    def __repr__(self) -> str:
        """Show only key names — never values."""
        keys = list(self._tokens.keys())
        return f"SecureTokenStore(keys={keys!r})"


def sanitize_for_display(text: str, token_store: SecureTokenStore) -> str:
    """Convenience wrapper around :meth:`SecureTokenStore.sanitize`."""
    return token_store.sanitize(text)


def safe_sanitize(token_store: SecureTokenStore | None, text: str) -> str:
    """Sanitize *text*, returning it unchanged when *token_store* is ``None``.

    Use at every point where an error/warning message containing a raw
    exception ``repr()`` may be persisted (``state.errors``, event payloads,
    on-disk logs). The ``None`` branch keeps test paths working without
    requiring every test to construct a SecureTokenStore.
    """
    if token_store is None:
        return text
    return token_store.sanitize(text)


# ---------------------------------------------------------------------------
# Process-wide secret registry
# ---------------------------------------------------------------------------
#
# The log handler is installed in ``main()``, long before any token exists:
# ``FerryConfig.token_store`` is only built inside the engine
# (``core/engine.py::_ensure_token_store``), and the GUI keeps its tokens in
# NiceGUI's per-tab store. A redaction filter therefore cannot reach any
# per-config store, so it consults this module-level one instead, and every
# entry point registers its tokens here as they arrive.
#
# Module-level mutable state is an established pattern in this codebase --
# ``migrator/api.py`` holds ``_circuit_state`` the same way, with
# ``_reset_circuit_state()`` for tests. ``reset_secret_registry()`` is the
# counterpart here and MUST be wired into the test fixtures: without it,
# secrets registered by one test leak into the whole pytest session.

_secret_registry: SecureTokenStore = SecureTokenStore({})


def register_secret(name: str, value: str) -> None:
    """Record *value* so the logging redaction filter can mask it.

    Empty values are ignored -- ``SecureTokenStore.sanitize`` skips them anyway,
    but rejecting them here keeps the registry meaningful.

    Safe to call repeatedly with the same name; the last value wins.
    """
    if not value:
        return
    _secret_registry.register(name, value)


def sanitize_secrets(text: str) -> str:
    """Mask every registered secret in *text*.

    Returns *text* unchanged when nothing is registered yet -- callers that need
    a floor for that window must apply their own pattern-based redaction.
    """
    return _secret_registry.sanitize(text)


def reset_secret_registry() -> None:
    """Drop every registered secret. For tests only.

    Mirrors ``migrator.api._reset_circuit_state``.
    """
    _secret_registry.clear()
