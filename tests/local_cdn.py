"""A real local HTTP server for the bounded-download tests (#980, #981).

aioresponses cannot stand in for multi-megabyte bodies (it trips an aiohttp 3.14
parser assertion), and a small monkeypatched cap would hide the streaming
behaviour the fix exists for. This serves real bytes over 127.0.0.1 and counts
what it actually sent, so a test can tell "the client stopped reading" from
"the client read everything and threw it away".
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

CHUNK = b"x" * 65536
# Large enough that a client reading it all is unmistakable next to any cap.
ENDLESS_LIMIT = 30_000_000


class LocalCDN:
    """Routes: /body/N, /chunked/N, /lying/N, /endless, /redirect, /cutoff/N."""

    def __init__(self) -> None:
        self.sent = 0
        self.target_hits = 0
        self.last_headers: dict[str, str] = {}
        # Where /redirect sends the client. Default: this server's own /target.
        self.redirect_to = "/target"
        self._server: TestServer | None = None

    def url(self, path: str) -> str:
        assert self._server is not None
        return str(self._server.make_url(path))

    async def _write(self, resp: web.StreamResponse, data: bytes) -> None:
        await resp.write(data)
        self.sent += len(data)

    async def _body(self, request: web.Request, *, chunked: bool) -> web.StreamResponse:
        self.last_headers = dict(request.headers)
        total = int(request.match_info["n"])
        resp = web.StreamResponse()
        resp.content_type = "image/png"  # the avatar downloader refuses non-images
        if chunked:
            resp.enable_chunked_encoding()
        else:
            resp.content_length = total
        await resp.prepare(request)
        left = total
        while left > 0:
            piece = CHUNK[: min(left, len(CHUNK))]
            await self._write(resp, piece)
            left -= len(piece)
        await resp.write_eof()
        return resp

    async def _sized(self, request: web.Request) -> web.StreamResponse:
        return await self._body(request, chunked=False)

    async def _chunked(self, request: web.Request) -> web.StreamResponse:
        return await self._body(request, chunked=True)

    async def _lying(self, request: web.Request) -> web.StreamResponse:
        """Declare N bytes, send one chunk, then stall until the client leaves."""
        resp = web.StreamResponse()
        resp.content_type = "image/png"
        resp.content_length = int(request.match_info["n"])
        await resp.prepare(request)
        await self._write(resp, CHUNK)
        await asyncio.sleep(60)
        return resp

    async def _endless(self, request: web.Request) -> web.StreamResponse:
        """No Content-Length: keep sending until the client stops reading."""
        resp = web.StreamResponse()
        resp.content_type = "image/png"
        resp.enable_chunked_encoding()
        await resp.prepare(request)
        while self.sent < ENDLESS_LIMIT:
            await self._write(resp, CHUNK)
        await resp.write_eof()
        return resp

    async def _redirect(self, request: web.Request) -> web.StreamResponse:
        raise web.HTTPFound(self.redirect_to)

    async def _target(self, request: web.Request) -> web.StreamResponse:
        self.target_hits += 1
        self.last_headers = dict(request.headers)
        return web.Response(body=b"redirected-body", content_type="image/png")

    async def _cutoff(self, request: web.Request) -> web.StreamResponse:
        """Declare N bytes, send part of them, then drop the connection."""
        resp = web.StreamResponse()
        resp.content_length = int(request.match_info["n"])
        await resp.prepare(request)
        await self._write(resp, CHUNK)
        transport: Any = request.transport
        transport.close()
        return resp

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/body/{n}", self._sized)
        app.router.add_get("/chunked/{n}", self._chunked)
        app.router.add_get("/lying/{n}", self._lying)
        app.router.add_get("/endless", self._endless)
        app.router.add_get("/redirect", self._redirect)
        app.router.add_get("/target", self._target)
        app.router.add_get("/cutoff/{n}", self._cutoff)
        return app


@contextlib.asynccontextmanager
async def local_cdn() -> AsyncIterator[LocalCDN]:
    cdn = LocalCDN()
    cdn._server = TestServer(cdn.app())
    await cdn._server.start_server()
    try:
        yield cdn
    finally:
        await cdn._server.close()


class RewritingSession:
    """A real ClientSession whose ``get`` sends any URL to the local server.

    The production code builds fixed https://cdn.discordapp.com URLs. This keeps
    the real client, real streaming and real redirect handling, and only swaps
    where the request lands. The kwargs each call passed are recorded.
    """

    def __init__(self, session: ClientSession, target_url: str) -> None:
        self._session = session
        self._target_url = target_url
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> Any:
        self.calls.append(dict(kwargs))
        return self._session.get(self._target_url, **kwargs)
