# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
High-level :class:`LoomClient` — facade over transport + store + bus.

The client is the single object Home Assistant (and any other
consumer) interacts with. It composes:

- :class:`HttpTransport` — REST round-trips, RFC 9457 problem+json
  handling, retry/backoff, capability handshake at connect.
- :class:`WsTransport` — WebSocket event stream with resume cursor
  and heartbeat.
- :class:`LoomStore` — in-memory mirror of the daemon's CCU model.
- :class:`EventBus` — typed pub/sub for ``LoomEvent`` subclasses.
- :class:`DevicesOperations` / :class:`DataPointsOperations` /
  :class:`HubOperations` / :class:`SystemOperations` /
  :class:`SchedulesOperations` / :class:`LinksOperations` — pythonic
  REST facades, exposed as attributes on the client.

Usage::

    async with LoomClient(config) as client:
        await client.bootstrap()
        # browse the store…
        for device in client.store.devices:
            print(device)
        # subscribe to events…
        client.events.subscribe(
            event_type=DataPointValueChangedEvent,
            handler=on_change,
        )
        # send a value…
        dp = client.store.get_data_point(address="VCU0001", channel=1, parameter="STATE")
        await dp.send_value(True)
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Iterable, Mapping
import contextlib
import logging
from typing import TYPE_CHECKING, Final, Self

from openccu_loom_client.bridge import bind_ws_events_to_store
from openccu_loom_client.events import (
    DeviceCreatedEvent,
    EventBus,
    SubscriptionGroup,
    event_from_envelope,
    new_data_points_created_event,
)
from openccu_loom_client.exceptions import LoomNotFoundError
from openccu_loom_client.operations import (
    AlarmOperations,
    AuthOperations,
    BackupOperations,
    CentralsOperations,
    ConfigOperations,
    CustomDataPointsOperations,
    DataPointsOperations,
    DevicesOperations,
    DiagnosticsOperations,
    GroupsOperations,
    HubOperations,
    LinksOperations,
    MatterOperations,
    SchedulesOperations,
    SessionsOperations,
    SystemOperations,
    UsersOperations,
    VisibilityOperations,
)
from openccu_loom_client.store import LoomStore
from openccu_loom_client.transport import HttpTransport, WsTransport

if TYPE_CHECKING:
    from types import TracebackType

    from openccu_loom_types.rest import DataPointSummary, DeviceChannel

    from openccu_loom_client.config import LoomConfig

_LOGGER: Final = logging.getLogger(__name__)

# Default WS topic subscriptions for the HA-equivalent feature set:
# datapoint value pushes, custom-DP state pushes, plus device +
# central + system + hub events from every CCU. ``datapoint.*`` and
# ``custom_data_point.*`` are the live-state plane — without them every
# entity freezes on its bootstrap value. ``alarm.*`` carries the alarm
# panel plane (daemon ≥ 0.42.0); a daemon without the alarm subsystem
# simply never publishes on it. Callers that want narrower scope can
# pass an explicit list to start_events().
_DEFAULT_WS_SUBSCRIPTIONS: Final = (
    "datapoint.*",
    "custom_data_point.*",
    "device.*",
    "central.*",
    "system.*",
    "hub.*",
    "alarm.*",
)

# Minimum spacing between replay-lost re-bootstraps, measured from the end of
# the previous one. A daemon stuck emitting ``replay_lost`` (or a queue-overflow
# resync fired per-frame) would otherwise schedule a fresh N×M snapshot walk the
# instant each prior walk finished. The just-taken snapshot is authoritative for
# far longer than this window and live events keep flowing meanwhile, so a burst
# collapses to at most one walk per (walk duration + cooldown).
_REBOOTSTRAP_COOLDOWN_SECONDS: Final = 30.0


def _channel_dp_map(
    *,
    device_channels: list[DeviceChannel] | None,
) -> dict[str, dict[int, list[DataPointSummary]]]:
    """
    Index a nested snapshot's ``device_channels`` by device + channel.

    Returns ``{device_address: {channel_number: [DataPointSummary]}}``.
    Empty when the daemon returned no nested data (older daemon, or the
    flat snapshot shape) — the caller then falls back to per-channel
    REST fetches.
    """
    return {
        entry.device_address: {channel.number: list(channel.data_points or ()) for channel in entry.channels or ()}
        for entry in device_channels or ()
    }


class LoomClient:
    """Composer of all client-side components for one openccu-loom daemon."""

    def __init__(
        self,
        *,
        config: LoomConfig,
        store: LoomStore | None = None,
        bus: EventBus | None = None,
        http_transport: HttpTransport | None = None,
        ws_transport: WsTransport | None = None,
    ) -> None:
        """Compose the store, bus, transports and operation façades."""
        self._config: Final = config
        self._http: Final = http_transport or HttpTransport(config=config)
        self._store: Final = store or LoomStore(transport=self._http)
        # Ensure the store always has the transport reference — important
        # when the caller injects a pre-built store built without one.
        self._store.set_transport(transport=self._http)
        self._bus: Final = bus or EventBus()
        self._ws_transport_external: Final = ws_transport
        self._ws: WsTransport | None = ws_transport
        self._wire_group: SubscriptionGroup | None = None
        self._dispatch_task: asyncio.Task[None] | None = None
        # Background tasks spawned off the dispatch / WS-reader loops so
        # network I/O (per-device reconcile, replay-lost re-bootstrap)
        # never blocks event delivery. Tracked so close() can cancel them.
        self._bg_tasks: set[asyncio.Task[None]] = set()
        self._rebootstrap_task: asyncio.Task[None] | None = None
        # Loop time the last replay-lost re-bootstrap finished, for the
        # cooldown that keeps a replay_lost / overflow burst from re-walking
        # the snapshot back-to-back (see _REBOOTSTRAP_COOLDOWN_SECONDS).
        self._last_rebootstrap_finished: float | None = None
        self._closing = False

        # Operations are stateless façades over the transport.
        self.devices: Final = DevicesOperations(transport=self._http)
        self.datapoints: Final = DataPointsOperations(transport=self._http)
        self.custom_data_points: Final = CustomDataPointsOperations(transport=self._http)
        self.hub: Final = HubOperations(transport=self._http)
        self.system: Final = SystemOperations(transport=self._http)
        self.schedules: Final = SchedulesOperations(transport=self._http)
        self.links: Final = LinksOperations(transport=self._http)
        self.groups: Final = GroupsOperations(transport=self._http)
        self.alarm: Final = AlarmOperations(transport=self._http)
        # Admin / ops surface — present for completeness; HA typically
        # touches only auth (token provisioning) and diagnostics.
        self.auth: Final = AuthOperations(transport=self._http)
        self.users: Final = UsersOperations(transport=self._http)
        self.centrals: Final = CentralsOperations(transport=self._http)
        self.config_admin: Final = ConfigOperations(transport=self._http)
        self.diagnostics: Final = DiagnosticsOperations(transport=self._http)
        self.backup: Final = BackupOperations(transport=self._http)
        self.sessions: Final = SessionsOperations(transport=self._http)
        self.matter: Final = MatterOperations(transport=self._http)
        self.visibility: Final = VisibilityOperations(transport=self._http)

    # ---- public state access ----

    @property
    def store(self) -> LoomStore:
        """Return the in-memory mirror of the daemon's CCU model."""
        return self._store

    @property
    def events(self) -> EventBus:
        """Return the event bus that fans out typed daemon events."""
        return self._bus

    @property
    def config(self) -> LoomConfig:
        """Return the connection configuration for this client."""
        return self._config

    # ---- async context manager ----

    async def __aenter__(self) -> Self:
        """Connect on context entry and return the client."""
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
        /,
    ) -> None:
        """Close the client on context exit."""
        await self.close()

    # ---- lifecycle ----

    async def connect(
        self,
        *,
        required_capabilities: Iterable[str] = (),
    ) -> None:
        """
        Open the HTTP session and run the capability handshake.

        Does NOT start the WS event stream — call :meth:`start_events`
        explicitly so callers can choose between snapshot-only mode
        (REST polling) and full push-mode (WS subscribed).
        """
        await self._http.connect(required_capabilities=required_capabilities)

    async def bootstrap(self, *, fetch_data_points: bool = True) -> None:
        """
        Populate the store from the daemon's current state.

        Steps:

        1. ``GET /snapshot?include=data_points`` → registers every device
           and, in the same response, the nested channels + data points
           (:attr:`Snapshot.device_channels`).
        2. For each device: ``GET /devices/{addr}`` to attach the
           firmware / availability detail the flat snapshot omits (and
           the authoritative channel list).
        3. Optional (``fetch_data_points=True``): attach each channel's
           DPs from the nested snapshot — no extra REST call. If the
           daemon did not return ``device_channels`` (older daemon), fall
           back to one ``GET …/data-points`` per channel.
        4. Attach the alarm-panel catalogue (``GET /alarm/panels`` +
           ``GET /alarm/state``). A 404 means the daemon's alarm
           subsystem is disabled (its routes are unmounted; there is no
           ``/info`` capability token yet) — the section is skipped.
        5. Emit one :class:`DataPointsCreatedEvent` carrying every
           device the store now knows, so HA-side spawn-entities
           subscribers fire once at the end of bootstrap.

        The nested snapshot collapses the formerly dominant cost — one
        ``GET …/data-points`` per channel (N*M REST calls) — into the
        single snapshot round trip. The per-device detail call (step 2)
        stays: ``firmware`` / the rich ``availability`` object are
        detail-only, not carried by the snapshot's device summaries.
        """
        include = "data_points" if fetch_data_points else None
        snapshot = await self.system.get_snapshot(include=include)
        self._store.load_snapshot(snapshot=snapshot)

        # load_snapshot derives the central id from the interface list.
        central_name = self._store.central_id or None

        # Nested snapshot: {device_address: {channel_number: [DataPointSummary]}}.
        # Empty when the daemon ignored ``include`` — bootstrap then falls
        # back to the per-channel data-point fetch (older-daemon path).
        dp_map = _channel_dp_map(device_channels=snapshot.device_channels) if fetch_data_points else {}

        for device_summary in snapshot.devices:
            await self._fetch_device_into_store(
                address=device_summary.address,
                fetch_data_points=fetch_data_points,
                channel_data_points=dp_map.get(device_summary.address),
            )

        await self._bootstrap_alarm_panels()

        # Announce the bootstrap completion as one batch event.
        await self._bus.publish(
            event=new_data_points_created_event(
                devices=list(self._store.devices),
                data_points=[
                    dp for device in self._store.devices for channel in device.channels for dp in channel.data_points
                ],
                central=central_name,
            )
        )

    async def _bootstrap_alarm_panels(self) -> None:
        """
        Populate the store's alarm-panel section (daemon ≥ 0.42.0).

        Feature-detects via the ``alarm.v1`` capability token (daemon
        ≥ 0.43.1 emits it exactly when the ``/alarm`` surface is
        mounted; the types pin makes such a daemon a connect()
        precondition, so an absent token reliably means "alarm off").
        The 404 probe stays as a fallback for injected transports that
        carry no ``/info`` payload — the daemon leaves every ``/alarm``
        route unmounted when the subsystem is disabled, so a
        :class:`LoomNotFoundError` equally means "no alarm". Live
        updates then ride the ``alarm.*`` WS topics bound by the bridge.
        """
        info = self._http.info
        if info is not None and "alarm.v1" not in (info.capabilities or []):
            _LOGGER.debug("daemon does not advertise alarm.v1 — alarm subsystem disabled, skipping panels")
            return
        try:
            panels = await self.alarm.list_panels()
        except LoomNotFoundError:
            _LOGGER.debug("daemon has no /alarm surface — alarm subsystem disabled, skipping panels")
            return
        self._store.attach_alarm_panels(panels=panels)
        if not panels:
            return
        statuses = await self.alarm.get_zone_statuses()
        self._store.attach_alarm_zone_statuses(statuses=statuses)

    async def start_events(
        self,
        *,
        subscriptions: Iterable[str] | None = None,
    ) -> None:
        """
        Open the WS event stream and wire it to the store + bus.

        Idempotent: re-calling is a no-op while the loop is alive.
        """
        if self._dispatch_task is not None and not self._dispatch_task.done():
            return

        if self._ws is None:
            self._ws = WsTransport(
                config=self._config,
                initial_subscriptions=list(subscriptions or _DEFAULT_WS_SUBSCRIPTIONS),
                on_replay_lost=self._on_replay_lost,
            )
            await self._ws.start()
        else:
            # Injected transport: it owns its own initial subscriptions and
            # replay-lost wiring, but an explicit subscriptions list passed
            # here must still take effect (it was silently dropped before).
            await self._ws.start()
            if subscriptions is not None:
                await self._ws.subscribe(topics=list(subscriptions))

        # Bridge: WS events → store apply_*
        self._wire_group = self._bus.create_subscription_group(name="loom-client-wire")
        bind_ws_events_to_store(bus=self._bus, store=self._store, group=self._wire_group)
        # The bridge only seeds a stub for a freshly-paired device; the
        # client owns the follow-up reconcile (fetch detail + DPs, then
        # announce) because it — unlike the store — knows the bus. Subscribed
        # after the bridge so the stub exists before the reconcile spawns.
        self._wire_group.subscribe(event_type=DeviceCreatedEvent, handler=self._on_device_created)

        # Dispatch loop: WsEnvelope → typed event → bus.publish.
        self._dispatch_task = asyncio.create_task(self._dispatch_loop(), name="openccu-loom-dispatch")

    async def close(self) -> None:
        """Tear down WS, HTTP, and all bus subscriptions."""
        self._closing = True
        # Stop the dispatch loop FIRST: it is the only thing that spawns new
        # reconcile tasks (on device.created), so cancelling it before draining
        # _bg_tasks closes the race where a late publish enqueues an orphan task
        # after the set was cleared.
        if self._dispatch_task is not None:
            self._dispatch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._dispatch_task
            self._dispatch_task = None
        # Now cancel in-flight reconcile / re-bootstrap work — with the dispatch
        # loop down, no new background task can appear — before tearing the
        # transports down, so nothing runs against a closed HTTP session.
        background = [*self._bg_tasks, self._rebootstrap_task]
        for task in background:
            if task is not None:
                task.cancel()
        for task in background:
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._bg_tasks.clear()
        self._rebootstrap_task = None
        if self._ws is not None:
            await self._ws.stop()
            if self._ws_transport_external is None:
                self._ws = None
        if self._wire_group is not None:
            self._wire_group.cancel()
            self._wire_group = None
        await self._http.close()

    # ---- internals ----

    def _spawn_background(self, *, coro: Coroutine[object, object, None], name: str) -> None:
        """Run ``coro`` as a tracked background task (auto-removed on completion)."""
        task = asyncio.create_task(coro, name=name)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _fetch_device_into_store(
        self,
        *,
        address: str,
        fetch_data_points: bool = True,
        channel_data_points: Mapping[int, list[DataPointSummary]] | None = None,
    ) -> None:
        """
        Load one device's full graph into the store (detail + per-channel DPs).

        Shared by :meth:`bootstrap` and the live ``device.created`` reconcile.

        When ``channel_data_points`` is supplied (the nested-snapshot path
        in :meth:`bootstrap`), each channel's DPs are read from that map
        instead of a per-channel ``GET …/data-points`` call. When it is
        ``None`` (the ``device.created`` reconcile has no snapshot to draw
        on, or an older daemon returned no nested data), the DPs are
        fetched per channel over REST.
        """
        detail = await self.devices.get_device_detail(address=address)
        self._store.attach_device_detail(detail=detail)
        if not fetch_data_points:
            return
        for channel_summary in detail.channels or ():
            if channel_data_points is not None:
                dps = list(channel_data_points.get(channel_summary.number, ()))
            else:
                dps = await self.devices.list_data_points(address=address, channel=channel_summary.number)
            self._store.attach_channel_data_points(
                device_address=address,
                channel_number=channel_summary.number,
                data_points=dps,
            )

    async def _dispatch_loop(self) -> None:
        """Pump WsEnvelope → typed event → EventBus."""
        assert self._ws is not None  # noqa: S101 — invariant: dispatch loop runs only after start_events()
        try:
            async for envelope in self._ws.events():
                event = event_from_envelope(envelope=envelope)
                await self._bus.publish(event=event)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("WS dispatch loop crashed")

    async def _on_device_created(self, event: DeviceCreatedEvent, /) -> None:
        """
        Reconcile a freshly-paired device so it spawns HA entities live.

        The wire bridge has already seeded a stub; here we fetch the full
        graph and announce it — off the dispatch loop, so loading the new
        device's channels/DPs doesn't stall event delivery.
        """
        if self._closing:
            return
        self._spawn_background(
            coro=self._reconcile_new_device(address=event.payload.device_address),
            name="openccu-loom-reconcile-device",
        )

    async def _reconcile_new_device(self, *, address: str) -> None:
        """Fetch detail + DPs for a new device and publish a DataPointsCreatedEvent."""
        try:
            await self._fetch_device_into_store(address=address)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("reconcile of newly-created device %s failed", address)
            return
        device = self._store.get_device(address=address)
        if device is None:
            return
        await self._bus.publish(
            event=new_data_points_created_event(
                devices=[device],
                data_points=[dp for channel in device.channels for dp in channel.data_points],
                central=self._store.central_id or None,
            )
        )

    async def _on_replay_lost(self, oldest_seq: int, /) -> None:
        """
        Schedule a store re-bootstrap after the daemon's replay buffer aged out.

        Called by the WS transport on the reader task when the requested
        events are no longer replayable — from the daemon's ``replay_lost``
        frame *and* the local envelope-queue overflow, this being their single
        funnel. The re-bootstrap runs as a tracked background task (never
        inline) so the N×M snapshot walk can't block the read loop and trip the
        inbound-ping deadline. De-duplicated two ways: a second trigger while a
        walk is running is dropped, and one arriving within
        ``_REBOOTSTRAP_COOLDOWN_SECONDS`` of the previous walk finishing is
        dropped too — so a burst can't re-walk the snapshot back-to-back.
        """
        if self._closing:
            return
        if self._rebootstrap_task is not None and not self._rebootstrap_task.done():
            # Expected de-dup, not an error: a fresh full-snapshot re-bootstrap
            # is already running and will subsume this loss. Kept at DEBUG so a
            # burst of replay-lost / queue-overflow triggers can't flood the log.
            _LOGGER.debug(
                "WS replay lost (oldest_seq=%s) — re-bootstrap already in progress, skipping",
                oldest_seq,
            )
            return
        if self._last_rebootstrap_finished is not None:
            since = asyncio.get_running_loop().time() - self._last_rebootstrap_finished
            if since < _REBOOTSTRAP_COOLDOWN_SECONDS:
                _LOGGER.debug(
                    "WS replay lost (oldest_seq=%s) — re-bootstrap cooldown (%.1fs of %.1fs), skipping",
                    oldest_seq,
                    since,
                    _REBOOTSTRAP_COOLDOWN_SECONDS,
                )
                return
        _LOGGER.warning("WS replay lost (oldest_seq=%s) — scheduling store re-bootstrap", oldest_seq)
        self._rebootstrap_task = asyncio.create_task(self._run_rebootstrap(), name="openccu-loom-rebootstrap")

    async def _run_rebootstrap(self) -> None:
        """Body of the replay-lost re-bootstrap; logs and swallows failures."""
        try:
            await self.bootstrap()
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("re-bootstrap after replay_lost failed")
        finally:
            # Stamp completion (success, failure, or cancellation) so the
            # cooldown in _on_replay_lost spaces out the next walk. On a
            # cancelled walk (close()) the stamp is harmless — _closing then
            # short-circuits _on_replay_lost before it is ever read.
            self._last_rebootstrap_finished = asyncio.get_running_loop().time()
