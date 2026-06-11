# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
``ChannelEventGroup`` — aiohomematic's device-trigger event-group surface.

HA spawns one ``event`` entity per (channel, device-trigger-event-type)
group and reads it back during orphan cleanup, so the compat layer builds
the same groups from the store's trigger-capable data points.

A data point is a device-trigger event when its parameter is a known
keypress / impulse / device-error parameter (the CCU parameter-name sets
below) and it emits events. Groups are keyed per channel by
``DeviceTriggerEventType``; the ``unique_id`` mirrors aiohomematic's
``event_group_{short}_{channel_unique_id}`` format exactly so HA entity
identities stay in lock-step.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from openccu_loom_types.enums import DataPointCategory, DataPointUsage, DeviceTriggerEventType

from openccu_loom_client.canonical import LOOM_NAMESPACE, generate_channel_unique_id

if TYPE_CHECKING:
    from openccu_loom_client.model import Channel, DataPoint
    from openccu_loom_client.store import LoomStore

# CCU parameter-name sets per device-trigger event type (mirrors
# aiohomematic.const CLICK_EVENTS / IMPULSE_EVENTS / DEVICE_ERROR_EVENTS).
_CLICK_PARAMS = frozenset(
    {
        "PRESS",
        "PRESS_CONT",
        "PRESS_LOCK",
        "PRESS_LONG",
        "PRESS_LONG_RELEASE",
        "PRESS_LONG_START",
        "PRESS_SHORT",
        "PRESS_UNLOCK",
    }
)
_IMPULSE_PARAMS = frozenset({"SEQUENCE_OK"})
_DEVICE_ERROR_PARAMS = frozenset({"ERROR", "SENSOR_ERROR"})

# Usage verdicts that exclude a DP from event groups. Mirrors the
# reference stack's creation gate: a suppressed parameter (e.g. HmIP-PS
# click events via IGNORE_DEVICES_FOR_DATA_POINT_EVENTS) never spawns an
# event there, so no keypress group may form around it here either.
_SUPPRESSED_USAGES = frozenset({"no_create", "ignored"})

# aiohomematic's DeviceTriggerEventType.short — the unique_id infix.
_TRIGGER_SHORT: dict[DeviceTriggerEventType, str] = {
    DeviceTriggerEventType.Keypress: "keypress",
    DeviceTriggerEventType.Impulse: "impulse",
    DeviceTriggerEventType.DeviceError: "device_error",
}


def _trigger_type(parameter: str) -> DeviceTriggerEventType | None:
    if parameter in _CLICK_PARAMS:
        return DeviceTriggerEventType.Keypress
    if parameter in _IMPULSE_PARAMS:
        return DeviceTriggerEventType.Impulse
    if parameter in _DEVICE_ERROR_PARAMS:
        return DeviceTriggerEventType.DeviceError
    return None


class ChannelEventGroup:
    """One device-trigger event group bound to a channel (per event type)."""

    __slots__ = (
        "_central_id",
        "_channel",
        "_event_type",
        "_events",
        "_last_triggered",
        "_registered",
    )

    _category = DataPointCategory.EventGroup

    def __init__(
        self,
        *,
        channel: Channel,
        event_type: DeviceTriggerEventType,
        events: tuple[DataPoint, ...],
        central_id: str,
    ) -> None:
        """Bind the group to its channel, event type, and member events."""
        self._channel = channel
        self._event_type = event_type
        self._events = events
        self._central_id = central_id
        self._registered = False
        self._last_triggered: TriggeredEvent | None = None

    @property
    def unique_id(self) -> str:
        """
        Return the canonical event-group key HA entities are bound to.

        Carries the ``loom_`` namespace like every other loom key — the
        aiohomematic twin registers ``event_group_<type>_<channel_uid>``
        for the same channel, and both entries may run in one HA
        instance.
        """
        channel_uid = generate_channel_unique_id(
            central_id=self._central_id, address=self._channel.address
        )
        return f"{LOOM_NAMESPACE}_event_group_{_TRIGGER_SHORT[self._event_type]}_{channel_uid}"

    @property
    def category(self) -> DataPointCategory:
        """Return the data-point category (``event_group``)."""
        return self._category

    @property
    def channel(self) -> Channel:
        """Return the channel this group belongs to."""
        return self._channel

    @property
    def device(self) -> Any:
        """Return the owning device, or ``None``."""
        return self._channel.device

    @property
    def device_trigger_event_type(self) -> DeviceTriggerEventType:
        """Return the device-trigger event type of this group."""
        return self._event_type

    @property
    def event_types(self) -> tuple[str, ...]:
        """Return the member parameter names, lower-cased (HA event types)."""
        return tuple(event.parameter.lower() for event in self._events)

    @property
    def events(self) -> tuple[DataPoint, ...]:
        """Return the trigger data points grouped here."""
        return self._events

    @property
    def name(self) -> str:
        """Return the short display name of the group."""
        return _TRIGGER_SHORT[self._event_type]

    @property
    def full_name(self) -> str:
        """Return the device-qualified name of the group."""
        device = self.device
        owner = device.name if device is not None else self._channel.address
        return f"{owner} {self.name}"

    @property
    def translation_key(self) -> str:
        """Return the HA translation key for this group."""
        return _TRIGGER_SHORT[self._event_type]

    @property
    def usage(self) -> DataPointUsage:
        """Return the data-point usage (``event``)."""
        return DataPointUsage.Event

    @property
    def available(self) -> bool:
        """Return whether the owning device is available."""
        device = self.device
        return bool(getattr(device, "available", True)) if device is not None else True

    @property
    def last_triggered_event(self) -> TriggeredEvent | None:
        """Return the most recent member trigger, or ``None`` before the first."""
        return self._last_triggered

    def record_trigger(self, *, parameter: str, value: Any) -> None:
        """Record an incoming member trigger (called by the refresh bridge)."""
        self._last_triggered = TriggeredEvent(parameter=parameter, value=value)

    @property
    def is_registered(self) -> bool:
        """Return whether HA has claimed this group."""
        return self._registered

    def register(self) -> None:
        """Mark the group as registered by an HA entity."""
        self._registered = True

    def unregister(self) -> None:
        """Mark the group as no longer registered."""
        self._registered = False


@dataclass(frozen=True, kw_only=True, slots=True)
class TriggeredEvent:
    """One recorded member trigger (HA reads ``parameter`` to fire the event)."""

    parameter: str
    value: Any = None


def build_event_groups(
    *,
    store: LoomStore,
    central_id: str,
    event_type: DeviceTriggerEventType | None = None,
    registered: bool | None = None,
) -> tuple[ChannelEventGroup, ...]:
    """Build the device-trigger event groups from the store's trigger DPs."""
    groups: list[ChannelEventGroup] = []
    for device in store.devices:
        for channel in device.channels:
            by_type: dict[DeviceTriggerEventType, list[DataPoint]] = {}
            for dp in channel.data_points:
                if not dp.emits_events:
                    continue
                if getattr(dp.summary, "usage", None) in _SUPPRESSED_USAGES:
                    continue
                resolved = _trigger_type(dp.parameter)
                if resolved is None:
                    continue
                by_type.setdefault(resolved, []).append(dp)
            for resolved, events in by_type.items():
                if event_type is not None and resolved != event_type:
                    continue
                group = ChannelEventGroup(
                    channel=channel,
                    event_type=resolved,
                    events=tuple(events),
                    central_id=central_id,
                )
                if registered is None or group.is_registered == registered:
                    groups.append(group)
    return tuple(groups)
