"""Trust policy for every outbound HTTPS call (issue #134).

Proxy support (issue #135) lands in the same module, because a proxy is part of
how an outbound session reaches the network.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
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


def test_strip_userinfo_handles_user_only_userinfo() -> None:
    """Killing: dropping the `or ""` from `password = url.password or ""`.

    The mirror of the password-only case, and the only one of the two that no
    test supplied an input for. yarl reports password as None for
    `http://ferryuser@corp:8080`, and encode_basic_auth interpolates with an
    f-string (helpers.py:127), so the mutant does not raise. It builds
    `ferryuser:None` and sends that to the proxy as a real credential, which
    fails authentication with a header Ferry believes it got right.
    """
    stripped, header = http._strip_userinfo(URL("http://ferryuser@corp:8080"))
    assert stripped == URL("http://corp:8080")
    assert header is not None
    assert base64.b64decode(header.split()[-1]).decode() == "ferryuser:"


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


def test_proxy_choice_requires_a_source() -> None:
    """Killing: a default on `source`. Task 2's bypass union branches on
    source == "os", so a ProxyChoice built without one silently takes the
    env-only path and drops the OS bypass list."""
    with pytest.raises(TypeError):
        http.ProxyChoice(URL("http://corp:8080"), None)  # type: ignore[call-arg]


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


def test_os_proxies_reaches_the_registry_getter() -> None:
    """Killing: dropping the second name from the getattr loop.

    Every other test in this file either nulls getproxies_registry or never
    reaches it, so `for name in ("getproxies_macosx_sysconf",):` survives them
    all. Windows is the primary platform for OS-supplied proxies and the
    registry getter is the only way Ferry reads them, so this branch carries
    decision 1 of the feature.
    """
    with (
        patch.object(urllib.request, "getproxies_macosx_sysconf", None, create=True),
        patch.object(
            urllib.request,
            "getproxies_registry",
            lambda: {"https": "http://registry-proxy:8080"},
            create=True,
        ),
    ):
        assert http._os_proxies() == {"https": "http://registry-proxy:8080"}


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


def test_os_proxy_bypass_reaches_the_darwin_getter() -> None:
    """Killing: a darwin branch that ignores its argument or returns a constant.

    The fourth of four loop-name combinations. Without it, that branch is
    exercised only by the type-only smoke test, which asserts isinstance(bool)
    and therefore cannot fail against a constant.
    """
    with (
        patch.object(
            urllib.request,
            "proxy_bypass_macosx_sysconf",
            lambda host: host == "internal.corp",
            create=True,
        ),
        patch.object(urllib.request, "proxy_bypass_registry", None, create=True),
    ):
        assert http._os_proxy_bypass("internal.corp") is True
        assert http._os_proxy_bypass("api.stoat.chat") is False


# --- Resolution, the merge and the bypass union (Task 2) ---------------------

CORP = {"https": "http://corp:8080", "http": "http://corp:8080"}
TARGET = "https://api.stoat.chat/x"


def test_unrelated_proxy_variable_does_not_mask_os_discovery(proxy_env, os_proxy) -> None:
    """SC-135-03. Killing: calling urllib.request.getproxies(), which is
    `getproxies_environment() or getproxies_<os>()`. {'no': 'localhost'} is
    truthy, so the OS half is skipped and the user gets no proxy AND no message."""
    with os_proxy(CORP), proxy_env(NO_PROXY="localhost"):
        choice = http.resolve_proxy(TARGET)
    assert choice is not None
    assert str(choice.url) == "http://corp:8080"
    assert choice.source == "os"


def test_environment_wins_for_a_scheme_it_defines(proxy_env, os_proxy) -> None:
    """SC-135-04. Killing: 'ignore getproxies_environment entirely'. The test
    above stays green against that mutant, which is why both exist."""
    with os_proxy(CORP), proxy_env(HTTPS_PROXY="http://env:3128"):
        choice = http.resolve_proxy(TARGET)
    assert choice is not None
    assert str(choice.url) == "http://env:3128"
    assert choice.source == "env"


def test_no_proxy_is_honoured_when_the_proxy_came_from_the_os(proxy_env, os_proxy) -> None:
    """SC-135-05, the round-5 Critical. Killing: routing the two bypass lists as
    if/else instead of a union, which discards an explicitly-set NO_PROXY
    whenever the proxy came from the OS. Linux never takes the OS branch and CI
    runs Linux only, so nothing else can see this."""
    with os_proxy(CORP, bypass=set()), proxy_env(NO_PROXY="api.stoat.chat"):
        assert http.resolve_proxy(TARGET) is None


def test_os_bypass_list_is_still_consulted(proxy_env, os_proxy) -> None:
    """SC-135-07. Killing: `bypassed = proxy_bypass_environment(...)` alone.
    The two tests above call _os_proxy_bypass against an empty set, where False
    is indistinguishable from never calling it, so this is what proves the call."""
    with os_proxy(CORP, bypass={"api.stoat.chat"}), proxy_env():
        assert http.resolve_proxy(TARGET) is None


def test_a_host_on_neither_list_is_still_proxied(proxy_env, os_proxy) -> None:
    """SC-135-08. Killing: an over-eager bypass that disables the feature."""
    with os_proxy(CORP, bypass=set()), proxy_env():
        assert http.resolve_proxy(TARGET) is not None


def test_no_proxy_still_exempts_an_env_supplied_proxy(proxy_env, os_proxy) -> None:
    with os_proxy({}), proxy_env(HTTPS_PROXY="http://env:3128", NO_PROXY="api.stoat.chat"):
        assert http.resolve_proxy(TARGET) is None


def test_emptied_scheme_stays_off(proxy_env, os_proxy) -> None:
    """SC-135-16. Killing: a merge that treats 'emptied' as 'unmentioned' and
    lets the OS half restore it, making the documented off switch do nothing."""
    with os_proxy(CORP), proxy_env(https_proxy=""):
        assert http.resolve_proxy(TARGET) is None


def test_mixed_case_emptied_scheme_also_suppresses(proxy_env, os_proxy) -> None:
    """SC-135-17. Killing: a k.islower() scan condition, which misses this and is
    inert on Windows, where os.environ upper-cases every key."""
    with os_proxy(CORP), proxy_env(HTTPS_proxy=""):
        assert http.resolve_proxy(TARGET) is None


def test_an_uppercase_empty_variable_does_not_suppress_a_lowercase_one(proxy_env, os_proxy) -> None:
    """Killing: filtering the environment half by `_suppressed_schemes`.

    stdlib's pop is case-SENSITIVE (urllib/request.py:2548-2554), so
    `HTTPS_PROXY=""` does not pop and `getproxies_environment()` returns the
    lowercase variable's value. Ferry's suppression scan is case-INsensitive.
    Applying it to the env half would drop a proxy the user explicitly set, with
    no proxy and no notice, where curl, requests and stdlib all proxy.
    """
    with os_proxy({}), proxy_env(HTTPS_PROXY="", https_proxy="http://mine:3128"):
        choice = http.resolve_proxy(TARGET)
    assert choice is not None
    assert str(choice.url) == "http://mine:3128"


@pytest.mark.parametrize("bad", ["http://host:notaport", "http://[::1"])
def test_a_malformed_proxy_never_raises(proxy_env, os_proxy, bad: str) -> None:
    """SC-135-22. Killing: an unguarded parse. Resolution runs inside
    ClientRequest.__init__, so a raise kills the first request of a migration
    from inside the constructor. Both of these raise ValueError in yarl."""
    with os_proxy({}), proxy_env(HTTPS_PROXY=bad):
        assert http.resolve_proxy(TARGET) is None


def test_the_choice_carries_the_stripped_url_and_the_credential(proxy_env, os_proxy) -> None:
    """Killing: `ProxyChoice(url=raw, authorization=None, source=source)`.

    That mutant discards _strip_userinfo's outputs and passes every other test
    in this task, because `str()` of the raw string compares equal and no other
    happy-path test feeds a credentialed proxy. aiohttp asserts
    `type(proxy) is URL` (client_reqrep.py:887-888), so it would kill the first
    request of every migration. Nothing before Task 3 would catch it.
    """
    with os_proxy({}), proxy_env(HTTPS_PROXY="http://ferryuser:SUPERSECRET@corp:8080"):
        choice = http.resolve_proxy(TARGET)
    assert choice is not None
    assert type(choice.url) is URL
    assert "SUPERSECRET" not in str(choice.url)
    assert choice.url == URL("http://corp:8080")
    assert choice.authorization is not None
    assert base64.b64decode(choice.authorization.split()[-1]).decode() == "ferryuser:SUPERSECRET"


def test_reset_clears_the_scan_the_memo_and_the_notices(proxy_env, os_proxy) -> None:
    """SC-135-13. Killing: a reset that clears half its state. That exact defect
    shipped into the v2.13.0 plan when a str.replace anchor silently no-opped."""
    with os_proxy(CORP), proxy_env():
        http.resolve_proxy(TARGET)
    assert http._proxy_scan is not None or http._bypass_memo
    http.reset_http_state()
    assert http._proxy_scan is None
    assert http._bypass_memo == {}
    assert http._proxy_notices == ()


# The nine tests below are not in the task brief. Each closes a branch of
# resolve_proxy, _scheme_map, _is_bypassed or _suppressed_schemes that the
# twelve above leave unexecuted or unpinned, found by enumerating the decisions
# in the source rather than by reading the brief back. Every one was confirmed
# red against a mutant of the exact line it claims to cover.


@pytest.mark.parametrize(
    ("env_value", "os_map"),
    [("socks5://sock:1080", {}), (None, {"https": "socks4://sock:1080"})],
)
def test_a_socks_proxy_resolves_to_none(proxy_env, os_proxy, env_value, os_map) -> None:
    """Killing: deleting the socks guard from resolve_proxy.

    Nothing else in the plan pins it. Task 5's socks tests assert on
    proxy_notices() only, and a notice does not stop the URL being handed to
    aiohttp, whose update_proxy raises ValueError for any scheme but http
    (client_reqrep.py). resolve_proxy runs inside ClientRequest.__init__, so
    that raise kills the first request of the migration.

    Both halves matter: the OS case also kills a guard keyed off the dict name
    rather than the parsed scheme, which is the shape getproxies_registry
    produces on Windows.
    """
    pairs = {"HTTPS_PROXY": env_value} if env_value is not None else {}
    with os_proxy(os_map), proxy_env(**pairs):
        assert http.resolve_proxy(TARGET) is None


def test_the_scan_is_cached(proxy_env) -> None:
    """Killing: a _scheme_map that rebuilds per call and never sets _proxy_scan.

    test_reset_clears_the_scan_the_memo_and_the_notices cannot fail against that
    mutant: its first assertion is a disjunction the memo half satisfies alone,
    and its second is trivially true when nothing was ever cached. So this is the
    only test that proves the scan half of the cache exists at all.

    The cache is cold at entry because the autouse fixture calls
    reset_http_state, which is also what makes `scan_spy.call_count == 1` an
    assertion about caching rather than about test order. Patches the seams
    directly rather than through os_proxy, because it needs the call count.
    """
    with (
        proxy_env(),
        patch("discord_ferry.core.http._os_proxies", return_value=dict(CORP)) as scan_spy,
        patch("discord_ferry.core.http._os_proxy_bypass", return_value=False),
    ):
        assert http.resolve_proxy(TARGET) is not None
        assert http._proxy_scan is not None
        assert http.resolve_proxy("https://other.invalid/y") is not None
        assert scan_spy.call_count == 1


def test_the_bypass_decision_is_memoised_including_misses(proxy_env) -> None:
    """Killing: dropping the memo, or memoising only the True answers.

    A miss that is not stored pays the OS syscall on every request to a host
    Ferry does proxy, which is the common case rather than the rare one. Also
    pins the key shape: (host, source), not host alone, because the answer
    differs by source.
    """
    http.reset_http_state()
    with (
        proxy_env(),
        patch("discord_ferry.core.http._os_proxies", return_value=dict(CORP)),
        patch("discord_ferry.core.http._os_proxy_bypass", return_value=False) as bypass_spy,
    ):
        assert http.resolve_proxy(TARGET) is not None
        assert http.resolve_proxy(TARGET) is not None
        assert bypass_spy.call_count == 1
    assert http._bypass_memo == {("api.stoat.chat", "os"): False}


def test_the_os_bypass_list_does_not_veto_an_env_supplied_proxy(proxy_env, os_proxy) -> None:
    """Killing: dropping the `source == "os"` guard from the union.

    A plain `a or b` reads like a simplification and passes every other test
    here, because they never combine an env-supplied proxy with a non-empty OS
    bypass list. It would let a machine-wide exception list veto a proxy the user
    set explicitly for this process, which is the opposite of the precedence the
    merge applies everywhere else.
    """
    with os_proxy(CORP, bypass={"api.stoat.chat"}), proxy_env(HTTPS_PROXY="http://env:3128"):
        choice = http.resolve_proxy(TARGET)
    assert choice is not None
    assert choice.source == "env"


def test_a_url_object_target_resolves_like_its_string(proxy_env, os_proxy) -> None:
    """The URL half of `url: str | URL`, unexecuted by every test above.

    Task 3's _FerryRequest passes the URL object form (args[1] is a yarl.URL),
    so this is the branch production takes, not the string one.
    """
    with os_proxy({}), proxy_env(HTTPS_PROXY="http://env:3128"):
        choice = http.resolve_proxy(URL(TARGET))
    assert choice is not None
    assert str(choice.url) == "http://env:3128"


def test_a_colon_in_the_proxy_login_never_raises(proxy_env, os_proxy) -> None:
    """The second half of the never-raise contract, and the one the brief names.

    _strip_userinfo raises ValueError when the login contains ':' (aiohttp
    helpers.py:125-126, RFC 7617), and a %3A in userinfo decodes to exactly that:
    yarl reports raw_user='user%3Aname' and user='user:name'. The malformed-proxy
    test cannot reach it, because its inputs die in URL(raw) one line earlier. A
    try narrowed to the parse alone therefore passes the whole suite and raises
    out of ClientRequest.__init__ on the first request of the migration.
    """
    with os_proxy({}), proxy_env(HTTPS_PROXY="http://user%3Aname:pw@corp:8080"):
        assert http.resolve_proxy(TARGET) is None


def test_a_malformed_target_never_raises(proxy_env, os_proxy) -> None:
    """The target parse, the other guarded parse in resolve_proxy.

    Distinct from test_a_malformed_proxy_never_raises, which feeds a bad PROXY
    url and never executes this guard. Same yarl ValueError, different line.
    """
    with os_proxy(CORP), proxy_env():
        assert http.resolve_proxy("http://[::1") is None


def test_the_scheme_map_merges_per_scheme(proxy_env, os_proxy) -> None:
    """`k not in env` on the OS half, which nothing else can observe.

    resolve_proxy consults env first, so a duplicated https entry in the OS half
    changes no resolution and every test above stays green. It is visible only
    here, and it matters to Task 5, whose notice loop walks both dicts and would
    report the same scheme twice.
    """
    with os_proxy(CORP), proxy_env(HTTPS_PROXY="http://env:3128"):
        env, os_side = http._scheme_map()
    assert env == {"https": "http://env:3128"}
    assert os_side == {"http": "http://corp:8080"}


def test_a_cgi_environment_keeps_the_http_scheme_suppressed(proxy_env, os_proxy) -> None:
    """CVE-2016-1000110, the branch of _suppressed_schemes nothing else enters.

    getproxies_environment pops 'http' when REQUEST_METHOD is set, because a
    remote client controls the Proxy header. Under the merge that pop is
    indistinguishable from the scheme never being mentioned, so without the
    REQUEST_METHOD clause the OS half puts the proxy straight back and Ferry
    reverses a stdlib mitigation it silently inherits everywhere else.
    """
    with os_proxy(CORP), proxy_env(REQUEST_METHOD="GET"):
        assert http.resolve_proxy("http://plain.invalid/x") is None
        assert http.resolve_proxy(TARGET) is not None


# --- The request subclass and the factory wiring (Task 3) --------------------
#
# Every test below that builds a _FerryRequest builds it DIRECTLY. That is not a
# shortcut: ClientSession only reaches self._request_class at client.py:780, and
# aioresponses replaces ClientSession._request wholesale, so under a mock the
# class is never constructed at all. The factory-installs test is what connects
# the two halves, and Task 4 drives the whole path over a real socket.


async def test_the_subclass_fills_the_proxy(proxy_env, os_proxy) -> None:
    """SC-135-09, half one. Killing: a subclass that ignores its resolver."""
    with os_proxy({}), proxy_env(HTTPS_PROXY="http://corp:8080"):
        req = http._FerryRequest("GET", URL(TARGET), loop=asyncio.get_running_loop())
    assert req.proxy == URL("http://corp:8080")


async def test_the_factory_installs_the_request_class() -> None:
    """SC-135-10, half two. Killing: 'wrote the subclass correctly, forgot
    request_class='. Measured: the subclass is constructed 0 times under
    aioresponses and 1 time on a real attempt, so no mocked test can see this,
    and the test above stays green against the mutant. Mirrors
    test_http.py's `connector._ssl is ctx`."""
    async with http.new_session() as session:
        assert session._request_class is http._FerryRequest
        assert type(session) is aiohttp.ClientSession


async def test_a_callers_own_request_class_survives() -> None:
    """SC-135-12. Killing: a hardcoded request_class= raising
    'TypeError: got multiple values for keyword argument'."""

    class Other(aiohttp.ClientRequest):
        pass

    async with http.new_session(request_class=Other) as session:
        assert session._request_class is Other


async def test_a_callers_explicit_proxy_wins(proxy_env, os_proxy) -> None:
    """The FALSE branch of `kwargs.get("proxy") is None`.

    Killing: filling the proxy in unconditionally, which would redirect a caller
    that named a specific proxy to the ambient one and attach a credential meant
    for a different host. proxy_headers stays None because update_proxy nulls it
    when nothing supplied any (client_reqrep.py:1335), so this also proves no
    header was injected.
    """
    caller = URL("http://caller-chose-this:9999")
    with os_proxy({}), proxy_env(HTTPS_PROXY="http://ferryuser:SUPERSECRET@corp:8080"):
        req = http._FerryRequest("GET", URL(TARGET), proxy=caller, loop=asyncio.get_running_loop())
    assert req.proxy == caller
    assert req.proxy_headers is None


async def test_the_url_can_arrive_as_a_keyword(proxy_env, os_proxy) -> None:
    """The `"url" in kwargs` half of the url lookup.

    aiohttp passes method and url positionally (client.py:780-782), so the
    positional half is the one production takes and this is the only test that
    enters the keyword half. Killing: `url = args[1]` with no kwargs lookup,
    which silently stops proxying for any caller using keywords.
    """
    with os_proxy({}), proxy_env(HTTPS_PROXY="http://corp:8080"):
        req = http._FerryRequest(method="GET", url=URL(TARGET), loop=asyncio.get_running_loop())
    assert req.proxy == URL("http://corp:8080")


async def test_a_missing_url_raises_type_error_not_index_error(proxy_env, os_proxy) -> None:
    """The `len(args) > 1` guard: a malformed call stays aiohttp's problem.

    Killing: a bare `args[1]`, which raises IndexError from Ferry's own frame
    before super() ever runs, so the caller is told the wrong thing about their
    own mistake. IndexError is not a TypeError, so that turns this red.

    It does NOT cover `_resolve_or_direct`'s `if url is None` guard. That guard
    stopped being observable here the moment the resolver call was wrapped in
    `except Exception`: without it, resolve_proxy(None) raises AttributeError,
    the wrapper swallows it, and this test still sees its TypeError. The test
    below is what pins that guard.
    """
    with os_proxy({}), proxy_env(HTTPS_PROXY="http://corp:8080"), pytest.raises(TypeError):
        http._FerryRequest("GET", loop=asyncio.get_running_loop())


async def test_a_missing_url_never_reaches_the_resolver(proxy_env, os_proxy) -> None:
    """`_resolve_or_direct`'s `if url is None: return None`.

    Killing: dropping that guard. The visible cost is no longer an exception,
    because `except Exception` catches the AttributeError resolve_proxy(None)
    raises; it is a warning logged with a full traceback on a request that never
    had a url to resolve. Asserting the resolver is not called pins the guard
    itself rather than one of its downstream symptoms.
    """
    with (
        os_proxy({}),
        proxy_env(HTTPS_PROXY="http://corp:8080"),
        patch("discord_ferry.core.http.resolve_proxy") as resolver,
        pytest.raises(TypeError),
    ):
        http._FerryRequest("GET", loop=asyncio.get_running_loop())
    resolver.assert_not_called()


async def test_a_raising_resolver_connects_direct(proxy_env, os_proxy, caplog) -> None:
    """SC-135 constraint 12, meant totally. Killing: narrowing the wrapper to
    `except (ValueError, TypeError)`.

    re.error is the concrete case. `_is_bypassed` reaches proxy_bypass_registry,
    which hands a registry-controlled ProxyOverride entry to
    `_proxy_bypass_winreg_override`, and `re.match(test, host)` raises re.error
    on an entry like `internal[`. re.error subclasses Exception directly, so it
    escapes resolve_proxy's own ValueError/TypeError guards and would kill the
    first request of a migration from inside ClientRequest.__init__ on exactly
    the Windows corporate machines this feature exists for.
    """
    boom = re.error("unterminated character set at position 8")
    with (
        os_proxy({}),
        proxy_env(HTTPS_PROXY="http://corp:8080"),
        patch("discord_ferry.core.http.resolve_proxy", side_effect=boom),
        caplog.at_level(logging.WARNING, logger="discord_ferry.core.http"),
    ):
        req = http._FerryRequest("GET", URL(TARGET), loop=asyncio.get_running_loop())
    assert req.proxy is None
    assert "connecting direct" in caplog.text


async def test_no_configured_proxy_leaves_the_request_direct(proxy_env, os_proxy) -> None:
    """The FALSE branch of `choice is not None`.

    Killing: `kwargs["proxy"] = choice.url` outside the guard, which raises
    AttributeError on None and would kill the first request of every migration
    on every machine that has no proxy at all.
    """
    with os_proxy({}), proxy_env():
        req = http._FerryRequest("GET", URL(TARGET), loop=asyncio.get_running_loop())
    assert req.proxy is None
    assert req.proxy_headers is None


async def test_the_credential_becomes_a_proxy_authorization_header(proxy_env, os_proxy) -> None:
    """The `choice.authorization is not None` branch.

    Killing: setting the proxy and dropping the header, which reaches the user
    as a 407 they cannot explain because the credential they configured was
    parsed, stripped, and then thrown away.
    """
    with os_proxy({}), proxy_env(HTTPS_PROXY="http://ferryuser:SUPERSECRET@corp:8080"):
        req = http._FerryRequest("GET", URL(TARGET), loop=asyncio.get_running_loop())
    assert req.proxy == URL("http://corp:8080")
    assert "SUPERSECRET" not in str(req.proxy)
    assert req.proxy_headers is not None
    encoded = req.proxy_headers["Proxy-Authorization"]
    assert base64.b64decode(encoded.split()[-1]).decode() == "ferryuser:SUPERSECRET"


async def test_a_callers_proxy_headers_are_merged_not_clobbered(proxy_env, os_proxy) -> None:
    """The merge. Killing: `kwargs["proxy_headers"] = {"Proxy-Authorization": ...}`,
    which drops every header the caller set.

    Direct construction is the only route into this branch today:
    ClientSession nulls proxy_headers whenever proxy is None (client.py:648-649),
    so through a session a caller's headers only ever arrive alongside a caller's
    proxy, which the guard above already declines to touch.
    """
    with os_proxy({}), proxy_env(HTTPS_PROXY="http://ferryuser:SUPERSECRET@corp:8080"):
        req = http._FerryRequest(
            "GET",
            URL(TARGET),
            proxy_headers={"X-Caller": "1"},
            loop=asyncio.get_running_loop(),
        )
    assert req.proxy_headers is not None
    assert req.proxy_headers["X-Caller"] == "1"
    assert "Proxy-Authorization" in req.proxy_headers


async def test_a_proxy_without_a_credential_sets_no_proxy_headers(proxy_env, os_proxy) -> None:
    """Killing: deleting the `if choice.authorization is not None:` guard.

    Dedenting the body runs `merged.setdefault("Proxy-Authorization", None)`,
    which inserts a None VALUE rather than leaving the mapping empty.
    connection_key then hashes (("Proxy-Authorization", None),) instead of None,
    and a `Proxy-Authorization: None` header reaches the CONNECT request. The
    empty-CIMultiDict mutant is invisible; this one is not.
    """
    with os_proxy({}), proxy_env(HTTPS_PROXY="http://corp:8080"):
        req = http._FerryRequest("GET", URL(TARGET), loop=asyncio.get_running_loop())
    assert req.proxy == URL("http://corp:8080")
    assert req.proxy_headers is None


@pytest.mark.parametrize(
    "spelling", ["Proxy-Authorization", "proxy-authorization", "PROXY-AUTHORIZATION"]
)
async def test_a_callers_credential_survives_in_any_case(
    proxy_env, os_proxy, spelling: str
) -> None:
    """Killing: a plain `dict` for `merged`.

    HTTP header names are case-insensitive. A dict's setdefault does not see a
    differently-cased key, so it inserts a SECOND Proxy-Authorization and both
    travel to the proxy (connector.py:606-610). The previous version of this
    test asserted len(getall(...)) == 1 while feeding only the canonical
    spelling, which is structurally guaranteed under a plain dict and therefore
    could not fail.
    """
    with os_proxy({}), proxy_env(HTTPS_PROXY="http://ferryuser:SECRET@corp:8080"):
        req = http._FerryRequest(
            "GET",
            URL(TARGET),
            proxy_headers={spelling: "Basic Y2FsbGVy"},
            loop=asyncio.get_running_loop(),
        )
    assert req.proxy_headers is not None
    assert len(req.proxy_headers.getall("Proxy-Authorization")) == 1
    assert req.proxy_headers["Proxy-Authorization"] == "Basic Y2FsbGVy"


async def test_one_requests_proxy_headers_do_not_leak_into_the_next(proxy_env, os_proxy) -> None:
    """A FRESH mapping per request. Killing: hoisting `merged` to a module-level
    mapping reused by every request, where the first caller's headers travel to
    the proxy on every later request.

    Now that `merged` is a CIMultiDict, update_proxy stores it by reference
    (client_reqrep.py:1342-1346 only re-wraps NON-MultiDict mappings), so a
    shared one would also collect the Host that connector.py:606-610 writes into
    it in place, and connection_key (client_reqrep.py:970-972) would shift under
    the pool between two requests to the same host.
    """
    with os_proxy({}), proxy_env(HTTPS_PROXY="http://ferryuser:SUPERSECRET@corp:8080"):
        loop = asyncio.get_running_loop()
        first = http._FerryRequest("GET", URL(TARGET), proxy_headers={"X-Caller": "1"}, loop=loop)
        second = http._FerryRequest("GET", URL(TARGET), loop=loop)
    assert first.proxy_headers is not None
    assert second.proxy_headers is not None
    assert "X-Caller" in first.proxy_headers
    assert "X-Caller" not in second.proxy_headers


# --- End to end on a real socket (Task 4) ------------------------------------
#
# No source of its own. These two drive the whole path that every test above
# only reaches in pieces: environment variable -> resolve_proxy -> _FerryRequest
# -> a CONNECT on a loopback socket. Nothing mocked below the socket, because
# aioresponses replaces ClientSession._request and would skip the request class
# entirely.


async def test_a_configured_proxy_receives_a_connect(fake_proxy, proxy_env, os_proxy) -> None:
    """SC-135-01 and SC-135-47. An HTTPS target on purpose: proxy_headers with
    encode_basic_auth sends nothing on an http target and works over CONNECT, so
    an http-target test would report a false failure."""
    make, captured = fake_proxy
    server = await make(b"403 Forbidden")
    port = server.sockets[0].getsockname()[1]
    async with server:
        with os_proxy({}), proxy_env(HTTPS_PROXY=f"http://ferryuser:SUPERSECRET@127.0.0.1:{port}"):
            caught: BaseException | None = None
            async with http.new_session() as session:
                try:
                    await session.get(TARGET, timeout=aiohttp.ClientTimeout(total=5))
                except BaseException as exc:  # noqa: BLE001
                    caught = exc

    lines = [ln for ln in captured[0].split("\r\n") if ln]
    assert lines[0].startswith("CONNECT api.stoat.chat:443")
    auth = [ln for ln in lines if ln.lower().startswith("proxy-authorization")]
    assert auth, "the credential never reached the proxy"
    assert base64.b64decode(auth[0].split()[-1]).decode() == "ferryuser:SUPERSECRET"
    # A sanity check on the CONNECT line's shape, NOT a stripping detector.
    # client_reqrep.py:1435-1438 builds the path from `self.url`, which
    # connector.py:1648 has already reassigned to the TARGET, so this line
    # structurally cannot carry the proxy's userinfo under any implementation.
    # The stripping mutant is killed by test_no_credential_reaches_the_exception.
    assert "SUPERSECRET" not in lines[0]
    assert isinstance(caught, aiohttp.ClientHttpProxyError)


async def test_no_credential_reaches_the_exception(fake_proxy, proxy_env, os_proxy) -> None:
    """SC-135-48. Kills the mutant that returns the unstripped proxy URL.

    Either assertion below kills it independently: `ClientResponseError.__str__`
    renders `str(request_info.real_url)` (client_exceptions.py:108-109) and
    yarl's __str__ does not hide userinfo, so the password reaches both
    `.password` and `str(exc)`.

    `reset_secret_registry()` does NOT protect these assertions, and an earlier
    version of this docstring wrongly said it did. Neither `.password` nor
    `str(exc)` passes through `sanitize_secrets`, which only the logging
    Formatter consults. The registry matters for SC-135-50, which asserts on
    ferry.log. The call stays as cheap insurance against a future assertion here
    that does read log output, and for no stronger reason than that.
    """
    reset_secret_registry()
    make, _ = fake_proxy
    server = await make(b"403 Forbidden")
    port = server.sockets[0].getsockname()[1]
    async with server:
        with os_proxy({}), proxy_env(HTTPS_PROXY=f"http://ferryuser:SUPERSECRET@127.0.0.1:{port}"):
            async with http.new_session() as session:
                with pytest.raises(aiohttp.ClientHttpProxyError) as caught:
                    await session.get(TARGET, timeout=aiohttp.ClientTimeout(total=5))

    exc = caught.value
    assert exc.request_info.real_url.password is None
    assert "SUPERSECRET" not in str(exc)
    # url is the TARGET, real_url is the PROXY. Getting these backwards names
    # the target twice and never the proxy.
    assert "api.stoat.chat" in str(exc.request_info.url)
    assert "127.0.0.1" in str(exc.request_info.real_url)


# --- Notices, the configurations Ferry cannot use (Task 5) -------------------
#
# NOT ONE TEST BELOW CALLS resolve_proxy BEFORE READING THE NOTICES, on purpose.
# Every reader of proxy_notices() -- the engine preflight, `build`, `rollback`,
# `probe` and the GUI export screen -- runs before the first request is made, so
# a resolve-time implementation returns () at all five and the configuration-time
# half of the feature ships inert. A test that resolved first would create the
# condition it then asserted, which is the shape that let this repo ship inert
# rollback redaction in v2.6.16.


def test_all_proxy_only_is_reported(proxy_env, os_proxy) -> None:
    """SC-135-18. Killing TWO implementations.

    First: delegating to get_env_proxy_for_url, which filters ALL_PROXY out
    before Ferry can see it.

    Second, and this is why the test does NOT call resolve_proxy first:
    recording notices as a side effect of resolution. Every reader of
    proxy_notices() -- engine preflight, build, rollback, probe, and the GUI
    export screen -- runs BEFORE any request is made, so a resolve-time
    implementation returns () at all five and the feature ships inert. A test
    that called resolve_proxy first would create the condition it then asserts,
    which is the shape that let this repo ship inert rollback redaction.
    """
    with os_proxy({}), proxy_env(ALL_PROXY="socks5://sock:1080"):
        notices = http.proxy_notices()
    assert any(n.kind == "all_proxy_only" for n in notices)


def test_the_notice_outcome_is_true(proxy_env, os_proxy) -> None:
    """SC-135-18. Killing: a notice claiming 'connected direct' when an OS proxy
    was in fact used."""
    with os_proxy(CORP), proxy_env(ALL_PROXY="socks5://sock:1080"):
        notices = http.proxy_notices()
    assert any("http" in n.outcome and "https" in n.outcome for n in notices)


def test_the_all_proxy_outcome_is_not_contradicted_by_its_own_siblings(proxy_env, os_proxy) -> None:
    """Killing: `covered` built from key membership rather than usability.

    The canonical SOCKS setup exports all three variables together, which
    shadowsocks, v2ray, `ssh -D` and Tor all document. Under a membership test
    the ALL_PROXY notice claims "Used the proxy configured for http, https
    instead." while the two notices printed directly after it say "Connected
    direct. SOCKS is not supported." The first line is false and contradicts the
    other two.

    `test_the_notice_outcome_is_true` cannot see this: it is the mirror case,
    and its docstring names only the direction where an OS proxy WAS used.
    """
    with (
        os_proxy({}),
        proxy_env(
            ALL_PROXY="socks5://127.0.0.1:1080",
            HTTP_PROXY="socks5://127.0.0.1:1080",
            HTTPS_PROXY="socks5://127.0.0.1:1080",
        ),
    ):
        lines = http.format_proxy_notices()
    joined = " ".join(lines)
    assert "Used the proxy configured for" not in joined
    assert any("ALL_PROXY is not supported" in line for line in lines)


def test_the_all_proxy_outcome_does_not_name_a_single_source(proxy_env, os_proxy) -> None:
    """Killing: an outcome that picks one source word for a mixed configuration.

    With HTTP_PROXY in the environment and https from the OS, the two covered
    schemes have different sources, so any one-word answer is wrong for one of
    them. The outcome names the schemes and leaves the source to
    `ferry tls-check`, which reports it per scheme.
    """
    with (
        os_proxy({"https": "http://corp:8080"}),
        proxy_env(ALL_PROXY="socks5://sock:1080", HTTP_PROXY="http://env:3128"),
    ):
        notices = http.proxy_notices()
    outcomes = " ".join(n.outcome for n in notices)
    assert "http" in outcomes
    assert "OS" not in outcomes
    assert "environment" not in outcomes


@pytest.mark.parametrize(
    ("env_value", "os_map"),
    [("socks5://sock:1080", {}), (None, {"https": "socks4://sock:1080"})],
)
def test_socks_is_detected_by_url_scheme(proxy_env, os_proxy, env_value, os_map) -> None:
    """SC-135-19. Killing: keying the check off the dict name. getproxies_registry
    writes socks4://host:port into proxies['http'] on Windows, the exact corporate
    population this targets."""
    pairs = {"HTTPS_PROXY": env_value} if env_value else {}
    with os_proxy(os_map), proxy_env(**pairs):
        notices = http.proxy_notices()
    assert any(n.kind == "socks" for n in notices)


def test_no_proxy_is_never_reported_as_a_broken_proxy(proxy_env, os_proxy) -> None:
    """SC-135-20. Killing: 'any key that is not http or https is unusable', which
    would report the user's own NO_PROXY, since getproxies_environment maps it to
    a {'no': ...} entry."""
    with os_proxy({}), proxy_env(NO_PROXY="example.com"):
        assert http.proxy_notices() == ()


def test_an_https_proxy_is_not_reported(proxy_env, os_proxy) -> None:
    """SC-135-21. Killing: a notice written by one task and deleted by another.
    S7 makes https:// usable."""
    with os_proxy({}), proxy_env(HTTPS_PROXY="https://tls-proxy:8443"):
        assert http.proxy_notices() == ()
        assert http.resolve_proxy(TARGET) is not None


def test_reset_actually_clears_the_notices(proxy_env, os_proxy) -> None:
    """The first assertion in the feature that can fail on `_proxy_notices = ()`.

    Task 2's reset test asserts `_proxy_notices == ()` before and after, which
    holds trivially because nothing in that task ever writes the global.
    Deleting the line from reset_http_state() left all 1503 tests green.

    It matters here because proxy_notices() returns early when the tuple is
    truthy, so a reset that forgets it leaks one test's notices into every later
    test in the session, and this task's positive assertions would pass on the
    leak.
    """
    with os_proxy({}), proxy_env(ALL_PROXY="socks5://sock:1080"):
        assert http.proxy_notices() != ()
    http.reset_http_state()
    assert http._proxy_notices == ()


def test_notices_exist_before_any_request_is_made(proxy_env, os_proxy) -> None:
    """The defect this task exists to prevent, pinned directly.

    Every reader of proxy_notices() runs before the first request. Recording
    notices inside resolve_proxy would make all five read sites return nothing.
    """
    with os_proxy({}), proxy_env(ALL_PROXY="socks5://sock:1080"):
        # No resolve_proxy call anywhere in this test, on purpose.
        assert http.proxy_notices() != ()


def test_proxy_notices_is_idempotent(proxy_env, os_proxy) -> None:
    """SC-135-23. Killing: a drain-on-read implementation, under which a second
    migration in one GUI process reports nothing and describe_proxy() then
    reports a clean configuration that is not clean."""
    with os_proxy({}), proxy_env(ALL_PROXY="socks5://sock:1080"):
        first = http.proxy_notices()
        second = http.proxy_notices()
        # `==` alone tests non-draining, not caching: ProxyNotice is a frozen
        # dataclass, so a rebuild-every-call implementation returns an EQUAL
        # tuple and passes. `is` is what pins the docstring's "builds once".
        assert first == second
        assert first is second


# The seven tests below are not in the task brief. Each closes a branch of
# proxy_notices() or _safe_display() that the eight above leave unexecuted or
# unpinned, found by enumerating the decisions in the source rather than by
# reading the brief back. Every one was confirmed red against a mutant of the
# exact line it claims to cover.


def test_a_malformed_proxy_url_is_reported(proxy_env, os_proxy) -> None:
    """The `except (ValueError, TypeError)` arm of the notice loop, which no test
    in the brief enters.

    Deleting that arm and letting URL(raw) raise would take proxy_notices() out
    through every one of its five callers, and the engine preflight is the first
    thing a migration runs. resolve_proxy already returns None for this input
    (test_a_malformed_proxy_never_raises), so without the notice the user is
    connected direct with nothing said.
    """
    with os_proxy({}), proxy_env(HTTPS_PROXY="http://[::1"):
        notices = http.proxy_notices()
    assert [(n.kind, n.scheme, n.display) for n in notices] == [
        ("malformed", "https", "<unparseable>")
    ]


@pytest.mark.parametrize("variable", ["ALL_PROXY", "HTTPS_PROXY"])
def test_a_notice_display_never_carries_userinfo(proxy_env, os_proxy, variable) -> None:
    """Constraint 5, at BOTH _safe_display call sites.

    The ALL_PROXY case covers the display built in the all_proxy_only branch and
    the HTTPS_PROXY case the one built in the socks branch; a `display=raw`
    mutant at either site survives the other test. Notices reach ferry.log and
    the GUI, and a proxy password is a credential like any other.
    """
    with os_proxy({}), proxy_env(**{variable: "socks5://ferryuser:SUPERSECRET@sock:1080"}):
        notices = http.proxy_notices()
    assert notices
    assert all("SUPERSECRET" not in n.display for n in notices)
    assert notices[0].display == "socks5://sock:1080"


@pytest.mark.parametrize("bad", ["http://[::1", "http://user%3Aname:pw@corp:8080"])
def test_building_a_display_never_raises(proxy_env, os_proxy, bad: str) -> None:
    """_safe_display's except arm, reached by both of the two paths into it.

    The first input dies in URL(raw); the second parses and then dies in
    _strip_userinfo, which raises ValueError when the login contains ':' and a
    %3A in userinfo decodes to exactly that (aiohttp helpers.py:125-126). A try
    narrowed to the parse alone therefore passes the first case and raises out of
    the preflight on the second.
    """
    with os_proxy({}), proxy_env(ALL_PROXY=bad):
        notices = http.proxy_notices()
    assert [n.display for n in notices] == ["<unparseable>"]


def test_the_all_proxy_outcome_names_a_scheme_the_environment_supplies(proxy_env, os_proxy) -> None:
    """The `s in env` half of the `covered` comprehension, which nothing else pins.

    Dropping that half leaves test_the_notice_outcome_is_true green, because both
    of its schemes come from the OS. It also survives
    test_the_all_proxy_outcome_does_not_name_a_single_source: that test's
    `"http" in outcomes` is satisfied by the substring inside "https", so an
    outcome naming https alone still passes it. An exact outcome on an
    environment-only configuration is what fails.
    """
    with (
        os_proxy({}),
        proxy_env(
            ALL_PROXY="socks5://sock:1080",
            HTTP_PROXY="http://env:3128",
            HTTPS_PROXY="http://env:3128",
        ),
    ):
        notices = http.proxy_notices()
    assert [n.outcome for n in notices] == ["Used the proxy configured for http, https instead."]


def test_the_kill_switch_silences_the_notices(proxy_env, os_proxy) -> None:
    """FERRY_DISABLE_PROXY, the one early return in proxy_notices nothing else
    takes. With the kill switch on, Ferry connects direct by instruction, so a
    notice saying it could not use a proxy would be a report of a decision the
    user made.
    """
    with os_proxy(CORP), proxy_env(ALL_PROXY="socks5://sock:1080", FERRY_DISABLE_PROXY="1"):
        assert http.proxy_notices() == ()


def test_format_proxy_notices_renders_one_line_per_notice(proxy_env, os_proxy) -> None:
    """The only test of format_proxy_notices, which the brief lists as a produced
    interface and then never exercises. Both shells render from it, so a
    KeyError or a missing field here reaches the user, not a test.
    """
    with os_proxy({}), proxy_env(ALL_PROXY="socks5://sock:1080"):
        lines = http.format_proxy_notices()
    assert lines == [
        "Proxy configuration Ferry cannot use: socks5://sock:1080 (all). "
        "Connected direct. ALL_PROXY is not supported (see issue #141)."
    ]


def test_format_proxy_notices_is_empty_on_a_clean_configuration(proxy_env, os_proxy) -> None:
    """A clean machine must produce no line at all, not an empty-string line that
    a shell would print as a blank row."""
    with os_proxy({}), proxy_env():
        assert http.format_proxy_notices() == []


def test_a_malformed_scheme_proxy_does_not_count_as_covered(proxy_env, os_proxy) -> None:
    """_usable's except arm, which nothing else enters.

    test_a_malformed_proxy_url_is_reported sets no ALL_PROXY, so it never
    evaluates `covered` at all. Without the guard here the ALL_PROXY notice
    would claim a scheme was used that cannot even be parsed, on the same
    configuration whose next line reports it as malformed.
    """
    with os_proxy({}), proxy_env(ALL_PROXY="socks5://sock:1080", HTTPS_PROXY="http://[::1"):
        notices = http.proxy_notices()
    # A list and an assertion, NOT next(). Under the mutant this test kills, the
    # raise from _usable is caught by proxy_notices' never-raises boundary and
    # the result is (), so next() died with a bare StopIteration that named
    # neither the test's subject nor its expectation.
    all_notices = [n for n in notices if n.kind == "all_proxy_only"]
    assert all_notices, "the ALL_PROXY notice must survive an unparseable sibling"
    assert all_notices[0].outcome == (
        "Connected direct. ALL_PROXY is not supported (see issue #141)."
    )
    assert any(n.kind == "malformed" for n in notices)


def test_the_kill_switch_wins_over_a_populated_cache(proxy_env, os_proxy) -> None:
    """The kill-switch check sits ABOVE the cache read.

    test_the_kill_switch_silences_the_notices cannot see the ordering: its cache
    is cold, so both placements return (). Below the cache read, a process that
    read notices at preflight and then had FERRY_DISABLE_PROXY set would keep
    serving them for a proxy layer that is now off. proxy_env restores the whole
    environment on exit, so setting the variable in place here is contained.
    """
    with os_proxy({}), proxy_env(ALL_PROXY="socks5://sock:1080"):
        assert http.proxy_notices() != ()
        os.environ["FERRY_DISABLE_PROXY"] = "1"
        assert http.proxy_notices() == ()


def test_an_unreadable_configuration_degrades_visibly(proxy_env, os_proxy) -> None:
    """The total boundary, and that it degrades VISIBLY rather than to ().

    Two things are asserted, and the second is the one that matters. Returning
    () would satisfy "never raises" while making "Ferry could not read the
    configuration" indistinguishable from "this machine is clean", which is the
    inversion these notices exist to prevent. So this asserts on the rendered
    line a user would see, not on the absence of output.

    `_scheme_map()` reaches the platform getters `getproxies_macosx_sysconf` and
    `getproxies_registry` through `_os_proxies`, and stdlib can raise there
    outside (ValueError, TypeError). `re.error` is used here because it
    subclasses Exception directly and so escapes both narrow guards; the
    documented `re.error` path in this module belongs to the SIBLING boundary
    (`proxy_bypass_registry`, reached only from resolve_proxy), not to this one.
    An earlier version of this docstring inherited that attribution verbatim and
    was wrong about it.
    """
    with (
        os_proxy({}),
        proxy_env(ALL_PROXY="socks5://sock:1080"),
        patch("discord_ferry.core.http._scheme_map", side_effect=re.error("boom")),
    ):
        notices = http.proxy_notices()
        lines = http.format_proxy_notices()
    assert [n.kind for n in notices] == ["unreadable"]
    assert lines == [
        "Proxy configuration Ferry cannot use: <unavailable> (?). "
        "Ferry could not read the proxy configuration and connected direct."
    ]


def test_an_unreadable_configuration_is_not_cached(proxy_env, os_proxy) -> None:
    """The degraded notice must NOT be frozen for the life of the process.

    A transient platform error at preflight would otherwise make every later
    reader in the same GUI process report an unreadable configuration, long
    after the machine started answering again. Caching it also inverts the
    warning: the log line would appear once and the wrong notice forever.
    """
    with os_proxy({}), proxy_env(ALL_PROXY="socks5://sock:1080"):
        with patch("discord_ferry.core.http._scheme_map", side_effect=re.error("boom")):
            assert [n.kind for n in http.proxy_notices()] == ["unreadable"]
        assert http._proxy_notices == ()
        assert [n.kind for n in http.proxy_notices()] == ["all_proxy_only"]
