# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
``aiohomematic.central.events``-compatible event-class surface.

Each name here either aliases one of the openccu-loom-client event
classes or wraps the underlying classes with the shape aiohomematic
exposed.

The most important compat note is ``DataPointStateChangedEvent`` —
aiohomematic used that name for what the daemon calls
``DataPointValueChangedEvent``. We alias it so HA-side subscribers
keep working.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar, Final

from openccu_loom_types.rest import Kind1 as Kind

from openccu_loom_client.events import (
    CentralStateChangedEvent,
    DataPointsCreatedEvent,
    DeviceCreatedEvent,
    DeviceRemovedEvent,
    EventBus,
    LoomEvent,
    OptimisticRollbackEvent,
    SubscriptionGroup,
    SystemStatusChangedEvent,
    UnsubscribeCallback,
)


@dataclass(slots=True, kw_only=True)
class DataPointStateChangedEvent(LoomEvent):
    """
    Uniform "a data point's state changed" notification, keyed by unique_id.

    Aiohomematic emits one event class for every data-point value change
    (generic, custom or hub) and HA entities subscribe to it with
    ``event_key=data_point.unique_id``. The daemon instead emits three
    distinct typed events (``datapoint.value_changed``,
    ``custom_data_point.state_changed``, ``hub.sysvar_changed``); the
    refresh bridge (:func:`...central.refresh.install_refresh_bridge`)
    fans all three into this single event so the entity-side
    subscription contract is satisfied uniformly.
    """

    data_point: Any = None
    type_id: ClassVar[str] = "client.data_point_state_changed"


class DeviceLifecycleEventType(StrEnum):
    """Subset of lifecycle transitions HA needs from the umbrella event."""

    CREATED = "created"
    REMOVED = "removed"
    AVAILABILITY_CHANGED = "availability_changed"


@dataclass(slots=True, kw_only=True)
class DeviceLifecycleEvent(LoomEvent):
    """
    Umbrella event matching aiohomematic's DeviceLifecycleEvent shape.

    Aiohomematic emitted one event class for "device added / removed /
    became (un)available" with a ``event_type`` discriminator. The
    daemon splits these into separate broadcasts
    (``device.created`` / ``device.removed`` / device-level
    ``system.status_changed``) — see ADR-0020. This class still
    exists so HA-side subscribers that subscribe to
    ``DeviceLifecycleEvent`` keep working; the higher-level client
    is expected to fan the typed broadcasts into one
    ``DeviceLifecycleEvent`` (left as a follow-up — phase 7).
    """

    event_type: DeviceLifecycleEventType
    device_address: str
    device_name: str | None = None
    interface_id: str | None = None
    central: str | None = None
    type_id: ClassVar[str] = "client.device_lifecycle"

    def __post_init__(self) -> None:
        """Default the routing ``event_key`` to the central id when unset."""
        if self.event_key is None and self.central is not None:
            self.event_key = self.central


@dataclass(slots=True, kw_only=True)
class DeviceTriggerEvent(LoomEvent):
    """
    Click / impulse / device-error event in aiohomematic shape.

    Carries the device + channel + parameter triple plus the trigger
    subtype string. The daemon's ``Keypress`` broadcast (single
    ``DeviceTriggerEventType.Keypress`` value with the subtype in the
    payload) is the wire source.
    """

    event_subtype: str
    device_address: str
    device_name: str | None = None
    channel_no: int | None = None
    parameter: str | None = None
    interface_id: str | None = None
    central: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    type_id: ClassVar[str] = "client.device_trigger"

    def __post_init__(self) -> None:
        """Default the routing ``event_key`` to the central id when unset."""
        if self.event_key is None and self.central is not None:
            self.event_key = self.central


__all__: Final = [
    # General
    "CentralStateChangedEvent",
    "DataPointStateChangedEvent",
    "DataPointsCreatedEvent",
    "DeviceCreatedEvent",
    "DeviceLifecycleEvent",
    "DeviceLifecycleEventType",
    "DeviceRemovedEvent",
    "DeviceTriggerEvent",
    "EventBus",
    "Kind",
    "LoomEvent",
    "OptimisticRollbackEvent",
    "SubscriptionGroup",
    "SystemStatusChangedEvent",
    "UnsubscribeCallback",
]
