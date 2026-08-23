# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Typed event classes that wrap ``WsEnvelope`` + payload.

The daemon's WebSocket surface emits a uniform envelope
``{topic, type, ts, seq, kind, payload}`` (see ``WsEnvelope`` in
``openccu_loom_types.ws``). The ``type`` discriminator selects the
concrete payload schema; this module mirrors that on the Python side
by giving each known wire type its own ``LoomEvent`` subclass that
holds the parsed payload directly.

Consumers subscribe to a specific subclass on the :class:`EventBus`
and never touch the raw envelope — the bus and the dispatcher
between them handle the parse + dispatch in one step.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
from typing import Any, ClassVar, Final

from openccu_loom_types.rest import Kind2 as Kind
from openccu_loom_types.ws import (
    AddonUpdateStatus,
    AlarmCountdownPayload,
    AlarmHealthChangedPayload,
    AlarmJournalAppendedPayload,
    AlarmNotificationPayload,
    AlarmPanelChangedPayload,
    AlarmReadinessChangedPayload,
    AlarmReminderPayload,
    AlarmStateChangedPayload,
    AlarmTriggeredPayload,
    AlarmWalkTestProgressPayload,
    CentralReadinessChangedPayload,
    CentralStateChangedPayload,
    CustomDataPointStateChangedPayload,
    DaemonStatusPayload,
    DataPointValueChangedPayload,
    DeviceAvailabilityChangedPayload,
    DeviceCreatedPayload,
    DeviceMetadataChangedPayload,
    DeviceRemovedPayload,
    DeviceTriggerPayload,
    HubConnectivityChangedPayload,
    HubCountChangedPayload,
    HubMetricChangedPayload,
    HubSystemUpdateChangedPayload,
    InstallModeChangedPayload,
    MatterCommissioningProgressPayload,
    MatterCommissioningWindowResponse,
    MatterEndpointAssembledPayload,
    MatterExposureUpdate,
    MatterFabric,
    MatterFabricRemovedPayload,
    OptimisticRollbackPayload,
    ProgramChangedPayload,
    ProgramExecutedPayload,
    ScheduleChangedPayload,
    SecurityClassChangedPayload,
    SecurityFaultChangedPayload,
    SecurityNotificationPayload,
    SecurityStateChangedPayload,
    SecurityZoneChangedPayload,
    SystemStatusChangedPayload,
    SysvarChangedPayload,
    WsEnvelope,
)
from pydantic import BaseModel

from openccu_loom_client.canonical import canonical_unique_id

_LOGGER: Final = logging.getLogger(__name__)


def data_point_event_key(*, serial_suffix: str, device_address: str, channel: int | str, parameter: str) -> str:
    """
    Rebuild a generic data point's canonical HA routing key.

    ``homematicip_local`` subscribes to value-change events with
    ``event_key=data_point.unique_id`` (one subscription per HA entity).
    The compat data-point layer derives its ``unique_id`` from the same
    function, keeping the two ends in lock-step.

    The key is built on aiohomematic's reference algorithm (via
    ``openccu_loom_client.canonical``) —
    the loom-namespaced canonical key ``loom_<routing-key>``, with the CCU
    ``serial_suffix`` in the central-id slot (devices carry no prefix;
    internal/virtual-remote addresses do). This is the rebuild path; when
    a daemon payload carries ``unique_id`` directly, callers prefer that.
    """
    return canonical_unique_id(
        serial_suffix=serial_suffix,
        address=f"{device_address}:{channel}",
        parameter=parameter,
    )


@dataclass(slots=True, kw_only=True)
class LoomEvent:
    """
    Base class for every typed event.

    Carries the envelope metadata so subscribers can correlate by
    ``seq`` (e.g. discard out-of-order replays) or by ``kind``
    (e.g. ignore ``initial`` snapshots after the bootstrap phase).

    The class-level ``type_id`` field is the ``WsEnvelope.type`` value
    this subclass binds to — used by :func:`event_from_envelope` to
    pick the right subclass without a registry import cycle.

    ``event_key`` is the optional routing key the :class:`EventBus`
    matches subscribers against. Subclasses populate it in
    ``__post_init__`` from a payload field (typically the central name
    or device address) so callers can subscribe with
    ``event_key="<central>"`` to scope to one CCU.
    """

    seq: int
    kind: Kind
    ts: Any  # datetime — kept as Any to avoid forcing aware/naive choice on consumers
    topic: str | None = None
    type: str = ""
    event_key: str | None = None

    # Concrete subclasses set this. The base class binds the empty
    # string so a bare LoomEvent never wins a dispatch round.
    type_id: ClassVar[str] = ""


@dataclass(slots=True, kw_only=True)
class DataPointValueChangedEvent(LoomEvent):
    """A single CCU data-point value changed (or refreshed)."""

    payload: DataPointValueChangedPayload
    type_id: ClassVar[str] = "datapoint.value_changed"

    def __post_init__(self) -> None:
        """Default the routing key to the payload's canonical unique id."""
        # Keyed by the daemon-supplied canonical ``unique_id`` so a
        # homematicip_local entity that subscribes with
        # ``event_key=data_point.unique_id`` receives exactly its own
        # value changes. The store→bridge handler subscribes without an
        # event_key, so it still sees every value change. (For a daemon
        # that omits ``unique_id``, the compat refresh bridge rebuilds the
        # key from the store's serial suffix.)
        if self.event_key is None:
            self.event_key = self.payload.unique_id

    @property
    def device_address(self) -> str:
        """Return the address of the device that owns this data point."""
        return self.payload.device_address

    @property
    def parameter(self) -> str:
        """Return the data point's parameter name."""
        return self.payload.parameter


@dataclass(slots=True, kw_only=True)
class CustomDataPointStateChangedEvent(LoomEvent):
    """Aggregated CDP-state snapshot (one event per CDP, not per wire DP)."""

    payload: CustomDataPointStateChangedPayload
    type_id: ClassVar[str] = "custom_data_point.state_changed"

    def __post_init__(self) -> None:
        """Default the routing key to the payload's central name."""
        if self.event_key is None:
            self.event_key = self.payload.central


@dataclass(slots=True, kw_only=True)
class CentralStateChangedEvent(LoomEvent):
    """The CentralUnit advanced through its lifecycle state machine."""

    payload: CentralStateChangedPayload
    type_id: ClassVar[str] = "central.state_changed"

    def __post_init__(self) -> None:
        """Default the routing key to the payload's central name."""
        if self.event_key is None:
            self.event_key = self.payload.central


@dataclass(slots=True, kw_only=True)
class CentralReadinessChangedEvent(LoomEvent):
    """
    The central's southbound bring-up advanced (daemon api 2.19.0).

    Reports the bring-up ``phase`` (``waiting_for_ccu`` → ``loading_hub`` →
    ``loading_devices`` → ``ready``), the latched ``ready`` flag and the
    interface-wiring progress. Consumers can gate on ``ready`` instead of
    inferring readiness from the lifecycle state alone.
    """

    payload: CentralReadinessChangedPayload
    type_id: ClassVar[str] = "central.readiness_changed"

    def __post_init__(self) -> None:
        """Default the routing key to the payload's central name."""
        if self.event_key is None:
            self.event_key = self.payload.central


@dataclass(slots=True, kw_only=True)
class SystemStatusChangedEvent(LoomEvent):
    """Aggregated system-health snapshot (interfaces, connectivity, …)."""

    payload: SystemStatusChangedPayload
    type_id: ClassVar[str] = "system.status_changed"

    def __post_init__(self) -> None:
        """Default the routing key to the payload's central name."""
        if self.event_key is None:
            self.event_key = self.payload.central


@dataclass(slots=True, kw_only=True)
class DaemonStatusChangedEvent(LoomEvent):
    """
    The daemon announces that it is stopping (daemon api ≥ 7.6.0).

    Rides the daemon-level topic ``system.daemon_status`` — not scoped to
    a central, so it carries no routing key. It is the WebSocket
    counterpart of the last will an MQTT broker retains: without it a
    stopping daemon and a dropped connection look identical to a client.

    Only a graceful stop can announce itself. A killed process never
    sends it, so a consumer still has to treat a silent connection loss
    as "gone" on its own.
    """

    payload: DaemonStatusPayload
    type_id: ClassVar[str] = "daemon_status.changed"


@dataclass(slots=True, kw_only=True)
class SysvarChangedEvent(LoomEvent):
    """A CCU system variable's value changed."""

    payload: SysvarChangedPayload
    type_id: ClassVar[str] = "hub.sysvar_changed"

    def __post_init__(self) -> None:
        """Default the routing key to the payload's central name."""
        if self.event_key is None:
            self.event_key = self.payload.central


@dataclass(slots=True, kw_only=True)
class ProgramExecutedEvent(LoomEvent):
    """A CCU program was triggered (by us, by HA, by the CCU itself)."""

    payload: ProgramExecutedPayload
    type_id: ClassVar[str] = "hub.program_executed"

    def __post_init__(self) -> None:
        """Default the routing key to the payload's central name."""
        if self.event_key is None:
            self.event_key = self.payload.central


@dataclass(slots=True, kw_only=True)
class ProgramChangedEvent(LoomEvent):
    """
    A CCU program's activity flag changed.

    A program is two controls: the flag decides whether it reacts at all,
    and the execution runs it once. The CCU refuses the execution while
    the flag is off, so ``execute_available`` travels with it and both of
    the program's entities re-render off one message.
    """

    payload: ProgramChangedPayload
    type_id: ClassVar[str] = "hub.program_changed"

    def __post_init__(self) -> None:
        """Default the routing key to the payload's central name."""
        if self.event_key is None:
            self.event_key = self.payload.central


@dataclass(slots=True, kw_only=True)
class InstallModeChangedEvent(LoomEvent):
    """
    The CCU pairing window opened or closed.

    ``homematicip_local`` mirrors install-mode state to HA so the user
    sees whether the CCU is currently accepting new devices. The push
    keeps that in sync without polling ``GET /install-mode``.
    """

    payload: InstallModeChangedPayload
    type_id: ClassVar[str] = "hub.install_mode_changed"

    def __post_init__(self) -> None:
        """Default the routing key to the payload's central name."""
        if self.event_key is None:
            self.event_key = self.payload.central


@dataclass(slots=True, kw_only=True)
class HubAlarmMessageCountChangedEvent(LoomEvent):
    """The CCU alarm-message count changed (push carries the count only)."""

    payload: HubCountChangedPayload
    type_id: ClassVar[str] = "hub.alarm_message"

    def __post_init__(self) -> None:
        """Default the routing key to the payload's central name."""
        if self.event_key is None:
            self.event_key = self.payload.central


@dataclass(slots=True, kw_only=True)
class HubServiceMessageCountChangedEvent(LoomEvent):
    """The CCU service-message count changed (push carries the count only)."""

    payload: HubCountChangedPayload
    type_id: ClassVar[str] = "hub.service_message"

    def __post_init__(self) -> None:
        """Default the routing key to the payload's central name."""
        if self.event_key is None:
            self.event_key = self.payload.central


@dataclass(slots=True, kw_only=True)
class HubInboxChangedEvent(LoomEvent):
    """The CCU inbox count changed (push carries the count only)."""

    payload: HubCountChangedPayload
    type_id: ClassVar[str] = "hub.inbox_changed"

    def __post_init__(self) -> None:
        """Default the routing key to the payload's central name."""
        if self.event_key is None:
            self.event_key = self.payload.central


@dataclass(slots=True, kw_only=True)
class HubMetricsChangedEvent(LoomEvent):
    """One CCU health/latency/age metric changed value."""

    payload: HubMetricChangedPayload
    type_id: ClassVar[str] = "hub.metrics_changed"

    def __post_init__(self) -> None:
        """Default the routing key to the payload's central name."""
        if self.event_key is None:
            self.event_key = self.payload.central


@dataclass(slots=True, kw_only=True)
class HubConnectivityChangedEvent(LoomEvent):
    """One interface's reachability (and optional latency) changed."""

    payload: HubConnectivityChangedPayload
    type_id: ClassVar[str] = "connectivity.changed"

    def __post_init__(self) -> None:
        """Default the routing key to the payload's central name."""
        if self.event_key is None:
            self.event_key = self.payload.central


@dataclass(slots=True, kw_only=True)
class HubSystemUpdateChangedEvent(LoomEvent):
    """The CCU firmware/system-update state changed (daemon api ≥ 1.19.0)."""

    payload: HubSystemUpdateChangedPayload
    type_id: ClassVar[str] = "hub.system_update_changed"

    def __post_init__(self) -> None:
        """Default the routing key to the payload's central name."""
        if self.event_key is None:
            self.event_key = self.payload.central


@dataclass(slots=True, kw_only=True)
class AddonUpdateStateChangedEvent(LoomEvent):
    """
    The daemon's add-on self-updater changed state (daemon api ≥ 3.3.0).

    Rides the ``system.addon_update`` topic: an update check finished, a
    download/install is progressing, or the updater failed. The add-on is
    the daemon's own package — daemon-global rather than per-central — so
    no routing key is set. Only emitted on platforms with the firmware-side
    installer (OpenCCU / RaspberryMatic); the REST surface
    (``GET /system/addon-update``) shares the same payload model.
    """

    payload: AddonUpdateStatus
    type_id: ClassVar[str] = "addon_update.state_changed"


@dataclass(slots=True, kw_only=True)
class DeviceCreatedEvent(LoomEvent):
    """
    A new device was paired and is now part of the registry.

    Forward-compatible with the daemon's deferred lifecycle-broadcast
    ask: the payload schema ships in 0.1.2 even though the broadcast
    isn't announced in wsapi.json yet.
    """

    payload: DeviceCreatedPayload
    type_id: ClassVar[str] = "device.created"

    def __post_init__(self) -> None:
        """Default the routing key to the payload's central name."""
        if self.event_key is None:
            self.event_key = self.payload.central


@dataclass(slots=True, kw_only=True)
class DeviceRemovedEvent(LoomEvent):
    """A device was unpaired / removed from the registry."""

    payload: DeviceRemovedPayload
    type_id: ClassVar[str] = "device.removed"

    def __post_init__(self) -> None:
        """Default the routing key to the payload's central name."""
        if self.event_key is None:
            self.event_key = self.payload.central


@dataclass(slots=True, kw_only=True)
class DeviceAvailabilityChangedEvent(LoomEvent):
    """
    A device's effective reachability flipped (daemon api ≥ 5.27.0).

    Rides the same ``device.{address}.lifecycle`` topic as
    ``device.created`` — subscribers route by the envelope ``type``.
    Fires for both causes: an interface that lost its CCU connection,
    and a device reporting UNREACH / STICKY_UNREACH on its own.
    ``available`` carries the post-transition state and matches the
    ``available`` field of the device's REST summary.
    """

    payload: DeviceAvailabilityChangedPayload
    type_id: ClassVar[str] = "device.availability_changed"

    def __post_init__(self) -> None:
        """Default the routing key to the payload's central name."""
        if self.event_key is None:
            self.event_key = self.payload.central


@dataclass(slots=True, kw_only=True)
class DeviceMetadataChangedEvent(LoomEvent):
    """
    A device was renamed or re-assigned (daemon api ≥ 7.6.0).

    Fires for a device or one of its channels being renamed, and for a
    changed room / function assignment.

    Rides the same ``device.{address}.lifecycle`` topic as
    ``device.created`` — subscribers route by the envelope ``type``.
    ``device_address`` is always the DEVICE address even when a channel
    changed, because a consumer materialises a device's name and area as
    one unit. The new values are not inlined: the payload says *what* to
    re-read, and the client re-reads the device detail.
    """

    payload: DeviceMetadataChangedPayload
    type_id: ClassVar[str] = "device.metadata_changed"

    def __post_init__(self) -> None:
        """Default the routing key to the payload's central name."""
        if self.event_key is None:
            self.event_key = self.payload.central


@dataclass(slots=True, kw_only=True)
class ScheduleChangedEvent(LoomEvent):
    """
    A channel's week profile changed (daemon api ≥ 7.6.0).

    Written through this daemon, or observed on the CCU. Rides the
    ``device.{address}.lifecycle`` topic. The profile body is not
    inlined — a week profile is large and a subscriber usually only
    needs to invalidate and re-read the channel's schedule.
    """

    payload: ScheduleChangedPayload
    type_id: ClassVar[str] = "schedules.changed"

    def __post_init__(self) -> None:
        """Default the routing key to the payload's central name."""
        if self.event_key is None:
            self.event_key = self.payload.central


@dataclass(slots=True, kw_only=True)
class DeviceTriggerEvent(LoomEvent):
    """
    A non-state device event (keypress, impulse, device error).

    Rides the ``device.{address}.channels.{channel}.trigger`` topic.
    Distinct from a value change: the CCU reports a momentary event
    (``event_type`` is one of ``DeviceTriggerEventType``) rather than a
    persisted value. Keyed by the per-data-point routing key so a
    subscriber can scope to one (device, channel, parameter).
    """

    payload: DeviceTriggerPayload
    type_id: ClassVar[str] = "device.trigger"

    def __post_init__(self) -> None:
        """Default the routing key to the payload's canonical unique id."""
        if self.event_key is None:
            self.event_key = self.payload.unique_id


@dataclass(slots=True, kw_only=True)
class DataPointOptimisticRolledBackEvent(LoomEvent):
    """
    An optimistic write was rolled back (TTL expiry or CCU rejection).

    The raw daemon broadcast. Rides the same per-data-point topic as
    ``datapoint.value_changed``, so it is keyed identically. The compat
    refresh bridge translates it into the public, aiohomematic-shaped
    :class:`~openccu_loom_client.events.synthetic.OptimisticRollbackEvent`
    that HA subscribes to (mirroring how ``datapoint.value_changed`` is
    bridged to ``DataPointStateChangedEvent``).
    """

    payload: OptimisticRollbackPayload
    type_id: ClassVar[str] = "datapoint.optimistic_rolled_back"

    def __post_init__(self) -> None:
        """Default the routing key to the payload's canonical unique id."""
        if self.event_key is None:
            self.event_key = self.payload.unique_id


@dataclass(slots=True, kw_only=True)
class AlarmStateChangedEvent(LoomEvent):
    """
    An alarm zone's arm-state machine advanced (daemon api ≥ 2.22.0).

    Carries the ``old_state`` → ``new_state`` transition plus the active
    ``mode`` and — on a trigger — the ``incident_id``. Keyed by
    ``zone_id`` so a subscriber can scope to one zone (the payload field
    was ``area_id`` before the api 3.0.0 rename); the compat refresh
    bridge resolves the zone to its panel entity.
    """

    payload: AlarmStateChangedPayload
    type_id: ClassVar[str] = "alarm.state_changed"

    def __post_init__(self) -> None:
        """Default the routing key to the payload's zone id."""
        if self.event_key is None:
            self.event_key = self.payload.zone_id


@dataclass(slots=True, kw_only=True)
class AlarmCountdownEvent(LoomEvent):
    """An exit/entry countdown tick for one alarm zone."""

    payload: AlarmCountdownPayload
    type_id: ClassVar[str] = "alarm.countdown"

    def __post_init__(self) -> None:
        """Default the routing key to the payload's zone id."""
        if self.event_key is None:
            self.event_key = self.payload.zone_id


@dataclass(slots=True, kw_only=True)
class AlarmReadinessChangedEvent(LoomEvent):
    """One alarm zone's per-mode readiness (blockers/warnings) changed."""

    payload: AlarmReadinessChangedPayload
    type_id: ClassVar[str] = "alarm.readiness_changed"

    def __post_init__(self) -> None:
        """Default the routing key to the payload's zone id."""
        if self.event_key is None:
            self.event_key = self.payload.zone_id


@dataclass(slots=True, kw_only=True)
class AlarmTriggeredEvent(LoomEvent):
    """An alarm zone entered the triggered state (new incident)."""

    payload: AlarmTriggeredPayload
    type_id: ClassVar[str] = "alarm.triggered"

    def __post_init__(self) -> None:
        """Default the routing key to the payload's zone id."""
        if self.event_key is None:
            self.event_key = self.payload.zone_id


@dataclass(slots=True, kw_only=True)
class AlarmJournalAppendedEvent(LoomEvent):
    """
    A new alarm-journal entry was written.

    ``zone_id`` is ``None`` for engine-global entries, so the routing
    key stays unset for those — zone-scoped subscribers only see their
    own entries, unscoped subscribers see everything.
    """

    payload: AlarmJournalAppendedPayload
    type_id: ClassVar[str] = "alarm.journal_appended"

    def __post_init__(self) -> None:
        """Default the routing key to the payload's zone id (if any)."""
        if self.event_key is None:
            self.event_key = self.payload.zone_id


@dataclass(slots=True, kw_only=True)
class AlarmWalkTestProgressEvent(LoomEvent):
    """A sensor was seen during an active walk test."""

    payload: AlarmWalkTestProgressPayload
    type_id: ClassVar[str] = "alarm.walktest_progress"

    def __post_init__(self) -> None:
        """Default the routing key to the payload's zone id."""
        if self.event_key is None:
            self.event_key = self.payload.zone_id


@dataclass(slots=True, kw_only=True)
class AlarmHealthChangedEvent(LoomEvent):
    """The alarm engine's overall health flag flipped (engine-global)."""

    payload: AlarmHealthChangedPayload
    type_id: ClassVar[str] = "alarm.health_changed"


@dataclass(slots=True, kw_only=True)
class AlarmPanelChangedEvent(LoomEvent):
    """
    A panel entity changed (state/availability) or was added/removed.

    Keyed by the daemon-computed panel ``unique_id`` — the same key the
    compat layer hands HA entities, so a panel entity can subscribe to
    exactly its own changes (mirroring ``datapoint.value_changed``).
    """

    payload: AlarmPanelChangedPayload
    type_id: ClassVar[str] = "alarm.panel_changed"

    def __post_init__(self) -> None:
        """Default the routing key to the payload's panel unique id."""
        if self.event_key is None:
            self.event_key = self.payload.unique_id


@dataclass(slots=True, kw_only=True)
class AlarmReminderEvent(LoomEvent):
    """An arm-schedule reminder fired for one alarm zone."""

    payload: AlarmReminderPayload
    type_id: ClassVar[str] = "alarm.reminder"

    def __post_init__(self) -> None:
        """Default the routing key to the payload's zone id."""
        if self.event_key is None:
            self.event_key = self.payload.zone_id


@dataclass(slots=True, kw_only=True)
class AlarmNotificationEvent(LoomEvent):
    """
    A notification-class alarm output fired (daemon ≥ 0.43.1).

    One-shot, per-zone and mode-filtered at fire time — never cancelled
    by a later silence. Consumers use it for user-land escalation
    (push message, logbook entry); it carries no panel state.
    """

    payload: AlarmNotificationPayload
    type_id: ClassVar[str] = "alarm.notification"

    def __post_init__(self) -> None:
        """Default the routing key to the payload's zone id."""
        if self.event_key is None:
            self.event_key = self.payload.zone_id


@dataclass(slots=True, kw_only=True)
class SecurityStateChangedEvent(LoomEvent):
    """
    The folded severity of the Security & Safety domain changed.

    Carries the fold only — the classes contributing to it and the
    standing fault count, not the detail. A consumer that needs the
    per-class source lists reads ``GET /security``.
    """

    payload: SecurityStateChangedPayload
    type_id: ClassVar[str] = "security.state_changed"


@dataclass(slots=True, kw_only=True)
class SecurityClassChangedEvent(LoomEvent):
    """
    One hazard or fault class went active or inactive.

    Also fires when the source set changes while the class stays
    active: a second smoke detector joining an existing fire is a
    change worth announcing even though the class was already on.

    The routing key is the class, so a consumer can subscribe with
    ``event_key="smoke"`` and never see a battery warning.
    """

    payload: SecurityClassChangedPayload
    type_id: ClassVar[str] = "security.class_changed"

    def __post_init__(self) -> None:
        """Default the routing key to the hazard/fault class."""
        if self.event_key is None:
            self.event_key = self.payload.class_


@dataclass(slots=True, kw_only=True)
class SecurityZoneChangedEvent(LoomEvent):
    """
    One alarm zone's security view changed.

    Never arrives on an installation without an alarm engine, where
    the domain still reports classes and faults.
    """

    payload: SecurityZoneChangedPayload
    type_id: ClassVar[str] = "security.zone_changed"

    def __post_init__(self) -> None:
        """Default the routing key to the payload's zone id."""
        if self.event_key is None:
            self.event_key = self.payload.zone_id


@dataclass(slots=True, kw_only=True)
class SecurityFaultChangedEvent(LoomEvent):
    """
    A fault opened, cleared or was acknowledged.

    ``acknowledged`` marks the third case: the condition is unchanged,
    the operator has merely stopped needing to be told. ``open_count``
    carries the standing count after the change, so a count entity
    needs no second read.
    """

    payload: SecurityFaultChangedPayload
    type_id: ClassVar[str] = "security.fault_changed"

    def __post_init__(self) -> None:
        """Default the routing key to the fault id."""
        if self.event_key is None:
            self.event_key = self.payload.fault_id


@dataclass(slots=True, kw_only=True)
class SecurityNotificationEvent(LoomEvent):
    """
    One rendered Security & Safety report.

    The only payload in the domain that carries prose, plus the i18n
    key and args to re-render it in the consumer's own locale.
    ``fault`` separates a fault report from a hazard report so the two
    can be routed apart without inspecting the class.

    A covert report (duress code, silent panic) reaches this broadcast
    only when the daemon runs ``alarm.duress_visibility: full``: the
    WebSocket is a local screen surface, and a wall tablet showing
    "duress code entered" defeats the covert trigger it reports. Under
    the other levels the report still reaches the daemon's webhook and
    raw MQTT event topic — it simply never reaches this client.
    """

    payload: SecurityNotificationPayload
    type_id: ClassVar[str] = "security.notification"

    def __post_init__(self) -> None:
        """Default the routing key to the hazard/fault class."""
        if self.event_key is None:
            self.event_key = self.payload.class_


@dataclass(slots=True, kw_only=True)
class MatterCommissioningProgressEvent(LoomEvent):
    """Progress update while a Matter device is being commissioned."""

    payload: MatterCommissioningProgressPayload
    type_id: ClassVar[str] = "matter.commissioning_progress"


@dataclass(slots=True, kw_only=True)
class MatterCommissioningWindowOpenedEvent(LoomEvent):
    """A Matter commissioning window was opened for pairing."""

    payload: MatterCommissioningWindowResponse
    type_id: ClassVar[str] = "matter.commissioning_window_opened"


@dataclass(slots=True, kw_only=True)
class MatterEndpointAssembledEvent(LoomEvent):
    """A Matter endpoint finished assembling from its CCU channels."""

    payload: MatterEndpointAssembledPayload
    type_id: ClassVar[str] = "matter.endpoint_assembled"


@dataclass(slots=True, kw_only=True)
class MatterExposableChangedEvent(LoomEvent):
    """A device's Matter exposability flag changed."""

    payload: MatterExposureUpdate
    type_id: ClassVar[str] = "matter.exposable_changed"


@dataclass(slots=True, kw_only=True)
class MatterFabricAddedEvent(LoomEvent):
    """A Matter fabric was added (a controller joined)."""

    payload: MatterFabric
    type_id: ClassVar[str] = "matter.fabric_added"


@dataclass(slots=True, kw_only=True)
class MatterFabricRemovedEvent(LoomEvent):
    """A Matter fabric was removed (a controller left)."""

    payload: MatterFabricRemovedPayload
    type_id: ClassVar[str] = "matter.fabric_removed"


@dataclass(slots=True, kw_only=True)
class UnknownLoomEvent(LoomEvent):
    """
    A broadcast whose ``type`` we don't know about (yet).

    Emitted instead of dropping so forward-compat with a newer daemon
    is observable rather than silent. ``raw_payload`` is whatever the
    envelope carried — typically a dict, but the daemon contract says
    nothing about the shape, so consumers should treat it as opaque.
    """

    raw_payload: Any = None
    type_id: ClassVar[str] = ""


# Registry: wire ``type`` string → (event class, payload class). The
# payload class is a Pydantic model from openccu_loom_types.ws; the
# event class is the dataclass wrapper above.
_EVENT_REGISTRY: Final[dict[str, tuple[Callable[..., LoomEvent], type[BaseModel]]]] = {
    DataPointValueChangedEvent.type_id: (DataPointValueChangedEvent, DataPointValueChangedPayload),
    CustomDataPointStateChangedEvent.type_id: (
        CustomDataPointStateChangedEvent,
        CustomDataPointStateChangedPayload,
    ),
    CentralStateChangedEvent.type_id: (CentralStateChangedEvent, CentralStateChangedPayload),
    CentralReadinessChangedEvent.type_id: (CentralReadinessChangedEvent, CentralReadinessChangedPayload),
    SystemStatusChangedEvent.type_id: (SystemStatusChangedEvent, SystemStatusChangedPayload),
    DaemonStatusChangedEvent.type_id: (DaemonStatusChangedEvent, DaemonStatusPayload),
    SysvarChangedEvent.type_id: (SysvarChangedEvent, SysvarChangedPayload),
    ProgramExecutedEvent.type_id: (ProgramExecutedEvent, ProgramExecutedPayload),
    ProgramChangedEvent.type_id: (ProgramChangedEvent, ProgramChangedPayload),
    InstallModeChangedEvent.type_id: (InstallModeChangedEvent, InstallModeChangedPayload),
    HubAlarmMessageCountChangedEvent.type_id: (HubAlarmMessageCountChangedEvent, HubCountChangedPayload),
    HubServiceMessageCountChangedEvent.type_id: (HubServiceMessageCountChangedEvent, HubCountChangedPayload),
    HubInboxChangedEvent.type_id: (HubInboxChangedEvent, HubCountChangedPayload),
    HubMetricsChangedEvent.type_id: (HubMetricsChangedEvent, HubMetricChangedPayload),
    HubConnectivityChangedEvent.type_id: (HubConnectivityChangedEvent, HubConnectivityChangedPayload),
    HubSystemUpdateChangedEvent.type_id: (HubSystemUpdateChangedEvent, HubSystemUpdateChangedPayload),
    AddonUpdateStateChangedEvent.type_id: (AddonUpdateStateChangedEvent, AddonUpdateStatus),
    DeviceCreatedEvent.type_id: (DeviceCreatedEvent, DeviceCreatedPayload),
    DeviceRemovedEvent.type_id: (DeviceRemovedEvent, DeviceRemovedPayload),
    DeviceAvailabilityChangedEvent.type_id: (
        DeviceAvailabilityChangedEvent,
        DeviceAvailabilityChangedPayload,
    ),
    DeviceMetadataChangedEvent.type_id: (DeviceMetadataChangedEvent, DeviceMetadataChangedPayload),
    ScheduleChangedEvent.type_id: (ScheduleChangedEvent, ScheduleChangedPayload),
    DeviceTriggerEvent.type_id: (DeviceTriggerEvent, DeviceTriggerPayload),
    DataPointOptimisticRolledBackEvent.type_id: (
        DataPointOptimisticRolledBackEvent,
        OptimisticRollbackPayload,
    ),
    AlarmStateChangedEvent.type_id: (AlarmStateChangedEvent, AlarmStateChangedPayload),
    AlarmCountdownEvent.type_id: (AlarmCountdownEvent, AlarmCountdownPayload),
    AlarmReadinessChangedEvent.type_id: (AlarmReadinessChangedEvent, AlarmReadinessChangedPayload),
    AlarmTriggeredEvent.type_id: (AlarmTriggeredEvent, AlarmTriggeredPayload),
    AlarmJournalAppendedEvent.type_id: (AlarmJournalAppendedEvent, AlarmJournalAppendedPayload),
    AlarmWalkTestProgressEvent.type_id: (AlarmWalkTestProgressEvent, AlarmWalkTestProgressPayload),
    AlarmHealthChangedEvent.type_id: (AlarmHealthChangedEvent, AlarmHealthChangedPayload),
    AlarmPanelChangedEvent.type_id: (AlarmPanelChangedEvent, AlarmPanelChangedPayload),
    AlarmReminderEvent.type_id: (AlarmReminderEvent, AlarmReminderPayload),
    AlarmNotificationEvent.type_id: (AlarmNotificationEvent, AlarmNotificationPayload),
    SecurityStateChangedEvent.type_id: (SecurityStateChangedEvent, SecurityStateChangedPayload),
    SecurityClassChangedEvent.type_id: (SecurityClassChangedEvent, SecurityClassChangedPayload),
    SecurityZoneChangedEvent.type_id: (SecurityZoneChangedEvent, SecurityZoneChangedPayload),
    SecurityFaultChangedEvent.type_id: (SecurityFaultChangedEvent, SecurityFaultChangedPayload),
    SecurityNotificationEvent.type_id: (SecurityNotificationEvent, SecurityNotificationPayload),
    MatterCommissioningProgressEvent.type_id: (
        MatterCommissioningProgressEvent,
        MatterCommissioningProgressPayload,
    ),
    MatterCommissioningWindowOpenedEvent.type_id: (
        MatterCommissioningWindowOpenedEvent,
        MatterCommissioningWindowResponse,
    ),
    MatterEndpointAssembledEvent.type_id: (
        MatterEndpointAssembledEvent,
        MatterEndpointAssembledPayload,
    ),
    MatterExposableChangedEvent.type_id: (MatterExposableChangedEvent, MatterExposureUpdate),
    MatterFabricAddedEvent.type_id: (MatterFabricAddedEvent, MatterFabric),
    MatterFabricRemovedEvent.type_id: (MatterFabricRemovedEvent, MatterFabricRemovedPayload),
}


def known_event_types() -> frozenset[str]:
    """
    Wire ``type`` strings this client knows how to deserialize.

    Useful for tests that assert a daemon's broadcast catalogue is
    fully covered, and for log lines that explain why an
    :class:`UnknownLoomEvent` was emitted.
    """
    return frozenset(_EVENT_REGISTRY)


def event_from_envelope(*, envelope: WsEnvelope) -> LoomEvent:
    """
    Convert a wire-level ``WsEnvelope`` into a typed event.

    Unknown ``type`` strings yield :class:`UnknownLoomEvent` — the
    raw payload is preserved so log analysis can recover it. Payload
    validation failures are logged but never raised: dropping a
    single malformed frame is better than killing the read loop.
    """
    binding = _EVENT_REGISTRY.get(envelope.type)
    common_kwargs: dict[str, Any] = {
        "seq": envelope.seq,
        "kind": envelope.kind,
        "ts": envelope.ts,
        "topic": envelope.topic,
        "type": envelope.type,
    }
    if binding is None:
        return UnknownLoomEvent(raw_payload=envelope.payload, **common_kwargs)

    event_cls, payload_cls = binding
    try:
        payload = payload_cls.model_validate(envelope.payload)
    except Exception as exc:  # noqa: BLE001 — any validation error degrades to UnknownLoomEvent
        _LOGGER.warning(
            "payload validation failed for %s (seq=%s): %s — emitting UnknownLoomEvent",
            envelope.type,
            envelope.seq,
            exc,
        )
        return UnknownLoomEvent(raw_payload=envelope.payload, **common_kwargs)
    return event_cls(payload=payload, **common_kwargs)
