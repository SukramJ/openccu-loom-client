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
from collections.abc import Iterable
import contextlib
import logging
from typing import TYPE_CHECKING, Final, Self

from openccu_loom_client.bridge import bind_ws_events_to_store
from openccu_loom_client.events import EventBus, SubscriptionGroup, event_from_envelope, new_data_points_created_event
from openccu_loom_client.operations import (
    AuthOperations,
    BackupOperations,
    CentralsOperations,
    ConfigOperations,
    CustomDataPointsOperations,
    DataPointsOperations,
    DevicesOperations,
    DiagnosticsOperations,
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

    from openccu_loom_client.config import LoomConfig

_LOGGER: Final = logging.getLogger(__name__)

# Default WS topic subscriptions for the HA-equivalent feature set:
# datapoint value pushes, custom-DP state pushes, plus device +
# central + system + hub events from every CCU. ``datapoint.*`` and
# ``custom_data_point.*`` are the live-state plane — without them every
# entity freezes on its bootstrap value. Callers that want narrower
# scope can pass an explicit list to start_events().
_DEFAULT_WS_SUBSCRIPTIONS: Final = (
    "datapoint.*",
    "custom_data_point.*",
    "device.*",
    "central.*",
    "system.*",
    "hub.*",
)


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
        self._closing = False

        # Operations are stateless façades over the transport.
        self.devices: Final = DevicesOperations(transport=self._http)
        self.datapoints: Final = DataPointsOperations(transport=self._http)
        self.custom_data_points: Final = CustomDataPointsOperations(transport=self._http)
        self.hub: Final = HubOperations(transport=self._http)
        self.system: Final = SystemOperations(transport=self._http)
        self.schedules: Final = SchedulesOperations(transport=self._http)
        self.links: Final = LinksOperations(transport=self._http)
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

        1. ``GET /snapshot`` → registers every device (summary only).
        2. For each device: ``GET /devices/{addr}`` to attach channels.
        3. Optional (``fetch_data_points=True``): for each channel,
           ``GET …/data-points`` to attach the DPs.
        4. Emit one :class:`DataPointsCreatedEvent` carrying every
           device the store now knows, so HA-side spawn-entities
           subscribers fire once at the end of bootstrap.

        For large CCUs step 3 is the dominant cost (N*M REST calls).
        The daemon's deferred streaming-snapshot ask (asks.md H1)
        will eventually fold all three steps into one streamed
        response — this implementation matches the unstreamed
        contract that's available today.
        """
        snapshot = await self.system.get_snapshot()
        self._store.load_snapshot(snapshot=snapshot)

        # load_snapshot derives the central id from the interface list.
        central_name = self._store.central_id or None

        for device_summary in snapshot.devices:
            detail = await self.devices.get_device_detail(address=device_summary.address)
            self._store.attach_device_detail(detail=detail)

            if not fetch_data_points:
                continue
            for channel_summary in detail.channels or ():
                dps = await self.devices.list_data_points(
                    address=device_summary.address,
                    channel=channel_summary.number,
                )
                self._store.attach_channel_data_points(
                    device_address=device_summary.address,
                    channel_number=channel_summary.number,
                    data_points=dps,
                )

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

        # Bridge: WS events → store apply_*
        self._wire_group = self._bus.create_subscription_group(name="loom-client-wire")
        bind_ws_events_to_store(bus=self._bus, store=self._store, group=self._wire_group)

        # Dispatch loop: WsEnvelope → typed event → bus.publish.
        self._dispatch_task = asyncio.create_task(self._dispatch_loop(), name="openccu-loom-dispatch")

    async def close(self) -> None:
        """Tear down WS, HTTP, and all bus subscriptions."""
        self._closing = True
        if self._dispatch_task is not None:
            self._dispatch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._dispatch_task
            self._dispatch_task = None
        if self._ws is not None:
            await self._ws.stop()
            if self._ws_transport_external is None:
                self._ws = None
        if self._wire_group is not None:
            self._wire_group.cancel()
            self._wire_group = None
        await self._http.close()

    # ---- internals ----

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

    async def _on_replay_lost(self, oldest_seq: int, /) -> None:
        """
        Re-bootstrap the store after the daemon's replay buffer aged out.

        Called by the WS transport when the requested events are no longer
        replayable; triggers a fresh bootstrap so the store re-syncs against
        ``/snapshot``. Background-runs on the WS reader task so it doesn't
        block the next frame.
        """
        _LOGGER.warning(
            "WS replay lost (oldest_seq=%s) — re-bootstrapping store",
            oldest_seq,
        )
        try:
            await self.bootstrap()
        except Exception:
            _LOGGER.exception("re-bootstrap after replay_lost failed")
