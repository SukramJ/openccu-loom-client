# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""WS-transport tests using a real aiohttp TestServer as the daemon side.

aiohttp.test_utils gives us a full-fidelity server (handshake, frame
parsing, close) without mocking — we control the daemon's responses
directly and can assert on every client frame it receives.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable

import aiohttp
import pytest
from aiohttp import web

from openccu_loom_client import BearerAuth, LoomConfig
from openccu_loom_client.transport import WsTransport

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
async def fake_daemon() -> AsyncIterator[
    Callable[[FakeDaemonScript], Awaitable[tuple[LoomConfig, list[dict]]]]
]:
    """Spawn a fresh fake-daemon WS server per test, returning a config
    bound to its address and a list collecting every frame the client
    sent.
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
        async with WsTransport(cfg, initial_subscriptions=["device.*", "hub.*"]):
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
        async with WsTransport(cfg, initial_subscriptions=["device.*"]) as ws:
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
        async with WsTransport(cfg, initial_subscriptions=["device.*"]):
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
        async with WsTransport(cfg, initial_subscriptions=["device.*"], on_replay_lost=on_lost):
            await asyncio.sleep(0.3)

        assert captured == [901]


class TestRuntimeSubscriptions:
    async def test_subscribe_sends_only_new_topics(self, fake_daemon) -> None:
        async def script(ws: web.WebSocketResponse, _rx: list[dict]) -> None:
            await asyncio.sleep(0.4)

        cfg, rx = await fake_daemon(script)
        async with WsTransport(cfg, initial_subscriptions=["device.*"]) as ws:
            await asyncio.sleep(0.1)
            # Existing topic — should be no-op.
            await ws.subscribe(["device.*"])
            # New topic — should send a fresh subscribe frame.
            await ws.subscribe(["hub.*"])
            await asyncio.sleep(0.15)

        subs = [f for f in rx if f.get("op") == "subscribe"]
        # Two subscribes total: initial + the "hub.*" addition.
        assert len(subs) == 2
        assert subs[1]["topics"] == ["hub.*"]

    async def test_unsubscribe_drops_topic_locally_and_on_wire(self, fake_daemon) -> None:
        async def script(ws: web.WebSocketResponse, _rx: list[dict]) -> None:
            await asyncio.sleep(0.3)

        cfg, rx = await fake_daemon(script)
        async with WsTransport(cfg, initial_subscriptions=["device.*", "hub.*"]) as ws:
            await asyncio.sleep(0.1)
            await ws.unsubscribe(["hub.*"])
            await asyncio.sleep(0.1)
            assert "hub.*" not in ws.subscriptions
            assert "device.*" in ws.subscriptions

        unsubs = [f for f in rx if f.get("op") == "unsubscribe"]
        assert len(unsubs) == 1
        assert unsubs[0]["topics"] == ["hub.*"]
