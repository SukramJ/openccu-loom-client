# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Async WebSocket transport for the openccu-loom event stream.

Implements the wire contract documented in
``docs/external-clients/topic-hierarchy.md`` of the daemon repo:

- Subscribe / unsubscribe by topic-prefix patterns.
- Server-pushed envelopes carry ``{topic, type, ts, seq, kind,
  payload}`` (see ``openccu_loom_types.ws.WsEnvelope``).
- Resume-after-reconnect via ``{"op":"subscribe", "since": <last_seq>}``
  per ADR-0022.
- ``replay_lost`` control frame signals the buffer aged the missing
  events out — caller must do a fresh ``GET /snapshot``.
- Heartbeat: daemon pings every 30s, client must pong within 60s.

The transport is the wire layer only. Dispatch of typed envelopes
into a domain event bus is Phase-3 work; here we expose a single
async iterator that yields validated ``WsEnvelope`` objects in
arrival order.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
import contextlib
import json
import logging
from typing import TYPE_CHECKING, Final, Self

import aiohttp
from openccu_loom_types.ws import WsEnvelope

from openccu_loom_client.exceptions import LoomTransportError

if TYPE_CHECKING:
    from types import TracebackType

    from openccu_loom_client.config import LoomConfig

_LOGGER: Final = logging.getLogger(__name__)

# Backoff schedule for WS reconnect attempts. Mirrors the HTTP
# transport's philosophy: short first retry to ride out flaps, then
# back off so a persistently-down daemon doesn't generate a tight
# reconnect loop. After the final entry the schedule clamps to that
# value (steady-state reconnect every 30 s).
_RECONNECT_BACKOFF: Final = (0.5, 2.0, 5.0, 15.0, 30.0)

# How long we wait for a server ping before considering the connection
# dead and forcing a reconnect. The daemon contract says 30 s ping
# cadence and 60 s deadline for our pong; we mirror that on our side
# for the inbound direction so a silent socket can't sit forever.
_INBOUND_PING_DEADLINE_SECONDS: Final = 60.0


ReplayLostHandler = Callable[[int], Awaitable[None]]
"""Async callback invoked when the daemon emits ``replay_lost``.

Argument is ``oldest_seq`` reported by the daemon — i.e. the
oldest event still in the buffer. The caller's job is to trigger
a snapshot-based resync; the transport itself stays subscribed.
"""


class WsTransport:
    """
    Stateful WebSocket transport against one openccu-loom daemon.

    Use as an async context manager::

        async with WsTransport(config, initial_subscriptions=["device.*"]) as ws:
            async for envelope in ws.events():
                ...

    Subscriptions and the last received ``seq`` survive reconnects;
    the read loop re-issues ``subscribe`` with ``since`` to ride
    over short network glitches.
    """

    def __init__(
        self,
        config: LoomConfig,
        *,
        initial_subscriptions: list[str] | None = None,
        on_replay_lost: ReplayLostHandler | None = None,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        """Configure the transport; the connection opens on :meth:`start`."""
        self._config: Final = config
        self._external_session: Final = session
        self._session: aiohttp.ClientSession | None = session
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._subscriptions: set[str] = set(initial_subscriptions or [])
        self._last_seq: int | None = None
        self._on_replay_lost: Final = on_replay_lost
        self._envelope_queue: asyncio.Queue[WsEnvelope] = asyncio.Queue()
        self._read_task: asyncio.Task[None] | None = None
        self._closing = False
        # Coordinates between the consumer (events()) and the producer
        # (read loop) so close() can cleanly unblock waiters.
        self._stopped = asyncio.Event()

    # ---- context manager ----

    async def __aenter__(self) -> Self:
        """Start the connection and return the transport."""
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Stop the connection on context exit."""
        await self.stop()

    # ---- lifecycle ----

    async def start(self) -> None:
        """Open the WS connection and start the background read loop."""
        if self._read_task is not None and not self._read_task.done():
            return
        if self._session is None or self._session.closed:
            self._session = self._external_session or aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=self._config.verify_tls),
            )
        self._closing = False
        self._stopped.clear()
        self._read_task = asyncio.create_task(self._run_forever(), name="openccu-loom-ws-read")

    async def stop(self) -> None:
        """Cancel the read loop and close the connection cleanly."""
        self._closing = True
        if self._ws is not None and not self._ws.closed:
            with contextlib.suppress(Exception):
                await self._ws.close()
        if self._read_task is not None:
            self._read_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._read_task
            self._read_task = None
        if self._session is not None and self._external_session is None:
            await self._session.close()
            self._session = None
        self._stopped.set()

    # ---- public API ----

    @property
    def last_seq(self) -> int | None:
        """
        Highest ``seq`` observed on a broadcast envelope so far.

        Persisted across reconnects so the next ``subscribe`` carries
        the right ``since`` cursor.
        """
        return self._last_seq

    @property
    def subscriptions(self) -> frozenset[str]:
        """Current subscription patterns (defensive copy)."""
        return frozenset(self._subscriptions)

    async def subscribe(self, topics: list[str]) -> None:
        """
        Add topic patterns to the active subscription set.

        Pushes a ``subscribe`` frame on the open connection if any of
        the topics are new. Idempotent: re-subscribing to an existing
        topic is a no-op locally and is not re-sent.
        """
        new = [t for t in topics if t not in self._subscriptions]
        if not new:
            return
        self._subscriptions.update(new)
        if self._ws is not None and not self._ws.closed:
            await self._send({"op": "subscribe", "topics": new})

    async def unsubscribe(self, topics: list[str]) -> None:
        """Drop topic patterns from the subscription set."""
        gone = [t for t in topics if t in self._subscriptions]
        if not gone:
            return
        self._subscriptions.difference_update(gone)
        if self._ws is not None and not self._ws.closed:
            await self._send({"op": "unsubscribe", "topics": gone})

    async def events(self) -> AsyncIterator[WsEnvelope]:
        """
        Yield validated envelopes in arrival order until ``stop()``.

        Backed by an unbounded internal queue — backpressure is the
        consumer's responsibility. The iterator terminates cleanly
        when ``stop()`` is called.
        """
        while not self._stopped.is_set():
            getter = asyncio.create_task(self._envelope_queue.get())
            waiter = asyncio.create_task(self._stopped.wait())
            done, pending = await asyncio.wait(
                {getter, waiter},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for p in pending:
                p.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await p
            if waiter in done:
                return
            yield getter.result()

    # ---- internals: connect / read / reconnect ----

    async def _run_forever(self) -> None:
        attempt = 0
        while not self._closing:
            try:
                await self._connect_and_read()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — keep the reconnect loop alive on any failure
                _LOGGER.warning(
                    "WS connection to %s failed (attempt %d): %s",
                    self._config.host,
                    attempt + 1,
                    exc,
                )
            if self._closing:
                # close() can flip _closing during the await above; mypy
                # keeps the while-condition's narrowing across the call and
                # wrongly flags this exit as unreachable.
                return  # type: ignore[unreachable]
            delay = _RECONNECT_BACKOFF[min(attempt, len(_RECONNECT_BACKOFF) - 1)]
            attempt += 1
            await asyncio.sleep(delay)

    async def _connect_and_read(self) -> None:
        assert self._session is not None  # noqa: S101 — invariant: session opened in connect()

        headers: dict[str, str] = {"User-Agent": self._config.user_agent}
        self._config.auth.apply_to_headers(headers)

        async with self._session.ws_connect(
            self._config.ws_url,
            headers=headers,
            heartbeat=None,  # daemon drives heartbeat; aiohttp default would double-ping
        ) as ws:
            self._ws = ws
            await self._send_initial_subscribe()
            try:
                await self._read_loop(ws)
            finally:
                self._ws = None

    async def _send_initial_subscribe(self) -> None:
        if not self._subscriptions:
            return
        frame: dict[str, object] = {
            "op": "subscribe",
            "topics": sorted(self._subscriptions),
        }
        if self._last_seq is not None:
            frame["since"] = self._last_seq
        await self._send(frame)

    async def _read_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        while not self._closing:
            try:
                msg = await asyncio.wait_for(
                    ws.receive(),
                    timeout=_INBOUND_PING_DEADLINE_SECONDS,
                )
            except TimeoutError as exc:
                msg_text = (
                    f"no daemon ping in {_INBOUND_PING_DEADLINE_SECONDS}s "
                    "— treating connection as dead"
                )
                raise LoomTransportError(msg_text) from exc

            if msg.type == aiohttp.WSMsgType.TEXT:
                await self._handle_text(msg.data)
            elif msg.type == aiohttp.WSMsgType.CLOSE:
                _LOGGER.info("daemon initiated WS close: %s", msg.data)
                return
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
                return
            elif msg.type == aiohttp.WSMsgType.ERROR:
                msg_text = f"WS error frame: {ws.exception()}"
                raise LoomTransportError(msg_text)
            # PING/PONG are handled by aiohttp's internals; the daemon's
            # in-band {"op":"ping"} arrives as a TEXT frame and is handled
            # in _handle_text below.

    async def _handle_text(self, raw: str) -> None:
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            _LOGGER.warning("ignoring non-JSON WS frame: %r", raw[:120])
            return
        if not isinstance(frame, dict):
            _LOGGER.warning("ignoring non-object WS frame: %r", raw[:120])
            return

        # Control frames carry "op"; broadcast envelopes carry "type" + "topic".
        op = frame.get("op")
        if op is not None:
            await self._handle_control(op, frame)
            return

        envelope = self._parse_envelope(frame)
        if envelope is None:
            return
        # Monotonic by daemon contract — keep the highest we've seen.
        if envelope.seq is not None and (self._last_seq is None or envelope.seq > self._last_seq):
            self._last_seq = envelope.seq
        await self._envelope_queue.put(envelope)

    async def _handle_control(self, op: str, frame: dict[str, object]) -> None:
        if op == "ping":
            await self._send({"op": "pong"})
        elif op == "replay_done":
            seq = frame.get("seq")
            _LOGGER.debug("replay_done at seq=%s", seq)
        elif op == "replay_lost":
            oldest = frame.get("oldest_seq")
            _LOGGER.warning(
                "WS replay buffer aged events out (oldest_seq=%s) — caller must resync",
                oldest,
            )
            if self._on_replay_lost is not None and isinstance(oldest, int):
                with contextlib.suppress(Exception):
                    await self._on_replay_lost(oldest)
        elif op in ("subscribe_ack", "unsubscribe_ack", "pong"):
            _LOGGER.debug("WS control: %s %s", op, frame)
        elif op == "reauth_ok":
            _LOGGER.info("WS reauth accepted")
        elif op == "reauth_failed":
            _LOGGER.warning("WS reauth rejected — daemon will close the connection")
        else:
            _LOGGER.debug("unknown WS control op %r: %s", op, frame)

    @staticmethod
    def _parse_envelope(frame: dict[str, object]) -> WsEnvelope | None:
        try:
            return WsEnvelope.model_validate(frame)
        except Exception as exc:  # noqa: BLE001 — drop malformed frames, never crash the reader
            _LOGGER.warning("dropping malformed WS envelope: %s | frame=%r", exc, frame)
            return None

    async def _send(self, frame: dict[str, object]) -> None:
        if self._ws is None or self._ws.closed:
            msg = "cannot send on closed WS"
            raise LoomTransportError(msg)
        await self._ws.send_str(json.dumps(frame))

    # ---- in-band auth refresh (per topic-hierarchy.md) ----

    async def reauth(self, token: str) -> None:
        """
        Swap the connection's bearer token without a reconnect.

        Useful when an operator revokes the active token via
        ``DELETE /auth/tokens/{id}`` and the client wants to present
        a freshly-issued one without losing its subscription state.
        """
        await self._send({"op": "reauth", "token": token})
