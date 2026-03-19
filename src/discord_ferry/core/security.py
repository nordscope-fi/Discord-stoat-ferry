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
