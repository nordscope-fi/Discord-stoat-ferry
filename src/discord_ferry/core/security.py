"""Secure token storage and sanitization utilities."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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


# ---------------------------------------------------------------------------
# Document redaction (issue #140, ADR-014)
# ---------------------------------------------------------------------------
#
# ``report.json`` and ``state.json`` are redacted where they are serialised, not at
# the ~59 ``state.warnings`` / ``state.errors`` append sites that feed them. A call
# site cannot forget a scrub it never has to make, and forgetting was the defect:
# four migrator modules called ``safe_sanitize`` and two did not.
#
# The map below is enumerated from every append site in ``src/``, not sampled from a
# representative one. ``errors`` uses BOTH member keys: ``message`` at four sites and
# ``error`` at ``core/engine.py``'s catch-all phase handler, which is the single most
# general error capture in the codebase.

# Fields whose entries are dicts carrying free text, and which members hold it.
_TEXT_MEMBERS: dict[str, frozenset[str]] = {
    "warnings": frozenset({"message"}),
    "errors": frozenset({"message", "error"}),
    "failed_messages": frozenset({"error", "content_preview"}),
}

# ``rollback_progress`` serialises as a dict rather than a list, so it is walked
# separately. Its ``failures`` entries come from the RollbackFailure dataclass.
_ROLLBACK_TEXT_MEMBERS: frozenset[str] = frozenset({"error"})


def scrub_document(
    doc: dict[str, Any], token_store: SecureTokenStore | None = None
) -> dict[str, Any]:
    """Return a copy of *doc* with free-text fields masked and structure untouched.

    Applies the process-wide registry and, when supplied, the per-config store. The
    two hold different secrets and neither is a superset of the other: the registry
    carries the proxy password and proxy authorization header registered by
    ``core/http.py``, while the config store carries the Stoat and Discord tokens the
    engine built it from.

    **Never widen this to walk the whole document.** ``SecureTokenStore.sanitize``
    does unbounded substring replacement, so a short human-chosen proxy password
    rewrites any identifier containing it: ``"12345678"`` turns the snowflake
    ``"1123456789012345678"`` into ``"1****34567890****345678"``. A rewritten
    ``channel_message_offsets`` value is a silently wrong resume position, which is a
    worse outcome than the leak this guards against. ADR-014 carries the measurement.

    The input is not mutated, so ``state.warnings`` keeps its raw text in memory and
    every existing assertion against it still holds.
    """

    def _mask(text: str) -> str:
        return sanitize_secrets(safe_sanitize(token_store, text))

    def _mask_entries(entries: object, members: frozenset[str]) -> object:
        if not isinstance(entries, list):
            return entries
        return [
            {
                key: _mask(value) if key in members and isinstance(value, str) else value
                for key, value in entry.items()
            }
            if isinstance(entry, dict)
            else entry
            for entry in entries
        ]

    out: dict[str, Any] = dict(doc)

    for field_name, members in _TEXT_MEMBERS.items():
        if field_name in out:
            out[field_name] = _mask_entries(out[field_name], members)

    rollback = out.get("rollback_progress")
    if isinstance(rollback, dict):
        out["rollback_progress"] = {
            **rollback,
            "failures": _mask_entries(rollback.get("failures"), _ROLLBACK_TEXT_MEMBERS),
        }

    guild = out.get("source_guild")
    if isinstance(guild, dict) and isinstance(guild.get("name"), str):
        out["source_guild"] = {**guild, "name": _mask(guild["name"])}

    return out
