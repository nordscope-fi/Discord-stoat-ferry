"""#964: the local web interface must refuse a Host header that is not loopback.

A hostile site can rebind its own hostname to 127.0.0.1. The browser then sends
requests to Ferry with ``Host: evil.example:8765``, and that page can reach the tool
pages that write files. The guard rejects every HTTP and websocket request whose Host
is not a loopback name.
"""

from __future__ import annotations

from typing import Any

import pytest
from nicegui import app

from discord_ferry import gui

Message = dict[str, Any]


async def _drive(stack: Any, scope: dict[str, Any]) -> list[Message]:
    """Run one ASGI scope through ``stack`` and return what it sent."""
    sent: list[Message] = []
    inbox: list[Message] = [{"type": "websocket.connect"}] if scope["type"] == "websocket" else []

    async def receive() -> Message:
        if inbox:
            return inbox.pop(0)
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    await stack(scope, receive, send)
    return sent


def _scope(
    kind: str,
    host: str | None,
    path: str = "/",
    *,
    origin: str | None = None,
    method: str = "GET",
    extra: tuple[tuple[bytes, bytes], ...] = (),
) -> dict[str, Any]:
    headers = [] if host is None else [(b"host", host.encode())]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    headers.extend(extra)
    scope: dict[str, Any] = {
        "type": kind,
        "asgi": {"version": "3.0"},
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", gui._PORT),
        "scheme": "ws" if kind == "websocket" else "http",
    }
    if kind == "http":
        scope["method"] = method
        scope["http_version"] = "1.1"
    else:
        scope["subprotocols"] = []
    return scope


async def _reaches_inner(host: str | None, kind: str = "http") -> bool:
    reached: list[bool] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        reached.append(True)

    guard = gui._LoopbackHostGuard(inner)
    await _drive(guard, _scope(kind, host))
    return bool(reached)


class TestGuardOnItsOwn:
    @pytest.mark.parametrize(
        "host",
        [
            f"127.0.0.1:{gui._PORT}",
            f"localhost:{gui._PORT}",
            f"[::1]:{gui._PORT}",
            f"LOCALHOST:{gui._PORT}",
            "127.0.0.1",
            "localhost",
            "[::1]",
        ],
    )
    @pytest.mark.parametrize("kind", ["http", "websocket"])
    async def test_loopback_host_passes(self, host: str, kind: str) -> None:
        assert await _reaches_inner(host, kind)

    @pytest.mark.parametrize(
        "host",
        [
            f"evil.example:{gui._PORT}",
            "evil.example",
            "127.0.0.1.evil.example",
            f"127.0.0.1.evil.example:{gui._PORT}",
            f"localhost.evil.example:{gui._PORT}",
            f"evil.example:{gui._PORT}@127.0.0.1",
            f"127.0.0.1:{gui._PORT + 1}",
            f"localhost:{gui._PORT + 1}",
            f"[::1]:{gui._PORT + 1}",
            "127.0.0.1:",
            "127.0.0.1:abc",
            "0.0.0.0:8765",
            "",
            None,
        ],
    )
    @pytest.mark.parametrize("kind", ["http", "websocket"])
    async def test_other_host_is_rejected(self, host: str | None, kind: str) -> None:
        assert not await _reaches_inner(host, kind)

    async def test_http_rejection_is_a_400(self) -> None:
        async def inner(scope: Any, receive: Any, send: Any) -> None:
            raise AssertionError("must not be reached")

        sent = await _drive(gui._LoopbackHostGuard(inner), _scope("http", "evil.example:8765"))
        assert sent[0]["type"] == "http.response.start"
        assert sent[0]["status"] == 400

    async def test_websocket_rejection_closes_before_accept(self) -> None:
        async def inner(scope: Any, receive: Any, send: Any) -> None:
            raise AssertionError("must not be reached")

        sent = await _drive(gui._LoopbackHostGuard(inner), _scope("websocket", "evil.example"))
        assert [m["type"] for m in sent] == ["websocket.close"]

    async def test_lifespan_is_not_filtered(self) -> None:
        reached: list[bool] = []

        async def inner(scope: Any, receive: Any, send: Any) -> None:
            reached.append(True)

        guard = gui._LoopbackHostGuard(inner)
        await guard({"type": "lifespan"}, None, None)  # type: ignore[arg-type]
        assert reached


class TestRegisteredOnTheRealApp:
    @pytest.fixture
    def guarded_stack(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        # Work on a copy so the module-global NiceGUI app is not left guarded for
        # later tests, which drive it under a Host of "test".
        monkeypatch.setattr(app, "user_middleware", list(app.user_middleware))
        gui._install_host_guard()
        return app.build_middleware_stack()

    async def test_run_gui_installs_the_guard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(app, "user_middleware", list(app.user_middleware))
        monkeypatch.setattr(gui, "_HAS_WEBVIEW", False)
        monkeypatch.setattr(gui.ui, "run", lambda **_kwargs: None)
        monkeypatch.setattr(gui, "_teardown_native_window", lambda: None)
        gui._run_gui()
        assert any(m.cls is gui._LoopbackHostGuard for m in app.user_middleware)

    def test_installing_twice_adds_one_guard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(app, "user_middleware", list(app.user_middleware))
        gui._install_host_guard()
        gui._install_host_guard()
        assert sum(m.cls is gui._LoopbackHostGuard for m in app.user_middleware) == 1

    async def test_page_with_bad_host_is_400(self, guarded_stack: Any) -> None:
        sent = await _drive(
            guarded_stack, _scope("http", "evil.example:8765", "/tools/blueprint-export")
        )
        assert sent[0]["status"] == 400

    async def test_socketio_polling_with_bad_host_is_400(self, guarded_stack: Any) -> None:
        sent = await _drive(
            guarded_stack, _scope("http", "evil.example:8765", "/_nicegui_ws/socket.io/")
        )
        assert sent[0]["status"] == 400

    async def test_socketio_websocket_with_bad_host_is_closed(self, guarded_stack: Any) -> None:
        sent = await _drive(
            guarded_stack, _scope("websocket", "evil.example:8765", "/_nicegui_ws/socket.io/")
        )
        assert [m["type"] for m in sent] == ["websocket.close"]

    @pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost:8765"])
    async def test_loopback_host_is_served(self, guarded_stack: Any, host: str) -> None:
        sent = await _drive(guarded_stack, _scope("http", host, "/definitely-not-a-route"))
        # Past the guard, the app answers for itself: a 404, not the guard's 400.
        assert sent[0]["status"] == 404


class TestGuardIsOutermost:
    """#1295: NiceGUI adds its session layers after Ferry's guard, and Starlette puts
    the last-added layer outermost. The request-tracking layer writes a session id on
    every request, so a guard left inside it hands a refused request a cookie."""

    @pytest.fixture
    def created_users(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        from nicegui import storage

        created: list[str] = []

        async def record(session_id: str) -> None:
            created.append(session_id)

        monkeypatch.setattr(storage.Storage, "secret", storage.Storage.secret)
        monkeypatch.setattr(app.storage, "_create_user_storage", record)
        return created

    async def test_refused_request_gets_no_cookie_and_no_user_storage(
        self, created_users: list[str]
    ) -> None:
        from nicegui import storage

        gui._install_host_guard()
        # What `Server.run` does after Ferry's install, before the first request.
        storage.set_storage_secret("test-secret")
        app.middleware_stack = None
        stack = app.build_middleware_stack()

        sent = await _drive(stack, _scope("http", "evil.example:8765", "/"))

        assert sent[0]["status"] == 400
        assert b"set-cookie" not in [key for key, _ in sent[0]["headers"]]
        assert created_users == []

    def test_reorder_reads_the_list_at_build_time_and_installs_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from starlette.middleware import Middleware
        from starlette.middleware.sessions import SessionMiddleware

        gui._install_host_guard()
        first = app.build_middleware_stack
        gui._install_host_guard()
        assert app.build_middleware_stack is first

        session = Middleware(SessionMiddleware, secret_key="x")
        monkeypatch.setattr(app, "user_middleware", [session, Middleware(gui._LoopbackHostGuard)])
        app.build_middleware_stack()
        assert [m.cls for m in app.user_middleware] == [gui._LoopbackHostGuard, SessionMiddleware]

        monkeypatch.setattr(app, "user_middleware", [session])
        app.build_middleware_stack()
        assert app.user_middleware == [session]


_FERRY = f"127.0.0.1:{gui._PORT}"
_POLLING = "/_nicegui_ws/socket.io/"


async def _drive_guard(scope: dict[str, Any]) -> tuple[list[Message], bool]:
    reached: list[bool] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        reached.append(True)

    sent = await _drive(gui._LoopbackHostGuard(inner), scope)
    return sent, bool(reached)


class TestOriginCheck:
    """#1295: NiceGUI's live connection answers any Origin with credentialed CORS,
    so the guard refuses a request a browser marks as coming from another site."""

    @pytest.mark.parametrize("origin", ["https://evil.example", "null", "http://127.0.0.1:9999"])
    @pytest.mark.parametrize(
        ("method", "path", "extra"),
        [
            ("GET", _POLLING, ()),
            ("OPTIONS", _POLLING, ((b"access-control-request-method", b"POST"),)),
            ("GET", "/", ()),
            ("POST", "/_nicegui/client/upload", ()),
        ],
    )
    async def test_foreign_origin_http_is_refused(
        self, origin: str, method: str, path: str, extra: tuple[tuple[bytes, bytes], ...]
    ) -> None:
        sent, reached = await _drive_guard(
            _scope("http", _FERRY, path, origin=origin, method=method, extra=extra)
        )
        assert not reached
        assert sent[0]["status"] == 400
        assert sent[1]["body"] == b"Invalid origin header"
        names = [key for key, _ in sent[0]["headers"]]
        assert not [n for n in names if n.startswith(b"access-control-")]
        assert b"set-cookie" not in names

    @pytest.mark.parametrize("origin", ["https://evil.example", "null"])
    async def test_foreign_origin_websocket_is_closed_before_accept(self, origin: str) -> None:
        sent, reached = await _drive_guard(_scope("websocket", _FERRY, _POLLING, origin=origin))
        assert not reached
        assert [m["type"] for m in sent] == ["websocket.close"]

    @pytest.mark.parametrize(
        "origin",
        [
            "http://127.0.0.1:8765",
            "http://localhost:8765",
            "http://[::1]:8765",
            "HTTP://LOCALHOST:8765",
        ],
    )
    @pytest.mark.parametrize("kind", ["http", "websocket"])
    async def test_ferry_origin_passes(self, origin: str, kind: str) -> None:
        _sent, reached = await _drive_guard(_scope(kind, _FERRY, _POLLING, origin=origin))
        assert reached

    @pytest.mark.parametrize("origin", ["http://localhost", "http://127.0.0.1", "http://[::1]"])
    async def test_origin_without_the_port_is_refused(self, origin: str) -> None:
        """#1700: a port-less origin is a page on port 80 of this machine, never Ferry."""
        sent, reached = await _drive_guard(_scope("http", _FERRY, _POLLING, origin=origin))
        assert not reached
        assert sent[0]["status"] == 400
        assert sent[1]["body"] == b"Invalid origin header"
        sent, reached = await _drive_guard(_scope("websocket", _FERRY, _POLLING, origin=origin))
        assert not reached
        assert [m["type"] for m in sent] == ["websocket.close"]

    def test_host_list_keeps_its_port_less_names(self) -> None:
        """#1700 changes origins only; the Host check still accepts port-less names."""
        assert gui._allowed_host_headers() == {
            "127.0.0.1",
            "127.0.0.1:8765",
            "localhost",
            "localhost:8765",
            "[::1]",
            "[::1]:8765",
        }

    def test_origins_follow_the_gui_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gui, "_PORT", 9876)
        assert gui._allowed_origins() == {
            "http://127.0.0.1:9876",
            "http://localhost:9876",
            "http://[::1]:9876",
        }

    async def test_no_origin_passes(self) -> None:
        _sent, reached = await _drive_guard(_scope("http", _FERRY, "/"))
        assert reached

    async def test_ferry_origin_upload_passes(self) -> None:
        _sent, reached = await _drive_guard(
            _scope(
                "http", _FERRY, "/_nicegui/client/upload", origin=f"http://{_FERRY}", method="POST"
            )
        )
        assert reached

    async def test_bad_host_still_names_the_host(self) -> None:
        sent, _reached = await _drive_guard(
            _scope("http", "evil.example", "/", origin="https://evil.example")
        )
        assert sent[1]["body"] == b"Invalid host header"
