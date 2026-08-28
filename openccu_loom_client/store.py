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

from collections.abc import Callable, Mapping, Sequence
from functools import cached_property
import logging
from typing import TYPE_CHECKING, Any, Final, Protocol, runtime_checkable

from openccu_loom_types.rest import (
    AlarmArmAccepted,
    AlarmMotionResetResult,
    AlarmPanelEntity,
    AlarmTriggeredMotionSensor,
    CalculatedDPSummary,
    ChannelSummary,
    CustomDPSummary,
    DataPointSummary,
    DeviceAvailability,
    DeviceDetail,
    DeviceFirmware,
    DeviceSummary,
    ProgramSummary,
    Snapshot,
    SysvarSummary,
)

from openccu_loom_client.canonical import serial_suffix as canonical_serial_suffix
from openccu_loom_client.model import (
    MASTER_ZONE_ID,
    AlarmPanel,
    Channel,
    CustomDataPoint,
    DataPoint,
    Device,
    Program,
    Sysvar,
)
from openccu_loom_client.operations.alarm import AlarmOperations
from openccu_loom_client.operations.custom_data_points import CustomDataPointsOperations
from openccu_loom_client.operations.datapoints import DataPointsOperations
from openccu_loom_client.operations.devices import DevicesOperations
from openccu_loom_client.operations.hub import HubOperations

if TYPE_CHECKING:
    from collections.abc import Iterable

    from openccu_loom_types.rest import AlarmZoneStatus
    from openccu_loom_types.ws import (
        AlarmCountdownPayload,
        AlarmHealthChangedPayload,
        AlarmPanelChangedPayload,
        AlarmReadinessChangedPayload,
        AlarmStateChangedPayload,
        AlarmTriggeredPayload,
        CustomDataPointStateChangedPayload,
        DataPointValueChangedPayload,
        DeviceAvailabilityChangedPayload,
        DeviceCreatedPayload,
        DeviceRemovedPayload,
        ProgramChangedPayload,
        ProgramExecutedPayload,
        SysvarChangedPayload,
    )

    from openccu_loom_client.transport.http import HttpTransport

_LOGGER: Final = logging.getLogger(__name__)

# Upper bound on the number of distinct devices the store will hold. Every
# store map is fed by daemon-supplied data — ``load_snapshot`` and the live
# ``device.created`` push both add net-new entries with no intrinsic ceiling —
# so a hostile or compromised daemon could stream unique addresses without
# limit and grow the process toward OOM. A real CCU tops out in the low
# thousands of devices, so this ceiling never bites legitimately; on exceed we
# refuse the new address (existing devices keep updating) and warn once.
_DEFAULT_MAX_DEVICES: Final = 20000


@runtime_checkable
class _CalculatedAvailabilityAware(Protocol):
    """
    Data point that carries the daemon's verdict on a derived value.

    Calculated data points are the only ones today: their ``available`` flag
    rides the calc-dps record, not the generic summary, because a derived value
    is only as good as the readings it was computed from. Declared structurally
    so the store keeps its independence from the compat layer that installs the
    calculated-DP factory.
    """

    def apply_calculated_availability(self, *, available: bool) -> None:
        """Record the daemon's verdict on the derived value."""


class LoomStore:
    """Process-local mirror of one daemon's CCU model."""

    def __init__(self, *, transport: HttpTransport | None = None, max_devices: int = _DEFAULT_MAX_DEVICES) -> None:
        """Initialise an empty store, optionally bound to a transport."""
        self._transport = transport
        self._max_devices: Final = max_devices
        # One-shot latch so a sustained flood of net-new addresses past the cap
        # emits a single warning rather than one per rejected device.
        self._device_cap_warned = False
        # The daemon central *name* (``snapshot.interfaces[].central_id``,
        # == ``payload.central``). Used to scope/annotate events, NOT as a
        # routing-key prefix.
        self._central_id: str = ""
        # The HA-facing central *name* (the integration's instance name, ==
        # the LoomCentralAdapter ``name``). HA links every device to this
        # central via ``Device.central_info.name``, so it must match the
        # adapter name — which may differ from the daemon ``central_id``.
        self._central_name: str = ""
        # The HA-facing locale; read back by Device.config_provider for
        # locale-aware schedule names. Defaults to English.
        self._locale: str = "en"
        # The daemon's entity-name catalogue (``GET /i18n/entities``), kept
        # here rather than pushed onto each object because the objects that
        # read it come and go: an alarm panel is rebuilt by a catalogue
        # reconcile and seeded from a bare push, and either path would have
        # to re-deliver the names. Empty until the compat layer fills it,
        # and empty forever against a daemon too old to serve the route —
        # a reader falls back to its own wording either way.
        self._entity_names: dict[str, str] = {}
        self._calculated_factory: Callable[..., DataPoint] | None = None
        # The CCU serial suffix (last 10 chars, lower-cased). This is the
        # central-id slot of every canonical HA routing key for hub /
        # internal / virtual-remote addresses (see
        # ``openccu_loom_client.canonical.canonical_unique_id``); the categorised
        # data-point layer reads it back off the store to build
        # ``unique_id``s bit-identical to the daemon's.
        self._serial_suffix: str = ""
        self._devices: dict[str, Device] = {}
        self._channels: dict[tuple[str, int], Channel] = {}
        self._data_points: dict[tuple[str, int, str], DataPoint] = {}
        # Calculated DPs live in ``_data_points`` alongside the generic ones
        # (so ``apply_value_changed`` routes to them uniformly), but they are
        # attached by a different path (the compat adapter's start-time
        # ``attach_channel_calculated_data_points``) than the generic
        # per-channel fetch. Tracking their keys lets
        # ``attach_channel_data_points`` — which re-runs on every (re)bootstrap,
        # including the replay-lost recovery — replace only the generic DPs it
        # owns without collaterally dropping calculated DPs it never re-adds.
        self._calculated_dp_keys: set[tuple[str, int, str]] = set()
        self._cdps: dict[tuple[str, str], CustomDataPoint] = {}
        # Secondary index (address, primary-channel-no) → CDP, kept in lock-step
        # with ``_cdps`` so the refresh bridge's per-value-event channel lookup
        # (``get_custom_data_point_by_channel``) is O(1) instead of a scan.
        self._cdp_by_channel: dict[tuple[str, int], CustomDataPoint] = {}
        self._programs: dict[str, Program] = {}
        self._sysvars: dict[str, Sysvar] = {}
        # Categorised hub twins, built by factories the compat layer installs.
        # A sysvar has exactly one twin, so it simply *is* the entry in
        # ``_sysvars``; a program has two (execute button + activity switch),
        # which do not fit one slot and live here keyed by program id.
        #
        # The store owning them is what keeps them fresh: an ``_upsert_*`` has
        # to reach the object a consumer already holds. Building the twins
        # elsewhere and caching them there froze every hub entity at its
        # bootstrap value — the summary was replaced on the store's copy while
        # Home Assistant kept reading the cached one.
        self._program_factory: Callable[..., Sequence[Program]] | None = None
        self._sysvar_factory: Callable[..., Sysvar] | None = None
        self._program_dps: dict[str, tuple[Program, ...]] = {}
        # Alarm panels keyed by the daemon-computed ``unique_id`` (one per
        # alarm zone + the aggregate master; daemon ≥ 0.42.0). Empty when the
        # daemon's alarm subsystem is disabled — the /alarm routes are then
        # unmounted and bootstrap skips the section.
        self._alarm_panels: dict[str, AlarmPanel] = {}
        # Secondary index zone_id → panel (the ``alarm.*`` pushes are
        # zone-scoped; only ``alarm.panel_changed`` carries the unique_id).
        self._alarm_panel_by_zone: dict[str, AlarmPanel] = {}
        # Engine-global health verdict from ``alarm.health_changed``.
        self._alarm_healthy: bool = True
        # Factory hook (compat layer) that builds categorised AlarmPanel
        # subclasses — same pattern as ``set_data_point_factory``.
        self._alarm_panel_factory: Callable[..., AlarmPanel] | None = None
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
        # Per-device week-profile data point (the compat layer's
        # ``WeekProfileDp``), registered by the adapter at bootstrap so the
        # base :class:`Device` can expose it as ``week_profile_data_point``.
        self._week_profile_dps: dict[str, Any] = {}

    # ---- central identity ----

    @property
    def central_id(self) -> str:
        """The daemon central *name* (for event scoping, not key prefixing)."""
        return self._central_id

    def set_central_id(self, *, central_id: str | None) -> None:
        """Record the daemon central name (from the bootstrap snapshot)."""
        self._central_id = central_id or ""

    @property
    def central_name(self) -> str:
        """The HA-facing central name (adapter name), falling back to the daemon id."""
        return self._central_name or self._central_id

    def set_central_name(self, *, central_name: str | None) -> None:
        """Record the HA-facing central name (the integration's instance name)."""
        self._central_name = central_name or ""

    @property
    def locale(self) -> str:
        """The HA-facing locale (drives translated schedule names)."""
        return self._locale

    def set_locale(self, *, locale: str | None) -> None:
        """Record the HA-facing locale (the integration's UI language)."""
        self._locale = locale or "en"

    @property
    def entity_names(self) -> Mapping[str, str]:
        """
        The daemon's entity-name catalogue, keyed as authored.

        Values are templates as the daemon authored them — a placeholder
        such as ``{iface}`` is the reader's to fill, because only the
        reader knows which interface it is naming.
        """
        return self._entity_names

    def set_entity_names(self, *, entries: Mapping[str, str]) -> None:
        """Record the daemon's entity-name catalogue for the active locale."""
        self._entity_names = dict(entries)

    @property
    def serial_suffix(self) -> str:
        """CCU serial suffix — the central-id slot of canonical HA keys."""
        return self._serial_suffix

    def set_serial(self, *, serial: str | None) -> None:
        """
        Record the CCU serial; stored as its canonical suffix.

        The serial comes from ``GET /system/ccu`` (``SystemCCUEntry.serial``)
        or is injected by the integration (HA's ``entry.unique_id``).
        """
        self._serial_suffix = canonical_serial_suffix(serial=serial) if serial else ""

    # ---- transport wiring ----

    def set_transport(self, *, transport: HttpTransport) -> None:
        """
        Attach a transport to the store.

        Used when the store is built before the client opens its session
        (e.g. integration tests).
        """
        self._transport = transport

    @property
    def transport(self) -> HttpTransport | None:
        """Return the bound transport, or ``None`` if none is attached yet."""
        return self._transport

    # ---- operation façades ----
    #
    # The store used to build its own requests. Seventeen of its eighteen path
    # literals existed byte-identically in ``operations/`` already, retry flag
    # included, so the same URL was maintained in two places — and a path
    # literal maintained twice drifts. These delegate instead.
    #
    # They are built lazily rather than in ``__init__`` because the transport
    # is late-bound: a store can be constructed before the client opens its
    # session (``set_transport``), and ``_require_transport`` is what turns
    # "no transport yet" into a readable error at the point of use.

    @cached_property
    def _ops_alarm(self) -> AlarmOperations:
        """Return the alarm façade, built on the bound transport."""
        return AlarmOperations(transport=self._require_transport())

    @cached_property
    def _ops_hub(self) -> HubOperations:
        """Return the hub façade, built on the bound transport."""
        return HubOperations(transport=self._require_transport())

    @cached_property
    def _ops_datapoints(self) -> DataPointsOperations:
        """Return the data-point façade, built on the bound transport."""
        return DataPointsOperations(transport=self._require_transport())

    @cached_property
    def _ops_devices(self) -> DevicesOperations:
        """Return the devices façade, built on the bound transport."""
        return DevicesOperations(transport=self._require_transport())

    @cached_property
    def _ops_cdps(self) -> CustomDataPointsOperations:
        """Return the custom-data-point façade, built on the bound transport."""
        return CustomDataPointsOperations(transport=self._require_transport())

    def _require_transport(self) -> HttpTransport:
        """Return the bound transport, raising if none is set (write-back path)."""
        if self._transport is None:
            msg = (
                "LoomStore has no transport bound — set one via set_transport() or construct the store with transport=…"
            )
            raise RuntimeError(msg)
        return self._transport

    def set_data_point_factory(self, *, factory: Callable[..., DataPoint] | None) -> None:
        """
        Install a factory that builds (subclasses of) :class:`DataPoint`.

        Must be set before :meth:`attach_channel_data_points` runs (i.e.
        before bootstrap). The aiohomematic-compat layer uses this to
        have the store hold categorised ``Dp*`` instances so HA-side
        ``isinstance`` dispatch works on the very objects the store
        keeps live.
        """
        self._data_point_factory = factory

    def _build_data_point(self, *, summary: DataPointSummary, device_address: str, channel_number: int) -> DataPoint:
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

    def set_custom_data_point_factory(self, *, factory: Callable[..., CustomDataPoint] | None) -> None:
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

    @staticmethod
    def _clear_value_override(*, dp: DataPoint) -> None:
        """
        Drop any optimistic HA-written value so a fresh daemon value shows through.

        The compat data-point layer overlays ``_value_override`` when HA
        writes a value optimistically; a genuine daemon value (whether from
        a ``value_changed`` push or an explicit REST re-sync) supersedes it.
        Shared by the push and refresh paths so they cannot drift.
        """
        if hasattr(dp, "_value_override"):
            del dp._value_override

    @staticmethod
    def _refresh_is_stale(*, current_modified_at: Any, incoming_modified_at: Any) -> bool:
        """
        Report whether a REST refresh is older than the in-store value.

        Closes a lost-update race: a live ``value_changed`` push can land
        between a ``refresh_*`` GET and its write-back, on a different task
        than the dispatch loop. Both timestamps must be present to compare;
        otherwise we let the refresh through (best effort).
        """
        return (
            current_modified_at is not None
            and incoming_modified_at is not None
            and incoming_modified_at < current_modified_at
        )

    async def refresh_custom_data_point(self, *, address: str, name: str) -> None:
        """
        Re-read one CDP's detail from the daemon and apply its state.

        Backs the compat ``load_data_point_value`` for custom entities.
        No-op without a transport or if the CDP is unknown.
        """
        if self._transport is None:
            return
        cdp = self._cdps.get((address, name))
        if cdp is None:
            return
        # Snapshot the apply-generation before the round-trip; if a live
        # ``state_changed`` push lands during the GET it bumps the counter,
        # and we drop this now-stale REST snapshot rather than overwrite the
        # newer pushed state (CDP state carries no wire timestamp to compare).
        generation = cdp._apply_generation
        summary = await self._ops_cdps.get(address=address, name=name)
        cdp = self._cdps.get((address, name))
        if cdp is None or cdp._apply_generation != generation:
            return
        if summary.state is not None:
            cdp._replace_state(state=summary.state)

    async def refresh_data_point(self, *, address: str, channel: int, parameter: str) -> None:
        """
        Re-read one data-point's value from the daemon and apply it.

        Backs the compat ``load_data_point_value`` call HA makes when an
        entity is added or manually refreshed. No-op if no transport is
        bound. Unknown data points are ignored (same as a missed push).
        """
        if self._transport is None:
            return
        summary = await self._ops_devices.get_data_point(address=address, channel=channel, parameter=parameter)
        dp = self._data_points.get((address, channel, parameter))
        if dp is None:
            return
        if self._refresh_is_stale(
            current_modified_at=dp.summary.modified_at,
            incoming_modified_at=summary.modified_at,
        ):
            return
        dp._replace_summary(summary=summary)
        self._clear_value_override(dp=dp)

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

    def get_channel_by_address(self, *, channel_address: str) -> Channel | None:
        """
        Return the channel for a canonical ``"ADDR:idx"`` address, or ``None``.

        The daemon serialises channel references (e.g. the sysvar/program
        device link) as one canonical string; this resolves it against the
        channel graph. Malformed or unknown addresses yield ``None``.
        """
        device_address, sep, raw_number = channel_address.partition(":")
        if not sep:
            return None
        try:
            number = int(raw_number)
        except ValueError:
            return None
        return self._channels.get((device_address, number))

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

    def get_custom_data_point_by_channel(self, *, address: str, channel_no: int) -> CustomDataPoint | None:
        """Return the CDP whose primary channel is ``channel_no``, or ``None`` (O(1))."""
        return self._cdp_by_channel.get((address, channel_no))

    def _register_cdp(self, *, key: tuple[str, str], cdp: CustomDataPoint) -> None:
        """Add a CDP to both the name-keyed map and the channel-keyed index."""
        self._cdps[key] = cdp
        self._cdp_by_channel[(key[0], cdp.summary.channel_no)] = cdp

    def _drop_cdp(self, *, key: tuple[str, str]) -> None:
        """Remove a CDP from both maps, keeping the channel index in lock-step."""
        cdp = self._cdps.pop(key, None)
        if cdp is not None:
            self._cdp_by_channel.pop((key[0], cdp.summary.channel_no), None)

    # ---- week-profile data points ----

    def set_week_profile_data_point(self, *, address: str, data_point: Any) -> None:
        """Register a device's week-profile data point (built by the compat adapter)."""
        self._week_profile_dps[address] = data_point

    def get_week_profile_data_point(self, *, address: str) -> Any:
        """Return a device's week-profile data point, or ``None`` if it has none."""
        return self._week_profile_dps.get(address)

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

    # ---- alarm panels ----

    @property
    def alarm_panels(self) -> Iterable[AlarmPanel]:
        """Every alarm panel currently known (empty when alarm is disabled)."""
        return self._alarm_panels.values()

    @property
    def alarm_healthy(self) -> bool:
        """The engine-global alarm health verdict (``alarm.health_changed``)."""
        return self._alarm_healthy

    def get_alarm_panel(self, *, unique_id: str) -> AlarmPanel | None:
        """Return the panel for the daemon-computed unique id, or ``None``."""
        return self._alarm_panels.get(unique_id)

    def get_alarm_panel_by_zone(self, *, zone_id: str) -> AlarmPanel | None:
        """Return the panel of one alarm zone (or the master), or ``None``."""
        return self._alarm_panel_by_zone.get(zone_id)

    def set_alarm_panel_factory(self, *, factory: Callable[..., AlarmPanel] | None) -> None:
        """
        Install a factory that builds (subclasses of) :class:`AlarmPanel`.

        Must be set before :meth:`attach_alarm_panels` runs. The
        aiohomematic-compat layer uses this to have the store hold the
        categorised alarm-control-panel class so HA-side ``isinstance``
        dispatch works on the live store objects.
        """
        self._alarm_panel_factory = factory

    def _build_alarm_panel(self, *, summary: AlarmPanelEntity) -> AlarmPanel:
        if self._alarm_panel_factory is not None:
            return self._alarm_panel_factory(summary=summary, store=self)
        return AlarmPanel(summary=summary, store=self)

    def _register_alarm_panel(self, *, panel: AlarmPanel) -> None:
        self._alarm_panels[panel.unique_id] = panel
        self._alarm_panel_by_zone[panel.zone_id] = panel

    def _drop_alarm_panel(self, *, unique_id: str) -> None:
        panel = self._alarm_panels.pop(unique_id, None)
        if panel is not None:
            self._alarm_panel_by_zone.pop(panel.zone_id, None)

    def attach_alarm_panels(self, *, panels: list[AlarmPanelEntity]) -> None:
        """
        Replace the alarm-panel catalogue (``GET /alarm/panels``).

        Reconciles in place: an existing panel keeps its live instance
        (summary replaced), new panels are built, vanished panels are
        dropped — same never-rebuild-on-update discipline as
        :meth:`attach_custom_data_points`.
        """
        incoming = {entity.unique_id for entity in panels}
        stale = [uid for uid in self._alarm_panels if uid not in incoming]
        for uid in stale:
            self._drop_alarm_panel(unique_id=uid)
        for entity in panels:
            existing = self._alarm_panels.get(entity.unique_id)
            if existing is not None:
                existing._replace_summary(summary=entity)
                # Keep the zone index in lock-step (the zone id of an
                # existing unique_id cannot really change, but cheap).
                self._alarm_panel_by_zone[entity.zone_id] = existing
                continue
            self._register_alarm_panel(panel=self._build_alarm_panel(summary=entity))

    def apply_triggered_motion(self, *, sensors: list[AlarmTriggeredMotionSensor]) -> None:
        """
        Seed the per-panel latched-detector counts (``GET /alarm/triggered-motion``).

        Pure mutation, no I/O: the caller does the read. Every panel is
        written, so a zone that dropped to zero is cleared rather than
        keeping a stale count, and the master panel receives the total
        — which is the same scope the daemon's aggregate reset covers.

        There is no ``alarm.*`` broadcast for a latch, so this is the
        only way the counts move. :meth:`LoomClient.refresh_triggered_motion`
        owns the read and the cadence.
        """
        per_zone: dict[str, int] = {}
        for sensor in sensors:
            per_zone[sensor.zone_id] = per_zone.get(sensor.zone_id, 0) + 1
        for panel in self._alarm_panels.values():
            count = len(sensors) if panel.is_master else per_zone.get(panel.zone_id, 0)
            panel._set_triggered_motion_count(count=count)

    def attach_alarm_zone_statuses(self, *, statuses: list[AlarmZoneStatus]) -> None:
        """
        Seed the live zone detail (``GET /alarm/state``) onto the panels.

        Unknown zones are ignored — :meth:`attach_alarm_panels` owns
        catalogue parity.
        """
        for status in statuses:
            panel = self._alarm_panel_by_zone.get(status.id)
            if panel is not None:
                panel._replace_status(status=status)

    def apply_alarm_panel_changed(self, *, payload: AlarmPanelChangedPayload) -> None:
        """
        Apply an ``alarm.panel_changed`` push (state/availability/lifecycle).

        ``removed`` drops the panel. A push for an unknown panel seeds a
        stub entry (mirroring :meth:`apply_device_created`) — the payload
        carries everything but ``supported_modes``, which the next
        catalogue reconcile (``GET /alarm/panels``) fills in. The
        effective code policy (``code_arm_required`` /
        ``code_disarm_required``, daemon ≥ 0.43.x) rides every push, so
        live policy edits propagate without a reconcile.
        """
        if payload.removed:
            self._drop_alarm_panel(unique_id=payload.unique_id)
            return
        panel = self._alarm_panels.get(payload.unique_id)
        if panel is None:
            stub = AlarmPanelEntity.model_validate(
                {
                    "unique_id": payload.unique_id,
                    "zone_id": payload.zone_id,
                    "name": payload.name,
                    "category": "alarm_control_panel",
                    "state": payload.state,
                    "available": payload.available,
                    "master": payload.zone_id == MASTER_ZONE_ID,
                    "code_arm_required": payload.code_arm_required,
                    "code_disarm_required": payload.code_disarm_required,
                }
            )
            self._register_alarm_panel(panel=self._build_alarm_panel(summary=stub))
            return
        panel._replace_summary(
            summary=panel.summary.model_copy(
                update={
                    "name": payload.name,
                    "state": payload.state,
                    "available": payload.available,
                    "code_arm_required": payload.code_arm_required,
                    "code_disarm_required": payload.code_disarm_required,
                }
            )
        )

    def apply_alarm_state_changed(self, *, payload: AlarmStateChangedPayload) -> None:
        """
        Fold an ``alarm.state_changed`` push into the zone's live detail.

        Only the zone-level detail (mode, countdown lifetime) updates
        here — the HA state token travels on the parallel
        ``alarm.panel_changed`` push, so it is never re-derived
        client-side.
        """
        panel = self._alarm_panel_by_zone.get(payload.zone_id)
        if panel is None:
            return
        panel._set_mode(mode=payload.mode.value if payload.mode is not None else None)
        # A countdown only survives the arming/pending phases; any other
        # transition ends it (the daemon stops ticking without a
        # terminating push).
        if payload.new_state.value not in ("arming", "pending"):
            panel._clear_countdown()

    def apply_alarm_countdown(self, *, payload: AlarmCountdownPayload) -> None:
        """Fold an ``alarm.countdown`` tick into the zone's live detail."""
        panel = self._alarm_panel_by_zone.get(payload.zone_id)
        if panel is None:
            return
        panel._set_countdown(
            kind=payload.kind.value,
            remaining_s=payload.remaining_s,
            total_s=payload.total_s,
        )

    def apply_alarm_readiness_changed(self, *, payload: AlarmReadinessChangedPayload) -> None:
        """Replace the zone's per-mode readiness from an ``alarm.readiness_changed`` push."""
        panel = self._alarm_panel_by_zone.get(payload.zone_id)
        if panel is None:
            return
        panel._set_readiness(readiness=payload.readiness)

    def apply_alarm_triggered(self, *, payload: AlarmTriggeredPayload) -> None:
        """Record the trigger detail (incident id, cause, sensor) on the panel."""
        panel = self._alarm_panel_by_zone.get(payload.zone_id)
        if panel is None:
            return
        panel._record_incident(
            incident_id=payload.incident_id,
            cause=payload.cause,
            sensor_name=payload.sensor_name,
        )

    def apply_alarm_health_changed(self, *, payload: AlarmHealthChangedPayload) -> None:
        """Latch the engine-global health flag (panel availability rides ``panel_changed``)."""
        self._alarm_healthy = payload.healthy

    async def arm_alarm_zone(
        self,
        *,
        zone_id: str,
        mode: str,
        code: str | None = None,
        force: bool | None = None,
        skip_delay: bool | None = None,
        bypass: list[str] | None = None,
    ) -> AlarmArmAccepted:
        """
        Arm one alarm zone.

        Wire: ``POST /alarm/zones/{id}/arm``. Not retried — arming has
        side effects (exit delay, chirps) and readiness may change
        between attempts. The resulting state travels back via the
        ``alarm.panel_changed`` push.

        Returns the daemon's acceptance record. This used to discard it, and
        discarding it was the reason the delegation below could not simply
        happen: the façade validates the response, so a caller that threw it
        away turned an empty body into a parse error. The record says what the
        daemon accepted — which zones, with what delay — and only the caller
        can decide whether that matches what the user asked for.
        """
        return await self._ops_alarm.arm_zone(
            zone_id=zone_id, mode=mode, force=force, skip_delay=skip_delay, bypass=bypass, code=code
        )

    async def disarm_alarm_zone(self, *, zone_id: str, code: str | None = None) -> None:
        """Disarm one alarm zone. Wire: ``POST /alarm/zones/{id}/disarm``. Not retried."""
        await self._ops_alarm.disarm_zone(zone_id=zone_id, code=code)

    async def silence_alarm_zone(self, *, zone_id: str, code: str | None = None) -> None:
        """Silence one zone's sounding outputs. Wire: ``POST /alarm/zones/{id}/silence``."""
        await self._ops_alarm.silence_zone(zone_id=zone_id, code=code)

    async def acknowledge_alarm_zone(self, *, zone_id: str, code: str | None = None) -> None:
        """Acknowledge an ended incident. Wire: ``POST /alarm/zones/{id}/acknowledge``."""
        await self._ops_alarm.acknowledge_zone(zone_id=zone_id, code=code)

    async def silence_all_alarm_zones(self) -> None:
        """Silence every sounding output (break-glass). Wire: ``POST /alarm/silence-all``."""
        await self._ops_alarm.silence_all()

    async def reset_alarm_zone_motion(self, *, zone_id: str) -> AlarmMotionResetResult:
        """
        Clear one zone's latched motion/presence detectors.

        Wire: ``POST /alarm/zones/{id}/reset-motion`` (daemon ≥ 0.58.1).
        Not retried — the reset writes to devices, so a blind replay is
        real radio traffic.

        Unlike the other alarm verbs this returns the daemon's result
        instead of ``None``: the counters distinguish "nothing was
        latched" (``reset == 0 and failed == 0``) from "detectors did
        not answer" (``failed > 0``), and only the caller can decide
        what that means for the user. There is no ``alarm.*`` broadcast
        for a reset pass, so the return value is the only report.
        """
        return await self._ops_alarm.reset_zone_motion(zone_id=zone_id)

    async def reset_all_alarm_motion(self) -> AlarmMotionResetResult:
        """
        Clear every latched motion/presence detector across all zones.

        Wire: ``POST /alarm/reset-motion`` (daemon ≥ 0.58.1). Not
        retried. Same counter semantics as :meth:`reset_alarm_zone_motion`.
        """
        return await self._ops_alarm.reset_all_motion()

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
        incoming = {(device_address, summary.name) for summary in cdps}
        stale = [k for k in self._cdps if k[0] == device_address and k not in incoming]
        for k in stale:
            self._drop_cdp(key=k)
        for summary in cdps:
            key = (device_address, summary.name)
            existing = self._cdps.get(key)
            if existing is not None:
                # Update in place — rebuilding would orphan the live twin HA
                # holds. Re-seed the live state from the fresh snapshot too.
                existing._replace_summary(summary=summary)
                if summary.state is not None:
                    existing._replace_state(state=summary.state)
                # Keep the channel index in lock-step in case the primary
                # channel moved between catalogue reads.
                self._cdp_by_channel[(device_address, summary.channel_no)] = existing
                continue
            # Seed the live state from the summary's snapshot (daemon
            # >= 0.x includes it in GET .../cdps) so entities start on
            # the real state instead of defaults until the first WS
            # ``custom_data_point.state_changed`` push arrives.
            self._register_cdp(
                key=key,
                cdp=self._build_custom_data_point(
                    summary=summary,
                    device_address=device_address,
                    initial_state=summary.state,
                ),
            )

    # ---- bulk load (bootstrap) ----

    def load_snapshot(self, *, snapshot: Snapshot) -> None:
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
            self.set_central_id(central_id=self._infer_central_id(snapshot=snapshot))
        for summary in snapshot.devices:
            self._upsert_device_summary(summary=summary)
        for prog in snapshot.programs or ():
            self._upsert_program(summary=prog)
        for sysvar in snapshot.sysvars or ():
            self._upsert_sysvar(summary=sysvar)

    def _infer_central_id(self, *, snapshot: Snapshot) -> str | None:
        """
        Derive this central's id from the snapshot's interface list.

        The daemon mediates *every* configured central, so the interface
        list may carry several distinct ``central_id`` values (the
        daemon-side central *names*). Adopting a foreign central's id
        would make ``_matches_central``-style filters accept that
        central's sysvars/programs/interfaces and leak its entities into
        this HA entry. Resolution order:

        1. the candidate equal to the configured :attr:`central_name`
           (the integration's instance name) — the only safe pick in a
           multi-central deployment;
        2. the single unique candidate when all interfaces agree
           (single-central deployment whose daemon name may differ from
           the HA instance name);
        3. ``None`` when the list is ambiguous — central-scoped filters
           then match the configured name only.
        """
        candidates = [iface.central_id for iface in snapshot.interfaces or () if iface.central_id]
        if self._central_name and self._central_name in candidates:
            return self._central_name
        unique = list(dict.fromkeys(candidates))
        if len(unique) == 1:
            return unique[0]
        if unique:
            _LOGGER.warning(
                "Snapshot reports multiple centrals %s and none matches the "
                "configured central name %r — leaving central_id unset so "
                "only payloads tagged %r are accepted",
                unique,
                self._central_name,
                self._central_name,
            )
        return None

    def attach_device_detail(self, *, detail: DeviceDetail) -> None:
        """
        Apply a full ``GET /devices/{addr}`` detail record.

        Idempotent: re-applying overwrites firmware / availability and
        re-registers every channel in the detail. Channels that are no
        longer present in the new detail are removed; their
        data-points go with them.
        """
        # DeviceDetail extends DeviceSummary, so we can pass it through
        # the upsert path that handles the common case.
        device = self._upsert_device_summary(summary=detail)
        if device is None:
            return  # device cap reached — a net-new address was refused
        device._attach_detail(
            firmware=detail.firmware,
            availability=detail.availability,
        )

        new_numbers: set[int] = set()
        for channel_summary in detail.channels or ():
            self._upsert_channel(summary=channel_summary)
            new_numbers.add(channel_summary.number)

        # Garbage-collect channels (and their DPs) that vanished.
        stale_keys = [k for k in self._channels if k[0] == detail.address and k[1] not in new_numbers]
        for stale in stale_keys:
            self._drop_channel(key=stale)

    def attach_channel_data_points(
        self,
        *,
        device_address: str,
        channel_number: int,
        data_points: list[DataPointSummary],
    ) -> None:
        """
        Register the data-points of one channel.

        Reconciles the channel's generic DPs against the daemon's
        authoritative catalogue *in place*: an existing DP keeps its live
        instance (its summary is replaced), genuinely new parameters are
        built, and parameters the daemon dropped are removed. Rebuilding a
        surviving DP would orphan the reference HA already holds — after a
        replay-lost re-bootstrap that would silently freeze every entity —
        so this follows the same never-rebuild-on-update discipline as
        :meth:`_upsert_program` / :meth:`_upsert_sysvar`.

        Calculated DPs share the ``_data_points`` map but are attached by a
        separate start-time path that this method never re-runs, so they are
        excluded from the stale sweep (see :attr:`_calculated_dp_keys`).
        """
        incoming = {(device_address, channel_number, dp.parameter) for dp in data_points}
        stale = [
            k
            for k in self._data_points
            if k[0] == device_address
            and k[1] == channel_number
            and k not in incoming
            and k not in self._calculated_dp_keys
        ]
        for s in stale:
            del self._data_points[s]

        for dp_summary in data_points:
            key = (device_address, channel_number, dp_summary.parameter)
            existing = self._data_points.get(key)
            if existing is None:
                self._data_points[key] = self._build_data_point(
                    summary=dp_summary,
                    device_address=device_address,
                    channel_number=channel_number,
                )
            else:
                existing._replace_summary(summary=dp_summary)

    # ---- live updates ----

    def apply_value_changed(self, *, payload: DataPointValueChangedPayload) -> None:
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
                "value_changed for unknown data-point %s — ignoring (catalogue out of sync; resync via /devices/%s)",
                ".".join(map(str, key)),
                payload.device_address,
            )
            return
        new_summary = dp.summary.model_copy(
            update={
                "value": payload.value,
                # The daemon owns this projection and puts it on the push
                # (api ≥ 7.7.0), so it is copied, never recomputed: an
                # absent value means `value` already is the displayable
                # number — a trivial multiplier, or a value no projection
                # applies to — and rebuilding it here from the summary's
                # multiplier would be a second opinion about the daemon's
                # own arithmetic. `value` stays the raw CCU wire value
                # because the write path sends it back unchanged.
                "display_value": payload.display_value,
                "observed": True,
                "modified_at": payload.modified_at,
                "last_changed_at": payload.modified_at,
                "last_seen_at": payload.modified_at,
            }
        )
        dp._replace_summary(summary=new_summary)
        self._clear_value_override(dp=dp)

    def apply_device_created(self, *, payload: DeviceCreatedPayload) -> None:
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
        if not self._can_admit_device(address=payload.device_address):
            return
        stub = DeviceSummary(
            address=payload.device_address,
            central=payload.central,
            interface=payload.interface_id or "",
            interface_id=payload.interface_id,
            model=payload.model,
            name=payload.device_address,
            available=True,
            channels_count=0,
            # Stub defaults — corrected by the next ``attach_device_detail``,
            # which carries the daemon's authoritative flags.
            updatable=False,
            update_available=False,
            master_pushes_config_pending=False,
            has_sub_devices=False,
            # Required on the summary since daemon api 7.23.0, and unknown
            # until the device is read: the push carries neither. An empty
            # firmware record and a reachability that mirrors ``available``
            # are the only honest stand-ins — both are replaced wholesale by
            # the next ``attach_device_detail``.
            firmware=DeviceFirmware(),
            availability=DeviceAvailability(IsReachable=True),
        )
        self._devices[payload.device_address] = Device(summary=stub, store=self)

    def apply_device_availability_changed(self, *, payload: DeviceAvailabilityChangedPayload) -> None:
        """
        Flip a device's ``available`` flag from a ``device.availability_changed`` push.

        The payload's ``available`` matches the ``available`` field of the
        device's REST summary, so the summary is the single place it lands;
        every reader (:attr:`Device.available`, the compat twins' per-entity
        ``available``) resolves it live from there. A push for an unknown
        device is logged + ignored (same rationale as
        :meth:`apply_value_changed` — the bootstrap owns catalogue parity).
        """
        device = self._devices.get(payload.device_address)
        if device is None:
            _LOGGER.debug(
                "availability_changed for unknown device %s — ignoring (catalogue out of sync)",
                payload.device_address,
            )
            return
        if device.summary.available == payload.available:
            return
        device._update_summary(summary=device.summary.model_copy(update={"available": payload.available}))

    def apply_device_removed(self, *, payload: DeviceRemovedPayload) -> None:
        """Drop a device and everything that hung off it."""
        addr = payload.device_address
        self._devices.pop(addr, None)
        stale_channels = [k for k in self._channels if k[0] == addr]
        for k in stale_channels:
            self._drop_channel(key=k)
        # CDPs are device-scoped, not channel-scoped — drop them too.
        stale_cdps = [cdp_key for cdp_key in self._cdps if cdp_key[0] == addr]
        for cdp_key in stale_cdps:
            self._drop_cdp(key=cdp_key)
        # The per-device week-profile DP is registered outside the channel
        # graph, so channel GC never reaches it — drop it explicitly, else
        # it accrues across unpair events (a slow leak).
        self._week_profile_dps.pop(addr, None)

    def apply_sysvar_changed(self, *, payload: SysvarChangedPayload) -> None:
        """
        Replace one sysvar's value from a ``hub.sysvar_changed`` push.

        Unknown sysvars are logged + ignored (same rationale as
        :meth:`apply_value_changed`).
        """
        sysvar = self._sysvars.get(payload.name)
        if sysvar is None:
            _LOGGER.debug("sysvar_changed for unknown sysvar %r — ignoring", payload.name)
            return
        new_summary = sysvar.summary.model_copy(
            update={
                "value": payload.value,
                "observed": True,
                # The push carries the device link (the same value the REST
                # summary holds — absent means unlinked), so a re-resolved
                # link (renamed variable, changed CCU channel assignment)
                # propagates live without a catalogue refresh.
                "channel": payload.channel,
                "device_address": payload.device_address,
            }
        )
        sysvar._replace_summary(summary=new_summary)

    def apply_program_executed(self, *, payload: ProgramExecutedPayload) -> None:
        """
        Acknowledge a program execution event.

        The catalogue itself doesn't change, but a subscriber may want to
        react to the event itself (logged / used by HA-side automations).
        A device link carried by the push is folded into the program's
        summary so consumers see the current attachment; an *absent* link
        is ambiguous on this event (unlinked vs. hub model not yet loaded
        on the daemon) and therefore never clears an existing one — the
        next catalogue refresh is authoritative for unlinking.
        """
        _LOGGER.debug(
            "program_executed: %s (trigger=%s, success=%s)",
            payload.program_id,
            payload.trigger,
            payload.success,
        )
        program = self._programs.get(payload.program_id)
        if program is None or not payload.channel:
            return
        summary = program.summary
        if payload.channel != summary.channel or payload.device_address != summary.device_address:
            self._upsert_program(
                summary=summary.model_copy(
                    update={"channel": payload.channel, "device_address": payload.device_address}
                )
            )

    def apply_program_changed(self, *, payload: ProgramChangedPayload) -> None:
        """
        Apply a program's activity flip from a ``hub.program_changed`` push.

        A CCU program is two controls: the activity flag decides whether it
        reacts at all, and the execution runs it once. The CCU refuses the
        execution while the flag is off, so the two travel together on this
        push and land on one summary — the button reads ``execute_available``,
        the switch reads ``active``, and both are views of the same record.

        Unknown programs are logged and ignored (same rationale as
        :meth:`apply_value_changed`). A device link carried by the push is
        folded in; an absent one never clears an existing link, matching
        :meth:`apply_program_executed`.
        """
        program = self._programs.get(payload.program_id)
        if program is None:
            _LOGGER.debug(
                "program_changed for unknown program %r — ignoring (catalogue out of sync)",
                payload.program_id,
            )
            return
        update: dict[str, Any] = {
            "active": payload.active,
            "execute_available": payload.execute_available,
        }
        if payload.channel:
            update["channel"] = payload.channel
            update["device_address"] = payload.device_address
        self._upsert_program(summary=program.summary.model_copy(update=update))

    def apply_custom_data_point_state_changed(self, *, payload: CustomDataPointStateChangedPayload) -> None:
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
        cdp._replace_state(state=payload.state or {})

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
        await self._ops_datapoints.set_value(
            address=address, channel=channel, parameter=parameter, value=value, priority=priority
        )

    async def set_sysvar(self, *, name: str, value: Any) -> None:
        """
        Write a sysvar's runtime value back to the CCU.

        Wire: ``PUT /sysvars/{name}``.
        """
        await self._ops_hub.set_sysvar(name=name, value=value)

    async def execute_program(self, *, program_id: str) -> None:
        """
        Trigger a CCU program.

        Wire: ``POST /programs/{id}/execute``. Not retried — programs
        can have side effects (cover open, notification send) where
        a double-invocation is the wrong default.
        """
        await self._ops_hub.execute_program(program_id=program_id)

    async def set_program_enabled(self, *, program_id: str, active: bool) -> None:
        """
        Activate or deactivate a CCU program, then re-read it.

        Wire: ``PATCH /programs/{id}`` with ``{"active": …}``. Idempotent —
        writing the state a program already has is a no-op on the CCU — so the
        call is retried.

        The daemon accepts the write as scheduled (202) and pushes
        ``hub.program_changed`` once the CCU confirms the flip. The re-read
        here is the belt to that braces: it settles the local view for a
        consumer that reads the program straight after the call, without
        waiting for the push.
        """
        await self._ops_hub.set_program_enabled(program_id=program_id, active=active)
        await self.refresh_program(program_id=program_id)

    async def refresh_program(self, *, program_id: str) -> None:
        """
        Re-read one program from the daemon and apply it.

        Wire: ``GET /programs/{id}``. No-op without a transport or when the
        daemon does not know the program (it may have been deleted between the
        catalogue read and this call).
        """
        if self._transport is None:
            return
        self._upsert_program(summary=await self._ops_hub.get_program(program_id=program_id))

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
        await self._ops_cdps.invoke(address=address, name=name, operation=operation, params=params, priority=priority)

    # ---- internals ----

    def _upsert_device_summary(self, *, summary: DeviceSummary) -> Device | None:
        device = self._devices.get(summary.address)
        if device is None:
            if not self._can_admit_device(address=summary.address):
                return None
            device = Device(summary=summary, store=self)
            self._devices[summary.address] = device
        else:
            device._update_summary(summary=summary)
        return device

    def _can_admit_device(self, *, address: str) -> bool:
        """
        Report whether a *net-new* device address may be admitted.

        Guards every growth path (``load_snapshot`` and the live
        ``device.created`` push) against unbounded map growth from a hostile
        daemon streaming unique addresses. Updates to an already-known address
        never reach here, so existing devices keep refreshing even at the cap.
        """
        if address in self._devices or len(self._devices) < self._max_devices:
            return True
        if not self._device_cap_warned:
            self._device_cap_warned = True
            _LOGGER.warning(
                "device cap reached (%d) — refusing net-new device %s and further new devices; "
                "existing devices continue to update",
                self._max_devices,
                address,
            )
        return False

    def attach_channel_calculated_data_points(
        self,
        *,
        device_address: str,
        channel_number: int,
        calculated: list[CalculatedDPSummary],
    ) -> None:
        """
        Register daemon-calculated DPs for one channel.

        They live in the same ``(address, channel, parameter)`` map as the
        generic data points, so ``apply_value_changed`` routes the daemon's
        ``datapoint.value_changed`` pushes to them without special-casing.
        """
        if self._calculated_factory is None:
            return
        for calc in calculated:
            key = (device_address, channel_number, calc.name)
            self._data_points[key] = self._calculated_factory(
                summary=calc,
                device_address=device_address,
                channel_number=channel_number,
                store=self,
            )
            self._calculated_dp_keys.add(key)

    def set_calculated_data_point_factory(self, *, factory: Callable[..., DataPoint] | None) -> None:
        """Install the categorised calculated-DP factory (compat layer)."""
        self._calculated_factory = factory

    def set_hub_data_point_factories(
        self,
        *,
        program_factory: Callable[..., Sequence[Program]] | None = None,
        sysvar_factory: Callable[..., Sysvar] | None = None,
    ) -> None:
        """
        Install the categorised hub-DP factories (compat layer).

        Mirrors :meth:`set_calculated_data_point_factory`: the store builds the
        categorised instance so there is exactly one live object per hub entity
        and an ``_upsert_*`` reaches the object a consumer already holds.
        Install before the first catalogue merge; entries already present are
        rebuilt so a late install is not silently half-applied.
        """
        self._program_factory = program_factory
        self._sysvar_factory = sysvar_factory
        for program in list(self._programs.values()):
            if program_factory is not None and program.id not in self._program_dps:
                self._program_dps[program.id] = tuple(
                    program_factory(
                        summary=program.summary,
                        store=self,
                        enabled_default=bool(program.summary.enabled_default),
                    )
                )
        if sysvar_factory is not None:
            for name, sysvar in list(self._sysvars.items()):
                if type(sysvar) is Sysvar:  # not yet categorised
                    self._sysvars[name] = sysvar_factory(
                        summary=sysvar.summary,
                        store=self,
                        enabled_default=bool(sysvar.summary.enabled_default),
                    )

    def program_data_points(self, *, program_id: str) -> tuple[Program, ...]:
        """
        Return the categorised twins of one program (button + switch).

        Empty when no factory is installed or the program is unknown.
        """
        return self._program_dps.get(program_id, ())

    async def refresh_calculated_data_point(self, *, address: str, channel: int, name: str) -> None:
        """Re-read one calculated DP from the daemon and apply its value."""
        if self._transport is None:
            return
        calc = await self._ops_devices.get_calculated_data_point(address=address, channel=channel, name=name)
        dp = self._data_points.get((address, channel, name))
        if dp is not None:
            if self._refresh_is_stale(
                current_modified_at=dp.summary.modified_at,
                incoming_modified_at=calc.modified_at,
            ):
                return
            new_summary = dp.summary.model_copy(
                update={
                    "value": calc.value,
                    "observed": calc.observed,
                    "modified_at": calc.modified_at,
                }
            )
            dp._replace_summary(summary=new_summary)
            # `available` has no slot on the generic summary shape, so it is
            # applied to the data point itself. A source the daemon flagged
            # leaves value and `observed` untouched — this flag is the only
            # thing that changes, and the re-read is where the client learns it.
            if isinstance(dp, _CalculatedAvailabilityAware):
                dp.apply_calculated_availability(available=calc.available)
            self._clear_value_override(dp=dp)

    async def refresh_device(self, *, address: str) -> None:
        """
        Re-fetch one device's detail (incl. firmware record) into the store.

        Backs the compat ``DpUpdate.refresh_firmware_data``. No-op
        without a transport.
        """
        if self._transport is None:
            return
        self.attach_device_detail(detail=await self._ops_devices.get_device_detail(address=address))

    async def update_device_firmware(self, *, address: str) -> None:
        """
        Trigger the device's OTA firmware update on the daemon.

        Wire: ``POST /devices/{addr}/firmware/update``. Never retried —
        a duplicated trigger could double-flash the device.
        """
        await self._ops_devices.update_firmware(address=address)

    def attach_hub_catalogue(
        self,
        *,
        sysvars: list[SysvarSummary] | None = None,
        programs: list[ProgramSummary] | None = None,
    ) -> None:
        """
        Merge the full hub catalogue into the store.

        The bootstrap snapshot only carries the hub data the daemon's
        snapshot index holds (in multi-central deployments that is the
        first central's set); ``GET /sysvars`` / ``GET /programs``
        return the complete daemon-wide catalogue — callers fetch those
        and merge them here.
        """
        for sysvar in sysvars or ():
            self._upsert_sysvar(summary=sysvar)
        for program in programs or ():
            self._upsert_program(summary=program)

    def _upsert_program(self, *, summary: ProgramSummary) -> None:
        existing = self._programs.get(summary.id)
        if existing is None:
            self._programs[summary.id] = Program(summary=summary, store=self)
            if self._program_factory is not None:
                self._program_dps[summary.id] = tuple(
                    self._program_factory(
                        summary=summary,
                        store=self,
                        enabled_default=bool(summary.enabled_default),
                    )
                )
            return
        existing._replace_summary(summary=summary)
        # The categorised twins carry their own summary reference; a consumer
        # holds one of *those*, so the update has to reach them too. Both read
        # the same record — the button's availability and the switch's state
        # are two views of one program.
        for twin in self._program_dps.get(summary.id, ()):
            twin._replace_summary(summary=summary)

    def _upsert_sysvar(self, *, summary: SysvarSummary) -> None:
        existing = self._sysvars.get(summary.name)
        if existing is None:
            self._sysvars[summary.name] = (
                self._sysvar_factory(summary=summary, store=self, enabled_default=bool(summary.enabled_default))
                if self._sysvar_factory is not None
                else Sysvar(summary=summary, store=self)
            )
            return
        existing._replace_summary(summary=summary)

    def _upsert_channel(self, *, summary: ChannelSummary) -> None:
        device_address = summary.address.split(":", 1)[0]
        key = (device_address, summary.number)
        existing = self._channels.get(key)
        if existing is None:
            self._channels[key] = Channel(summary=summary, store=self)
        else:
            # Update in place — rebuilding would orphan the live reference a
            # consumer already holds (freezing it after a re-bootstrap).
            existing._replace_summary(summary=summary)

    def _drop_channel(self, *, key: tuple[str, int]) -> None:
        self._channels.pop(key, None)
        # Drop any DPs hanging off this channel (generic and calculated).
        stale_dps = [k for k in self._data_points if k[0] == key[0] and k[1] == key[1]]
        for k in stale_dps:
            del self._data_points[k]
            self._calculated_dp_keys.discard(k)
