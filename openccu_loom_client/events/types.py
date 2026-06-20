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

from openccu_loom_types.rest import Kind1 as Kind
from openccu_loom_types.ws import (
    CentralStateChangedPayload,
    CustomDataPointStateChangedPayload,
    DataPointValueChangedPayload,
    DeviceCreatedPayload,
    DeviceRemovedPayload,
    DeviceTriggerPayload,
    InstallModeChangedPayload,
    MatterCommissioningProgressPayload,
    MatterCommissioningWindowResponse,
    MatterEndpointAssembledPayload,
    MatterExposureUpdate,
    MatterFabric,
    MatterFabricRemovedPayload,
    OptimisticRollbackPayload,
    ProgramExecutedPayload,
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
class SystemStatusChangedEvent(LoomEvent):
    """Aggregated system-health snapshot (interfaces, connectivity, …)."""

    payload: SystemStatusChangedPayload
    type_id: ClassVar[str] = "system.status_changed"

    def __post_init__(self) -> None:
        """Default the routing key to the payload's central name."""
        if self.event_key is None:
            self.event_key = self.payload.central


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
    SystemStatusChangedEvent.type_id: (SystemStatusChangedEvent, SystemStatusChangedPayload),
    SysvarChangedEvent.type_id: (SysvarChangedEvent, SysvarChangedPayload),
    ProgramExecutedEvent.type_id: (ProgramExecutedEvent, ProgramExecutedPayload),
    InstallModeChangedEvent.type_id: (InstallModeChangedEvent, InstallModeChangedPayload),
    DeviceCreatedEvent.type_id: (DeviceCreatedEvent, DeviceCreatedPayload),
    DeviceRemovedEvent.type_id: (DeviceRemovedEvent, DeviceRemovedPayload),
    DeviceTriggerEvent.type_id: (DeviceTriggerEvent, DeviceTriggerPayload),
    DataPointOptimisticRolledBackEvent.type_id: (
        DataPointOptimisticRolledBackEvent,
        OptimisticRollbackPayload,
    ),
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
