# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
In-memory mirror of the daemon's device / channel / data-point model.

The store is the single source of truth for the client's view of the
CCU state. It's populated by one of two paths:

1. *Bootstrap* — :meth:`load_snapshot` consumes a ``GET /snapshot``
   response and registers every device (summary only). Channels and
   data-points attach later via :meth:`attach_device_detail` /
   :meth:`attach_channel_data_points`, typically issued per device by
   the bootstrap workflow.
2. *Live updates* — :meth:`apply_value_changed`,
   :meth:`apply_device_created`, :meth:`apply_device_removed` feed
   wire-side events (received by the WS transport, dispatched by the
   event bus) back into the in-memory graph.

Write-back uses the same transport reference:
:meth:`set_value` translates one :class:`DataPoint.send_value` call
into a daemon ``PUT …/data-points/{param}/value`` request.

The store does **not** depend on the event bus. A higher-level
component (the future ``LoomClient``) wires WS events → store
+ bus together; the store itself stays directly unit-testable.
"""

from __future__ import annotations

from collections.abc import Callable
import logging
from typing import TYPE_CHECKING, Any, Final

from aiohomematic_contract import serial_suffix as contract_serial_suffix
from openccu_loom_types.rest import (
    ChannelSummary,
    CustomDPSummary,
    DataPointSummary,
    DeviceDetail,
    DeviceSummary,
    ProgramSummary,
    Snapshot,
    SysvarSummary,
)

from openccu_loom_client.model import Channel, CustomDataPoint, DataPoint, Device, Program, Sysvar

if TYPE_CHECKING:
    from collections.abc import Iterable

    from openccu_loom_types.ws import (
        CustomDataPointStateChangedPayload,
        DataPointValueChangedPayload,
        DeviceCreatedPayload,
        DeviceRemovedPayload,
        ProgramExecutedPayload,
        SysvarChangedPayload,
    )

    from openccu_loom_client.transport.http import HttpTransport

_LOGGER: Final = logging.getLogger(__name__)


class LoomStore:
    """Process-local mirror of one daemon's CCU model."""

    def __init__(self, *, transport: HttpTransport | None = None) -> None:
        """Initialise an empty store, optionally bound to a transport."""
        self._transport = transport
        # The daemon central *name* (``snapshot.interfaces[].central_id``,
        # == ``payload.central``). Used to scope/annotate events, NOT as a
        # routing-key prefix.
        self._central_id: str = ""
        # The HA-facing central *name* (the integration's instance name, ==
        # the LoomCentralAdapter ``name``). HA links every device to this
        # central via ``Device.central_info.name``, so it must match the
        # adapter name — which may differ from the daemon ``central_id``.
        self._central_name: str = ""
        # The CCU serial suffix (last 10 chars, lower-cased). This is the
        # central-id slot of every canonical HA routing key for hub /
        # internal / virtual-remote addresses (see
        # ``aiohomematic_contract.canonical_unique_id``); the categorised
        # data-point layer reads it back off the store to build
        # ``unique_id``s bit-identical to the daemon's.
        self._serial_suffix: str = ""
        self._devices: dict[str, Device] = {}
        self._channels: dict[tuple[str, int], Channel] = {}
        self._data_points: dict[tuple[str, int, str], DataPoint] = {}
        self._cdps: dict[tuple[str, str], CustomDataPoint] = {}
        self._programs: dict[str, Program] = {}
        self._sysvars: dict[str, Sysvar] = {}
        # Optional hook: a callable that builds a DataPoint (or a
        # categorised subclass of it) from the same arguments
        # DataPoint takes. The aiohomematic-compat layer injects a
        # factory that returns the right ``Dp*`` subclass so HA-side
        # ``isinstance`` dispatch works while the store still owns the
        # single, live-updated instance per data point. ``None`` =
        # build a plain :class:`DataPoint`.
        self._data_point_factory: Callable[..., DataPoint] | None = None
        # Same hook for Custom Data Points — the compat layer injects a
        # factory that returns the right categorised ``CustomDp*`` class.
        self._cdp_factory: Callable[..., CustomDataPoint] | None = None

    # ---- central identity ----

    @property
    def central_id(self) -> str:
        """The daemon central *name* (for event scoping, not key prefixing)."""
        return self._central_id

    def set_central_id(self, central_id: str | None) -> None:
        """Record the daemon central name (from the bootstrap snapshot)."""
        self._central_id = central_id or ""

    @property
    def central_name(self) -> str:
        """The HA-facing central name (adapter name), falling back to the daemon id."""
        return self._central_name or self._central_id

    def set_central_name(self, central_name: str | None) -> None:
        """Record the HA-facing central name (the integration's instance name)."""
        self._central_name = central_name or ""

    @property
    def serial_suffix(self) -> str:
        """CCU serial suffix — the central-id slot of canonical HA keys."""
        return self._serial_suffix

    def set_serial(self, serial: str | None) -> None:
        """
        Record the CCU serial; stored as its canonical suffix.

        The serial comes from ``GET /system/ccu`` (``SystemCCUEntry.serial``)
        or is injected by the integration (HA's ``entry.unique_id``).
        """
        self._serial_suffix = contract_serial_suffix(serial) if serial else ""

    # ---- transport wiring ----

    def set_transport(self, transport: HttpTransport) -> None:
        """
        Attach a transport to the store.

        Used when the store is built before the client opens its session
        (e.g. integration tests).
        """
        self._transport = transport

    def set_data_point_factory(self, factory: Callable[..., DataPoint] | None) -> None:
        """
        Install a factory that builds (subclasses of) :class:`DataPoint`.

        Must be set before :meth:`attach_channel_data_points` runs (i.e.
        before bootstrap). The aiohomematic-compat layer uses this to
        have the store hold categorised ``Dp*`` instances so HA-side
        ``isinstance`` dispatch works on the very objects the store
        keeps live.
        """
        self._data_point_factory = factory

    def _build_data_point(
        self, *, summary: DataPointSummary, device_address: str, channel_number: int
    ) -> DataPoint:
        if self._data_point_factory is not None:
            return self._data_point_factory(
                summary=summary,
                device_address=device_address,
                channel_number=channel_number,
                store=self,
            )
        return DataPoint(
            summary=summary,
            device_address=device_address,
            channel_number=channel_number,
            store=self,
        )

    def set_custom_data_point_factory(self, factory: Callable[..., CustomDataPoint] | None) -> None:
        """Install a factory that builds (subclasses of) :class:`CustomDataPoint`."""
        self._cdp_factory = factory

    def _build_custom_data_point(
        self,
        *,
        summary: CustomDPSummary,
        device_address: str,
        initial_state: dict[str, Any] | None = None,
    ) -> CustomDataPoint:
        if self._cdp_factory is not None:
            return self._cdp_factory(
                summary=summary,
                device_address=device_address,
                store=self,
                initial_state=initial_state,
            )
        return CustomDataPoint(
            summary=summary,
            device_address=device_address,
            store=self,
            initial_state=initial_state,
        )

    async def refresh_custom_data_point(self, *, address: str, name: str) -> None:
        """
        Re-read one CDP's detail from the daemon and apply its state.

        Backs the compat ``load_data_point_value`` for custom entities.
        No-op without a transport or if the CDP is unknown.
        """
        if self._transport is None:
            return
        payload = await self._transport.request("GET", f"/devices/{address}/cdps/{name}")
        cdp = self._cdps.get((address, name))
        if cdp is not None and isinstance(payload, dict):
            state = payload.get("state")
            if isinstance(state, dict):
                cdp._replace_state(state)

    async def refresh_data_point(self, *, address: str, channel: int, parameter: str) -> None:
        """
        Re-read one data-point's value from the daemon and apply it.

        Backs the compat ``load_data_point_value`` call HA makes when an
        entity is added or manually refreshed. No-op if no transport is
        bound. Unknown data points are ignored (same as a missed push).
        """
        if self._transport is None:
            return
        payload = await self._transport.request(
            "GET", f"/devices/{address}/channels/{channel}/data-points/{parameter}"
        )
        summary = DataPointSummary.model_validate(payload)
        dp = self._data_points.get((address, channel, parameter))
        if dp is not None:
            dp._replace_summary(summary)

    # ---- read access ----

    @property
    def devices(self) -> Iterable[Device]:
        """All currently-known devices, in insertion order."""
        return self._devices.values()

    def get_device(self, *, address: str) -> Device | None:
        """Return the device for the given address, or ``None``."""
        return self._devices.get(address)

    def get_channel(self, *, address: str, number: int) -> Channel | None:
        """Return the channel for the given address and number, or ``None``."""
        return self._channels.get((address, number))

    def get_data_point(self, *, address: str, channel: int, parameter: str) -> DataPoint | None:
        """Return the data point for the given address, channel and parameter."""
        return self._data_points.get((address, channel, parameter))

    def channels_of(self, *, address: str) -> list[Channel]:
        """All channels of one device, sorted by channel number."""
        return sorted(
            (c for k, c in self._channels.items() if k[0] == address),
            key=lambda c: c.number,
        )

    @property
    def data_points(self) -> Iterable[DataPoint]:
        """Every data point currently known, across all devices/channels."""
        return self._data_points.values()

    def data_points_of(self, *, address: str, channel: int) -> list[DataPoint]:
        """All data-points of one (device, channel) pair, sorted by parameter."""
        return sorted(
            (dp for k, dp in self._data_points.items() if k[0] == address and k[1] == channel),
            key=lambda dp: dp.parameter,
        )

    # ---- custom data points (CDPs) ----

    def get_custom_data_point(self, *, address: str, name: str) -> CustomDataPoint | None:
        """Return the custom data point for the given address and name."""
        return self._cdps.get((address, name))

    @property
    def custom_data_points(self) -> Iterable[CustomDataPoint]:
        """Every custom data point currently known, across all devices."""
        return self._cdps.values()

    def custom_data_points_of(self, *, address: str) -> list[CustomDataPoint]:
        """All CDPs registered for a device, sorted by name."""
        return sorted(
            (cdp for k, cdp in self._cdps.items() if k[0] == address),
            key=lambda c: c.name,
        )

    # ---- programs ----

    @property
    def programs(self) -> Iterable[Program]:
        """Every program currently known."""
        return self._programs.values()

    def get_program(self, *, program_id: str) -> Program | None:
        """Return the program for the given id, or ``None``."""
        return self._programs.get(program_id)

    # ---- sysvars ----

    @property
    def sysvars(self) -> Iterable[Sysvar]:
        """Every system variable currently known."""
        return self._sysvars.values()

    def get_sysvar(self, *, name: str) -> Sysvar | None:
        """Return the system variable for the given name, or ``None``."""
        return self._sysvars.get(name)

    # ---- CDP attach ----

    def attach_custom_data_points(
        self,
        *,
        device_address: str,
        cdps: list[CustomDPSummary],
    ) -> None:
        """
        Replace the CDP catalogue for one device.

        Used after :meth:`CustomDataPointsOperations.list_for_device`
        during bootstrap. Subsequent state changes attach via
        :meth:`apply_custom_data_point_state_changed`.
        """
        stale = [k for k in self._cdps if k[0] == device_address]
        for k in stale:
            del self._cdps[k]
        for summary in cdps:
            key = (device_address, summary.name)
            self._cdps[key] = self._build_custom_data_point(
                summary=summary,
                device_address=device_address,
            )

    # ---- bulk load (bootstrap) ----

    def load_snapshot(self, snapshot: Snapshot) -> None:
        """
        Populate the device-level graph from a fresh ``/snapshot``.

        Channels and data-points are not part of the snapshot
        envelope (see DeviceSummary in openapi.yaml) — fetch them
        per device via :meth:`attach_device_detail` and
        :meth:`attach_channel_data_points`. Programs and sysvars
        come along in the same response so they load here in one
        pass.

        The ``central_id`` is taken from the snapshot's interface list
        (it is a component of every hub / internal / virtual-remote
        routing key), unless one was already pinned via
        :meth:`set_central_id`.
        """
        if not self._central_id:
            self.set_central_id(self._infer_central_id(snapshot))
        for summary in snapshot.devices:
            self._upsert_device_summary(summary)
        for prog in snapshot.programs or ():
            self._upsert_program(prog)
        for sysvar in snapshot.sysvars or ():
            self._upsert_sysvar(sysvar)

    @staticmethod
    def _infer_central_id(snapshot: Snapshot) -> str | None:
        """Derive the central id from the first interface that carries one."""
        for iface in snapshot.interfaces or ():
            if iface.central_id:
                return iface.central_id
        return None

    def attach_device_detail(self, detail: DeviceDetail) -> None:
        """
        Apply a full ``GET /devices/{addr}`` detail record.

        Idempotent: re-applying overwrites firmware / availability and
        re-registers every channel in the detail. Channels that are no
        longer present in the new detail are removed; their
        data-points go with them.
        """
        # DeviceDetail extends DeviceSummary, so we can pass it through
        # the upsert path that handles the common case.
        device = self._upsert_device_summary(detail)
        device._attach_detail(
            firmware=detail.firmware,
            availability=detail.availability,
        )

        new_numbers: set[int] = set()
        for channel_summary in detail.channels or ():
            self._upsert_channel(channel_summary)
            new_numbers.add(channel_summary.number)

        # Garbage-collect channels (and their DPs) that vanished.
        stale_keys = [
            k for k in self._channels if k[0] == detail.address and k[1] not in new_numbers
        ]
        for stale in stale_keys:
            self._drop_channel(stale)

    def attach_channel_data_points(
        self,
        *,
        device_address: str,
        channel_number: int,
        data_points: list[DataPointSummary],
    ) -> None:
        """
        Register the data-points of one channel.

        Replaces any previously-registered DPs for the same channel —
        the daemon's catalogue is authoritative.
        """
        # Drop the prior DPs for this channel (we replace wholesale).
        stale = [k for k in self._data_points if k[0] == device_address and k[1] == channel_number]
        for s in stale:
            del self._data_points[s]

        for dp_summary in data_points:
            key = (device_address, channel_number, dp_summary.parameter)
            self._data_points[key] = self._build_data_point(
                summary=dp_summary,
                device_address=device_address,
                channel_number=channel_number,
            )

    # ---- live updates ----

    def apply_value_changed(self, payload: DataPointValueChangedPayload) -> None:
        """
        Update one data-point's value from a ``datapoint.value_changed`` push.

        Missing data-points are logged but not auto-created — the
        bootstrap workflow is responsible for catalogue parity. A
        spurious event for an unknown DP is almost always a sign that
        the catalogue is stale; logging it surfaces that without
        silently inventing entries that the daemon doesn't believe in.
        """
        key = (payload.device_address, payload.channel, payload.parameter)
        dp = self._data_points.get(key)
        if dp is None:
            _LOGGER.debug(
                "value_changed for unknown data-point %s — ignoring "
                "(catalogue out of sync; resync via /devices/%s)",
                ".".join(map(str, key)),
                payload.device_address,
            )
            return
        new_summary = dp.summary.model_copy(
            update={
                "value": payload.value,
                "observed": True,
                "modified_at": payload.modified_at,
                "last_changed_at": payload.modified_at,
                "last_seen_at": payload.modified_at,
            }
        )
        dp._replace_summary(new_summary)
        # A fresh daemon value supersedes any optimistic value HA wrote
        # (the compat data-point layer overlays ``_value_override``).
        if hasattr(dp, "_value_override"):
            del dp._value_override

    def apply_device_created(self, payload: DeviceCreatedPayload) -> None:
        """
        Register a freshly-paired device as a stub entry.

        The push payload carries only address / model / interface_id
        — channels and data-points still need to be fetched via
        ``GET /devices/{addr}``. We seed a minimal :class:`Device` so
        callers can immediately reference it; the next
        ``attach_device_detail`` call completes the graph.
        """
        if payload.device_address in self._devices:
            return
        stub = DeviceSummary(
            address=payload.device_address,
            interface=payload.interface_id or "",
            interface_id=payload.interface_id,
            model=payload.model,
            name=payload.device_address,
            available=True,
            channels_count=0,
        )
        self._devices[payload.device_address] = Device(summary=stub, store=self)

    def apply_device_removed(self, payload: DeviceRemovedPayload) -> None:
        """Drop a device and everything that hung off it."""
        addr = payload.device_address
        self._devices.pop(addr, None)
        stale_channels = [k for k in self._channels if k[0] == addr]
        for k in stale_channels:
            self._drop_channel(k)
        # CDPs are device-scoped, not channel-scoped — drop them too.
        stale_cdps = [cdp_key for cdp_key in self._cdps if cdp_key[0] == addr]
        for cdp_key in stale_cdps:
            del self._cdps[cdp_key]

    def apply_sysvar_changed(self, payload: SysvarChangedPayload) -> None:
        """
        Replace one sysvar's value from a ``hub.sysvar_changed`` push.

        Unknown sysvars are logged + ignored (same rationale as
        :meth:`apply_value_changed`).
        """
        sysvar = self._sysvars.get(payload.name)
        if sysvar is None:
            _LOGGER.debug("sysvar_changed for unknown sysvar %r — ignoring", payload.name)
            return
        new_summary = sysvar.summary.model_copy(update={"value": payload.value, "observed": True})
        sysvar._replace_summary(new_summary)

    def apply_program_executed(self, payload: ProgramExecutedPayload) -> None:
        """
        Acknowledge a program execution event.

        The catalogue itself doesn't change, but a subscriber may want to
        react to the event itself (logged / used by HA-side automations).
        """
        _LOGGER.debug(
            "program_executed: %s (trigger=%s, success=%s)",
            payload.program_id,
            payload.trigger,
            payload.success,
        )

    def apply_custom_data_point_state_changed(
        self, payload: CustomDataPointStateChangedPayload
    ) -> None:
        """
        Replace one CDP's state dict from a ``custom_data_point.state_changed`` push.

        Unknown CDPs are logged at debug + ignored — same rationale as
        :meth:`apply_value_changed`: the bootstrap workflow is
        responsible for catalogue parity. A spurious event hints at a
        stale catalogue, not a missing entry.
        """
        key = (payload.device_address, payload.name)
        cdp = self._cdps.get(key)
        if cdp is None:
            _LOGGER.debug(
                "custom_data_point.state_changed for unknown CDP %s.%s — ignoring",
                payload.device_address,
                payload.name,
            )
            return
        cdp._replace_state(payload.state or {})

    # ---- write-back ----

    async def set_value(
        self,
        *,
        address: str,
        channel: int,
        parameter: str,
        value: Any,
        priority: str | None = None,
    ) -> None:
        """Translate a domain ``send_value`` into a daemon REST call."""
        if self._transport is None:
            msg = (
                "LoomStore has no transport bound — set one via "
                "set_transport() or construct the store with transport=…"
            )
            raise RuntimeError(msg)
        body: dict[str, Any] = {"value": value}
        if priority is not None:
            body["priority"] = priority
        path = f"/devices/{address}/channels/{channel}/data-points/{parameter}/value"
        await self._transport.request(
            "PUT",
            path,
            json_body=body,
            allow_retry=True,  # PUT here is idempotent — the daemon serializes the write.
        )

    async def set_sysvar(self, *, name: str, value: Any) -> None:
        """
        Write a sysvar's runtime value back to the CCU.

        Wire: ``PUT /sysvars/{name}``.
        """
        if self._transport is None:
            msg = "LoomStore has no transport bound — set one via set_transport()"
            raise RuntimeError(msg)
        await self._transport.request(
            "PUT",
            f"/sysvars/{name}",
            json_body={"value": value},
            allow_retry=True,
        )

    async def execute_program(self, *, program_id: str) -> None:
        """
        Trigger a CCU program.

        Wire: ``POST /programs/{id}/execute``. Not retried — programs
        can have side effects (cover open, notification send) where
        a double-invocation is the wrong default.
        """
        if self._transport is None:
            msg = "LoomStore has no transport bound — set one via set_transport()"
            raise RuntimeError(msg)
        await self._transport.request(
            "POST",
            f"/programs/{program_id}/execute",
            allow_retry=False,
        )

    async def invoke_custom_data_point(
        self,
        *,
        address: str,
        name: str,
        operation: str,
        params: dict[str, Any] | None = None,
        priority: str | None = None,
    ) -> None:
        """Translate a domain ``CustomDataPoint.invoke`` into a CDP-invoke REST call."""
        if self._transport is None:
            msg = (
                "LoomStore has no transport bound — set one via "
                "set_transport() or construct the store with transport=…"
            )
            raise RuntimeError(msg)
        body: dict[str, Any] = {}
        if params is not None:
            body["params"] = params
        if priority is not None:
            body["priority"] = priority
        # Always send a JSON body: the daemon parses the body strictly and
        # rejects an empty payload with 400 "Invalid JSON: EOF", so a bare
        # operation (turn_on without params) must POST ``{}``.
        await self._transport.request(
            "POST",
            f"/devices/{address}/cdps/{name}/{operation}",
            json_body=body,
            allow_retry=False,  # CDP operations may not be idempotent (e.g. cover open).
        )

    # ---- internals ----

    def _upsert_device_summary(self, summary: DeviceSummary) -> Device:
        device = self._devices.get(summary.address)
        if device is None:
            device = Device(summary=summary, store=self)
            self._devices[summary.address] = device
        else:
            device._update_summary(summary)
        return device

    def _upsert_program(self, summary: ProgramSummary) -> None:
        existing = self._programs.get(summary.id)
        if existing is None:
            self._programs[summary.id] = Program(summary=summary, store=self)
        else:
            existing._replace_summary(summary)

    def _upsert_sysvar(self, summary: SysvarSummary) -> None:
        existing = self._sysvars.get(summary.name)
        if existing is None:
            self._sysvars[summary.name] = Sysvar(summary=summary, store=self)
        else:
            existing._replace_summary(summary)

    def _upsert_channel(self, summary: ChannelSummary) -> None:
        device_address = summary.address.split(":", 1)[0]
        key = (device_address, summary.number)
        self._channels[key] = Channel(summary=summary, store=self)

    def _drop_channel(self, key: tuple[str, int]) -> None:
        self._channels.pop(key, None)
        # Drop any DPs hanging off this channel.
        stale_dps = [k for k in self._data_points if k[0] == key[0] and k[1] == key[1]]
        for k in stale_dps:
            del self._data_points[k]
