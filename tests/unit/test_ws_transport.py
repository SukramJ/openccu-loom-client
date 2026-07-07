# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
WS-transport tests using a real aiohttp TestServer as the daemon side.

aiohttp.test_utils gives us a full-fidelity server (handshake, frame
parsing, close) without mocking — we control the daemon's responses
directly and can assert on every client frame it receives.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
import json

import aiohttp
from aiohttp import web
import pytest

from openccu_loom_client import BearerAuth, LoomConfig
from openccu_loom_client.exceptions import LoomTransportError
from openccu_loom_client.transport import WsTransport
import openccu_loom_client.transport.ws as ws_module


def _envelope(*, seq: int, value: float = 0.0) -> str:
    """Return a minimal valid value-changed envelope as a JSON string."""
    return json.dumps(
        {
            "topic": "device.0001.channels.1.data_points.LEVEL",
            "type": "datapoint.value_changed",
            "ts": "2026-05-24T08:42:13Z",
            "seq": seq,
            "kind": "change",
            "payload": {
                "central": "home",
                "device_address": "0001",
                "channel": 1,
                "parameter": "LEVEL",
                "paramset_key": "VALUES",
                "value": value,
                "modified_at": "2026-05-24T08:42:13Z",
            },
        }
    )


# Helper to build a daemon-side handler that records every client
# frame and lets the test script-send back canned frames.

FakeDaemonScript = Callable[[web.WebSocketResponse, list[dict]], Awaitable[None]]


def _make_app(script: FakeDaemonScript, received_frames: list[dict]) -> web.Application:
    async def handler(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        # Drain client frames into received_frames in the background
        # so the script can interleave reads + writes.
        async def reader() -> None:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    received_frames.append(json.loads(msg.data))
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                    return

        reader_task = asyncio.create_task(reader())
        try:
            await script(ws, received_frames)
        finally:
            reader_task.cancel()
            await ws.close()
        return ws

    app = web.Application()
    app.router.add_get("/api/v1/events", handler)
    return app


@pytest.fixture
async def fake_daemon() -> AsyncIterator[Callable[[FakeDaemonScript], Awaitable[tuple[LoomConfig, list[dict]]]]]:
    """
    Spawn a fresh fake-daemon WS server per test.

    Returns a config bound to its address and a list collecting every
    frame the client sent.
    """
    runners: list[web.AppRunner] = []
    received: list[list[dict]] = []

    async def boot(script: FakeDaemonScript) -> tuple[LoomConfig, list[dict]]:
        rx: list[dict] = []
        received.append(rx)
        app = _make_app(script, rx)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        runners.append(runner)
        cfg = LoomConfig(
            host="127.0.0.1",
            port=port,
            tls=False,
            auth=BearerAuth(token="t"),
            request_timeout_seconds=2.0,
        )
        return cfg, rx

    yield boot
    for r in runners:
        await r.cleanup()


class TestSubscribeAndReceive:
    async def test_initial_subscribe_sent_with_topics(self, fake_daemon) -> None:
        async def script(ws: web.WebSocketResponse, _rx: list[dict]) -> None:
            await asyncio.sleep(0.2)  # let the client send first

        cfg, rx = await fake_daemon(script)
        async with WsTransport(config=cfg, initial_subscriptions=["device.*", "hub.*"]):
            await asyncio.sleep(0.3)

        subs = [f for f in rx if f.get("op") == "subscribe"]
        assert len(subs) == 1
        assert sorted(subs[0]["topics"]) == ["device.*", "hub.*"]
        assert "since" not in subs[0]

    async def test_envelope_arrives_and_seq_is_tracked(self, fake_daemon) -> None:
        envelope = {
            "topic": "device.0001.channels.1.data_points.LEVEL",
            "type": "datapoint.value_changed",
            "ts": "2026-05-24T08:42:13Z",
            "seq": 42,
            "kind": "change",
            "payload": {
                "central": "home",
                "device_address": "0001",
                "channel": 1,
                "parameter": "LEVEL",
                "paramset_key": "VALUES",
                "value": 0.5,
                "previous": 0.0,
                "modified_at": "2026-05-24T08:42:13Z",
            },
        }

        async def script(ws: web.WebSocketResponse, _rx: list[dict]) -> None:
            await asyncio.sleep(0.05)
            await ws.send_str(json.dumps(envelope))
            await asyncio.sleep(0.2)

        cfg, _rx = await fake_daemon(script)
        async with WsTransport(config=cfg, initial_subscriptions=["device.*"]) as ws:
            it = ws.events()
            env = await asyncio.wait_for(anext(it), timeout=1.0)
            assert env.seq == 42
            assert ws.last_seq == 42

    async def test_ping_is_answered_with_pong(self, fake_daemon) -> None:
        async def script(ws: web.WebSocketResponse, _rx: list[dict]) -> None:
            await asyncio.sleep(0.05)
            await ws.send_str(json.dumps({"op": "ping"}))
            await asyncio.sleep(0.2)

        cfg, rx = await fake_daemon(script)
        async with WsTransport(config=cfg, initial_subscriptions=["device.*"]):
            await asyncio.sleep(0.3)

        pongs = [f for f in rx if f.get("op") == "pong"]
        assert len(pongs) == 1


class TestReplayLost:
    async def test_replay_lost_invokes_handler(self, fake_daemon) -> None:
        captured: list[int] = []

        async def on_lost(oldest: int) -> None:
            captured.append(oldest)

        async def script(ws: web.WebSocketResponse, _rx: list[dict]) -> None:
            await asyncio.sleep(0.05)
            await ws.send_str(json.dumps({"op": "replay_lost", "oldest_seq": 901}))
            await asyncio.sleep(0.2)

        cfg, _rx = await fake_daemon(script)
        async with WsTransport(config=cfg, initial_subscriptions=["device.*"], on_replay_lost=on_lost):
            await asyncio.sleep(0.3)

        assert captured == [901]

    async def test_replay_lost_without_oldest_seq_still_invokes_handler(self, fake_daemon) -> None:
        """B2: a malformed replay_lost (no oldest_seq) must still trigger the resync."""
        captured: list[int] = []

        async def on_lost(oldest: int) -> None:
            captured.append(oldest)

        async def script(ws: web.WebSocketResponse, _rx: list[dict]) -> None:
            await asyncio.sleep(0.05)
            await ws.send_str(json.dumps({"op": "replay_lost"}))  # no oldest_seq field
            await asyncio.sleep(0.2)

        cfg, _rx = await fake_daemon(script)
        async with WsTransport(config=cfg, initial_subscriptions=["device.*"], on_replay_lost=on_lost):
            await asyncio.sleep(0.3)

        # Sentinel -1 → callback fired despite the missing field (no silent skip).
        assert captured == [-1]


class TestRuntimeSubscriptions:
    async def test_subscribe_sends_only_new_topics(self, fake_daemon) -> None:
        async def script(ws: web.WebSocketResponse, _rx: list[dict]) -> None:
            await asyncio.sleep(0.4)

        cfg, rx = await fake_daemon(script)
        async with WsTransport(config=cfg, initial_subscriptions=["device.*"]) as ws:
            await asyncio.sleep(0.1)
            # Existing topic — should be no-op.
            await ws.subscribe(topics=["device.*"])
            # New topic — should send a fresh subscribe frame.
            await ws.subscribe(topics=["hub.*"])
            await asyncio.sleep(0.15)

        subs = [f for f in rx if f.get("op") == "subscribe"]
        # Two subscribes total: initial + the "hub.*" addition.
        assert len(subs) == 2
        assert subs[1]["topics"] == ["hub.*"]

    async def test_unsubscribe_drops_topic_locally_and_on_wire(self, fake_daemon) -> None:
        async def script(ws: web.WebSocketResponse, _rx: list[dict]) -> None:
            await asyncio.sleep(0.3)

        cfg, rx = await fake_daemon(script)
        async with WsTransport(config=cfg, initial_subscriptions=["device.*", "hub.*"]) as ws:
            await asyncio.sleep(0.1)
            await ws.unsubscribe(topics=["hub.*"])
            await asyncio.sleep(0.1)
            assert "hub.*" not in ws.subscriptions
            assert "device.*" in ws.subscriptions

        unsubs = [f for f in rx if f.get("op") == "unsubscribe"]
        assert len(unsubs) == 1
        assert unsubs[0]["topics"] == ["hub.*"]


class TestResilience:
    """N4: reconnect/resume, heartbeat-timeout, reauth ack/failure, queue overflow."""

    async def test_reconnect_resumes_with_since_cursor(self, fake_daemon) -> None:
        conns = {"n": 0}

        async def script(ws: web.WebSocketResponse, rx: list[dict]) -> None:
            conns["n"] += 1
            if conns["n"] == 1:
                await asyncio.sleep(0.15)  # let the client subscribe
                await ws.send_str(_envelope(seq=42))  # advances last_seq
                await asyncio.sleep(0.1)  # then the handler returns → daemon closes
            else:
                await asyncio.sleep(0.4)  # keep the resumed connection open

        cfg, rx = await fake_daemon(script)
        async with WsTransport(config=cfg, initial_subscriptions=["device.*"]):
            await asyncio.sleep(1.3)  # 1st conn (~0.25s) + 0.5s backoff + 2nd conn

        subs = [f for f in rx if f.get("op") == "subscribe"]
        assert len(subs) >= 2, f"expected a reconnect subscribe, got {subs}"
        assert "since" not in subs[0]
        assert subs[1].get("since") == 42  # resume from the last seen seq

    async def test_inbound_ping_timeout_forces_reconnect(self, fake_daemon, monkeypatch) -> None:
        monkeypatch.setattr(ws_module, "_INBOUND_PING_DEADLINE_SECONDS", 0.3)
        conns = {"n": 0}

        async def script(ws: web.WebSocketResponse, _rx: list[dict]) -> None:
            conns["n"] += 1
            # Stay silent past the deadline on the first connection → the client
            # treats the socket as dead and reconnects.
            await asyncio.sleep(0.5 if conns["n"] == 1 else 0.4)

        cfg, _rx = await fake_daemon(script)
        async with WsTransport(config=cfg, initial_subscriptions=["device.*"]):
            await asyncio.sleep(1.4)

        assert conns["n"] >= 2, "silent socket past the deadline should have reconnected"

    async def test_reauth_ok_mirrors_token_to_config(self, fake_daemon) -> None:
        async def script(ws: web.WebSocketResponse, rx: list[dict]) -> None:
            for _ in range(100):
                if any(f.get("op") == "reauth" for f in rx):
                    await ws.send_str(json.dumps({"op": "reauth_ok"}))
                    break
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.2)

        cfg, _rx = await fake_daemon(script)
        transport = WsTransport(config=cfg)
        async with transport:
            await asyncio.sleep(0.1)
            await transport.reauth(token="rotated-token")
        assert isinstance(cfg.auth, BearerAuth)
        assert cfg.auth.token == "rotated-token"  # mirrored for reconnects

    async def test_reauth_failed_raises_and_fires_callback(self, fake_daemon) -> None:
        async def script(ws: web.WebSocketResponse, rx: list[dict]) -> None:
            for _ in range(100):
                if any(f.get("op") == "reauth" for f in rx):
                    await ws.send_str(json.dumps({"op": "reauth_failed"}))
                    break
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.2)

        cfg, _rx = await fake_daemon(script)
        auth_failed: list[bool] = []

        async def on_auth_failed() -> None:
            auth_failed.append(True)

        transport = WsTransport(config=cfg, on_auth_failed=on_auth_failed)
        async with transport:
            await asyncio.sleep(0.1)
            with pytest.raises(LoomTransportError):
                await transport.reauth(token="bad-token")
            await asyncio.sleep(0.05)
        assert auth_failed == [True]
        assert isinstance(cfg.auth, BearerAuth)
        assert cfg.auth.token == "t"  # NOT mirrored on rejection

    async def test_queue_overflow_forces_resync(self, fake_daemon, monkeypatch) -> None:
        monkeypatch.setattr(ws_module, "_ENVELOPE_QUEUE_MAXSIZE", 3)
        resyncs: list[int] = []

        async def on_replay_lost(oldest_seq: int) -> None:
            resyncs.append(oldest_seq)

        async def script(ws: web.WebSocketResponse, _rx: list[dict]) -> None:
            await asyncio.sleep(0.15)
            # No consumer drains the queue → maxsize=3 fills, the rest overflow.
            for i in range(8):
                await ws.send_str(_envelope(seq=i))
            await asyncio.sleep(0.3)

        cfg, _rx = await fake_daemon(script)
        async with WsTransport(config=cfg, on_replay_lost=on_replay_lost):
            await asyncio.sleep(0.7)

        # Exactly one resync for the whole overflow episode: the latch keeps a
        # sustained flood from re-firing the resync (and flooding the log) once
        # per dropped event. This is the regression guard for the log storm.
        assert resyncs == [-1]  # the overflow sentinel, fired once

    async def test_queue_overflow_latch_rearms_after_drain(self, monkeypatch) -> None:
        """
        A drained overflow latch re-arms so the next episode resyncs again.

        A sustained overflow resyncs once; after the consumer drains the queue
        back below the low-watermark the latch re-arms, so a *fresh* overflow
        episode forces another resync. Driven deterministically through
        ``_handle_text`` / ``events()`` — no server, no sleeps.
        """
        monkeypatch.setattr(ws_module, "_ENVELOPE_QUEUE_MAXSIZE", 3)
        monkeypatch.setattr(ws_module, "_ENVELOPE_QUEUE_LOW_WATER", 1)
        resyncs: list[int] = []

        async def on_replay_lost(oldest_seq: int) -> None:
            resyncs.append(oldest_seq)

        cfg = LoomConfig(host="127.0.0.1", port=1, tls=False, auth=BearerAuth(token="t"))
        transport = WsTransport(config=cfg, on_replay_lost=on_replay_lost)

        # Fill (3) then overflow (2 more) with no consumer draining.
        for i in range(5):
            await transport._handle_text(raw=_envelope(seq=i))
        assert resyncs == [-1]
        assert transport._overflowing is True

        # Drain through the public iterator until below the low-watermark.
        events = transport.events()
        await events.__anext__()  # qsize 3 → 2, still latched
        await events.__anext__()  # qsize 2 → 1 (≤ low-water) → latch clears
        assert transport._overflowing is False

        # A brand-new overflow must force a second resync.
        for i in range(5, 10):
            await transport._handle_text(raw=_envelope(seq=i))
        assert resyncs == [-1, -1]
        assert transport._overflowing is True

        await events.aclose()


class TestAuthRejection:
    """A permanently-rejected handshake credential must stop the reconnect loop, not spin forever."""

    async def test_handshake_401_fires_callback_and_ends_stream(self) -> None:
        async def handler(request: web.Request) -> web.Response:
            return web.Response(status=401, text="Unauthorized")

        app = web.Application()
        app.router.add_get("/api/v1/events", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        cfg = LoomConfig(host="127.0.0.1", port=port, tls=False, auth=BearerAuth(token="dead"))

        auth_failures: list[bool] = []

        async def on_auth_failed() -> None:
            auth_failures.append(True)

        try:
            transport = WsTransport(config=cfg, on_auth_failed=on_auth_failed)
            await transport.start()
            # The reconnect loop must terminate (not spin) on the 401; the
            # read task ends and the stopped event is set.
            async for _ in transport.events():
                pass
            assert auth_failures == [True]
            assert transport._stopped.is_set()  # the loop ended, didn't spin
        finally:
            await transport.stop()
            await runner.cleanup()


class TestEnvelopeParsing:
    """_parse_envelope forward-compatibility and malformed-frame handling."""

    def _frame(self, *, kind: str) -> dict:
        return {
            "topic": "device.0001.channels.1.data_points.LEVEL",
            "type": "datapoint.value_changed",
            "ts": "2026-05-24T08:42:13Z",
            "seq": 7,
            "kind": kind,
            "payload": {
                "central": "home",
                "device_address": "0001",
                "channel": 1,
                "parameter": "LEVEL",
                "paramset_key": "VALUES",
                "value": 1.0,
                "modified_at": "2026-05-24T08:42:13Z",
            },
        }

    def test_unknown_kind_is_coerced_not_dropped(self) -> None:
        # A daemon that introduces a new `kind` enum value must not blackhole
        # the frame — its payload/type are still actionable.
        envelope = WsTransport._parse_envelope(self._frame(kind="snapshot"))
        assert envelope is not None
        assert envelope.kind.value == ws_module._DEFAULT_ENVELOPE_KIND
        assert envelope.seq == 7
        assert envelope.type == "datapoint.value_changed"

    def test_known_kind_is_preserved(self) -> None:
        envelope = WsTransport._parse_envelope(self._frame(kind="refresh"))
        assert envelope is not None
        assert envelope.kind.value == "refresh"

    def test_structurally_malformed_frame_is_dropped(self) -> None:
        # A frame broken beyond an unknown kind (missing required seq) is
        # dropped, not force-coerced.
        bad = self._frame(kind="snapshot")
        del bad["seq"]
        assert WsTransport._parse_envelope(bad) is None
