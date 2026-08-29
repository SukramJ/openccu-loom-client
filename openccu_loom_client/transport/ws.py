# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Async WebSocket transport for the openccu-loom event stream.

Implements the wire contract documented in
``docs/external-clients/topic-hierarchy.md`` of the daemon repo:

- Subscribe / unsubscribe by topic-prefix patterns.
- Server-pushed envelopes carry ``{topic, type, ts, seq, kind,
  payload}`` (see ``openccu_loom_client.wire.ws.WsEnvelope``).
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
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
import contextlib
from dataclasses import replace
from http import HTTPStatus
import json
import logging
import random
from typing import TYPE_CHECKING, Final, Self

import aiohttp
from pydantic import ValidationError

from openccu_loom_client.auth import BearerAuth
from openccu_loom_client.exceptions import LoomTransportError
from openccu_loom_client.wire.ws import WsEnvelope

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

# Fraction of the ladder step applied as random jitter (± this share). Several
# clients that lived through the same daemon outage otherwise come back in
# lockstep, and the moment they pick is the worst one: a restarting daemon is
# pulling the CCU exactly then. The jitter only spreads arrivals — it never
# shortens the wait below (1 - share) of the step, so the "no tight loop"
# property of the ladder is preserved.
_RECONNECT_JITTER_SHARE: Final = 0.2

# A reconnect only resets the backoff ladder once the connection has been
# up at least this long. Otherwise a daemon that accepts the upgrade and
# then immediately closes would reset the backoff on every cycle, busy-
# looping reconnects at the shortest 0.5 s step instead of backing off.
_HEALTHY_CONNECTION_SECONDS: Final = 10.0

# Live-update kind a forward-compatible envelope falls back to when the
# daemon introduces a `kind` enum value this build's types don't know yet.
# Coercing (rather than dropping the frame) keeps the payload flowing, the
# same way an unknown `type` degrades gracefully downstream.
_DEFAULT_ENVELOPE_KIND: Final = "change"

# How long we wait for a server ping before considering the connection
# dead and forcing a reconnect. The daemon contract says 30 s ping
# cadence and 60 s deadline for our pong; we mirror that on our side
# for the inbound direction so a silent socket can't sit forever.
_INBOUND_PING_DEADLINE_SECONDS: Final = 60.0

# Upper bound on the in-memory envelope queue. A slow consumer (e.g. a long
# re-bootstrap) on a busy CCU would otherwise let the reader grow the queue
# without limit → OOM. On overflow the backlog is stale anyway, so we drop it
# and force a resync (fresh snapshot) — the store rebuilds rather than drifting.
_ENVELOPE_QUEUE_MAXSIZE: Final = 4096

# Aggregate byte ceiling on the in-memory envelope queue. The item-count cap
# alone does not bound memory: ``WsEnvelope.payload`` is ``Any`` and each frame
# can be up to ``_MAX_WS_MSG_SIZE``, so a full queue of maximally-sized frames
# would be item-count × frame-size. During a slow-consumer window (a long
# re-bootstrap) a busy or hostile daemon could flood large valid envelopes and
# push transient memory toward multi-GB before the count cap engages. We track
# queued bytes (by source-frame length) and force the same drop-and-resync once
# this ceiling is crossed, whichever limit is hit first.
_ENVELOPE_QUEUE_MAX_BYTES: Final = 64 * 1024 * 1024

# Explicit per-frame size cap for the WS upgrade (aiohttp defaults to 4 MiB).
# Broadcast envelopes are per-event and tiny (a value change is a few hundred
# bytes); a small ceiling bounds both a single frame's memory and the worst-case
# repr volume of a malformed frame, without threatening any legitimate payload.
_MAX_WS_MSG_SIZE: Final = 1024 * 1024

# Upper bound on how much of a rejected frame is echoed into a WARNING log line.
# A malformed frame is daemon-controlled and can be near ``_MAX_WS_MSG_SIZE``;
# logging it verbatim (as the sibling non-JSON paths already avoid) lets a peer
# amplify wire bytes into unbounded log volume and repr CPU. Truncate it.
_MAX_FRAME_LOG_CHARS: Final = 200

# Low-watermark that ends an overflow episode. While the queue is full the
# producer drops events and forces exactly one resync per episode (see the
# ``_overflowing`` latch in ``_handle_text``); the consumer clears the latch
# only once it has drained the backlog back below this level, so a sustained
# flood emits one warning + one resync instead of one per dropped event.
_ENVELOPE_QUEUE_LOW_WATER: Final = _ENVELOPE_QUEUE_MAXSIZE // 2


ReplayLostHandler = Callable[[int], Awaitable[None]]
"""Async callback invoked when the daemon emits ``replay_lost``.

Argument is ``oldest_seq`` reported by the daemon — i.e. the
oldest event still in the buffer. The caller's job is to trigger
a snapshot-based resync; the transport itself stays subscribed.
"""

AuthFailedHandler = Callable[[], Awaitable[None]]
"""Async callback invoked when the daemon rejects an in-band ``reauth``.

The caller can re-provision a token (and call :meth:`WsTransport.reauth`
again) or tear the client down — without it, a rejected token would let the
reconnect loop spin forever against a dead credential.
"""

ConnectionStateHandler = Callable[[bool], Awaitable[None]]
"""Async callback invoked when the WS connection goes up (True) or down (False).

The transport reconnects on its own, so this is not a request for the caller to
do anything about the socket. It is the only way anything above the transport
can learn that push has stopped: a dropped connection is exactly what the
daemon cannot announce, and without this signal a consumer keeps presenting the
last values it received as though they were still live.
"""

HeartbeatHandler = Callable[[float], Awaitable[None]]
"""Async callback invoked with the client↔daemon round-trip time in milliseconds.

Measured by the daemon: it stamps an opaque token onto each ping, we echo it
back, and the round trip it computes against its own clock arrives on the next
ping as ``rtt_ms``. Nothing here times anything, and the two clocks need no
agreement.

This is the distance to the *daemon*. The hub's ``connection_latency`` metric
reports the daemon-to-CCU leg, which has entirely different causes — a slow
reverse proxy on this side is invisible there.
"""

# How long :meth:`WsTransport.reauth` waits for the daemon's ack control frame.
_REAUTH_ACK_TIMEOUT_SECONDS: Final = 10.0


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
        *,
        config: LoomConfig,
        initial_subscriptions: list[str] | None = None,
        on_replay_lost: ReplayLostHandler | None = None,
        on_auth_failed: AuthFailedHandler | None = None,
        on_connection_state: ConnectionStateHandler | None = None,
        on_heartbeat: HeartbeatHandler | None = None,
        session: aiohttp.ClientSession | None = None,
        classify: bool = False,
    ) -> None:
        """
        Configure the transport; the connection opens on :meth:`start`.

        Whether unfinished-onboarding devices are filtered out comes from
        :attr:`LoomConfig.released_only`, so the WS subscribe option and the
        REST snapshot query cannot disagree — the daemon's contract asks for
        them to be paired, since one without the other yields a snapshot
        missing a device that live pushes then talk about (or the reverse).

        ``classify`` opts into the daemon's inline classification (api ≥
        5.34.0): every ``subscribe`` frame carries ``classify: true``, and
        ``datapoint.value_changed`` payloads then include ``category`` and
        ``data_point_type``. Default off — this client classifies through
        the store's data-point factories, so the extra fields are only for
        consumers that read the wire payloads directly. An older daemon
        ignores the unknown field.
        """
        self._config: Final = config
        self._classify: Final = classify
        # Rides every subscribe frame, initial and runtime alike, because the
        # daemon applies it per connection: a reconnect that omitted it would
        # silently start delivering withheld devices again, and the drop would
        # be invisible — the stream still works, it just carries more.
        self._released_only: Final = config.released_only
        self._external_session: Final = session
        self._session: aiohttp.ClientSession | None = session
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._subscriptions: set[str] = set(initial_subscriptions or [])
        self._last_seq: int | None = None
        self._on_replay_lost: Final = on_replay_lost
        self._on_auth_failed: Final = on_auth_failed
        self._on_connection_state: Final = on_connection_state
        self._on_heartbeat: Final = on_heartbeat
        # Latest client↔daemon round trip the daemon reported, or None until
        # the second heartbeat of a connection (the first has nothing to time).
        self._last_rtt_ms: float | None = None
        # Last state handed to the callback, so a reconnect storm does not
        # emit a run of identical notifications.
        self._reported_connected = False
        # Pending in-band reauth ack (resolved by _handle_control on
        # reauth_ok/reauth_failed); None when no reauth is in flight.
        self._reauth_ack: asyncio.Future[bool] | None = None
        self._envelope_queue: asyncio.Queue[WsEnvelope] = asyncio.Queue(maxsize=_ENVELOPE_QUEUE_MAXSIZE)
        # Source-frame byte size per queued envelope, kept FIFO-aligned with
        # ``_envelope_queue`` (single-threaded event loop, so producer append and
        # consumer popleft stay in lock-step). ``_queued_bytes`` is their running
        # sum, used to enforce ``_ENVELOPE_QUEUE_MAX_BYTES`` alongside the count.
        self._envelope_sizes: deque[int] = deque()
        self._queued_bytes = 0
        self._dropped_count = 0
        # Latch for the current overflow episode: set on the first dropped
        # envelope, cleared by the consumer once the queue drains back below
        # the low-watermark. Producer and consumer share one event loop, so
        # plain attribute access needs no lock.
        self._overflowing = False
        self._overflow_start_dropped = 0
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
        /,
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
        await self._report_connection_state(connected=False)
        self._stopped.set()

    # ---- public API ----

    @property
    def last_rtt_ms(self) -> float | None:
        """
        Latest client↔daemon round trip in milliseconds, or ``None``.

        ``None`` until the daemon has timed one: the first ping of a connection
        has no preceding pong to measure, and a daemon older than api 7.13.0
        never reports one at all.
        """
        return self._last_rtt_ms

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

    async def subscribe(self, *, topics: list[str]) -> None:
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
            frame: dict[str, object] = {"op": "subscribe", "topics": new}
            if self._classify:
                frame["classify"] = True
            if self._released_only:
                frame["released_only"] = True
            await self._send(frame=frame)

    async def unsubscribe(self, *, topics: list[str]) -> None:
        """Drop topic patterns from the subscription set."""
        gone = [t for t in topics if t in self._subscriptions]
        if not gone:
            return
        self._subscriptions.difference_update(gone)
        if self._ws is not None and not self._ws.closed:
            await self._send(frame={"op": "unsubscribe", "topics": gone})

    async def events(self) -> AsyncIterator[WsEnvelope]:
        """
        Yield validated envelopes in arrival order until ``stop()``.

        Backed by a bounded internal queue (overflow forces a resync, see
        :data:`_ENVELOPE_QUEUE_MAXSIZE`). The iterator terminates cleanly when
        ``stop()`` is called. The stop-waiter is created once and reused, so
        only a single ``queue.get()`` task is spawned per envelope (not two).
        """
        waiter = asyncio.create_task(self._stopped.wait())
        try:
            while not self._stopped.is_set():
                getter = asyncio.create_task(self._envelope_queue.get())
                done, _pending = await asyncio.wait(
                    {getter, waiter},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if waiter in done:
                    getter.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await getter
                    return
                # One envelope just left the queue — keep the byte tally in
                # lock-step with the count (FIFO-aligned with the producer).
                if self._envelope_sizes:
                    self._queued_bytes -= self._envelope_sizes.popleft()
                # Ending an overflow episode: once the backlog has drained back
                # below the low-watermark, clear the latch so a later overflow
                # warns and resyncs afresh. Report the episode's drop tally.
                if self._overflowing and self._envelope_queue.qsize() <= _ENVELOPE_QUEUE_LOW_WATER:
                    self._overflowing = False
                    _LOGGER.warning(
                        "WS envelope queue drained below %d — overflow resolved; %d events dropped this episode",
                        _ENVELOPE_QUEUE_LOW_WATER,
                        self._dropped_count - self._overflow_start_dropped + 1,
                    )
                yield getter.result()
        finally:
            waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await waiter

    # ---- internals: connect / read / reconnect ----

    async def _run_forever(self) -> None:
        attempt = 0
        loop = asyncio.get_running_loop()
        while not self._closing:
            started = loop.time()
            try:
                await self._connect_and_read()
            except asyncio.CancelledError:
                raise
            except aiohttp.WSServerHandshakeError as exc:
                if exc.status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
                    # A permanently-rejected credential must not spin the
                    # reconnect loop forever. Surface it (optional callback)
                    # and end the stream so the consumer's events() unblocks
                    # instead of hanging on a socket that will never open.
                    _LOGGER.error(
                        "WS handshake rejected by %s with status %d — credential not accepted; "
                        "stopping the reconnect loop",
                        self._config.host,
                        exc.status,
                    )
                    await self._report_connection_state(connected=False)
                    if self._on_auth_failed is not None:
                        with contextlib.suppress(Exception):
                            await self._on_auth_failed()
                    self._stopped.set()
                    return
                _LOGGER.warning(
                    "WS connection to %s failed (attempt %d): %s",
                    self._config.host,
                    attempt + 1,
                    exc,
                )
            except Exception as exc:  # noqa: BLE001 — keep the reconnect loop alive on any failure
                _LOGGER.warning(
                    "WS connection to %s failed (attempt %d): %s",
                    self._config.host,
                    attempt + 1,
                    exc,
                )
            else:
                # Reset the backoff only after a connection that actually
                # stayed up — a clean but immediate close (accept-then-drop)
                # is treated like a failure so the ladder still escalates.
                if loop.time() - started >= _HEALTHY_CONNECTION_SECONDS:
                    attempt = 0
            if self._closing:
                # close() can flip _closing during the await above; mypy
                # keeps the while-condition's narrowing across the call and
                # wrongly flags this exit as unreachable.
                return  # type: ignore[unreachable]
            delay = self._backoff_delay(attempt=attempt)
            attempt += 1
            await asyncio.sleep(delay)

    @staticmethod
    def _backoff_delay(*, attempt: int) -> float:
        """
        Return the reconnect delay for ``attempt``, jittered.

        The ladder clamps to its last entry, so the steady state against a
        permanently-down daemon is one attempt per final step. The jitter is
        symmetric around that step (see :data:`_RECONNECT_JITTER_SHARE`) so a
        fleet of clients recovering from one outage does not arrive together.
        """
        step = _RECONNECT_BACKOFF[min(attempt, len(_RECONNECT_BACKOFF) - 1)]
        spread = step * _RECONNECT_JITTER_SHARE
        return step + random.uniform(-spread, spread)  # noqa: S311 # nosec B311 — spreads reconnect arrivals, not a secret

    async def _connect_and_read(self) -> None:
        assert self._session is not None  # noqa: S101 — invariant: session opened in connect()

        headers: dict[str, str] = {"User-Agent": self._config.user_agent}
        self._config.auth.apply_to_headers(headers=headers)

        async with self._session.ws_connect(
            self._config.ws_url,
            headers=headers,
            heartbeat=None,  # daemon drives heartbeat; aiohttp default would double-ping
            max_msg_size=_MAX_WS_MSG_SIZE,  # cap per-frame size (aiohttp default 4 MiB)
        ) as ws:
            self._ws = ws
            await self._send_initial_subscribe()
            # Announced only after the subscribe frame is away, so a consumer
            # acting on "connected" (re-checking the contract, re-bootstrapping)
            # cannot run against a socket that is not yet subscribed.
            await self._report_connection_state(connected=True)
            try:
                await self._read_loop(ws=ws)
            finally:
                self._ws = None
                await self._report_connection_state(connected=False)

    async def _send_initial_subscribe(self) -> None:
        if not self._subscriptions:
            return
        frame: dict[str, object] = {
            "op": "subscribe",
            "topics": sorted(self._subscriptions),
        }
        if self._classify:
            frame["classify"] = True
        if self._released_only:
            frame["released_only"] = True
        if self._last_seq is not None:
            frame["since"] = self._last_seq
        await self._send(frame=frame)

    async def _read_loop(self, *, ws: aiohttp.ClientWebSocketResponse) -> None:
        while not self._closing:
            try:
                msg = await asyncio.wait_for(
                    ws.receive(),
                    timeout=_INBOUND_PING_DEADLINE_SECONDS,
                )
            except TimeoutError as exc:
                msg_text = f"no daemon ping in {_INBOUND_PING_DEADLINE_SECONDS}s — treating connection as dead"
                raise LoomTransportError(msg_text) from exc

            if msg.type == aiohttp.WSMsgType.TEXT:
                await self._handle_text(raw=msg.data)
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

    async def _handle_text(self, *, raw: str) -> None:
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
            await self._handle_control(op=op, frame=frame)
            return

        envelope = self._parse_envelope(frame)
        if envelope is None:
            return
        # Monotonic by daemon contract — keep the highest we've seen.
        if envelope.seq is not None and (self._last_seq is None or envelope.seq > self._last_seq):
            self._last_seq = envelope.seq
        # Overflow on either bound — item count OR aggregate bytes — since a
        # burst of large frames can blow the byte budget long before the count.
        frame_size = len(raw)
        if self._queued_bytes + frame_size > _ENVELOPE_QUEUE_MAX_BYTES:
            await self._register_overflow()
            return
        try:
            self._envelope_queue.put_nowait(envelope)
        except asyncio.QueueFull:
            await self._register_overflow()
            return
        self._envelope_sizes.append(frame_size)
        self._queued_bytes += frame_size

    async def _register_overflow(self) -> None:
        """
        Drop the overflowing envelope and force exactly one resync per episode.

        Reached when either the item-count or the byte budget is exhausted. The
        backlog is stale anyway, so we drop and force a resync (fresh snapshot);
        the store rebuilds rather than drifting. Latch the episode: warn and
        trigger the resync only on the first drop, then stay silent until the
        consumer drains below the low-watermark and clears the latch (see
        ``events()``). Without the latch a sustained flood logged — and re-fired
        the resync — once per dropped event (thousands of lines/sec).
        """
        self._dropped_count += 1
        if not self._overflowing:
            self._overflowing = True
            self._overflow_start_dropped = self._dropped_count
            _LOGGER.warning(
                "WS envelope queue full (maxsize=%d, ~%d bytes queued) — forcing resync; %d events dropped so far",
                _ENVELOPE_QUEUE_MAXSIZE,
                self._queued_bytes,
                self._dropped_count,
            )
            await self._trigger_resync(oldest_seq=-1)

    async def _handle_control(self, *, op: str, frame: dict[str, object]) -> None:
        if op == "ping":
            # `rtt_ms` on this ping is the round trip of the PREVIOUS heartbeat,
            # measured by the daemon against its own clock — so it needs no
            # agreement between the two clocks, and we need no timer of our own.
            if (rtt := frame.get("rtt_ms")) is not None and isinstance(rtt, int | float):
                self._last_rtt_ms = float(rtt)
                if self._on_heartbeat is not None:
                    with contextlib.suppress(Exception):
                        await self._on_heartbeat(float(rtt))
            # Echo the token back, when the daemon sent one. A bare pong is a
            # valid heartbeat and the contract says so — it just cannot be
            # timed, which is why the daemon never had an rtt to report before.
            pong: dict[str, object] = {"op": "pong"}
            if (echo := frame.get("echo")) is not None:
                pong["echo"] = echo
            await self._send(frame=pong)
        elif op == "replay_done":
            seq = frame.get("seq")
            _LOGGER.debug("replay_done at seq=%s", seq)
        elif op == "replay_lost":
            oldest = frame.get("oldest_seq")
            _LOGGER.warning(
                "WS replay buffer aged events out (oldest_seq=%s) — caller must resync",
                oldest,
            )
            # Re-anchor the resume cursor on the seq space the daemon just
            # named. Without this the cursor only ever grows (see
            # _handle_text), which breaks resume for good across a daemon
            # restart: the daemon's seq counter restarts at 0, every live
            # envelope then carries a *smaller* seq than the cursor we kept
            # from the previous incarnation, so the cursor never moves again.
            # Each later reconnect re-sends that stale `since`, the daemon
            # answers `Lost` (it is above its current top — see Hub.Replay),
            # and the client re-walks the whole snapshot every single time,
            # even where the replay ring could have served it.
            #
            # Deliberately only here, not in the shared _trigger_resync: the
            # queue-overflow path funnels through that same callback with a
            # -1 sentinel, and its cursor is perfectly good — adopting the
            # sentinel there would corrupt it.
            if isinstance(oldest, int) and oldest >= 0:
                self._last_seq = oldest
            else:
                # A malformed or absent anchor: drop the cursor entirely
                # rather than keep one the daemon has just told us it
                # cannot place. The next subscribe then omits `since` and
                # asks for the live stream, which is what the re-bootstrap
                # this triggers is about to make authoritative anyway.
                self._last_seq = None
            # Fire the resync callback unconditionally: the buffer has aged
            # out regardless of whether the daemon attached a usable
            # oldest_seq. Coerce a missing / non-int value to a -1 sentinel
            # so a malformed frame can't silently skip the resync (the
            # callback only logs the value; the resync starts from a fresh
            # snapshot either way).
            await self._trigger_resync(oldest_seq=oldest if isinstance(oldest, int) else -1)
        elif op in ("subscribe_ack", "unsubscribe_ack", "pong"):
            _LOGGER.debug("WS control: %s %s", op, frame)
        elif op == "reauth_ok":
            _LOGGER.info("WS reauth accepted")
            if self._reauth_ack is not None and not self._reauth_ack.done():
                self._reauth_ack.set_result(True)
        elif op == "reauth_failed":
            _LOGGER.warning("WS reauth rejected — daemon will close the connection")
            if self._reauth_ack is not None and not self._reauth_ack.done():
                self._reauth_ack.set_result(False)
            if self._on_auth_failed is not None:
                with contextlib.suppress(Exception):
                    await self._on_auth_failed()
        else:
            _LOGGER.debug("unknown WS control op %r: %s", op, frame)

    async def _report_connection_state(self, *, connected: bool) -> None:
        """
        Hand a connection transition to the callback, de-duplicated.

        Only transitions are reported: the reconnect loop calls this on every
        cycle, and a daemon that is down for an hour would otherwise produce a
        disconnect notification every 30 seconds. Handler errors are swallowed
        for the same reason the resync callback swallows them — a consumer's
        bug must not take the reader loop down with it.
        """
        if connected == self._reported_connected:
            return
        self._reported_connected = connected
        if self._on_connection_state is not None:
            with contextlib.suppress(Exception):
                await self._on_connection_state(connected)

    async def _trigger_resync(self, *, oldest_seq: int) -> None:
        """
        Invoke the resync callback (caller re-snapshots), swallowing handler errors.

        Shared by the daemon's ``replay_lost`` control frame and the local
        envelope-queue overflow path: both mean buffered events were lost, so
        the store must rebuild from a fresh snapshot. The callback is expected
        to de-duplicate concurrent invocations (it schedules one re-bootstrap).
        """
        if self._on_replay_lost is not None:
            with contextlib.suppress(Exception):
                await self._on_replay_lost(oldest_seq)

    @staticmethod
    def _parse_envelope(frame: dict[str, object]) -> WsEnvelope | None:
        try:
            return WsEnvelope.model_validate(frame)
        except ValidationError as exc:
            # Forward compatibility: if the *only* problem is that the daemon
            # sent a `kind` enum value this build's types don't know, coerce
            # it to the default live-update kind and re-validate rather than
            # blackholing the whole frame — its payload/type may still be
            # actionable (mirrors the graceful unknown-`type` degradation).
            if WsTransport._is_unknown_kind_only(exc=exc):
                coerced: dict[str, object] = {**frame, "kind": _DEFAULT_ENVELOPE_KIND}
                try:
                    envelope = WsEnvelope.model_validate(coerced)
                except ValidationError as retry_exc:
                    _LOGGER.warning(
                        "dropping malformed WS envelope: %s | frame=%s", retry_exc, WsTransport._short_frame(frame)
                    )
                    return None
                _LOGGER.debug(
                    "coerced unknown WS envelope kind %r to %r | topic=%s",
                    frame.get("kind"),
                    _DEFAULT_ENVELOPE_KIND,
                    frame.get("topic"),
                )
                return envelope
            _LOGGER.warning("dropping malformed WS envelope: %s | frame=%s", exc, WsTransport._short_frame(frame))
            return None
        except Exception as exc:  # noqa: BLE001 — drop malformed frames, never crash the reader
            _LOGGER.warning("dropping malformed WS envelope: %s | frame=%s", exc, WsTransport._short_frame(frame))
            return None

    @staticmethod
    def _short_frame(frame: object) -> str:
        """Return a length-bounded repr of a daemon frame for safe logging."""
        text = repr(frame)
        if len(text) <= _MAX_FRAME_LOG_CHARS:
            return text
        return f"{text[:_MAX_FRAME_LOG_CHARS]}… (+{len(text) - _MAX_FRAME_LOG_CHARS} chars)"

    @staticmethod
    def _is_unknown_kind_only(*, exc: ValidationError) -> bool:
        """Report whether the sole validation error is an unknown ``kind`` enum value."""
        errors = exc.errors()
        return bool(errors) and all(
            err.get("loc") == ("kind",) and str(err.get("type", "")).startswith("enum") for err in errors
        )

    async def _send(self, *, frame: dict[str, object]) -> None:
        if self._ws is None or self._ws.closed:
            msg = "cannot send on closed WS"
            raise LoomTransportError(msg)
        await self._ws.send_str(json.dumps(frame))

    # ---- in-band auth refresh (per topic-hierarchy.md) ----

    async def reauth(self, *, token: str) -> None:
        """
        Swap the connection's bearer token without a reconnect.

        Useful when an operator revokes the active token via
        ``DELETE /auth/tokens/{id}`` and the client wants to present
        a freshly-issued one without losing its subscription state.

        Awaits the daemon's ``reauth_ok`` / ``reauth_failed`` ack. On success
        the new token is mirrored into ``config.auth`` so a later reconnect
        carries it (without this the socket would silently revert to the old
        token). On rejection — or ack timeout — raises :class:`LoomTransportError`
        so the caller learns the credential is dead instead of looping forever.
        """
        ack: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._reauth_ack = ack
        try:
            await self._send(frame={"op": "reauth", "token": token})
            try:
                accepted = await asyncio.wait_for(ack, timeout=_REAUTH_ACK_TIMEOUT_SECONDS)
            except TimeoutError as exc:
                msg = f"no reauth ack within {_REAUTH_ACK_TIMEOUT_SECONDS}s"
                raise LoomTransportError(msg) from exc
        finally:
            self._reauth_ack = None
        if not accepted:
            msg = "daemon rejected the reauth token"
            raise LoomTransportError(msg)
        # Mirror the accepted token so reconnects present it (BearerAuth is
        # frozen, so replace the config's auth method with an updated copy).
        if isinstance(self._config.auth, BearerAuth):
            self._config.auth = replace(self._config.auth, token=token)
