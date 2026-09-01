"""Tests for SecureTokenStore and sanitize_for_display."""

from __future__ import annotations

import pytest

from discord_ferry.core.security import (
    SecureTokenStore,
    contains_registered_secret,
    register_secret,
    safe_sanitize,
    sanitize_for_display,
    sanitize_secrets,
    sanitize_secrets_for_public_output,
    scrub_document,
)

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
# masked() per-key policy -- issue #149
# ---------------------------------------------------------------------------


def test_masked_fully_masked_key_returns_stars_only_for_long_value() -> None:
    """A proxy password is human-chosen and often short.

    The default ``****{last4}`` shape exposes a large fraction of it, so a key
    registered as fully masked renders as bare ``****`` regardless of length.
    """
    store = SecureTokenStore({})
    store.register("proxy_password", "hunter2horse", fully_mask=True)
    assert store.masked("proxy_password") == "****"


def test_masked_fully_masked_key_returns_stars_only_for_short_value() -> None:
    store = SecureTokenStore({})
    store.register("proxy_password", "x", fully_mask=True)
    assert store.masked("proxy_password") == "****"


def test_masked_default_register_keeps_last_four_tail() -> None:
    """Opaque tokens (Stoat/Discord) keep the existing tail shape."""
    store = SecureTokenStore({})
    store.register("stoat", "abcde12345")
    assert store.masked("stoat") == "****2345"


def test_clear_drops_the_fully_masked_policy_too() -> None:
    store = SecureTokenStore({})
    store.register("proxy_password", "hunter2horse", fully_mask=True)
    store.clear()
    store.register("proxy_password", "hunter2horse")
    assert store.masked("proxy_password") == "****orse"


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


def test_sanitize_fully_masked_key_replaces_with_stars_only() -> None:
    """A fully masked key replaces with bare ``****``, leaving no tail (#149)."""
    store = SecureTokenStore({})
    store.register("proxy_password", "hunter2horse", fully_mask=True)
    assert store.sanitize("failed: hunter2horse") == "failed: ****"


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


# ---------------------------------------------------------------------------
# scrub_document() -- issue #140, ADR-014
# ---------------------------------------------------------------------------


def test_scrub_document_masks_named_text_members() -> None:
    """warnings.message and errors.error both carry interpolated exception text."""
    register_secret("proxy_password", "hunter2horse")
    doc = {
        "warnings": [{"type": "x", "phase": "structure", "message": "failed for hunter2horse"}],
        "errors": [{"phase": "structure", "type": "phase_failed", "error": "boom hunter2horse"}],
    }

    out = scrub_document(doc)

    assert "hunter2horse" not in out["warnings"][0]["message"]
    assert "hunter2horse" not in out["errors"][0]["error"]
    assert out["warnings"][0]["type"] == "x"
    assert out["errors"][0]["phase"] == "structure"


def test_scrub_document_never_touches_identifiers() -> None:
    """SC-8.1. The case earlier revisions of the #140 design failed.

    A short human-chosen proxy password is a substring of real Discord snowflakes,
    and SecureTokenStore.sanitize does unbounded substring replacement. Scrubbing a
    whole document therefore rewrote identifiers; a rewritten channel_message_offsets
    value is a silently wrong resume position. This design must never touch them.
    """
    register_secret("proxy_password", "12345678")
    doc = {
        "role_map": {"1123456789012345678": "01ABCDEF"},
        "channel_message_offsets": {"7712345678901234567": "1123456789012345678"},
        "current_phase": "structure",
        "attachments_uploaded": 12345678,
    }

    assert scrub_document(doc) == doc


def test_scrub_document_masks_nested_dataclass_members() -> None:
    """failed_messages and rollback_progress.failures both carry exception text."""
    register_secret("proxy_password", "hunter2horse")
    doc = {
        "failed_messages": [
            {
                "discord_msg_id": "1123456789012345678",
                "error": "send failed: hunter2horse",
                "content_preview": "hi hunter2horse",
                "retry_count": 2,
            }
        ],
        "rollback_progress": {
            "channels_deleted": 3,
            "failures": [
                {
                    "entity_type": "channel",
                    "stoat_id": "01ABC",
                    "error": "delete failed: hunter2horse",
                    "http_status": 500,
                }
            ],
        },
    }

    out = scrub_document(doc)

    failed = out["failed_messages"][0]
    assert "hunter2horse" not in failed["error"]
    assert "hunter2horse" not in failed["content_preview"]
    assert failed["discord_msg_id"] == "1123456789012345678"
    assert failed["retry_count"] == 2
    failure = out["rollback_progress"]["failures"][0]
    assert "hunter2horse" not in failure["error"]
    assert failure["stoat_id"] == "01ABC"
    assert failure["http_status"] == 500
    assert out["rollback_progress"]["channels_deleted"] == 3


def test_scrub_document_leaves_structural_members_of_a_scrubbed_entry_alone() -> None:
    """The identifier members of a text-bearing entry must survive.

    Found by mutation testing chunk 1: an implementation that scrubbed every string
    member of an entry, rather than only the named ones, passed every other test
    here. It passed because the registered secret was not a substring of the
    identifier being asserted on, so the assertion could not fail against the
    defect it existed to catch. The secret below IS a substring of both identifiers,
    which is what makes this discriminate.
    """
    register_secret("proxy_password", "12345678")
    doc = {
        "failed_messages": [
            {
                "discord_msg_id": "1123456789012345678",
                "stoat_channel_id": "0112345678ABCDEF",
                "error": "send failed for 12345678",
                "retry_count": 12345678,
            }
        ],
        "errors": [{"phase": "12345678-phase", "type": "x", "message": "boom 12345678"}],
    }

    out = scrub_document(doc)

    failed = out["failed_messages"][0]
    assert failed["discord_msg_id"] == "1123456789012345678"
    assert failed["stoat_channel_id"] == "0112345678ABCDEF"
    assert failed["retry_count"] == 12345678
    assert "12345678" not in failed["error"]
    # phase is structural: consumers group on it, so mangling it would break them
    assert out["errors"][0]["phase"] == "12345678-phase"
    assert "12345678" not in out["errors"][0]["message"]


def test_scrub_document_handles_absent_and_null_fields() -> None:
    """rollback_progress is None until a rollback runs, and optional fields may be absent."""
    register_secret("proxy_password", "hunter2horse")
    doc = {"rollback_progress": None, "current_phase": "structure"}

    assert scrub_document(doc) == doc


def test_scrub_document_applies_the_config_store_too() -> None:
    """The registry holds the proxy secrets; the config store holds the API tokens.

    Neither is a superset of the other, so a document scrubbed with only one of them
    leaks whatever the other holds.
    """
    store = make_store(stoat="tok_aaaaaaaaaaaaaaaaaaaa")
    doc = {"warnings": [{"message": "rejected tok_aaaaaaaaaaaaaaaaaaaa"}]}

    out = scrub_document(doc, store)

    assert "tok_aaaaaaaaaaaaaaaaaaaa" not in out["warnings"][0]["message"]


def test_scrub_document_is_idempotent() -> None:
    """A resumed run loads already-scrubbed warnings and writes them again."""
    register_secret("proxy_password", "hunter2horse")
    doc = {"warnings": [{"message": "failed for hunter2horse"}]}

    once = scrub_document(doc)

    assert scrub_document(once) == once


def test_scrub_document_masks_nothing_when_no_secret_registered() -> None:
    """Masking keys off registered values, never a pattern, so ordinary text survives."""
    doc = {"warnings": [{"message": "an ordinary failure with no secret"}]}

    assert scrub_document(doc) == doc


def test_scrub_document_masks_validation_results_detail() -> None:
    """validation_results.results[].detail can embed a proxy credential (#268)."""
    register_secret("proxy_password", "hunter2horse")
    doc = {
        "validation_results": {
            "results": [
                {
                    "name": "tail:123",
                    "status": "unverifiable",
                    "kind": "check_error",
                    "detail": "could not read: Network error: hunter2horse",
                    "discord_id": "123",
                    "stoat_id": "456",
                    "expected": None,
                    "found": None,
                }
            ],
            "counts": {"ok": 0, "warn": 0, "fail": 0, "unverifiable": 1},
            "has_failures": False,
        }
    }

    out = scrub_document(doc)

    assert "hunter2horse" not in out["validation_results"]["results"][0]["detail"]
    assert out["validation_results"]["results"][0]["discord_id"] == "123"
    assert out["validation_results"]["counts"]["unverifiable"] == 1


def test_scrub_document_masks_report_validation_key() -> None:
    """reporter.py copies validation_results as 'validation' in report.json."""
    register_secret("proxy_password", "hunter2horse")
    doc = {
        "validation": {
            "results": [
                {
                    "name": "test",
                    "status": "unverifiable",
                    "kind": "check_error",
                    "detail": "proxy auth hunter2horse failed",
                    "discord_id": None,
                    "stoat_id": None,
                    "expected": None,
                    "found": None,
                }
            ],
            "counts": {"ok": 0},
            "has_failures": False,
        }
    }

    out = scrub_document(doc)

    assert "hunter2horse" not in out["validation"]["results"][0]["detail"]


def test_scrub_document_leaves_the_input_unmutated() -> None:
    """The caller's document must survive: state.warnings stays raw in memory."""
    register_secret("proxy_password", "hunter2horse")
    doc = {"warnings": [{"message": "failed for hunter2horse"}]}

    scrub_document(doc)

    assert doc["warnings"][0]["message"] == "failed for hunter2horse"


# ---------------------------------------------------------------------------
# register_secret() fully_mask policy -- issue #149
# ---------------------------------------------------------------------------


def test_register_secret_fully_mask_propagates_to_sanitize_secrets() -> None:
    """The process-wide registry must honour the per-key policy, not just a store."""
    register_secret("proxy_password", "hunter2horse", fully_mask=True)
    assert sanitize_secrets("failed: hunter2horse") == "failed: ****"


def test_register_secret_default_keeps_last_four_tail() -> None:
    register_secret("stoat", "abcde12345")
    assert sanitize_secrets("token abcde12345") == "token ****2345"


def test_sanitize_secrets_for_public_output_removes_all_tails() -> None:
    register_secret("stoat", "hunter2horse")

    assert sanitize_secrets_for_public_output("x hunter2horse y ****orse") == "x **** y ****"


def test_sanitize_secrets_for_public_output_removes_urlsafe_mask_tails() -> None:
    assert sanitize_secrets_for_public_output("x ****-_aB") == "x ****"


def test_sanitize_secrets_for_public_output_masks_multiple_complete_values() -> None:
    register_secret("stoat", "stoat-value-1234")
    register_secret("discord", "discord-value-5678")

    result = sanitize_secrets_for_public_output(
        "stoat-value-1234 and discord-value-5678 and stoat-value-1234"
    )

    assert result == "**** and **** and ****"


def test_sanitize_secrets_for_public_output_leaves_clean_text_unchanged() -> None:
    assert sanitize_secrets_for_public_output("No credentials here") == "No credentials here"


def test_contains_registered_secret_checks_complete_non_empty_values() -> None:
    register_secret("ignored", "")
    register_secret("stoat", "stoat-value-1234")

    assert contains_registered_secret("prefix stoat-value-1234 suffix") is True
    assert contains_registered_secret("prefix ****1234 suffix") is False
    assert contains_registered_secret("") is False
