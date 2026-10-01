# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Box-ingress mode: the client reaching the daemon through an openccu-lite box.

``FakeBoxGate`` is a real in-process aiohttp server playing the box's web
server: its own auth API at the root (wire form per godevccu
``pkg/litefake/CONTRACT.md`` §A.2) and a session gate in front of
``/addons/loom/`` that redirects to a shell login page unless ``?sid=`` names
a live session. Requests that pass the gate are served from responses
registered per ``(method, path)`` (the daemon-side path, prefix stripped), the
way ``tests/helpers/mock_daemon.py`` does, and the WS endpoint behind it
accepts upgrades.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
import json
import secrets
from typing import Any

import aiohttp
from aiohttp import web
from multidict import CIMultiDict, CIMultiDictProxy
import pytest
from yarl import URL

from openccu_loom_client import (
    BearerAuth,
    BoxIngressConfig,
    LoomBoxGateError,
    LoomBoxLoginError,
    LoomConfig,
    LoomHttpError,
    LoomTransportError,
)
from openccu_loom_client.boxgate import BoxGateSession
from openccu_loom_client.transport import HttpTransport, WsTransport
import openccu_loom_client.transport.ws as ws_module
from openccu_loom_client.wire import DAEMON_API_VERSION
from tests.helpers import MockDaemon

_BOX_USER = "admin"
_BOX_PASSWORD = "box-secret-pw"
_DAEMON_TOKEN = "testtoken1234"
_PREFIX = "/addons/loom"
_SHELL_LOGIN = "/shell/login"
_SID_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"

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
    # see (client-sent session header stripped, validated session stamped).
    forwarded_headers: dict[str, str] | None = None
    bounced: bool = False


@dataclass(slots=True)
class _Stub:
    status: int = 200
    payload: Any = None
    content_type: str | None = None


@dataclass(slots=True)
class FakeBoxGate:
    """In-process fake of an openccu-lite box's web server and session gate."""

    host: str = "127.0.0.1"
    port: int = 0
    sids: set[str] = field(default_factory=set)
    minted: list[str] = field(default_factory=list)
    requests: list[GateRequest] = field(default_factory=list)
    logins: int = 0
    failed_logins: int = 0
    logouts: int = 0
    ws_connections: list[str] = field(default_factory=list)
    # Simulates a box account without access to the add-on: every session,
    # however fresh, is bounced.
    refuse_all: bool = False
    # Plays an older box whose gate redirects EVERY caller to the shell
    # login, browsers and API clients alike (the shape the occulited
    # checkout's gate script documents).
    legacy_redirects: bool = False
    _responses: dict[tuple[str, str], deque[_Stub]] = field(default_factory=dict)
    _active_ws: list[web.WebSocketResponse] = field(default_factory=list)
    _ws_accepted: asyncio.Condition = field(default_factory=asyncio.Condition)
    _runner: web.AppRunner | None = None

    # ---- registration ----

    def add_response(
        self, method: str, path: str, *, payload: Any = None, status: int = 200, content_type: str | None = None
    ) -> None:
        """Queue a daemon response for ``method path`` (daemon-side path, prefix stripped)."""
        self._responses.setdefault((method.upper(), path), deque()).append(
            _Stub(status=status, payload=payload, content_type=content_type)
        )

    def invalidate_all(self) -> None:
        """Expire every live session, as a box restart or a session timeout would."""
        self.sids.clear()

    async def drop_websockets(self) -> None:
        """Close every open WS connection from the server side."""
        for ws in list(self._active_ws):
            await ws.close()

    # ---- lifecycle ----

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

    def config(self, *, password: str = _BOX_PASSWORD) -> LoomConfig:
        """Return a LoomConfig in box-ingress mode pointed at this fake."""
        return LoomConfig(
            host=self.host,
            # Deliberately a port nothing listens on: ingress mode must not use it.
            port=1,
            tls=False,
            auth=BearerAuth(token=_DAEMON_TOKEN, label="test"),
            request_timeout_seconds=2.0,
            box_ingress=BoxIngressConfig(username=_BOX_USER, password=password, port=self.port),
        )

    # ---- handling ----

    def _mint_sid(self) -> str:
        sid = "".join(secrets.choice(_SID_ALPHABET) for _ in range(26))
        self.sids.add(sid)
        self.minted.append(sid)
        return sid

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        body = await request.read()
        rec = GateRequest(
            method=request.method,
            path=request.path,
            query=dict(request.query),
            headers=dict(request.headers),
            body=body,
        )
        self.requests.append(rec)
        if request.path == "/api/auth/v1/login" and request.method == "POST":
            return self._login(body=body)
        if request.path == "/api/auth/v1/logout" and request.method == "POST":
            sid = request.headers.get("Authorization", "").removeprefix("Bearer ")
            if sid not in self.sids:
                return web.json_response({"error": "unauthenticated", "message": "login required"}, status=401)
            self.sids.discard(sid)
            self.logouts += 1
            return web.Response(status=204)
        if request.path == _SHELL_LOGIN:
            return web.Response(text="<html>box login</html>", content_type="text/html")
        if request.path.startswith(f"{_PREFIX}/"):
            return await self._gate(request=request, rec=rec)
        return web.Response(status=404)

    def _login(self, *, body: bytes) -> web.Response:
        creds = json.loads(body) if body else {}
        if creds.get("username") != _BOX_USER or creds.get("password") != _BOX_PASSWORD:
            self.failed_logins += 1
            return web.json_response({"error": "unauthenticated", "message": "login required"}, status=401)
        self.logins += 1
        return web.json_response(
            {
                "sid": self._mint_sid(),
                "user": _BOX_USER,
                "role": "admin",
                "level": "administer",
                "account_id": "acc-1",
                "must_change_password": False,
            }
        )

    async def _gate(self, *, request: web.Request, rec: GateRequest) -> web.StreamResponse:
        sid = request.query.get("sid")
        if self.refuse_all or sid is None or sid not in self.sids:
            rec.bounced = True
            # Measured against a real openccu-lite box (0.83.0 round): the
            # gate redirects BROWSERS (Accept: text/html) to the shell login
            # and answers every other caller — API clients, WS upgrades —
            # with a plain 401 text/html error page instead.
            if self.legacy_redirects or "text/html" in request.headers.get("Accept", ""):
                raise web.HTTPFound(location=f"{_SHELL_LOGIN}?next={_PREFIX}/")
            return web.Response(status=401, text="<html>401 Unauthorized</html>", content_type="text/html")
        forwarded = {k: v for k, v in request.headers.items() if k.lower() != "x-occulite-session"}
        forwarded["X-Occulite-Session"] = sid
        rec.forwarded_headers = forwarded
        inner = request.path.removeprefix(_PREFIX)
        if inner == "/api/v1/events":
            return await self._websocket(request=request, sid=sid)
        queue = self._responses.get((request.method, inner))
        if not queue:
            return web.json_response({"title": f"no stub for {request.method} {inner}"}, status=404)
        stub = queue.popleft() if len(queue) > 1 else queue[0]
        if stub.payload is None:
            return web.Response(status=stub.status)
        if stub.content_type is not None:
            return web.json_response(stub.payload, status=stub.status, content_type=stub.content_type)
        return web.json_response(stub.payload, status=stub.status)

    async def _websocket(self, *, request: web.Request, sid: str) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.ws_connections.append(sid)
        self._active_ws.append(ws)
        async with self._ws_accepted:
            self._ws_accepted.notify_all()
        try:
            async for _msg in ws:
                pass
        finally:
            self._active_ws.remove(ws)
        return ws

    # ---- views ----

    async def wait_for_ws_connections(self, *, count: int) -> None:
        """Block until ``count`` WS upgrades have passed the gate (5 s ceiling)."""
        async with asyncio.timeout(5.0), self._ws_accepted:
            await self._ws_accepted.wait_for(lambda: len(self.ws_connections) >= count)

    def bounced(self, *, path: str) -> int:
        """Count gate bounces for a daemon-side path."""
        return sum(1 for r in self.requests if r.bounced and r.path == f"{_PREFIX}{path}")

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
        cfg = LoomConfig(
            host="box.local",
            auth=BearerAuth(token=_DAEMON_TOKEN),
            box_ingress=BoxIngressConfig(username=_BOX_USER, password=_BOX_PASSWORD),
        )
        assert cfg.http_base_url == "https://box.local:443/addons/loom/api/v1"
        assert cfg.ws_url == "wss://box.local:443/addons/loom/api/v1/events"
        assert cfg.box_api_base_url == "https://box.local:443"

    def test_ingress_without_tls_defaults_to_port_80(self) -> None:
        cfg = LoomConfig(
            host="box.local",
            tls=False,
            auth=BearerAuth(token=_DAEMON_TOKEN),
            box_ingress=BoxIngressConfig(username=_BOX_USER, password=_BOX_PASSWORD, path_prefix="/x/"),
        )
        assert cfg.http_base_url == "http://box.local:80/x/api/v1"
        assert cfg.box_api_base_url == "http://box.local:80"

    def test_direct_mode_unchanged(self) -> None:
        cfg = LoomConfig(host="loom.local", auth=BearerAuth(token=_DAEMON_TOKEN))
        assert cfg.http_base_url == "https://loom.local:8119/api/v1"
        assert cfg.ws_url == "wss://loom.local:8119/api/v1/events"
        assert cfg.box_api_base_url is None
        assert cfg.create_central_url() == "https://loom.local:8119"

    def test_create_central_url_points_at_the_ingress(self) -> None:
        cfg = LoomConfig(
            host="box.local",
            auth=BearerAuth(token=_DAEMON_TOKEN),
            box_ingress=BoxIngressConfig(username=_BOX_USER, password=_BOX_PASSWORD),
        )
        assert cfg.create_central_url() == "https://box.local:443/addons/loom"

    def test_repr_hides_box_password(self) -> None:
        cfg = LoomConfig(
            host="box.local",
            auth=BearerAuth(token=_DAEMON_TOKEN),
            box_ingress=BoxIngressConfig(username=_BOX_USER, password=_BOX_PASSWORD),
        )
        assert _BOX_PASSWORD not in repr(cfg)


class TestHttpThroughGate:
    async def test_connect_logs_in_and_handshake_passes_gate(self, gate: FakeBoxGate, http: HttpTransport) -> None:
        assert http.info is not None
        assert http.info.version == "1.2.3"
        assert gate.logins == 1
        (info_req,) = gate.forwarded(method="GET", path="/api/v1/info")
        assert info_req.query["sid"] in gate.sids
        # Login ran before the handshake, so the handshake was never bounced.
        assert gate.bounced(path="/api/v1/info") == 0
        assert http.box_gate is not None
        assert info_req.query["sid"] not in repr(http.box_gate)

    @pytest.mark.parametrize("method", ["GET", "POST"])
    async def test_invalidated_session_relogs_once(self, gate: FakeBoxGate, http: HttpTransport, method: str) -> None:
        gate.add_response(method, "/api/v1/things", payload={"ok": True})
        gate.invalidate_all()

        body = {"x": 1} if method == "POST" else None
        assert await http.request(method=method, path="/things", json_body=body) == {"ok": True}

        assert gate.logins == 2
        assert gate.bounced(path="/api/v1/things") == 1
        (served,) = gate.forwarded(method=method, path="/api/v1/things")
        assert served.query["sid"] in gate.sids
        if method == "POST":
            assert json.loads(served.body) == {"x": 1}

    async def test_concurrent_bounces_share_one_relogin(self, gate: FakeBoxGate, http: HttpTransport) -> None:
        gate.add_response("GET", "/api/v1/things", payload={"ok": True})
        gate.invalidate_all()

        results = await asyncio.gather(*(http.request(method="GET", path="/things") for _ in range(5)))

        assert results == [{"ok": True}] * 5
        assert gate.logins == 2

    async def test_query_params_are_kept_alongside_sid(self, gate: FakeBoxGate, http: HttpTransport) -> None:
        gate.add_response("GET", "/api/v1/things", payload={})
        await http.request(method="GET", path="/things", params={"central": "home"})
        (served,) = gate.forwarded(method="GET", path="/api/v1/things")
        assert served.query["central"] == "home"
        assert served.query["sid"] in gate.sids

    async def test_wrong_credentials_raise_login_error(self, gate: FakeBoxGate) -> None:
        gate.add_response("GET", "/api/v1/info", payload=_INFO_RESPONSE)
        transport = HttpTransport(config=gate.config(password="wrong"), backoff_sequence=())
        with pytest.raises(LoomBoxLoginError, match="401"):
            await transport.connect()
        assert gate.failed_logins == 1
        assert not any(r.path.startswith(_PREFIX) for r in gate.requests)
        await transport.close()

    async def test_relogin_works_through_a_legacy_redirecting_gate(
        self, gate: FakeBoxGate, http: HttpTransport
    ) -> None:
        # Older boxes redirect every caller; the 3xx bounce shape must keep
        # working next to the 401 shape current boxes answer API clients.
        gate.legacy_redirects = True
        gate.add_response("GET", "/api/v1/things", payload={"ok": True})
        gate.invalidate_all()
        assert await http.request(method="GET", path="/things") == {"ok": True}
        assert gate.logins == 2

    async def test_daemon_problem_401_is_not_a_bounce(self, gate: FakeBoxGate, http: HttpTransport) -> None:
        # A 401 the DAEMON answers travels through the gate with a valid sid
        # and is application/problem+json by contract. It must surface as the
        # daemon auth error it is — not trigger a box relogin.
        gate.add_response(
            "GET",
            "/api/v1/things",
            payload={"type": "about:blank", "title": "Unauthorized", "status": 401},
            status=401,
            content_type="application/problem+json",
        )
        logins_before = gate.logins
        with pytest.raises(LoomHttpError) as err:
            await http.request(method="GET", path="/things")
        assert err.value.status == 401
        assert not isinstance(err.value, LoomBoxGateError)
        assert gate.logins == logins_before

    async def test_unreachable_box_login_is_a_transport_error(self) -> None:
        # Nothing listens on port 1. An unreachable box must surface as the
        # transport condition it is — retryable — and never as the
        # credential verdict LoomBoxLoginError, which callers treat as final.
        config = LoomConfig(
            host="127.0.0.1",
            tls=False,
            auth=BearerAuth(token=_DAEMON_TOKEN, label="test"),
            request_timeout_seconds=0.5,
            box_ingress=BoxIngressConfig(username=_BOX_USER, password=_BOX_PASSWORD, port=1),
        )
        transport = HttpTransport(config=config, backoff_sequence=())
        try:
            with pytest.raises(LoomTransportError) as err:
                await transport.connect()
            assert not isinstance(err.value, LoomBoxLoginError)
        finally:
            await transport.close()

    async def test_bounce_after_fresh_login_raises_gate_error(self, gate: FakeBoxGate) -> None:
        gate.add_response("GET", "/api/v1/info", payload=_INFO_RESPONSE)
        gate.add_response("GET", "/api/v1/things", payload={"ok": True})
        # A backoff schedule on purpose: GET is a retryable verb, and the gate
        # error must not be fed into the backoff retries.
        http = HttpTransport(config=gate.config(), backoff_sequence=(0.01, 0.01))
        await http.connect()
        gate.refuse_all = True

        try:
            with pytest.raises(LoomBoxGateError) as err:
                await http.request(method="GET", path="/things")
        finally:
            await http.close()

        assert not isinstance(err.value, LoomBoxLoginError)
        # One relogin, one retry — and no backoff retry on top of it.
        assert gate.logins == 2
        assert gate.bounced(path="/api/v1/things") == 2

    async def test_daemon_auth_rides_untouched_and_sid_never_in_headers(
        self, gate: FakeBoxGate, http: HttpTransport
    ) -> None:
        gate.add_response("POST", "/api/v1/things", payload={})
        gate.invalidate_all()
        await http.request(method="POST", path="/things", json_body={})

        sids = set(gate.minted)
        assert len(sids) == 2
        for req in gate.requests:
            for value in req.headers.values():
                assert not any(sid in value for sid in sids), (req.path, value)
            if req.path.startswith(_PREFIX):
                assert req.headers["Authorization"] == f"Bearer {_DAEMON_TOKEN}"
                assert "X-Occulite-Session" not in req.headers
            if req.path == "/api/auth/v1/login":
                # The daemon credential never goes to the box's own auth API.
                assert "Authorization" not in req.headers
        for req in gate.requests:
            if req.forwarded_headers is not None:
                assert req.forwarded_headers["Authorization"] == f"Bearer {_DAEMON_TOKEN}"
                assert req.forwarded_headers["X-Occulite-Session"] == req.query["sid"]

    async def test_close_logs_out(self, gate: FakeBoxGate) -> None:
        gate.add_response("GET", "/api/v1/info", payload=_INFO_RESPONSE)
        transport = HttpTransport(config=gate.config(), backoff_sequence=())
        await transport.connect()
        assert len(gate.sids) == 1
        await transport.close()
        assert gate.logouts == 1
        assert gate.sids == set()


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
            assert all("sid" not in r.query for r in mock_daemon.requests)
        finally:
            await transport.close()


class TestWsThroughGate:
    async def test_ws_carries_sid_and_recovers_after_invalidation(
        self, gate: FakeBoxGate, http: HttpTransport, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ws_module, "_RECONNECT_BACKOFF", (0.01,))
        monkeypatch.setattr(ws_module, "_RECONNECT_JITTER_SHARE", 0.0)
        ws = WsTransport(config=http_config(http), initial_subscriptions=["device.*"], box_gate=http.box_gate)
        await ws.start()
        try:
            await gate.wait_for_ws_connections(count=1)
            first_sid = gate.ws_connections[0]
            assert gate.logins == 1  # the REST login is reused, not repeated

            gate.invalidate_all()
            await gate.drop_websockets()
            await gate.wait_for_ws_connections(count=2)

            second_sid = gate.ws_connections[1]
            assert second_sid != first_sid
            assert second_sid in gate.sids
            assert gate.logins == 2
            assert gate.bounced(path="/api/v1/events") == 1
            upgrades = [r for r in gate.requests if r.forwarded_headers is not None and r.path.endswith("/events")]
            assert all(r.headers["Authorization"] == f"Bearer {_DAEMON_TOKEN}" for r in upgrades)
        finally:
            await ws.stop()

    async def test_ws_bounce_after_fresh_login_raises_gate_error(self, gate: FakeBoxGate, http: HttpTransport) -> None:
        gate.refuse_all = True
        ws = WsTransport(config=http_config(http), box_gate=http.box_gate)
        ws._session = aiohttp.ClientSession()
        try:
            with pytest.raises(LoomBoxGateError):
                await ws._connect_and_read()
            assert gate.logins == 2
            assert gate.bounced(path="/api/v1/events") == 2
        finally:
            await ws._session.close()

    def test_ws_bounce_detector_tells_gate_401_from_daemon_401(self) -> None:
        def handshake_error(*, status: int, content_type: str | None) -> aiohttp.WSServerHandshakeError:
            url = URL("wss://box.local/addons/loom/api/v1/events")
            info = aiohttp.RequestInfo(url=url, method="GET", headers=CIMultiDictProxy(CIMultiDict()), real_url=url)
            headers = CIMultiDictProxy(CIMultiDict({"Content-Type": content_type} if content_type else {}))
            return aiohttp.WSServerHandshakeError(info, (), status=status, message="x", headers=headers)

        is_bounce = WsTransport._is_gate_bounce
        # The box gate's two measured shapes:
        assert is_bounce(exc=handshake_error(status=401, content_type="text/html"))
        assert is_bounce(exc=handshake_error(status=302, content_type=None))
        # The daemon's own 401 is problem+json by contract — not a bounce:
        assert not is_bounce(exc=handshake_error(status=401, content_type="application/problem+json"))
        assert not is_bounce(exc=handshake_error(status=403, content_type="application/problem+json"))

    async def test_refused_box_login_stops_the_reconnect_loop(
        self, gate: FakeBoxGate, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A box that ANSWERS the login and says no is a credential verdict:
        # the reconnect loop must stop after one attempt (like the daemon's
        # own 401/403) instead of re-sending the password every backoff cycle.
        monkeypatch.setattr(ws_module, "_RECONNECT_BACKOFF", (0.01,))
        monkeypatch.setattr(ws_module, "_RECONNECT_JITTER_SHARE", 0.0)
        config = gate.config(password="wrong")
        session = aiohttp.ClientSession()
        box = BoxGateSession(config=config, session_getter=lambda: session)
        auth_failed = asyncio.Event()

        async def on_auth_failed() -> None:
            auth_failed.set()

        ws = WsTransport(config=config, box_gate=box, session=session, on_auth_failed=on_auth_failed)
        await ws.start()
        try:
            await asyncio.wait_for(auth_failed.wait(), timeout=2.0)
            await asyncio.wait_for(ws._stopped.wait(), timeout=2.0)
        finally:
            await ws.stop()
            await session.close()
        assert gate.failed_logins == 1
        assert gate.logins == 0
        assert gate.bounced(path="/api/v1/events") == 0


def http_config(transport: HttpTransport) -> LoomConfig:
    """Return the config a transport was built with."""
    return transport._config
