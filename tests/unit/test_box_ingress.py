# SPDX-License-Identifier: MIT
# Copyright (C) 2026 SukramJ.

"""
Box-ingress mode: the client reaching the daemon through an openccu-lite box.

``FakeBoxGate`` is a real in-process aiohttp server playing the box's web
server and its gate in front of ``/addons/loom/``, as occulited's
``docs/system-api.md`` (*The gate and API tokens*) describes it for
openccu-lite 1.0.0-dev.36: a request passes when its
``Authorization: Bearer`` names a token holding ``addon:openccu-loom`` or
``*``; an unknown token is answered 401, a token without the scope 403
``forbidden``. A request the gate lets through has the token handed on in
``X-Occulite-Session`` with ``X-Occulite-Auth: token``, and is served from
responses registered per ``(method, path)`` (the daemon-side path, prefix
stripped); the WS endpoint behind it accepts upgrades.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
import json
from typing import Any

import aiohttp
from aiohttp import web
import pytest

from openccu_loom_client import (
    BearerAuth,
    BoxIngressConfig,
    LoomBoxGateError,
    LoomBoxTokenError,
    LoomConfig,
    LoomHttpError,
    LoomTransportError,
    NoAuth,
)
from openccu_loom_client.transport import HttpTransport, WsTransport
import openccu_loom_client.transport.ws as ws_module
from openccu_loom_client.wire import DAEMON_API_VERSION
from tests.helpers import MockDaemon

_TOKEN = "olt_0123456789abcdef0123456789abcdef"
_OTHER_ADDON_TOKEN = "olt_fedcba9876543210fedcba9876543210"
_FULL_TOKEN = "olt_ffffffffffffffffffffffffffffffff"
_PREFIX = "/addons/loom"
_SHELL_LOGIN = "/shell/login"

# Same shape as the handshake payload in tests/unit/test_http_transport.py.
_INFO_RESPONSE: dict[str, Any] = {
    "version": "1.2.3",
    "api_version": DAEMON_API_VERSION,
    "commit": "deadbeef",
    "build_date": "2026-05-24T10:00:00Z",
    "addon_build": False,
    "started_at": "2026-05-24T10:01:00Z",
    "uptime": "PT60S",
    "capabilities": ["rest.v1", "ws.broadcasts.v1", "errors.problem_details.v1"],
    "schema_digest": "",
    "config_ui_url": "",
}


@dataclass(slots=True)
class GateRequest:
    """One request the fake box received, as the client sent it."""

    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: bytes
    # Set when the gate let the request through: the headers the daemon would
    # see (client-sent identity headers stripped, the accepted token stamped).
    forwarded_headers: dict[str, str] | None = None
    refused: int | None = None


@dataclass(slots=True)
class _Stub:
    status: int = 200
    payload: Any = None
    content_type: str | None = None


@dataclass(slots=True)
class FakeBoxGate:
    """In-process fake of an openccu-lite box's web server and token gate."""

    host: str = "127.0.0.1"
    port: int = 0
    # secret -> scopes, as the box stores them.
    tokens: dict[str, list[str]] = field(
        default_factory=lambda: {
            _TOKEN: ["addon:openccu-loom"],
            _OTHER_ADDON_TOKEN: ["addon:redmatic"],
            _FULL_TOKEN: ["*"],
        }
    )
    requests: list[GateRequest] = field(default_factory=list)
    ws_connections: list[str] = field(default_factory=list)
    # Plays a box older than the token gate, which redirects every caller it
    # cannot authenticate to the shell login.
    legacy_redirects: bool = False
    _responses: dict[tuple[str, str], deque[_Stub]] = field(default_factory=dict)
    _active_ws: list[web.WebSocketResponse] = field(default_factory=list)
    _ws_accepted: asyncio.Condition = field(default_factory=asyncio.Condition)
    _runner: web.AppRunner | None = None

    def add_response(
        self, method: str, path: str, *, payload: Any = None, status: int = 200, content_type: str | None = None
    ) -> None:
        """Queue a daemon response for ``method path`` (daemon-side path, prefix stripped)."""
        self._responses.setdefault((method.upper(), path), deque()).append(
            _Stub(status=status, payload=payload, content_type=content_type)
        )

    def revoke(self, token: str) -> None:
        """Delete a token on the box."""
        self.tokens.pop(token, None)

    async def drop_websockets(self) -> None:
        """Close every open WS connection from the server side."""
        for ws in list(self._active_ws):
            await ws.close()

    async def start(self) -> FakeBoxGate:
        """Start on an ephemeral port."""
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host=self.host, port=0)
        await site.start()
        self.port = self._runner.addresses[0][1]
        return self

    async def stop(self) -> None:
        """Stop the server."""
        await self.drop_websockets()
        if self._runner is not None:
            await self._runner.cleanup()

    def config(self, *, token: str = _TOKEN) -> LoomConfig:
        """Return a LoomConfig in box-ingress mode pointed at this fake."""
        return LoomConfig(
            host=self.host,
            # Deliberately a port nothing listens on: ingress mode must not use it.
            port=1,
            tls=False,
            auth=NoAuth(),
            request_timeout_seconds=2.0,
            box_ingress=BoxIngressConfig(token=token, port=self.port),
        )

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        rec = GateRequest(
            method=request.method,
            path=request.path,
            query=dict(request.query),
            headers=dict(request.headers),
            body=await request.read(),
        )
        self.requests.append(rec)
        if request.path == _SHELL_LOGIN:
            return web.Response(text="<html>box login</html>", content_type="text/html")
        if request.path.startswith(f"{_PREFIX}/"):
            return await self._gate(request=request, rec=rec)
        return web.Response(status=404)

    async def _gate(self, *, request: web.Request, rec: GateRequest) -> web.StreamResponse:
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        scopes = self.tokens.get(token)
        if scopes is None:
            rec.refused = 401
            if self.legacy_redirects:
                raise web.HTTPFound(location=f"{_SHELL_LOGIN}?next={_PREFIX}/")
            return web.Response(status=401, text="<html>401 Unauthorized</html>", content_type="text/html")
        if "addon:openccu-loom" not in scopes and "*" not in scopes:
            rec.refused = 403
            return web.json_response({"error": "forbidden"}, status=403)
        drop = {"x-occulite-session", "x-occulite-auth", "x-occulite-token"}
        forwarded = {k: v for k, v in request.headers.items() if k.lower() not in drop}
        forwarded |= {"X-Occulite-Session": token, "X-Occulite-Auth": "token", "X-Occulite-Token": "ha"}
        rec.forwarded_headers = forwarded
        inner = request.path.removeprefix(_PREFIX)
        if inner == "/api/v1/events":
            return await self._websocket(request=request, token=token)
        queue = self._responses.get((request.method, inner))
        if not queue:
            return web.json_response({"title": f"no stub for {request.method} {inner}"}, status=404)
        stub = queue.popleft() if len(queue) > 1 else queue[0]
        if stub.payload is None:
            return web.Response(status=stub.status)
        if stub.content_type is not None:
            return web.json_response(stub.payload, status=stub.status, content_type=stub.content_type)
        return web.json_response(stub.payload, status=stub.status)

    async def _websocket(self, *, request: web.Request, token: str) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.ws_connections.append(token)
        self._active_ws.append(ws)
        async with self._ws_accepted:
            self._ws_accepted.notify_all()
        try:
            async for _msg in ws:
                pass
        finally:
            self._active_ws.remove(ws)
        return ws

    async def wait_for_ws_connections(self, *, count: int) -> None:
        """Block until ``count`` WS upgrades have passed the gate (5 s ceiling)."""
        async with asyncio.timeout(5.0), self._ws_accepted:
            await self._ws_accepted.wait_for(lambda: len(self.ws_connections) >= count)

    def refused(self, *, path: str) -> list[int]:
        """Return the statuses the gate refused a daemon-side path with."""
        return [r.refused for r in self.requests if r.refused is not None and r.path == f"{_PREFIX}{path}"]

    def forwarded(self, *, method: str, path: str) -> list[GateRequest]:
        """Return the requests the gate let through to ``method path``."""
        return [
            r
            for r in self.requests
            if r.forwarded_headers is not None and r.method == method and r.path == f"{_PREFIX}{path}"
        ]


@pytest.fixture
async def gate() -> AsyncIterator[FakeBoxGate]:
    """Start a fake box and tear it down."""
    box = await FakeBoxGate().start()
    try:
        yield box
    finally:
        await box.stop()


@pytest.fixture
async def http(gate: FakeBoxGate) -> AsyncIterator[HttpTransport]:
    """Return a connected transport in box-ingress mode."""
    gate.add_response("GET", "/api/v1/info", payload=_INFO_RESPONSE)
    transport = HttpTransport(config=gate.config(), backoff_sequence=())
    await transport.connect()
    try:
        yield transport
    finally:
        await transport.close()


class TestConfig:
    def test_ingress_urls_use_box_port_and_prefix(self) -> None:
        cfg = LoomConfig(host="box.local", auth=NoAuth(), box_ingress=BoxIngressConfig(token=_TOKEN))
        assert cfg.http_base_url == "https://box.local:443/addons/loom/api/v1"
        assert cfg.ws_url == "wss://box.local:443/addons/loom/api/v1/events"

    def test_ingress_without_tls_defaults_to_port_80(self) -> None:
        cfg = LoomConfig(
            host="box.local", tls=False, auth=NoAuth(), box_ingress=BoxIngressConfig(token=_TOKEN, path_prefix="/x/")
        )
        assert cfg.http_base_url == "http://box.local:80/x/api/v1"

    def test_direct_mode_unchanged(self) -> None:
        cfg = LoomConfig(host="loom.local", auth=BearerAuth(token="daemon-token"))
        assert cfg.http_base_url == "https://loom.local:8119/api/v1"
        assert cfg.ws_url == "wss://loom.local:8119/api/v1/events"
        assert cfg.create_central_url() == "https://loom.local:8119"

    def test_create_central_url_points_at_the_ingress(self) -> None:
        cfg = LoomConfig(host="box.local", auth=NoAuth(), box_ingress=BoxIngressConfig(token=_TOKEN))
        assert cfg.create_central_url() == "https://box.local:443/addons/loom"

    def test_repr_hides_box_token(self) -> None:
        cfg = LoomConfig(host="box.local", auth=NoAuth(), box_ingress=BoxIngressConfig(token=_TOKEN))
        assert _TOKEN not in repr(cfg)
        assert _TOKEN not in repr(cfg.box_ingress)

    def test_a_daemon_credential_beside_the_box_token_is_refused(self) -> None:
        # The box token occupies the one Authorization header the gate reads;
        # a daemon credential could not travel beside it.
        with pytest.raises(ValueError, match="NoAuth"):
            LoomConfig(
                host="box.local", auth=BearerAuth(token="daemon-token"), box_ingress=BoxIngressConfig(token=_TOKEN)
            )


class TestHttpThroughGate:
    async def test_handshake_carries_the_box_token_and_reaches_the_daemon(
        self, gate: FakeBoxGate, http: HttpTransport
    ) -> None:
        assert http.info is not None
        assert http.info.version == "1.2.3"
        (info_req,) = gate.forwarded(method="GET", path="/api/v1/info")
        assert info_req.headers["Authorization"] == f"Bearer {_TOKEN}"
        assert info_req.query == {}
        assert info_req.forwarded_headers is not None
        assert info_req.forwarded_headers["X-Occulite-Session"] == _TOKEN

    @pytest.mark.parametrize("method", ["GET", "POST"])
    async def test_requests_pass_with_their_body_and_query(
        self, gate: FakeBoxGate, http: HttpTransport, method: str
    ) -> None:
        gate.add_response(method, "/api/v1/things", payload={"ok": True})
        body = {"x": 1} if method == "POST" else None
        result = await http.request(method=method, path="/things", params={"central": "home"}, json_body=body)
        assert result == {"ok": True}
        (served,) = gate.forwarded(method=method, path="/api/v1/things")
        assert served.query == {"central": "home"}
        if method == "POST":
            assert json.loads(served.body) == {"x": 1}

    async def test_extra_headers_cannot_displace_the_box_token(self, gate: FakeBoxGate) -> None:
        gate.add_response("GET", "/api/v1/info", payload=_INFO_RESPONSE)
        config = gate.config()
        config.extra_headers["Authorization"] = "Bearer something-else"
        transport = HttpTransport(config=config, backoff_sequence=())
        try:
            await transport.connect()
        finally:
            await transport.close()
        (info_req,) = gate.forwarded(method="GET", path="/api/v1/info")
        assert info_req.headers["Authorization"] == f"Bearer {_TOKEN}"

    async def test_unknown_token_raises_token_error_without_retry(self, gate: FakeBoxGate) -> None:
        gate.add_response("GET", "/api/v1/info", payload=_INFO_RESPONSE)
        # A backoff schedule on purpose: GET is a retryable verb, and a gate
        # refusal must not be fed into the backoff retries.
        transport = HttpTransport(config=gate.config(token="olt_" + "1" * 32), backoff_sequence=(0.01, 0.01))
        try:
            with pytest.raises(LoomBoxTokenError, match="401"):
                await transport.connect()
        finally:
            await transport.close()
        assert gate.refused(path="/api/v1/info") == [401]

    async def test_token_for_another_addon_raises_gate_error_without_retry(self, gate: FakeBoxGate) -> None:
        gate.add_response("GET", "/api/v1/info", payload=_INFO_RESPONSE)
        transport = HttpTransport(config=gate.config(token=_OTHER_ADDON_TOKEN), backoff_sequence=(0.01, 0.01))
        try:
            with pytest.raises(LoomBoxGateError, match="403") as err:
                await transport.connect()
        finally:
            await transport.close()
        assert not isinstance(err.value, LoomBoxTokenError)
        assert gate.refused(path="/api/v1/info") == [403]

    async def test_token_revoked_mid_session_raises_token_error(self, gate: FakeBoxGate, http: HttpTransport) -> None:
        gate.add_response("GET", "/api/v1/things", payload={"ok": True})
        gate.revoke(_TOKEN)
        with pytest.raises(LoomBoxTokenError):
            await http.request(method="GET", path="/things")

    async def test_full_access_token_passes(self, gate: FakeBoxGate) -> None:
        gate.add_response("GET", "/api/v1/info", payload=_INFO_RESPONSE)
        transport = HttpTransport(config=gate.config(token=_FULL_TOKEN), backoff_sequence=())
        try:
            info = await transport.connect()
        finally:
            await transport.close()
        assert info.version == "1.2.3"

    async def test_a_box_that_redirects_raises_gate_error(self, gate: FakeBoxGate) -> None:
        gate.legacy_redirects = True
        gate.add_response("GET", "/api/v1/info", payload=_INFO_RESPONSE)
        transport = HttpTransport(config=gate.config(token="olt_" + "2" * 32), backoff_sequence=())
        try:
            with pytest.raises(LoomBoxGateError, match="redirected"):
                await transport.connect()
        finally:
            await transport.close()

    @pytest.mark.parametrize("status", [401, 403])
    async def test_daemon_problem_answers_are_not_gate_refusals(
        self, gate: FakeBoxGate, http: HttpTransport, status: int
    ) -> None:
        # The daemon's own 401/403 travels through the gate and is
        # application/problem+json by contract: it surfaces as the daemon error.
        gate.add_response(
            "GET",
            "/api/v1/things",
            payload={"type": "about:blank", "title": "No", "status": status},
            status=status,
            content_type="application/problem+json",
        )
        with pytest.raises(LoomHttpError) as err:
            await http.request(method="GET", path="/things")
        assert err.value.status == status
        assert not isinstance(err.value, LoomBoxGateError)

    async def test_unreachable_box_is_a_transport_error(self) -> None:
        # Nothing listens on port 1. An unreachable box must surface as the
        # transport condition it is — retryable — and never as a gate verdict.
        config = LoomConfig(
            host="127.0.0.1",
            tls=False,
            auth=NoAuth(),
            request_timeout_seconds=0.5,
            box_ingress=BoxIngressConfig(token=_TOKEN, port=1),
        )
        transport = HttpTransport(config=config, backoff_sequence=())
        try:
            with pytest.raises(LoomTransportError) as err:
                await transport.connect()
            assert not isinstance(err.value, LoomBoxGateError)
        finally:
            await transport.close()


class TestDirectModeRedirectUnchanged:
    async def test_redirect_without_ingress_is_an_http_error(self, mock_daemon: MockDaemon) -> None:
        mock_daemon.get("/api/v1/info", payload=_INFO_RESPONSE)
        mock_daemon.get("/api/v1/things", status=302, headers={"Location": "http://elsewhere.invalid/"})
        transport = HttpTransport(config=mock_daemon.config, backoff_sequence=())
        await transport.connect()
        try:
            with pytest.raises(LoomHttpError) as err:
                await transport.request(method="GET", path="/things")
            assert err.value.status == 302
            assert not isinstance(err.value, LoomBoxGateError)
        finally:
            await transport.close()


class TestWsThroughGate:
    async def test_ws_carries_the_box_token_and_reconnects(
        self, gate: FakeBoxGate, http: HttpTransport, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ws_module, "_RECONNECT_BACKOFF", (0.01,))
        monkeypatch.setattr(ws_module, "_RECONNECT_JITTER_SHARE", 0.0)
        ws = WsTransport(config=http._config, initial_subscriptions=["device.*"])
        await ws.start()
        try:
            await gate.wait_for_ws_connections(count=1)
            await gate.drop_websockets()
            await gate.wait_for_ws_connections(count=2)
        finally:
            await ws.stop()
        assert gate.ws_connections == [_TOKEN, _TOKEN]
        upgrades = gate.forwarded(method="GET", path="/api/v1/events")
        assert upgrades
        assert all(r.headers["Authorization"] == f"Bearer {_TOKEN}" for r in upgrades)
        assert all(r.query == {} for r in upgrades)

    @pytest.mark.parametrize(("token", "status"), [("olt_" + "3" * 32, 401), (_OTHER_ADDON_TOKEN, 403)])
    async def test_a_refused_token_stops_the_reconnect_loop(
        self, gate: FakeBoxGate, monkeypatch: pytest.MonkeyPatch, token: str, status: int
    ) -> None:
        # A token the gate refuses does not clear on its own: one attempt,
        # then the loop stops and reports, instead of retrying every cycle.
        monkeypatch.setattr(ws_module, "_RECONNECT_BACKOFF", (0.01,))
        monkeypatch.setattr(ws_module, "_RECONNECT_JITTER_SHARE", 0.0)
        session = aiohttp.ClientSession()
        auth_failed = asyncio.Event()

        async def on_auth_failed() -> None:
            auth_failed.set()

        ws = WsTransport(config=gate.config(token=token), session=session, on_auth_failed=on_auth_failed)
        await ws.start()
        try:
            await asyncio.wait_for(auth_failed.wait(), timeout=2.0)
            await asyncio.wait_for(ws._stopped.wait(), timeout=2.0)
        finally:
            await ws.stop()
            await session.close()
        assert gate.refused(path="/api/v1/events") == [status]

    async def test_a_refused_upgrade_surfaces_as_the_gate_error(self, gate: FakeBoxGate) -> None:
        ws = WsTransport(config=gate.config(token=_OTHER_ADDON_TOKEN))
        ws._session = aiohttp.ClientSession()
        try:
            with pytest.raises(LoomBoxGateError, match="403"):
                await ws._connect_and_read()
        finally:
            await ws._session.close()
