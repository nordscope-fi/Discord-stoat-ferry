"""Trust policy for every outbound HTTPS call (issue #134).

Proxy support (issue #135) lands in the same module, because a proxy is part of
how an outbound session reaches the network.
"""

from __future__ import annotations

import base64
import ssl
import urllib.request
from pathlib import Path
from unittest.mock import patch

import aiohttp
import certifi
import pytest
from yarl import URL

from discord_ferry.core import http
from discord_ferry.core.security import reset_secret_registry, sanitize_secrets

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
    assert _SRC.is_dir(), "the src tree must actually resolve, or rglob silently yields nothing"
    allowlist = {"core/http.py"}
    offenders: list[str] = []
    for path in _SRC.rglob("*.py"):
        rel = path.relative_to(_SRC).as_posix()
        if rel in allowlist:
            continue
        if "aiohttp.ClientSession(" in path.read_text(encoding="utf-8"):
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


async def test_new_session_carries_the_ferry_context() -> None:
    """The factory must install the context it built, not aiohttp's default.

    Without this, replacing new_session's body with a bare ClientSession()
    passes every other test in the suite, including the Windows CI gate.
    """
    http.reset_http_state()
    ctx = http._get_ssl_context()
    async with http.new_session() as session:
        connector = session.connector
        assert isinstance(connector, aiohttp.TCPConnector)
        assert connector._ssl is ctx


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


def test_tls_hint_finds_a_wrapped_certificate_error() -> None:
    """The chain walk must actually walk."""
    key = aiohttp.client_reqrep.ConnectionKey("api.github.com", 443, True, True, None, None, None)
    inner = aiohttp.ClientConnectorCertificateError(key, ssl.SSLCertVerificationError("bad"))
    outer = RuntimeError("download failed")
    outer.__cause__ = inner
    assert "api.github.com" in (http.tls_hint(outer) or "")


def test_hint_is_a_message_not_an_exception_type() -> None:
    """SC-134-23.

    Checks only that tls_hint() returns a str for a certificate error. It does
    not verify that any of the six call sites still raise their original
    exception type; a new exception class raised at one of those sites (for
    example connect.py) would not be caught here.

    The reason type preservation matters: StoatConnectionError sits under
    FerryError, not MigrationError. A single new exception class would escape
    cli.py's four `except MigrationError` handlers, or swallow gui.py's
    `except DiscordAuthError`.
    """
    key = aiohttp.client_reqrep.ConnectionKey("h", 443, True, True, None, None, None)
    exc = aiohttp.ClientConnectorCertificateError(key, ssl.SSLCertVerificationError("x"))
    assert isinstance(http.tls_hint(exc), str)


# ---------------------------------------------------------------------------
# Proxy support (issue #135)
# ---------------------------------------------------------------------------


def test_os_seams_never_raise_on_any_platform() -> None:
    """SC-135-02 support. The seams must work on Linux, where neither getter exists."""
    assert isinstance(http._os_proxies(), dict)
    assert isinstance(http._os_proxy_bypass("example.com"), bool)


def test_os_seams_fall_through_when_no_getter_exists() -> None:
    """The Linux path. A direct stdlib call would raise AttributeError here."""
    with (
        patch.object(urllib.request, "getproxies_macosx_sysconf", None, create=True),
        patch.object(urllib.request, "getproxies_registry", None, create=True),
        patch.object(urllib.request, "proxy_bypass_macosx_sysconf", None, create=True),
        patch.object(urllib.request, "proxy_bypass_registry", None, create=True),
    ):
        assert http._os_proxies() == {}
        assert http._os_proxy_bypass("example.com") is False


def test_strip_userinfo_removes_credentials_and_keeps_them() -> None:
    """SC-135-46. Killing: passing the URL through unstripped, which authenticates
    correctly and is therefore the tempting implementation."""
    stripped, header = http._strip_userinfo(URL("http://ferryuser:SUPERSECRET@corp:8080"))
    assert "SUPERSECRET" not in str(stripped)
    assert stripped == URL("http://corp:8080")
    assert header is not None and header.startswith("Basic ")
    assert base64.b64decode(header.split()[-1]).decode() == "ferryuser:SUPERSECRET"


def test_strip_userinfo_leaves_a_clean_url_alone() -> None:
    stripped, header = http._strip_userinfo(URL("http://corp:8080"))
    assert stripped == URL("http://corp:8080")
    assert header is None


def test_strip_userinfo_handles_password_only_userinfo() -> None:
    """Killing: `or` in place of `and` in the guard.

    yarl reports raw_user as None for an empty username (_parse.py:130), so an
    `or` short-circuits on this input and returns the URL with the password
    still in it, which is then assigned to kwargs["proxy"] and rendered by every
    downstream exception repr. Both existing strip tests pass under that mutant:
    the credentialed URL has neither part None, the clean URL has both.
    """
    stripped, header = http._strip_userinfo(URL("http://:PROXYTOKEN@corp:8080"))
    assert "PROXYTOKEN" not in str(stripped)
    assert stripped == URL("http://corp:8080")
    assert header is not None
    assert base64.b64decode(header.split()[-1]).decode() == ":PROXYTOKEN"


def test_strip_userinfo_registers_both_credential_forms() -> None:
    """SC-135-49. Killing: registering only the plaintext, or neither.

    sanitize masks by exact substring, and the base64 form is what travels and
    what RequestInfo.headers holds. This is the second redaction layer; without
    it sanitize_secrets can never mask a proxy credential reaching ferry.log.
    """
    reset_secret_registry()
    _, header = http._strip_userinfo(URL("http://ferryuser:SUPERSECRET@corp:8080"))
    assert header is not None
    masked = sanitize_secrets(f"plaintext=SUPERSECRET encoded={header}")
    assert "SUPERSECRET" not in masked
    assert header.split()[-1] not in masked


def test_os_proxies_returns_what_the_platform_getter_reports() -> None:
    """Killing: a seam that ignores the OS entirely and always returns {}.

    Nothing else in the 15-task plan executes the getattr branch: every later
    test patches the seams themselves. Without this, `_os_proxies` could be
    `return {}` and ship, passing the whole suite while leaving "Ferry honours
    OS proxy settings" inert on both platforms where it matters.

    This is the one place patching a stdlib name is correct, because Task 1 is
    the seam's own unit test. create=True makes it run identically on
    ubuntu-latest, where neither getter exists.
    """
    with (
        patch.object(
            urllib.request,
            "getproxies_macosx_sysconf",
            lambda: {"https": "http://corp:8080"},
            create=True,
        ),
        patch.object(urllib.request, "getproxies_registry", None, create=True),
    ):
        assert http._os_proxies() == {"https": "http://corp:8080"}


def test_os_proxy_bypass_returns_what_the_platform_getter_reports() -> None:
    """Killing: a seam that always returns False, which reads as 'never bypass'.

    Also pins the second name in the getattr loop: the registry variant is
    reached only when the darwin one is absent, which nothing else proves.
    """
    with (
        patch.object(urllib.request, "proxy_bypass_macosx_sysconf", None, create=True),
        patch.object(
            urllib.request,
            "proxy_bypass_registry",
            lambda host: host == "internal.corp",
            create=True,
        ),
    ):
        assert http._os_proxy_bypass("internal.corp") is True
        assert http._os_proxy_bypass("api.stoat.chat") is False
