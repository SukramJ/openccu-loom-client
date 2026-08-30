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

from openccu_loom_client.compat.aiohomematic.model.naming import channel_display_name
from openccu_loom_client.wire.enums import DataPointCategory, DataPointUsage, DeviceTriggerEventType

if TYPE_CHECKING:
    from openccu_loom_client.model import Channel, DataPoint
    from openccu_loom_client.store import LoomStore

# Usage verdicts that exclude a DP from event groups. Mirrors the
# reference stack's creation gate: a suppressed parameter (e.g. HmIP-PS
# click events via IGNORE_DEVICES_FOR_DATA_POINT_EVENTS) never spawns an
# event there, so no keypress group may form around it here either.
_SUPPRESSED_USAGES = frozenset({"no_create", "ignored"})

# aiohomematic's DeviceTriggerEventType.short — the flavour slug, used for the
# HA translation key and the group's fallback name.
_TRIGGER_SHORT: dict[DeviceTriggerEventType, str] = {
    DeviceTriggerEventType.Keypress: "keypress",
    DeviceTriggerEventType.Impulse: "impulse",
    DeviceTriggerEventType.DeviceError: "device_error",
}

# The daemon names an event group's flavour with the same slug, so reading it
# back is a vocabulary lookup rather than a classification: which CCU
# parameters constitute a keypress is the daemon's answer and arrives in the
# summary. Derived from the map above so the two cannot drift apart.
_TRIGGER_BY_KIND: dict[str, DeviceTriggerEventType] = {slug: t for t, slug in _TRIGGER_SHORT.items()}


class ChannelEventGroup:
    """One device-trigger event group bound to a channel (per event type)."""

    __slots__ = (
        "_channel",
        "_event_type",
        "_events",
        "_last_triggered",
        "_registered",
        "_unique_id",
    )

    _category = DataPointCategory.EventGroup

    def __init__(
        self,
        *,
        channel: Channel,
        event_type: DeviceTriggerEventType,
        events: tuple[DataPoint, ...],
        unique_id: str,
    ) -> None:
        """Bind the group to its channel, event type, member events and key."""
        self._channel = channel
        self._event_type = event_type
        self._events = events
        self._unique_id = unique_id
        self._registered = False
        self._last_triggered: TriggeredEvent | None = None

    @property
    def unique_id(self) -> str:
        """
        Return the canonical event-group key HA entities are bound to.

        Served by the daemon in ``ChannelSummary.event_groups``. It used to be
        recomputed here from the namespace, the flavour slug and the channel
        id — byte-identical to the daemon's answer, and therefore invisible
        while it stayed that way. The daemon is the naming authority; a
        consumer that rebuilds a key it is handed is one release away from
        disagreeing with it.
        """
        return self._unique_id

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
        """
        Return the channel-derived display name of the group.

        Mirrors aiohomematic's ``ChannelEventGroup.name``
        (``name_data.channel_name`` from ``get_event_name``): a
        default-named channel renders ``ch<no>`` (empty on channel 0), a
        user-renamed channel keeps its custom name minus the device-name
        prefix ("Galerie aus" → "aus").
        """
        device = self.device
        if device is None:
            return _TRIGGER_SHORT[self._event_type]
        return channel_display_name(
            store=self._channel._store,  # noqa: SLF001 — package-internal store handle
            device=device,
            channel_no=self._channel.number,
        )

    @property
    def full_name(self) -> str:
        """Return the device-qualified name of the group."""
        device = self.device
        owner = device.name if device is not None else self._channel.address
        return f"{owner} {self.name}".strip()

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
    """
    Build the device-trigger event groups the daemon declares for each channel.

    The grouping, the flavour and the routing key all come from
    ``ChannelSummary.event_groups``. This used to classify CCU parameter names
    here against local sets and rebuild the key from its parts — the fourth
    copy of that set in this family of repositories, and a copy of an
    enumerable domain set caps its holder at whatever its author wrote down.
    That is not hypothetical: the daemon's own MQTT plane kept such a copy and
    published keypresses alone while the model had known three event kinds all
    along.

    ``central_id`` is retained for call compatibility and is no longer used to
    derive anything.

    The usage gate stays local on purpose. The daemon groups every event
    source of a channel; this layer additionally drops parameters the
    reference stack never spawns an event for (``no_create`` / ``ignored``),
    which is a consumer-side visibility rule rather than a fact about the
    device. A group whose members are all suppressed does not materialise.
    """
    del central_id
    groups: list[ChannelEventGroup] = []
    for device in store.devices:
        for channel in device.channels:
            for declared in channel.summary.event_groups or ():
                resolved = _TRIGGER_BY_KIND.get(declared.kind)
                if resolved is None:
                    # A flavour this client does not model yet. Skipping is
                    # correct — inventing a DeviceTriggerEventType for it
                    # would spawn an entity HA has no translation for.
                    continue
                if event_type is not None and resolved != event_type:
                    continue
                members = frozenset(declared.parameters)
                events = tuple(
                    dp
                    for dp in channel.data_points
                    if dp.parameter in members and dp.emits_events and dp.summary.usage not in _SUPPRESSED_USAGES
                )
                if not events:
                    continue
                group = ChannelEventGroup(
                    channel=channel,
                    event_type=resolved,
                    events=events,
                    unique_id=declared.unique_id,
                )
                if registered is None or group.is_registered == registered:
                    groups.append(group)
    return tuple(groups)
