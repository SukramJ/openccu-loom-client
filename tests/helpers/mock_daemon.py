# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Lightweight aiohttp-based mock of the openccu-loom REST daemon.

A real in-process ``aiohttp`` server (started via ``AppRunner`` /
``TCPSite``, mirroring aiohomematic's ``MockJsonRpc`` helper) replaces
the previous ``aioresponses`` URL-stubbing. A single catch-all handler
serves responses that tests register per ``(method, path)`` ahead of —
or during — the request, so the ergonomics stay close to the old stub
API while exercising the genuine ``aiohttp`` client path.

Responses for the same ``(method, path)`` are consumed in registration
order (FIFO), so retry tests can queue e.g. a ``502`` followed by a
``200``. Once a single response remains it is reused for any further
calls, which keeps "retry exhausted" tests (many identical failures)
from needing an exact call count.

Every received request is recorded in :attr:`MockDaemon.requests` for
body / header / query assertions.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import json
from typing import Any

from aiohttp import web

from openccu_loom_client import BearerAuth, LoomConfig


@dataclass(slots=True)
class RecordedRequest:
    """One request the mock daemon received, captured for assertions."""

    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        """Decode the recorded request body as JSON."""
        return json.loads(self.body) if self.body else None


@dataclass(slots=True)
class _StubResponse:
    """A queued response for a ``(method, path)`` key."""

    status: int = 200
    payload: Any = None
    body: bytes | None = None
    content_type: str = "application/json"
    delay: float = 0.0


class MockDaemon:
    """In-process aiohttp mock of the daemon's REST surface."""

    def __init__(self) -> None:
        """Build the app with a catch-all route; the server starts in :meth:`start`."""
        self._app = web.Application()
        self._app.router.add_route("*", "/{tail:.*}", self._handle)
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._responses: dict[tuple[str, str], deque[_StubResponse]] = {}
        self.requests: list[RecordedRequest] = []
        self.host = "127.0.0.1"
        self.port = 0

    # ---- response registration (full REST path, e.g. "/api/v1/info") ----

    def add_response(
        self,
        method: str,
        path: str,
        *,
        payload: Any = None,
        status: int = 200,
        body: bytes | None = None,
        content_type: str = "application/json",
        delay: float = 0.0,
    ) -> None:
        """Queue a response for the next call to ``method path`` (``delay`` s server-side)."""
        key = (method.upper(), path)
        self._responses.setdefault(key, deque()).append(
            _StubResponse(status=status, payload=payload, body=body, content_type=content_type, delay=delay)
        )

    def get(self, path: str, **kwargs: Any) -> None:
        """Queue a response for a ``GET`` to ``path``."""
        self.add_response("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> None:
        """Queue a response for a ``POST`` to ``path``."""
        self.add_response("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> None:
        """Queue a response for a ``PUT`` to ``path``."""
        self.add_response("PUT", path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> None:
        """Queue a response for a ``PATCH`` to ``path``."""
        self.add_response("PATCH", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> None:
        """Queue a response for a ``DELETE`` to ``path``."""
        self.add_response("DELETE", path, **kwargs)

    # ---- lifecycle ----

    async def start(self) -> MockDaemon:
        """Start the server on an ephemeral port and record the bound host/port."""
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host=self.host, port=0)
        await self._site.start()
        bound = self._runner.addresses[0]
        self.host, self.port = bound[0], bound[1]
        return self

    async def stop(self) -> None:
        """Stop and clean up the server."""
        if self._site is not None:
            await self._site.stop()
        if self._runner is not None:
            await self._runner.cleanup()

    @property
    def config(self) -> LoomConfig:
        """Return a LoomConfig pointing the client at this mock server."""
        return LoomConfig(
            host=self.host,
            port=self.port,
            tls=False,
            auth=BearerAuth(token="testtoken1234", label="test"),
            request_timeout_seconds=1.0,
        )

    # ---- request handling ----

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        body = await request.read()
        self.requests.append(
            RecordedRequest(
                method=request.method,
                path=request.path,
                query=dict(request.query),
                headers=dict(request.headers),
                body=body,
            )
        )
        queue = self._responses.get((request.method, request.path))
        if not queue:
            return web.json_response(
                {
                    "type": "https://openccu-loom.dev/errors/not_found",
                    "title": f"no stub registered for {request.method} {request.path}",
                    "status": 404,
                },
                status=404,
            )
        stub = queue.popleft() if len(queue) > 1 else queue[0]
        if stub.delay:
            await asyncio.sleep(stub.delay)
        if stub.payload is not None:
            return web.json_response(stub.payload, status=stub.status)
        if stub.body is not None:
            return web.Response(body=stub.body, status=stub.status, content_type=stub.content_type)
        return web.Response(status=stub.status)
