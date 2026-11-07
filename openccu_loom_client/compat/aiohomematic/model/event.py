# SPDX-License-Identifier: MIT
# Copyright (C) 2026 SukramJ.

"""
``aiohomematic.model.event`` twins — the device's trigger sources.

aiohomematic's ``Device.generic_events`` returns one ``GenericEvent`` per
event parameter of a channel: ``ClickEvent`` for a keypress, ``ImpulseEvent``
and ``DeviceErrorEvent`` for the other two flavours. Home Assistant builds its
device triggers from them (``homematicip_local/device_trigger.py``: the event
type, the usage gate and ``get_event_data``).

On this backend a trigger source is a data point the daemon lists as a member
of a channel event group (``ChannelSummary.event_groups``), so the flavour comes
from the daemon, not from a local parameter-name set. The twins wrap that data
point. They satisfy aiohomematic's surface, not its classes: a consumer that
dispatches on ``isinstance(x, aiohomematic.model.event.ClickEvent)`` has to
pair the reference class with :class:`ClickEvent`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from openccu_loom_client.compat.aiohomematic._upstream import DataPointUsage, DeviceTriggerEventType, EventData
from openccu_loom_client.wire.enums import DataPointUsage as WireDataPointUsage

if TYPE_CHECKING:
    from openccu_loom_client.model.channel import Channel
    from openccu_loom_client.model.data_point import DataPoint


class GenericEvent:
    """One event source of a channel, as aiohomematic's ``GenericEvent``."""

    __slots__ = ("_data_point",)

    _device_trigger_event_type: ClassVar[DeviceTriggerEventType]

    def __init__(self, *, data_point: DataPoint) -> None:
        """Wrap the store's event data point."""
        self._data_point = data_point

    @property
    def channel(self) -> Channel | None:
        """Return the channel of the event source."""
        return self._data_point.channel

    @property
    def channel_no(self) -> int:
        """Return the channel number of the event source."""
        return self._data_point.channel_number

    @property
    def event_type(self) -> DeviceTriggerEventType:
        """Return the device-trigger flavour of the event."""
        return self._device_trigger_event_type

    @property
    def parameter(self) -> str:
        """Return the CCU parameter of the event source."""
        return self._data_point.parameter

    @property
    def usage(self) -> DataPointUsage:
        """
        Return the usage, as aiohomematic's ``GenericEvent.usage``.

        ``EVENT``, or ``NO_CREATE`` when the channel operation mode excludes
        the source; the daemon carries that gate as the data point's usage.
        """
        if self._data_point.summary.usage == WireDataPointUsage.NoCreate:
            return DataPointUsage.NO_CREATE
        return DataPointUsage.EVENT

    def get_event_data(self, *, value: Any = None) -> EventData:
        """
        Return the event data, as aiohomematic's ``get_event_data``.

        The fields are the ones the keypress push reports for this source
        (``central/refresh.py``): a device trigger fires when the HA bus event
        matches the data it was built from.
        """
        device = self._data_point.device
        return EventData(
            interface_id=(device.interface_id or "") if device is not None else "",
            model=(device.model or "") if device is not None else "",
            device_address=self._data_point.device_address,
            channel_no=self._data_point.channel_number,
            parameter=self._data_point.parameter,
            value=value,
        )


class ClickEvent(GenericEvent):
    """A keypress source, as aiohomematic's ``ClickEvent``."""

    __slots__ = ()

    _device_trigger_event_type = DeviceTriggerEventType.KEYPRESS


class ImpulseEvent(GenericEvent):
    """An impulse source, as aiohomematic's ``ImpulseEvent``."""

    __slots__ = ()

    _device_trigger_event_type = DeviceTriggerEventType.IMPULSE


class DeviceErrorEvent(GenericEvent):
    """A device-error source, as aiohomematic's ``DeviceErrorEvent``."""

    __slots__ = ()

    _device_trigger_event_type = DeviceTriggerEventType.DEVICE_ERROR


# The daemon names an event-group flavour by aiohomematic's
# DeviceTriggerEventType.short ("keypress", "impulse", "device_error").
_EVENT_CLASS_BY_KIND: dict[str, type[GenericEvent]] = {
    cls._device_trigger_event_type.short: cls for cls in (ClickEvent, ImpulseEvent, DeviceErrorEvent)
}


def make_generic_event(*, data_point: DataPoint, kind: str) -> GenericEvent | None:
    """
    Store factory: wrap an event data point in the twin for its flavour.

    ``None`` for a source the reference spawns no event for (usage
    ``ignored``) and for a flavour this client does not model.
    """
    if data_point.summary.usage == WireDataPointUsage.Ignored:
        return None
    cls = _EVENT_CLASS_BY_KIND.get(kind)
    return None if cls is None else cls(data_point=data_point)


__all__ = ["ClickEvent", "DeviceErrorEvent", "GenericEvent", "ImpulseEvent", "make_generic_event"]
