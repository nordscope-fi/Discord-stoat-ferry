"""One place that builds every outbound HTTPS session.

Issue #134: the packaged Windows binary could not verify api.github.com,
because Ferry inherited whatever trust the operating system offered and had no
fallback. Every session now trusts the union of the OS store and certifi's
bundled roots.

certifi is imported at module level on purpose. That is what guarantees
PyInstaller's analysis includes the package, so pyinstaller-hooks-contrib's
hook-certifi.py fires and cacert.pem reaches the frozen app. Relying on
nicegui's httpx chain to pull it in would make the whole fix inert the day that
chain changes.
"""

from __future__ import annotations

import logging
import os
import ssl
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
import certifi
from aiohttp import encode_basic_auth

# A runtime import for the same reason as yarl below: _FerryRequest builds a
# CIMultiDict per request, and aiohttp stores a MultiDict subclass by reference
# rather than re-wrapping it (client_reqrep.py:1342-1346).
from multidict import CIMultiDict  # noqa: TC002

# A runtime import, not a TYPE_CHECKING one. ProxyChoice.url has to stay
# resolvable by typing.get_type_hints, and aiohttp checks a proxy with
# `assert type(proxy) is URL` (client_reqrep.py:888), so this module and
# aiohttp must hold the same class object.
from yarl import URL  # noqa: TC002

from discord_ferry.core.security import register_secret

logger = logging.getLogger(__name__)

# Cached because 8 call sites may build sessions repeatedly and each build
# parses a PEM bundle. Only the CONTEXT is cached: a TCPConnector or
# ClientSession resolves a running loop at construction (connector.py:298,
# client.py:353), and cli.py calls asyncio.run() four separate times, so a
# cached connector would hand a dead loop to the second command in a process.
_ssl_context: ssl.SSLContext | None = None
# Records which branch _build_ssl_context took. Inferring it from certificate
# counts does not work: in the fallback the context IS the plain default one, so
# any "visible >= baseline" comparison is trivially true and would report a
# healthy union while the bundle silently failed to load.
_trust_source: str = "unbuilt"


def _build_ssl_context() -> ssl.SSLContext:
    """Return a context trusting the OS store plus certifi's roots.

    create_default_context() is called with NO cafile, capath or cadata.
    Passing any of them suppresses load_default_certs(), which is what loads
    the OS store (and, on Windows, the Windows certificate stores). The result
    would silently be certifi INSTEAD of the OS store, breaking every machine
    that depends on a corporate root.
    """
    global _trust_source  # noqa: PLW0603
    context = ssl.create_default_context()
    bundle = "<unresolved>"
    try:
        bundle = certifi.where()
        context.load_verify_locations(cafile=bundle)
        _trust_source = "union"
    except OSError:
        # ssl.SSLError subclasses OSError, so catching OSError alone covers
        # both a missing/unreadable bundle path and a malformed PEM file.
        _trust_source = "fallback"
        # Degrade to exactly today's behaviour, never to unverified transport.
        # A missing bundle is a packaging slip; it must not cost all networking.
        logger.warning(
            "could not load the bundled CA roots from %s; "
            "falling back to the operating system trust store only",
            bundle,
            exc_info=True,
        )
    return context


def _get_ssl_context() -> ssl.SSLContext:
    """Return the cached context, building it on first use.

    Lazy so commands that never open a socket do not pay the PEM parse. The
    build does blocking disk I/O on the running loop; aiohttp builds its own
    contexts at import time for that reason (connector.py:892-896). One parse
    of a few tens of milliseconds, once per process, is accepted.

    Whichever context is produced, success or fallback, is the one cached, so
    a failure warning is logged once rather than per session.
    """
    global _ssl_context  # noqa: PLW0603 - module-level cache, mirrors api._circuit_state
    if _ssl_context is None:
        _ssl_context = _build_ssl_context()
    return _ssl_context


def reset_http_state() -> None:
    """Drop the cached context, its trust-source marker and the proxy state.

    Mirrors logging_setup.reset_logging and security.reset_secret_registry.
    tests/conftest.py calls this from its autouse fixture, so a test that
    exercises the fallback cannot poison the cache for the rest of the session,
    and the spy test always sees a cold cache.

    Every module global resets together. Leaving _trust_source behind would let
    a test that forces the fallback branch leave "fallback" behind for every
    later test, and after a bare reset the module would claim "union" while no
    context has been built yet. The same applies to the three proxy globals:
    the scan, the per-host bypass memo and the notices are all derived from one
    environment reading, so clearing a subset leaves the module answering from
    two different worlds.
    """
    global _ssl_context, _trust_source, _proxy_scan, _proxy_notices  # noqa: PLW0603
    _ssl_context = None
    _trust_source = "unbuilt"
    _proxy_scan = None
    _bypass_memo.clear()
    _proxy_notices = ()


def describe_trust() -> dict[str, str]:
    """Report the trust configuration, for `ferry tls-check` and CI.

    Builds through the real code path rather than re-deriving it, so the report
    cannot drift from what sessions actually use. Reads the CACHED context, on
    purpose: that is what sessions actually use, and rebuilding here would drop
    a context already in service mid-migration, breaking `_get_ssl_context`'s
    "one parse, once per process" cost model and re-logging the fallback
    warning on every call. Tests that must observe a fresh build call
    `reset_http_state()` themselves, same as `tests/test_http.py` already does.
    """
    try:
        bundle = certifi.where()
    except OSError:
        bundle = "<unresolved>"
    readable = bundle != "<unresolved>" and Path(bundle).is_file()
    context = _get_ssl_context()
    return {
        "ca-bundle": bundle,
        "ca-bundle-readable": "true" if readable else "false",
        # Read from the flag the build set, NOT inferred from counts. A readable
        # but malformed bundle still raises, and a count comparison would then
        # report a healthy union while trust had silently degraded.
        "trust-source": _trust_source,
        "ca-visible": str(len(context.get_ca_certs(binary_form=True))),
    }


def new_session(**kwargs: Any) -> aiohttp.ClientSession:
    """Build a ClientSession carrying Ferry's trust policy.

    A fresh TCPConnector every call, deliberately: it binds to the running
    loop. No cleanup is attempted if ClientSession() raises after the connector
    exists, because TCPConnector.close() is async and cannot be awaited here,
    and BaseConnector.__del__ returns early when _conns is empty
    (connector.py:354-358), which it always is for a connector that never
    opened a connection.

    NEVER subclass ClientSession. aioresponses patches
    ClientSession._request on the class; a subclass defining _request would
    shadow the patch and silently disable HTTP mocking across the test suite.
    The proxy is installed through the REQUEST class instead, which is defined
    at the bottom of this module beside the resolution it calls.

    setdefault, never `request_class=_FerryRequest` as an argument: a caller
    passing their own would then raise TypeError for a repeated keyword.
    """
    kwargs.setdefault("request_class", _FerryRequest)
    connector = aiohttp.TCPConnector(ssl=_get_ssl_context())
    return aiohttp.ClientSession(connector=connector, **kwargs)


def tls_hint(exc: BaseException) -> str | None:
    """Return actionable guidance if `exc`'s chain holds a certificate failure.

    Returns a MESSAGE, never an exception. The six call sites raise four
    different types that callers select on, and StoatConnectionError sits under
    FerryError rather than MigrationError. A single new class would either
    escape cli.py's four `except MigrationError` handlers, printing a raw
    traceback, or swallow gui.py's specific `except DiscordAuthError`.

    The walk is bounded by a visited set: a self-referential __context__ would
    otherwise spin forever.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, aiohttp.ClientConnectorCertificateError):
            host = getattr(current, "host", "the server")
            port = getattr(current, "port", 443)
            return (
                f" Could not verify the TLS certificate for {host}:{port}. "
                "This usually means antivirus with HTTPS scanning, a corporate "
                "proxy, or a certificate authority missing from this machine. "
                "Point SSL_CERT_FILE at a CA bundle that includes it, or see "
                "the troubleshooting guide."
            )
        current = current.__cause__ or current.__context__
    return None


# ---------------------------------------------------------------------------
# Proxy support (issue #135)
#
# A proxy is part of how an outbound session reaches the network, so it lives
# beside the trust policy rather than in a module of its own.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProxyChoice:
    """A resolved proxy. The password is NEVER stored as its own attribute.

    Only the pre-encoded Proxy-Authorization value is kept, so no field exists
    that could be interpolated into a message in plaintext. `url` is a yarl.URL
    because ClientRequest.__init__ asserts `type(proxy) is URL`
    (client_reqrep.py:887-888).
    """

    url: URL
    # repr=False matches the project convention for credential-bearing fields
    # (config.py:23, :53; core/security.py:16). base64 is not obfuscation.
    authorization: str | None = field(repr=False)
    # No default. field(repr=False) supplies none, so `authorization` stays
    # required and `source` needs none either. A default would let
    # ProxyChoice(url, header) build with an empty source, and Task 2's bypass
    # union branches on source == "os".
    source: str  # "env" or "os"


@dataclass(frozen=True)
class ProxyNotice:
    """A proxy configuration Ferry found but cannot use."""

    kind: str  # stable identifier, e.g. "all_proxy_only", "socks", "malformed"
    scheme: str
    display: str  # redacted, never carries userinfo
    outcome: str  # what Ferry did instead, e.g. "used the OS proxy", "connected direct"


# Cached because the environment and platform scan costs about 45 us per call
# through the macOS sysconf path. Only the SCAN is cached; the per-host bypass
# decision is memoised separately and keyed by (host, source), because the
# answer differs by proxy source.
_proxy_scan: tuple[dict[str, str], dict[str, str]] | None = None
_bypass_memo: dict[tuple[str, str], bool] = {}
_proxy_notices: tuple[ProxyNotice, ...] = ()


def _os_proxies() -> dict[str, str]:
    """The OS proxy map, or {} where no platform getter exists.

    getproxies_macosx_sysconf is defined only under `if sys.platform == 'darwin'`
    and getproxies_registry only under `elif os.name == 'nt'`; the else branch at
    urllib/request.py:2808-2811 defines neither. Resolving with getattr is what
    lets this module import on Linux, and it is the seam every test patches.
    """
    for name in ("getproxies_macosx_sysconf", "getproxies_registry"):
        fn = getattr(urllib.request, name, None)
        if fn is not None:
            return dict(fn())
    return {}


def _os_proxy_bypass(host: str) -> bool:
    """The OS bypass list (Windows ProxyOverride, macOS ExceptionsList), or False."""
    for name in ("proxy_bypass_macosx_sysconf", "proxy_bypass_registry"):
        fn = getattr(urllib.request, name, None)
        if fn is not None:
            return bool(fn(host))
    return False


def _strip_userinfo(url: URL) -> tuple[URL, str | None]:
    """Split credentials out of a proxy URL, and register both forms as secrets.

    This is the ONLY place in the feature where the plaintext password and the
    encoded header value are both in hand. resolve_proxy receives only the
    encoded form, so if registration does not happen here it cannot happen at
    all.

    BOTH forms are registered. sanitize masks by exact substring
    (core/security.py:52-64), and what actually travels, and what
    RequestInfo.headers holds, is the base64 form, which
    ClientResponseError.__repr__ renders. Registering only the plaintext is the
    v2.12.1 shape, where the desktop app never called register_secret and tokens
    reached ferry.log in clear text for eleven releases.

    Replaces aiohttp's strip_auth_from_url, which is NOT in
    aiohttp.helpers.__all__ and is therefore a private symbol.

    ONE DELIBERATE DIVERGENCE: aiohttp's path encodes with latin1
    (helpers.py:200-201), this uses encode_basic_auth's utf-8 default
    (helpers.py:119). For a non-ASCII password the two produce different base64.
    utf-8 is the better default (latin1 raises UnicodeEncodeError outside
    Latin-1) and encode_basic_auth is the API aiohttp's own deprecation notice
    steers callers to, so the divergence is intended rather than accidental.

    Raises ValueError when the login contains ':' (helpers.py:125-126), which a
    %3A in userinfo decodes to. Task 2's caller guards for this and turns it
    into a ProxyNotice.
    """
    if url.raw_user is None and url.raw_password is None:
        return url, None
    password = url.password or ""
    header = encode_basic_auth(url.user or "", password)
    register_secret("proxy_password", password)
    register_secret("proxy_authorization", header)
    return url.with_user(None), header


def _suppressed_schemes() -> set[str]:
    """Schemes turned off by an empty `<scheme>_proxy`.

    stdlib's screen (urllib/request.py:2536), widened to any case. The widening
    is deliberate and is why this must not filter the environment half: see
    _scheme_map. A k.islower() test would be wrong twice: it misses
    HTTPS_proxy="", which stdlib does pop, and on Windows os.environ upper-cases
    every key, making the rule inert on the one platform where the OS half has
    content to suppress.
    """
    out: set[str] = set()
    for key, value in os.environ.items():
        if len(key) > 6 and key[-6:].lower() == "_proxy" and not value:
            out.add(key[:-6].lower())
    if "REQUEST_METHOD" in os.environ:
        # CVE-2016-1000110: stdlib pops 'http' in a CGI context. Under the merge
        # a popped scheme becomes indistinguishable from an unmentioned one and
        # the OS half would restore it, reversing the behaviour Ferry inherits.
        out.add("http")
    return out


def _scheme_map() -> tuple[dict[str, str], dict[str, str]]:
    """Return (env_proxies, os_proxies), merged per scheme, cached.

    NEVER urllib.request.getproxies(): it is
    `getproxies_environment() or getproxies_<os>()`, so any single *_proxy
    variable short-circuits the OS half. Measured: NO_PROXY=localhost alone
    yields {'no': 'localhost'} and a registry proxy is never seen.
    """
    global _proxy_scan  # noqa: PLW0603
    if _proxy_scan is None:
        # Suppression applies to the OS half ONLY. The environment half is
        # stdlib's job and stdlib already does it: an empty lowercase
        # `<scheme>_proxy` is popped in getproxies_environment's second pass
        # (urllib/request.py:2548-2554). Filtering env here as well would be
        # worse than redundant, because that pop is CASE-SENSITIVE while
        # _suppressed_schemes is not. With `HTTPS_PROXY=""` and
        # `https_proxy="http://x"`, stdlib returns {"https": "http://x"} (the
        # uppercase name fails its `name[-6:] == "_proxy"` test) while Ferry
        # would suppress "https" and connect direct, where curl, requests and
        # stdlib all proxy. No proxy and no notice, which is precisely the
        # outcome the merge exists to prevent.
        #
        # What suppression IS for: stdlib popping a scheme from env does not
        # stop _os_proxies() supplying one for the same scheme, so without this
        # filter the OS half would silently restore a switch the user turned off.
        suppressed = _suppressed_schemes()
        env = dict(urllib.request.getproxies_environment())
        os_side = {k: v for k, v in _os_proxies().items() if k not in suppressed and k not in env}
        _proxy_scan = (env, os_side)
    return _proxy_scan


def _is_bypassed(host: str, source: str, env: dict[str, str]) -> bool:
    """The two bypass lists UNION, they do not alternate.

    Alternation discards an explicitly-set NO_PROXY whenever the proxy came
    from the OS, so a user exempting their self-hosted Stoat has that traffic
    proxied anyway. proxy_bypass_environment is safe to call unconditionally:
    it returns False when the 'no' key is absent (urllib/request.py:2567-2570).

    Memoised by (host, source), and MISSES are memoised too, or every request to
    a bypassed host pays the syscall.
    """
    key = (host, source)
    if key not in _bypass_memo:
        # Silenced for typing only: typeshed does not stub
        # proxy_bypass_environment. Neither it nor getproxies_environment is in
        # urllib.request.__all__ and the stubs happen to cover only the second.
        # CPython defines both at module level on every platform (verified at
        # urllib/request.py:2557 on 3.12.12), unlike the platform getters
        # _os_proxies reaches by getattr, so no fallback is needed here.
        _bypass_memo[key] = bool(
            urllib.request.proxy_bypass_environment(host, env)  # type: ignore[attr-defined]
        ) or (source == "os" and _os_proxy_bypass(host))
    return _bypass_memo[key]


def resolve_proxy_or_raise(url: str | URL) -> ProxyChoice | None:
    """The proxy for `url`, or None. RAISES whatever the platform raises.

    The guards below cover this function's own URL parses only. `_scheme_map`
    and `_is_bypassed` reach platform code Ferry does not control, and stdlib can
    raise there outside (ValueError, TypeError).

    Call this only when you need to tell "could not read the configuration" apart
    from "no proxy is configured", and catch it yourself. `describe_proxy` and
    `run_dce_export` do, because reporting the first as the second is a
    diagnostic that lies. Everything else calls `resolve_proxy`.

    The raise is deliberate and load-bearing. `_scheme_map` assigns its cache
    AFTER the call that can fail, so letting the exception through leaves the
    cache cold and the next call retries the platform. Swallowing it lower down,
    inside `_os_proxies`, was tried and rejected: the assignment then runs with an
    empty result and freezes "no OS proxy" for the life of the process, so one
    transient fault stops every later request using the proxy. Issue #148.
    """
    if os.environ.get("FERRY_DISABLE_PROXY"):
        return None
    try:
        target = URL(url) if isinstance(url, str) else url
    except (ValueError, TypeError):
        return None

    env, os_side = _scheme_map()
    scheme = target.scheme
    if scheme in env:
        raw, source = env[scheme], "env"
    elif scheme in os_side:
        raw, source = os_side[scheme], "os"
    else:
        return None

    try:
        parsed = URL(raw)
        if parsed.scheme.startswith("socks"):
            return None
        stripped, authorization = _strip_userinfo(parsed)
    except (ValueError, TypeError):
        return None

    if _is_bypassed(target.host or "", source, env):
        return None
    return ProxyChoice(url=stripped, authorization=authorization, source=source)


def resolve_proxy(url: str | URL) -> ProxyChoice | None:
    """The proxy for `url`, or None. NEVER raises: this runs inside
    ClientRequest.__init__, so a raise kills the first request of a migration.

    A thin wrapper over `resolve_proxy_or_raise`, so the promise is a property of
    this function rather than of whichever caller happens to sit behind a
    boundary. Before #148 it was delivered by one of three callers, and the other
    two inherited only the narrow guards, which is how `ferry tls-check` could
    traceback on a machine where the platform misbehaved.
    """
    try:
        return resolve_proxy_or_raise(url)
    except Exception:  # noqa: BLE001
        logger.warning(
            "Could not read the proxy configuration; connecting direct.",
            exc_info=True,
        )
        return None


def proxy_notices() -> tuple[ProxyNotice, ...]:
    """Configurations Ferry found but cannot use.

    Forces the scan and evaluates the configuration ITSELF. It must not depend
    on resolve_proxy having run: every caller (engine preflight, build,
    rollback, probe, the GUI export screen) runs before the first request.

    Pure and idempotent: builds once when there is anything to report, then
    returns the same tuple on every call and consumes nothing, so a second
    migration in the same GUI process reports what the first did. The engine
    decides what to emit; this never does.

    "Builds once" is qualified on purpose. An empty result is falsy, so a clean
    machine re-runs the evaluation on every call, and `proxy_notices() is
    proxy_notices()` holds there only because CPython interns the empty tuple.
    The scan underneath (`_scheme_map`) is cached either way, so the repeat
    costs a dict walk rather than a syscall.

    NEVER raises, like `resolve_proxy`, which since #148 is the function that
    defines this contract rather than inheriting it from a caller. `_scheme_map()`
    reaches the platform getters `getproxies_macosx_sysconf` and
    `getproxies_registry` through `_os_proxies`, and stdlib can raise there
    outside (ValueError, TypeError).

    This function keeps its own boundary rather than calling `resolve_proxy`,
    because it evaluates the configuration itself and must run before the first
    request. It also degrades differently, to a visible `unreadable` notice
    rather than a silent direct connection.

    This paragraph has now been wrong twice, in opposite directions, so it is
    worth stating what is actually true. It first claimed a concrete `re.error`
    case reached this path. It then claimed that case was real but belonged to
    the sibling boundary instead. Neither holds: `_proxy_bypass_winreg_override`
    uses `fnmatch`, not `re.match`, on every Python Ferry supports, so no
    `re.error` case exists on either path. Corrected at the final review of the
    proxy branch, alongside the sibling docstring that carried the same claim.

    What justifies this boundary is the platform getters above, which Ferry does
    not control and which stdlib can raise from outside (ValueError, TypeError).

    Every caller runs at preflight, so a raise here would abort the first thing a
    migration does in exchange for a message it was only ever going to print.
    """
    global _proxy_notices  # noqa: PLW0603
    # ABOVE the cache read, not below it. resolve_proxy checks the kill switch
    # first, so today nothing can reach this with a populated cache; a later
    # task that sets the variable after preflight would otherwise keep serving
    # notices for a proxy layer the user has since turned off.
    if os.environ.get("FERRY_DISABLE_PROXY"):
        return ()
    if _proxy_notices:
        return _proxy_notices
    try:
        _proxy_notices = _scan_notices()
    except Exception:  # noqa: BLE001
        logger.warning("Could not read the proxy configuration; connecting direct.", exc_info=True)
        # Degrade VISIBLY. Returning () here would make "Ferry could not read the
        # configuration" indistinguishable from "this machine is clean", which is
        # the exact inversion these notices exist to prevent, and the warning
        # above lands in a log file nobody is reading at preflight.
        #
        # NOT cached, deliberately. With five preflight readers the warning
        # repeats, and that is the better trade than freezing a transient
        # platform error for the life of the process. Do not "fix" it by
        # assigning to _proxy_notices.
        return (
            ProxyNotice(
                kind="unreadable",
                scheme="?",
                display="<unavailable>",
                outcome="Ferry could not read the proxy configuration and connected direct.",
            ),
        )
    return _proxy_notices


def _scan_notices() -> tuple[ProxyNotice, ...]:
    """Evaluate the scanned configuration. Called only by proxy_notices().

    Split out so the never-raises boundary above is a single small statement
    rather than a try wrapped around forty lines, where a later edit could drift
    outside it without anyone noticing.
    """
    env, os_side = _scheme_map()
    found: list[ProxyNotice] = []

    if "all" in env:
        # "Covered" means a scheme that actually RESOLVES, not one whose key is
        # merely present. Key membership alone produces a self-contradicting
        # notice on the commonest SOCKS setup, where shadowsocks, v2ray, `ssh -D`
        # and Tor all document exporting ALL_PROXY, HTTP_PROXY and HTTPS_PROXY
        # together as socks5. Ferry connects direct for both schemes
        # (resolve_proxy returns None at the socks guard), yet a membership test
        # reports "Used the proxy configured for http, https instead." on the
        # line directly above two notices saying "Connected direct."
        covered = [s for s in ("http", "https") if _usable(env.get(s) or os_side.get(s))]
        found.append(
            ProxyNotice(
                kind="all_proxy_only",
                scheme="all",
                display=_safe_display(env["all"]),
                # Name the schemes, never a single source. The source is
                # per-scheme: HTTP_PROXY in the environment with https from the
                # OS would make any one-word answer wrong for one of them.
                #
                # This comment used to hand off to `ferry tls-check` as
                # reporting the source PER SCHEME. It does not. describe_proxy
                # writes one `proxy-source` key inside its scheme loop, so on a
                # mixed configuration the last scheme to resolve wins. Corrected
                # at the final review; the handoff promise is dropped rather
                # than the diagnostic widened, because release.yml asserts on
                # the single key name.
                outcome=(
                    f"Used the proxy configured for {', '.join(covered)} instead."
                    if covered
                    else "Connected direct. ALL_PROXY is not supported (see issue #141)."
                ),
            )
        )

    for scheme, raw in (*env.items(), *os_side.items()):
        if scheme not in ("http", "https"):
            continue
        try:
            parsed = URL(raw)
        except (ValueError, TypeError):
            found.append(
                ProxyNotice(
                    kind="malformed",
                    scheme=scheme,
                    display="<unparseable>",
                    outcome="Connected direct.",
                )
            )
            continue
        if parsed.scheme.startswith("socks"):
            found.append(
                ProxyNotice(
                    kind="socks",
                    scheme=scheme,
                    display=_safe_display(raw),
                    outcome="Connected direct. SOCKS is not supported (see issue #141).",
                )
            )

    return tuple(found)


def _usable(raw: str | None) -> bool:
    """True when `raw` is a proxy Ferry can actually use. Never raises.

    The `raw is None` guard is what makes the call below type-check. `raw` is
    `str | None`, and deleting the guard removes the narrowing, so mypy reports
    `Argument 1 to "URL" has incompatible type "str | None"` [arg-type].
    Measured, and `mypy src/` is in the project verification command, so the gate
    catches that mutant even though no pytest assertion can: at runtime the
    except arm would catch the resulting TypeError from URL(None) and return the
    same False. An earlier version of this comment called the guard
    "documentation rather than behaviour", which was wrong for having checked
    only one of the two tools that guard this file.
    """
    if raw is None:
        return False
    try:
        return not URL(raw).scheme.startswith("socks")
    except (ValueError, TypeError):
        return False


def _safe_display(raw: str) -> str:
    """A proxy URL with userinfo removed, for display. Never raises.

    NOT free of side effects, despite the name. `_strip_userinfo` registers both
    the plaintext password and the encoded header as secrets, so reading notices
    mutates the process-wide registry. That is the intended outcome and this is
    the ONLY path that reaches it for an ALL_PROXY or a socks credential:
    resolve_proxy returns at the socks guard one line before it would strip.
    """
    try:
        return str(_strip_userinfo(URL(raw))[0])
    except (ValueError, TypeError):
        return "<unparseable>"


def format_proxy_notices() -> list[str]:
    """One user-facing line per notice. Lives here, not in a shell, so both
    cli.py and gui.py render the same text without gui.py importing cli.py.
    """
    return [
        f"Proxy configuration Ferry cannot use: {n.display} ({n.scheme}). {n.outcome}"
        for n in proxy_notices()
    ]


def proxy_hint(exc: BaseException, *, target: str) -> str | None:
    """Actionable guidance if `exc`'s chain holds a proxy failure.

    Returns a MESSAGE, never an exception type, for the reason in tls_hint's
    docstring. `target` has NO default: a str | None = None would let a
    half-wired call site type-check clean and emit exactly the targetless
    message this parameter exists to prevent.

    Proxy identity comes from request_info.real_url, or .host/.port on
    ClientProxyConnectionError, which has no request_info at all. NEVER from
    request_info.url, which is the TARGET.
    """
    # Guarded for the same reason resolve_proxy guards the identical
    # construction: every call site is inside an `except` block, and
    # structure.py passes state.autumn_url, which is SERVER-supplied. A raise
    # here replaces the error the user was about to be told about with a
    # ValueError from the handler that was meant to explain it.
    try:
        target_host = URL(target).host or target
    except (ValueError, TypeError):
        target_host = target
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        proxy = _proxy_identity(current)
        if proxy is not None:
            detail = ""
            status = getattr(current, "status", None)
            if status == 407:
                detail = " The proxy requires authentication; put credentials in the proxy URL."
            elif status == 403:
                detail = " The proxy refused the connection."
            return (
                f" The request to {target_host} went through the proxy at {proxy}, "
                f"which did not accept it.{detail} Set FERRY_DISABLE_PROXY=1 to "
                "bypass the proxy, or NO_PROXY to exempt this host."
            )
        current = current.__cause__ or current.__context__
    return None


def _proxy_identity(exc: BaseException) -> str | None:
    """`host:port` of the proxy, or None if `exc` is not a proxy failure.

    Also matches a certificate error whose host equals the resolved proxy: with
    an https:// proxy, connector.py:1630-1632 builds the error from proxy_req's
    key, so tls_hint would otherwise tell the user to point SSL_CERT_FILE at a
    bundle for a host they never configured.
    """
    if isinstance(exc, aiohttp.ClientHttpProxyError):
        real = exc.request_info.real_url
        return f"{real.host}:{real.port}"
    if isinstance(exc, aiohttp.ClientProxyConnectionError):
        return f"{getattr(exc, 'host', '?')}:{getattr(exc, 'port', '?')}"
    if isinstance(exc, aiohttp.ClientConnectorCertificateError):
        host, port = getattr(exc, "host", None), getattr(exc, "port", None)
        scan = _proxy_scan
        # `host is not None` guards the degenerate match. The getattr defaults
        # are None and a scheme-less value parses to host=None, so without it
        # None == None matches and the hint renders "None:None".
        #
        # It and the `candidate.host is None` check below are ALTERNATIVES, not
        # complements: either alone closes that match, so with this one present
        # the other cannot change any outcome and no test can tell them apart.
        # Both are kept because each states its own half of the invariant at the
        # point it applies. Do not read the pair as two necessary conditions.
        if scan is not None and host is not None:
            for key, raw in (*scan[0].items(), *scan[1].items()):
                # http and https ONLY, and not because the others are unlikely.
                # The env half is getproxies_environment() verbatim, which keys
                # NO_PROXY as 'no' (measured: NO_PROXY=https://stoat.internal:8443
                # yields {'no': 'https://stoat.internal:8443'}). Scheme-prefixing
                # NO_PROXY is a common mistake, and without this filter it makes
                # every genuine certificate error for that host resolve as a
                # proxy identity. Because the sites read
                # `proxy_hint(...) or tls_hint(...)`, the correct SSL_CERT_FILE
                # advice would then be REPLACED by proxy advice naming a host
                # that is not a proxy -- on self-hosted Stoat behind a private
                # CA, which is exactly where that advice matters.
                #
                # These two keys are also the only ones Ferry can proxy through:
                # resolve_proxy indexes env/os_side by the TARGET's scheme.
                if key not in ("http", "https"):
                    continue
                try:
                    candidate = URL(raw)
                except (ValueError, TypeError):
                    continue
                if candidate.host is None:
                    continue
                if candidate.host == host and candidate.port == port:
                    return f"{host}:{port}"
    return None


def describe_proxy() -> dict[str, str]:
    """Proxy state, for `ferry tls-check` and CI. Never prints userinfo.

    Reports what resolution ACTUALLY produced, not what is configured. A
    diagnostic that reports something other than what happens is the failure
    v2.13.0 avoided by reading trust-source from a flag.
    """
    if os.environ.get("FERRY_DISABLE_PROXY"):
        return {
            "proxy-http": "none",
            "proxy-https": "none",
            "proxy-source": "none",
            "proxy-disabled": "true",
        }
    out = {"proxy-disabled": "false", "proxy-source": "none"}
    any_failed = False
    for scheme in ("http", "https"):
        # The RAISING sibling, deliberately. resolve_proxy returns None for a read
        # failure and for a clean machine alike, so calling it here would report
        # "none" for both, and CI would not notice: release.yml asserts only that
        # the proxy-source key is present, whatever its value. A diagnostic that
        # reports "no proxy" when it means "could not look" is the failure this
        # function's own docstring is about. Issue #148.
        try:
            choice = resolve_proxy_or_raise(f"{scheme}://example.invalid/")
        except Exception:  # noqa: BLE001
            logger.warning("Could not read the proxy configuration for %s.", scheme, exc_info=True)
            out[f"proxy-{scheme}"] = "unreadable"
            any_failed = True
            continue
        if choice is None:
            out[f"proxy-{scheme}"] = "none"
        else:
            out[f"proxy-{scheme}"] = f"{choice.url.host}:{choice.url.port}"
            out["proxy-source"] = choice.source
    # A real resolved source wins the summary. Overwriting it with "unreadable"
    # because the OTHER scheme failed prints a live proxy next to a line denying
    # it, which is the contradiction this design rejected an alternative for. The
    # failing scheme still reports its own failure in its own key, so nothing is
    # hidden.
    if any_failed and out["proxy-source"] == "none":
        out["proxy-source"] = "unreadable"
    return out


def proxy_error_is_permanent(exc: BaseException) -> bool:
    """True when no retry can help.

    A 502, 503, 504 or connect timeout CAN recover, and those are exactly the
    shapes api.py and autumn.py already retry in the direct case. Keeping this
    separate from the hint is what stops proxy support turning a transient
    upstream blip into a hard abort.
    """
    if isinstance(exc, aiohttp.ClientHttpProxyError):
        return exc.status in (403, 407)
    return isinstance(exc, aiohttp.ClientProxyConnectionError)


class _FerryRequest(aiohttp.ClientRequest):
    """Fills in the proxy when the caller gave none.

    Subclassing ClientRequest is safe. Subclassing ClientSession is NOT:
    aioresponses patches ClientSession._request on the class, and a subclass
    defining _request would shadow the patch and disable HTTP mocking across 21
    test modules.

    Note that self._request_class(...) is reached only at aiohttp
    client.py:780, so under aioresponses this class is never constructed. That
    is why the factory-installs test exists separately.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if kwargs.get("proxy") is None:
            # `"url" in kwargs`, not `kwargs.get("url") or ...`: yarl.URL defines
            # __bool__ (yarl/_url.py:566-567), so a falsy url= keyword would be
            # discarded and args[1] read instead.
            url = kwargs["url"] if "url" in kwargs else (args[1] if len(args) > 1 else None)
            choice = self._resolve_or_direct(url)
            if choice is not None:
                kwargs["proxy"] = choice.url
                if choice.authorization is not None:
                    # A FRESH mapping per request, so one caller's headers can
                    # never leak into the next. CIMultiDict, not dict: HTTP
                    # header names are case-insensitive, and a plain dict's
                    # setdefault would not see a caller's `proxy-authorization`,
                    # insert a second entry, and send TWO credentials to the
                    # proxy (connector.py:606-610 writes both).
                    merged: CIMultiDict[str] = CIMultiDict(kwargs.get("proxy_headers") or {})
                    merged.setdefault("Proxy-Authorization", choice.authorization)
                    kwargs["proxy_headers"] = merged
        super().__init__(*args, **kwargs)

    @staticmethod
    def _resolve_or_direct(url: object) -> ProxyChoice | None:
        """Resolve, or fall through to a direct connection. NEVER raises.

        This WAS the boundary where the "resolution never raises" invariant
        actually held, which was the defect #148 fixed: the invariant belonged to
        a caller rather than to the function that promised it, so the other two
        callers inherited only the narrow guards. `resolve_proxy` now holds it.

        The boundary stays as defense in depth over platform code Ferry does not
        control, not as habit.

        What the `if url is None` guard above buys is worth stating precisely,
        because #148 changed it. `resolve_proxy_or_raise(None)` raises
        `AttributeError` at `target.scheme`, from Ferry's own frame rather than
        from a platform seam. `resolve_proxy` now catches that itself and returns
        None, so dropping the guard would no longer produce an exception here.
        It would produce a logged warning with a full traceback on every request
        that never had a url to resolve. The guard prevents the noise, not a
        crash, and the test below pins it by asserting the resolver is never
        called rather than by asserting on a symptom.

        An earlier version of this docstring justified the boundary with a
        specific mechanism: that `_proxy_bypass_winreg_override` calls
        `re.match(test, host)` and so raises `re.error` on a ProxyOverride entry
        holding a regex metacharacter. That is wrong on every Python Ferry
        supports. CPython 3.11 and 3.12 both use `fnmatch(host, test)` there, and
        `fnmatch.translate` escapes an unterminated `[` rather than raising. The
        `re.match` form lives in `requests`, not the standard library. Corrected
        at the final review of this branch.

        The boundary stays, as defense in depth over that platform code. One
        unguarded path is still real on 3.12: `_proxy_bypass_macosx_sysconf`
        indexes `proxy_settings['exclude_simple']` without a guard, which raises
        `KeyError`, and `KeyError` subclasses Exception directly rather than
        ValueError or TypeError.
        """
        if url is None:
            return None
        try:
            # `url` is typed `object` because it arrives through **kwargs, and the
            # `is None` check above is the only narrowing available without
            # re-parsing. resolve_proxy accepts `str | URL` and guards its own
            # parse, so a wrong type becomes None rather than a raise.
            return resolve_proxy(url)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            logger.warning(
                "Could not resolve a proxy for this request; connecting direct.",
                exc_info=True,
            )
            return None
