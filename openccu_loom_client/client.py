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
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Mapping
import contextlib
import logging
from typing import TYPE_CHECKING, Final, Self

from openccu_loom_types.rest import DeviceDetail

from openccu_loom_client.bridge import bind_ws_events_to_store
from openccu_loom_client.capabilities import Capability
from openccu_loom_client.events import (
    DeviceCreatedEvent,
    DeviceReleasedEvent,
    EventBus,
    SubscriptionGroup,
    event_from_envelope,
    new_auth_failed_event,
    new_connection_state_changed_event,
    new_daemon_latency_changed_event,
    new_data_points_created_event,
)
from openccu_loom_client.exceptions import BaseLoomException, LoomIncompatibleVersionError, LoomNotFoundError
from openccu_loom_client.operations import (
    AlarmOperations,
    BackupOperations,
    CustomDataPointsOperations,
    DataPointsOperations,
    DevicesOperations,
    DiagnosticsOperations,
    HubOperations,
    I18nOperations,
    LinksOperations,
    SchedulesOperations,
    SecurityOperations,
    SystemOperations,
    VisibilityOperations,
)
from openccu_loom_client.store import LoomStore
from openccu_loom_client.transport import HttpTransport, WsTransport

if TYPE_CHECKING:
    from types import TracebackType

    from openccu_loom_types.rest import Channel, DataPointSummary, DeviceChannel, DeviceSummary, Health, Info, Readiness

    from openccu_loom_client.config import LoomConfig

_LOGGER: Final = logging.getLogger(__name__)

# Default WS topic subscriptions for the HA-equivalent feature set:
# datapoint value pushes, custom-DP state pushes, plus device +
# central + system + hub events from every CCU. ``datapoint.*`` and
# ``custom_data_point.*`` are the live-state plane — without them every
# entity freezes on its bootstrap value. ``alarm.*`` carries the alarm
# panel plane (daemon ≥ 0.42.0); a daemon without the alarm subsystem
# simply never publishes on it. ``security.*`` carries the Security &
# Safety plane (daemon ≥ 0.54.0, api 5.1.0) across its three topics —
# state, faults and notifications; before that release the domain had no
# push at all and a consumer had to re-read GET /security on its own
# schedule to learn that a smoke detector had fired. Callers that want
# narrower scope can pass an explicit list to start_events().
_DEFAULT_WS_SUBSCRIPTIONS: Final = (
    "datapoint.*",
    "custom_data_point.*",
    "device.*",
    "central.*",
    "system.*",
    "hub.*",
    "alarm.*",
    "security.*",
)

# Minimum spacing between replay-lost re-bootstraps, measured from the end of
# the previous one. A daemon stuck emitting ``replay_lost`` (or a queue-overflow
# resync fired per-frame) would otherwise schedule a fresh N×M snapshot walk the
# instant each prior walk finished. The just-taken snapshot is authoritative for
# far longer than this window and live events keep flowing meanwhile, so a burst
# collapses to at most one walk per (walk duration + cooldown).
_REBOOTSTRAP_COOLDOWN_SECONDS: Final = 30.0

# How often wait_until_ready() polls. The ceiling it polls against is
# LoomConfig.readiness_wait_seconds, because how long a caller can afford to
# block is the caller's question, not this module's.
_READINESS_POLL_SECONDS: Final = 3.0


# Upper bound on reconcile fan-out. Each reconcile is one GET /devices/{addr}
# plus one GET …/data-points per channel, so an unfiltered batch of device
# lifecycle events used to translate into hundreds of concurrent requests —
# against a daemon that is typically mid-bring-up when they arrive. The filters
# in _on_device_created should keep batches small; this is the backstop for the
# case they do not, and it only paces the work, never drops it.
_MAX_CONCURRENT_RECONCILES: Final = 4

# Creation source that means "restored from the daemon's persisted description
# cache at boot", not "a device arrived". See DeviceCreatedPayload.source in the
# daemon's openapi.yaml (documented from 0.65.3; absent on older daemons).
_SOURCE_CACHE_RESTORE: Final = "CACHE"


RebootstrapHook = Callable[[], Awaitable[None]]
"""Async callback invoked after a store re-bootstrap has refilled the store.

Lets a layer built on top of the store repeat its own bootstrap half — see
:meth:`LoomClient.set_rebootstrap_hook`.
"""


def _is_cache_restore(*, source: str | None) -> bool:
    """Report whether a ``device.created`` payload is a boot cache restore."""
    return source is not None and source.upper() == _SOURCE_CACHE_RESTORE


def _channel_map(
    *,
    device_channels: list[DeviceChannel] | None,
) -> dict[str, list[Channel]]:
    """
    Index a nested snapshot's ``device_channels`` by device address.

    ``Channel`` is a subclass of ``ChannelSummary`` carrying every field the
    detail response's channel list carries, plus the nested data points — so
    a snapshot expanded with ``include=channels`` supplies everything
    :meth:`LoomStore.attach_device_detail` needs, and no per-device
    ``GET /devices/{address}`` is required.

    Empty when the daemon returned no nested data; the caller then falls
    back to the per-device detail call.
    """
    return {entry.device_address: list(entry.channels or ()) for entry in device_channels or ()}


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
        # Paces reconcile fan-out (see _MAX_CONCURRENT_RECONCILES). Created
        # lazily on first use so constructing a client outside a running loop
        # stays possible.
        self._reconcile_slots: asyncio.Semaphore | None = None
        self._connected = False
        self._rebootstrap_hook: RebootstrapHook | None = None
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
        self.alarm: Final = AlarmOperations(transport=self._http)
        # The Security & Safety domain runs with or without the alarm
        # engine, so it is wired unconditionally next to it rather than
        # behind the alarm capability token.
        self.security: Final = SecurityOperations(transport=self._http)
        # The daemon's own entity-name vocabulary. Read once at bootstrap so
        # the compat layer renders the daemon's words instead of keeping a
        # second copy of them (daemon ≥ 0.54.0; older ones answer 404 and
        # every consumer falls back to its own token).
        self.i18n: Final = I18nOperations(transport=self._http)
        # Admin / ops surface — present for completeness; HA typically
        # touches only auth (token provisioning) and diagnostics.
        self.diagnostics: Final = DiagnosticsOperations(transport=self._http)
        self.backup: Final = BackupOperations(transport=self._http)
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

    @property
    def info(self) -> Info | None:
        """
        Return the daemon's ``/info`` payload, or ``None`` before connect.

        The handshake already validates and caches it. Without a public
        accessor a consumer that wants one field from it either re-requests
        ``/info`` or reaches into the transport — both worse than exposing
        what is already held.
        """
        return self._http.info

    def has_capability(self, capability: Capability | str, /) -> bool:
        """
        Report whether the daemon advertises ``capability``.

        Answers ``False`` before connect and for a daemon whose ``/info``
        carries no capability list — both mean "this client has no
        evidence the feature is there", which is the answer a caller
        should act on either way.

        A ``True`` here means the daemon is **configured** for the
        capability, not that the subsystem is healthy right now. Read the
        daemon's ``/health`` components for that.
        """
        info = self._http.info
        if info is None:
            return False
        return str(capability) in (info.capabilities or [])

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

    async def get_health(self) -> Health | None:
        """
        Return the daemon's liveness probe, or ``None`` when it cannot be read.

        Distinct from :meth:`has_capability`, and the daemon's own contract
        insists on the distinction: a capability token means the daemon is
        *configured* for something, not that the subsystem is working at this
        instant. ``/health`` is what reports the latter, with the collapse
        already applied server-side (``healthy`` / ``degraded`` / ``unhealthy``
        / ``unknown``).

        Never raises: a health probe that cannot be reached is itself only weak
        evidence — the answer a caller should act on is "no evidence", which is
        what ``None`` says.
        """
        try:
            return await self.system.get_health()
        except BaseLoomException as err:
            _LOGGER.debug("daemon health probe unavailable: %s", err)
            return None

    async def get_readiness(self) -> Readiness | None:
        """
        Return this central's southbound bring-up readiness, or ``None``.

        The daemon reports it on ``GET /system/ccu`` per central: a ``phase``
        walking ``waiting_for_ccu`` → ``loading_hub`` → ``loading_devices`` →
        ``ready``, and a latched ``ready`` flag. This is the difference between
        "the daemon has not reached the CCU yet" and "the CCU has no devices",
        which is otherwise invisible — ``GET /snapshot`` answers 200 with empty
        lists in both cases and never 5xx.

        Scoped to :attr:`LoomStore.central_id` when one is known, since a daemon
        may mediate several CCUs and another one's readiness says nothing about
        this client's. ``None`` when the endpoint is unreadable (an older
        daemon, or a credential the daemon narrows the CCU coordinates away
        from).
        """
        try:
            entries = await self.system.list_system_ccus()
        except BaseLoomException as err:
            _LOGGER.debug("readiness unavailable (GET /system/ccu): %s", err)
            return None
        if not entries:
            return None
        central = self._store.central_id
        entry = next((e for e in entries if getattr(e, "name", None) == central), None) if central else None
        if entry is None:
            # No central pinned yet (readiness is typically read before the
            # snapshot names one), or the daemon mediates exactly one.
            entry = entries[0] if len(entries) == 1 else None
        if entry is None:
            _LOGGER.debug("readiness: no /system/ccu entry matches central %r", central)
            return None
        return getattr(entry, "readiness", None)

    async def wait_until_ready(self, *, timeout_seconds: float | None = None) -> bool:
        """
        Poll the daemon's readiness until its bring-up has latched, or give up.

        Bootstrapping before the daemon has reached the CCU is the failure this
        exists to prevent, and it is a quiet one: ``GET /snapshot`` answers 200
        with empty lists while the central is still in ``waiting_for_ccu``, so
        the bootstrap "succeeds", the store is empty, and a consumer announces
        no entities at all.

        ``timeout_seconds`` defaults to :attr:`LoomConfig.readiness_wait_seconds`,
        so a caller that cares about its own startup latency sets it once on the
        config rather than at every call site. A value of 0 skips the wait.

        Returns ``True`` when readiness latched (or the daemon does not report
        readiness at all, which is the older-daemon case and must not block
        anyone), ``False`` on timeout. A ``False`` is not fatal: the caller may
        bootstrap anyway and rely on the daemon's resync push when the CCU
        arrives — this only avoids paying for a walk that is known to be empty.
        """
        if timeout_seconds is None:
            timeout_seconds = self._config.readiness_wait_seconds
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        logged = False
        while True:
            readiness = await self.get_readiness()
            if readiness is None:
                return True  # nothing reports readiness here; never block on it
            if readiness.ready:
                return True
            if not logged:
                _LOGGER.info(
                    "daemon has not finished its southbound bring-up (phase=%s, interfaces %s/%s) — waiting",
                    getattr(readiness.phase, "value", readiness.phase),
                    readiness.interfaces_loaded,
                    readiness.interfaces_total,
                )
                logged = True
            if asyncio.get_running_loop().time() >= deadline:
                _LOGGER.warning(
                    "daemon still not ready after %.0fs (phase=%s) — continuing anyway; "
                    "the daemon's resync push will re-bootstrap the store once the CCU arrives",
                    timeout_seconds,
                    getattr(readiness.phase, "value", readiness.phase),
                )
                return False
            await asyncio.sleep(_READINESS_POLL_SECONDS)

    async def bootstrap(self, *, fetch_data_points: bool = True) -> None:
        """
        Populate the store from the daemon's current state.

        Steps:

        1. ``GET /snapshot?include=data_points`` → registers every device
           and, in the same response, the nested channels + data points
           (:attr:`Snapshot.device_channels`).
        2. For each device: attach the channel list from that same
           response. Since daemon api 7.23.0 the summary also carries
           ``firmware`` and ``availability``, so nothing is left that
           would need a per-device ``GET /devices/{addr}``.
        3. Optional (``fetch_data_points=True``): attach each channel's
           DPs from the nested snapshot — no extra REST call. If the
           daemon did not return ``device_channels``, fall back to the
           per-device detail call and one ``GET …/data-points`` per
           channel.
        4. Attach the alarm-panel catalogue (``GET /alarm/panels`` +
           ``GET /alarm/state``). A 404 means the daemon's alarm
           subsystem is disabled (its routes are unmounted; there is no
           ``/info`` capability token yet) — the section is skipped.
        5. Emit one :class:`DataPointsCreatedEvent` carrying every
           device the store now knows, so HA-side spawn-entities
           subscribers fire once at the end of bootstrap.

        A bootstrap is therefore one request. The M — one
        ``GET …/data-points`` per channel — went with the nested snapshot;
        the N — one detail call per device — goes here, because the fields
        that forced it are on the summary now. The fallback path is kept
        rather than deleted: it costs one branch and it is what answers a
        daemon that ignores ``include``.
        """
        include = "channels,data_points" if fetch_data_points else "channels"
        snapshot = await self.system.get_snapshot(include=include, released_only=self._config.released_only)
        self._store.load_snapshot(snapshot=snapshot)

        # load_snapshot derives the central id from the interface list.
        central_name = self._store.central_id or None

        # Nested snapshot: {device_address: {channel_number: [DataPointSummary]}}.
        # Empty when the daemon ignored ``include`` — bootstrap then falls
        # back to the per-channel data-point fetch (older-daemon path).
        dp_map = _channel_dp_map(device_channels=snapshot.device_channels) if fetch_data_points else {}
        channel_map = _channel_map(device_channels=snapshot.device_channels)

        for device_summary in snapshot.devices:
            if (channels := channel_map.get(device_summary.address)) is not None:
                self._attach_device_from_snapshot(
                    summary=device_summary,
                    channels=channels,
                    channel_data_points=dp_map.get(device_summary.address) if fetch_data_points else None,
                )
                continue
            # The daemon returned no nested channels for this device — read it
            # the long way round.
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
        if info is not None and not self.has_capability(Capability.ALARM):
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
        await self.refresh_triggered_motion()

    async def refresh_triggered_motion(self) -> None:
        """
        Re-read the latched motion detectors and update the panel counts.

        The daemon broadcasts no latch event, so the counts cannot ride
        the ``alarm.*`` pushes like the rest of the panel state — they
        are refreshed by re-reading ``GET /alarm/triggered-motion``.
        Callers pick the cadence; the compat adapter schedules this off
        the alarm events that plausibly move a latch.

        Never raises: a daemon below 0.58.0 has no such route, and a
        failed refresh must not take down whatever event handling
        triggered it. The counts then simply keep their previous value.
        """
        try:
            sensors = await self.alarm.list_triggered_motion()
        except BaseLoomException as err:
            _LOGGER.debug("triggered-motion refresh failed (counts keep their previous value): %s", err)
            return
        self._store.apply_triggered_motion(sensors=sensors)

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
                on_auth_failed=self._on_auth_failed,
                on_connection_state=self._on_connection_state,
                on_heartbeat=self._on_heartbeat,
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
        #
        # Cancel any group a previous start_events() left behind first. The
        # idempotence guard above only holds while the dispatch task is alive,
        # and the one path that ends it without close() — the WS transport
        # giving up on a rejected credential — is exactly the one where a
        # caller retries start_events() as its recovery. Re-assigning the
        # attribute without cancelling leaves the old subscriptions live on the
        # bus, so every event would be applied to the store twice.
        if self._wire_group is not None:
            self._wire_group.cancel()
        self._wire_group = self._bus.create_subscription_group(name="loom-client-wire")
        bind_ws_events_to_store(bus=self._bus, store=self._store, group=self._wire_group)
        # The bridge only seeds a stub for a freshly-paired device; the
        # client owns the follow-up reconcile (fetch detail + DPs, then
        # announce) because it — unlike the store — knows the bus. Subscribed
        # after the bridge so the stub exists before the reconcile spawns.
        self._wire_group.subscribe(event_type=DeviceCreatedEvent, handler=self._on_device_created)
        # Onboarding release (daemon ≥ 0.66.1). With released_only on, the
        # device.created frame for a withheld device never arrives — this is
        # the frame that says it became adoptable, and the daemon never
        # withholds it. Same reconcile as a fresh pairing: the device is
        # complete on the daemon side by now, we simply have not loaded it.
        self._wire_group.subscribe(event_type=DeviceReleasedEvent, handler=self._on_device_released)

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

    def _attach_device_from_snapshot(
        self,
        *,
        summary: DeviceSummary,
        channels: list[Channel],
        channel_data_points: Mapping[int, list[DataPointSummary]] | None,
    ) -> None:
        """
        Attach one device's graph from the snapshot alone — no REST call.

        ``DeviceDetail`` extends ``DeviceSummary`` by exactly one field,
        ``channels``, and a snapshot expanded with ``include=channels``
        carries those. So the detail response can be assembled here rather
        than fetched, which is what turns an N+1-request bootstrap into a
        single request.
        """
        detail = DeviceDetail(**summary.model_dump(), channels=channels)
        self._store.attach_device_detail(detail=detail)
        if channel_data_points is None:
            return
        for channel in channels:
            self._store.attach_channel_data_points(
                device_address=summary.address,
                channel_number=channel.number,
                data_points=list(channel_data_points.get(channel.number, ())),
            )

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

        Two filters keep a batch of these from becoming a REST storm against a
        daemon that is usually busy when they arrive:

        - ``source == CACHE`` is skipped outright. That is the daemon restoring
          devices from its persisted description cache at boot — a whole fleet
          at once, all of it already covered by the snapshot walk a consumer
          runs anyway. The other sources are single-device events (``NEW`` a
          pairing, ``REFRESH`` a factory-reset re-pair, ``MANUAL`` an operator
          accept) and are worth a round trip. Daemon ≥ 0.65.3 documents the
          vocabulary; an older one sends no ``source`` at all, in which case
          nothing is skipped and behaviour is unchanged.
        - a device the store already holds complete is skipped. Nothing in the
          payload adds to what a finished graph already has.

        Whatever survives both is still bounded by
        :data:`_MAX_CONCURRENT_RECONCILES`, so even an unforeseen burst walks
        the daemon a few devices at a time instead of all at once.
        """
        if self._closing:
            return
        if _is_cache_restore(source=event.payload.source):
            _LOGGER.debug(
                "device.created for %s carries source=CACHE — boot restore, not a new device; skipping reconcile",
                event.payload.device_address,
            )
            return
        if self._store_holds_complete_device(address=event.payload.device_address):
            _LOGGER.debug(
                "device.created for %s is already complete in the store — skipping reconcile",
                event.payload.device_address,
            )
            return
        self._spawn_background(
            coro=self._reconcile_new_device(address=event.payload.device_address),
            name="openccu-loom-reconcile-device",
        )

    async def _on_device_released(self, event: DeviceReleasedEvent, /) -> None:
        """
        Adopt a device whose onboarding the operator just finished.

        Reached only in the ``released_only`` mode this client defaults to;
        without the filter the device was adopted at ``device.created`` and is
        already complete, which the guard below detects either way.

        Unlike ``device.created`` there is no store stub to build on — the wire
        bridge never saw the device — so the reconcile fetches the whole graph,
        which is what it does for a new pairing anyway.
        """
        if self._closing:
            return
        address = event.payload.device_address
        if self._store_holds_complete_device(address=address):
            _LOGGER.debug("device.released for %s, already complete in the store — nothing to adopt", address)
            return
        _LOGGER.info("device %s finished onboarding — adopting it", address)
        self._spawn_background(
            coro=self._reconcile_new_device(address=address),
            name="openccu-loom-adopt-released-device",
        )

    def _store_holds_complete_device(self, *, address: str) -> bool:
        """
        Report whether the store already has this device with channels and data points.

        "Complete" is deliberately strict: a device whose channels are all empty
        is a stub the wire bridge just seeded, and that is exactly the case a
        reconcile exists for. A device with at least one populated channel came
        from a snapshot walk or an earlier reconcile and needs no second one.
        """
        device = self._store.get_device(address=address)
        if device is None:
            return False
        channels = list(device.channels)
        return bool(channels) and any(channel.data_points for channel in channels)

    async def _reconcile_new_device(self, *, address: str) -> None:
        """Fetch detail + DPs for a new device and publish a DataPointsCreatedEvent."""
        if self._reconcile_slots is None:
            self._reconcile_slots = asyncio.Semaphore(_MAX_CONCURRENT_RECONCILES)
        async with self._reconcile_slots:
            await self._reconcile_new_device_now(address=address)

    async def _reconcile_new_device_now(self, *, address: str) -> None:
        """Body of :meth:`_reconcile_new_device`, run while holding a slot."""
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

    @property
    def connected(self) -> bool:
        """
        Report whether the event stream is currently connected.

        ``False`` before :meth:`start_events`, while the transport is between
        reconnect attempts, and after a credential was rejected. A consumer
        rendering daemon-sourced state should treat it as the freshness flag it
        is: the store keeps its last values across a drop, and nothing else
        says they stopped being updated.
        """
        return self._connected

    @property
    def daemon_latency_ms(self) -> float | None:
        """
        Latest client↔daemon round trip in milliseconds, or ``None``.

        Measured by the daemon across the heartbeat it already runs, so reading
        it costs nothing. ``None`` before the stream has completed a second
        heartbeat, and against a daemon that does not time its pings.
        """
        return self._ws.last_rtt_ms if self._ws is not None else None

    async def _on_heartbeat(self, latency_ms: float, /) -> None:
        """Publish the round trip the daemon just reported."""
        await self._bus.publish(event=new_daemon_latency_changed_event(latency_ms=latency_ms))

    async def _on_connection_state(self, connected: bool, /) -> None:
        """
        Publish a WS connect / disconnect transition, and re-check the contract.

        The transport de-duplicates, so this only ever sees real transitions.

        On a reconnect the daemon on the other end may not be the process this
        client handshook with — an outage long enough to notice is long enough
        for it to have been upgraded. The handshake was a connect-time one-shot,
        so that went unnoticed until some later call met a reshaped payload.
        Re-checking here keeps the failure at its cause. It runs as a background
        task: this callback is invoked from the transport's reader loop, and a
        REST round trip inline there would sit inside the inbound-ping deadline.
        """
        self._connected = connected
        if connected and not self._closing:
            self._spawn_background(coro=self._recheck_contract(), name="openccu-loom-recheck-contract")
        await self._bus.publish(event=new_connection_state_changed_event(connected=connected))

    async def _recheck_contract(self) -> None:
        """Re-run the ``/info`` handshake; log loudly when the peer became incompatible."""
        try:
            await self._http.recheck_contract()
        except asyncio.CancelledError:
            raise
        except LoomIncompatibleVersionError:
            # Deliberately not fatal here. Tearing the client down from a
            # background task would strand the consumer with no way to report
            # why; the exception is raised again by the next REST call the
            # consumer makes, where it can be handled in context.
            _LOGGER.error(
                "the daemon reachable after this reconnect is no longer contract-compatible with this build; "
                "REST calls will fail until the daemon or openccu-loom-types is updated",
            )
        except Exception:
            _LOGGER.debug("contract re-check after reconnect failed", exc_info=True)

    async def _on_auth_failed(self) -> None:
        """
        Publish the credential rejection that just ended the event stream.

        The transport stops its reconnect loop on a 401/403 — correctly, since
        retrying a rejected credential can only hammer the daemon with one that
        cannot start working again. That left the stream dead and silent: the
        dispatch loop ran out, and no consumer was told. Publishing it makes the
        one condition a consumer must act on (re-provision, then restart the
        stream) visible.
        """
        _LOGGER.error(
            "the daemon rejected this client's credential — the event stream has stopped; "
            "provide a fresh credential and call start_events() again",
        )
        await self._bus.publish(event=new_auth_failed_event(reason="credential_rejected"))

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

    def set_rebootstrap_hook(self, hook: RebootstrapHook | None, /) -> None:
        """
        Install a callback invoked after every store re-bootstrap.

        The re-bootstrap refills the store, and for a plain store consumer that
        is the whole job. A layer that built anything *on top* of the store at
        bootstrap time — the compat layer's custom data points, schedules,
        combined data points, hub catalogue, and the entity announcement HA
        needs to spawn anything at all — has to repeat its half, or the store
        silently gains devices that never become entities.

        That gap is worst in exactly the case the re-bootstrap exists for: a
        client that started while the daemon had not reached the CCU sees an
        empty snapshot, and when the CCU arrives the daemon's resync push
        refills the store correctly while the consumer above it learns nothing
        until it is reloaded.

        One hook, replacing any previous one; pass ``None`` to clear. Failures
        are logged and swallowed, like the walk itself.
        """
        self._rebootstrap_hook = hook

    async def _run_rebootstrap(self) -> None:
        """Body of the replay-lost re-bootstrap; logs and swallows failures."""
        try:
            await self.bootstrap()
            if (hook := self._rebootstrap_hook) is not None:
                await hook()
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
