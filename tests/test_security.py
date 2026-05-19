"""Tests for SecureTokenStore and sanitize_for_display."""

from __future__ import annotations

import pytest

from discord_ferry.core.security import SecureTokenStore, safe_sanitize, sanitize_for_display

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_store(**tokens: str) -> SecureTokenStore:
    return SecureTokenStore(tokens)


# ---------------------------------------------------------------------------
# masked()
# ---------------------------------------------------------------------------


def test_masked_long_token_shows_last_four() -> None:
    store = make_store(tok="abcde12345")
    assert store.masked("tok") == "****2345"


def test_masked_exactly_five_chars() -> None:
    store = make_store(tok="abcde")
    assert store.masked("tok") == "****bcde"


def test_masked_short_token_returns_stars_only() -> None:
    store = make_store(tok="abc")
    assert store.masked("tok") == "****"


def test_masked_single_char_token() -> None:
    store = make_store(tok="x")
    assert store.masked("tok") == "****"


def test_masked_missing_key_raises_key_error() -> None:
    store = make_store(tok="abcde12345")
    with pytest.raises(KeyError):
        store.masked("nonexistent")


# ---------------------------------------------------------------------------
# get()
# ---------------------------------------------------------------------------


def test_get_returns_raw_value() -> None:
    store = make_store(stoat="my-secret-token")
    assert store.get("stoat") == "my-secret-token"


def test_get_missing_key_raises_key_error() -> None:
    store = make_store(tok="val")
    with pytest.raises(KeyError):
        store.get("missing")


# ---------------------------------------------------------------------------
# sanitize()
# ---------------------------------------------------------------------------


def test_sanitize_replaces_token_in_text() -> None:
    store = make_store(stoat="supersecret1234")
    result = store.sanitize("Error: token supersecret1234 was rejected")
    assert "supersecret1234" not in result
    assert "****1234" in result


def test_sanitize_replaces_multiple_tokens() -> None:
    store = make_store(stoat="tok_stoat_abc", discord="tok_discord_xyz")
    text = "stoat=tok_stoat_abc discord=tok_discord_xyz"
    result = store.sanitize(text)
    assert "tok_stoat_abc" not in result
    assert "tok_discord_xyz" not in result


def test_sanitize_skips_empty_string_tokens() -> None:
    """Empty tokens must not cause all empty-string matches to be replaced."""
    store = make_store(tok="", real="abc12345")
    text = "nothing special here"
    result = store.sanitize(text)
    # Text must be unchanged aside from the non-empty token (which isn't present)
    assert result == text


def test_sanitize_no_op_when_token_absent() -> None:
    store = make_store(tok="not_in_text_xyz")
    text = "clean log line"
    assert store.sanitize(text) == text


def test_sanitize_multiple_occurrences() -> None:
    store = make_store(tok="secret99")
    text = "secret99 and also secret99 again"
    result = store.sanitize(text)
    assert "secret99" not in result
    assert result.count("****") == 2


# ---------------------------------------------------------------------------
# __repr__ safety
# ---------------------------------------------------------------------------


def test_repr_does_not_contain_token_values() -> None:
    store = make_store(stoat="supersecret", discord="anothersecret")
    r = repr(store)
    assert "supersecret" not in r
    assert "anothersecret" not in r


def test_repr_contains_key_names() -> None:
    store = make_store(stoat="val1", discord="val2")
    r = repr(store)
    assert "stoat" in r
    assert "discord" in r


def test_repr_class_name() -> None:
    store = make_store(tok="val")
    assert repr(store).startswith("SecureTokenStore(")


# ---------------------------------------------------------------------------
# Caller cannot mutate internal state
# ---------------------------------------------------------------------------


def test_mutations_to_source_dict_do_not_affect_store() -> None:
    tokens: dict[str, str] = {"tok": "original"}
    store = SecureTokenStore(tokens)
    tokens["tok"] = "mutated"
    assert store.get("tok") == "original"


# ---------------------------------------------------------------------------
# sanitize_for_display convenience wrapper
# ---------------------------------------------------------------------------


def test_sanitize_for_display_delegates_to_store() -> None:
    store = make_store(tok="mytoken12345")
    text = "token is mytoken12345 here"
    result = sanitize_for_display(text, store)
    assert "mytoken12345" not in result
    assert "****2345" in result


# ---------------------------------------------------------------------------
# safe_sanitize None-tolerant wrapper (used by state.errors.append callsites)
# ---------------------------------------------------------------------------


def test_safe_sanitize_returns_text_unchanged_when_store_is_none() -> None:
    """Test paths construct FerryConfig without a token_store; helper must no-op."""
    assert safe_sanitize(None, "anything goes here") == "anything goes here"


def test_safe_sanitize_masks_registered_tokens() -> None:
    store = make_store(stoat="stoat_abc12345", discord="disc_xyz98765")
    text = "stoat_abc12345 leaked alongside disc_xyz98765"
    result = safe_sanitize(store, text)
    assert "stoat_abc12345" not in result
    assert "disc_xyz98765" not in result
    assert "****2345" in result
    assert "****8765" in result


def test_safe_sanitize_at_state_errors_call_site() -> None:
    """Regression for #47: a sanitized exception repr does not leak the raw token.

    Mirrors the wrapping pattern used in migrator/{emoji,reactions,pins,messages}.py
    and core/engine.py. If a Stoat/Discord/Autumn aiohttp exception's repr() ever
    contains a token (e.g. token-in-URL leak from a misconfigured HTTP library),
    the helper masks it before it lands in state.errors.
    """
    store = make_store(stoat="stoat_secret_token_value")
    state_errors: list[dict[str, str]] = []
    try:
        raise RuntimeError("401 at https://api.test/?token=stoat_secret_token_value")
    except Exception as exc:  # noqa: BLE001
        state_errors.append(
            {
                "phase": "messages",
                "type": "phase_failed",
                "message": safe_sanitize(store, str(exc)),
            }
        )
    last = state_errors[-1]["message"]
    assert "stoat_secret_token_value" not in last
    assert "****alue" in last
