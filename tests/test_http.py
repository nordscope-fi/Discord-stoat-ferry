"""Trust policy for every outbound HTTPS call (issue #134)."""

from __future__ import annotations

import ssl
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import aiohttp
import certifi
import pytest

from discord_ferry.core import http

if TYPE_CHECKING:
    pass

_SRC = Path(__file__).resolve().parent.parent / "src" / "discord_ferry"


def test_context_starts_from_the_default_context_with_no_ca_arguments() -> None:
    """SC-134-7, the PRIMARY replacement detector.

    Two wrong implementations drop the OS store and both are caught here:
      (a) ssl.SSLContext(PROTOCOL_TLS_CLIENT) never calls create_default_context,
          so call_count is 0;
      (b) create_default_context(cafile=certifi.where()) suppresses
          load_default_certs entirely, so the kwargs check fails.

    call_count == 1 is not decoration. A warm module cache means the factory
    never calls create_default_context, the spy records nothing, and an
    assertion written only as "no call had CA arguments" passes over an empty
    list. That is the same vacuous shape this project already had to fix once.
    """
    http.reset_http_state()

    real = ssl.create_default_context
    with patch("ssl.create_default_context", side_effect=real) as spy:
        http._build_ssl_context()

    assert spy.call_count == 1, "context must be built exactly once, from a cold cache"
    _, kwargs = spy.call_args
    for forbidden in ("cafile", "capath", "cadata"):
        assert forbidden not in kwargs, (
            f"create_default_context must receive no {forbidden}: passing one "
            "suppresses load_default_certs() and drops the OS trust store"
        )


def test_context_trusts_the_union_of_os_and_certifi() -> None:
    """SC-134-8, corroborating only.

    binary_form=True is mandatory: the default dict form is unhashable and
    set() raises TypeError.

    Known limitation: on a platform whose default context reports no
    certificates (trust resolved through a hashed capath), this collapses to
    union == certifi and a replacement implementation satisfies it too. The
    real detector is the test above.
    """
    http.reset_http_state()

    default = ssl.create_default_context()
    certifi_only = ssl.create_default_context(cafile=certifi.where())
    union = http._build_ssl_context()

    d = set(default.get_ca_certs(binary_form=True))
    c = set(certifi_only.get_ca_certs(binary_form=True))
    u = set(union.get_ca_certs(binary_form=True))

    assert u == d | c
    assert u >= c
    assert u >= d


def test_verification_stays_strict() -> None:
    """SC-134-14."""
    http.reset_http_state()
    ctx = http._build_ssl_context()
    assert ctx.verify_mode is ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_context_is_cached() -> None:
    """SC-134-9."""
    http.reset_http_state()
    real = ssl.create_default_context
    with patch("ssl.create_default_context", side_effect=real) as spy:
        http._get_ssl_context()
        http._get_ssl_context()
    assert spy.call_count == 1


def test_reset_clears_the_cache() -> None:
    """SC-134-10. Without this, every later test sees a warm cache."""
    http.reset_http_state()
    first = http._get_ssl_context()
    http.reset_http_state()
    second = http._get_ssl_context()
    assert first is not second


def test_fallback_keeps_verification_strict(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """SC-134-15.

    The tempting wrong fix is ssl._create_unverified_context() or
    verify_mode = CERT_NONE. Either turns a packaging slip into silent
    unverified transport.
    """
    http.reset_http_state()
    missing = tmp_path / "nope.pem"
    with caplog.at_level("WARNING"), patch.object(certifi, "where", return_value=str(missing)):
        ctx = http._build_ssl_context()

    assert ctx.verify_mode is ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    assert str(missing) in caplog.text


def test_module_imports_certifi() -> None:
    """SC-134-13: guarantees PyInstaller's analysis sees the dependency."""
    assert hasattr(http, "certifi")


def test_no_bare_client_session_outside_the_factory() -> None:
    """SC-134-1 and SC-134-2, a reintroduction guard only.

    This is a text scan. This project bans text scans as COVERAGE, because
    inspect.getsource assertions let an 11-release outage ship green. It is
    admissible here purely to stop a new bare session appearing later; the
    behavioural proof that the factory works lives in the aioresponses and
    ownership tests.
    """
    allowlist = {"core/http.py"}
    offenders: list[str] = []
    for path in _SRC.rglob("*.py"):
        rel = path.relative_to(_SRC).as_posix()
        if rel in allowlist:
            continue
        if "ClientSession(" in path.read_text(encoding="utf-8"):
            offenders.append(rel)
    assert not offenders, (
        f"these files build a session directly instead of using core.http.new_session: {offenders}"
    )
    assert allowlist == {"core/http.py"}, "the allowlist must not grow"


async def test_factory_session_is_intercepted_by_aioresponses() -> None:
    """SC-134-3.

    The trap: a ClientSession subclass defining _request shadows the attribute
    aioresponses patches, silently disabling mocking in 22 test files. A custom
    connector is fine, which is what this proves.
    """
    from aioresponses import aioresponses

    with aioresponses() as mocked:
        mocked.get("https://example.invalid/x", status=200, payload={"ok": True})
        async with http.new_session() as session:
            resp = await session.get("https://example.invalid/x")
            assert (await resp.json())["ok"] is True


def test_tls_hint_recognises_a_certificate_error() -> None:
    """SC-134-19, SC-134-21."""
    key = aiohttp.client_reqrep.ConnectionKey("api.github.com", 443, True, True, None, None, None)
    exc = aiohttp.ClientConnectorCertificateError(key, ssl.SSLCertVerificationError("bad"))
    hint = http.tls_hint(exc)
    assert hint is not None
    assert "api.github.com" in hint
    assert "SSL_CERT_FILE" in hint
    assert "antivirus" in hint.lower()
    assert "proxy" in hint.lower()


@pytest.mark.parametrize(
    "exc",
    [ValueError("nope"), TimeoutError(), aiohttp.ClientOSError("boom")],
)
def test_tls_hint_ignores_unrelated_errors(exc: BaseException) -> None:
    """SC-134-20.

    ClientConnectorCertificateError IS an OSError subclass, so an
    isinstance(exc, OSError) check would wrongly match ClientOSError and attach
    TLS advice to unrelated failures. So would matching the word "certificate"
    in a message.
    """
    assert http.tls_hint(exc) is None


def test_tls_hint_survives_a_self_referential_chain() -> None:
    """SC-134-22: a naive while-loop over __context__ would spin forever."""
    exc = ValueError("loop")
    exc.__context__ = exc
    assert http.tls_hint(exc) is None
