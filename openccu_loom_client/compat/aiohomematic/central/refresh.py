# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Refresh bridge: daemon value events → uniform ``DataPointStateChangedEvent``.

``homematicip_local`` entities (generic, custom and hub alike) subscribe
to :class:`DataPointStateChangedEvent` keyed by their ``unique_id`` to
know when to re-read state. The daemon instead emits distinct typed
events. This bridge subscribes to them on the same bus and re-publishes
one :class:`DataPointStateChangedEvent` per change, keyed by the matching
``unique_id``. Each key is built on aiohomematic's reference algorithm
(via ``openccu_loom_client.canonical``, bit-identical to aiohomematic),
using the ``central`` id the daemon stamps on every payload:

* ``DataPointValueChangedEvent`` → generic DP unique id
  (``generate_unique_id(address=addr:channel, parameter=…)``)
* ``CustomDataPointStateChangedEvent`` → custom DP unique id
  (``generate_unique_id(address=addr:channel_no)`` — the primary channel)
* ``SysvarChangedEvent`` → sysvar unique id
  (``generate_unique_id(address="sysvar", parameter=hub_slug(name))``)

It also bridges the daemon's optimistic-rollback broadcast:

* ``DataPointOptimisticRolledBackEvent`` (raw ``datapoint.optimistic_rolled_back``)
  → the public, aiohomematic-shaped
  :class:`~openccu_loom_client.events.synthetic.OptimisticRollbackEvent`.

The store→model bridge (value application) is unaffected — it keeps
consuming the original typed events without an ``event_key`` filter.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
import logging
from typing import TYPE_CHECKING, Any, Final, cast

from openccu_loom_client.compat.aiohomematic._upstream import (
    CentralState,
    CentralStateChangedEvent,
    DataPointKey,
    DataPointStateChangedEvent,
    DeviceLifecycleEvent,
    DeviceLifecycleEventType,
    DeviceRemovedEvent as AioDeviceRemovedEvent,
    DeviceTriggerEvent,
    DeviceTriggerEventType,
    OptimisticRollbackEvent,
    ParamsetKey,
)
from openccu_loom_client.compat.aiohomematic.model.custom import custom_unique_id
from openccu_loom_client.compat.aiohomematic.model.hub import sysvar_unique_id
from openccu_loom_client.events import (
    AlarmCountdownEvent,
    AlarmPanelChangedEvent,
    AlarmReadinessChangedEvent,
    AlarmStateChangedEvent,
    AlarmTriggeredEvent,
    CentralStateChangedEvent as LoomCentralStateChangedEvent,
    CustomDataPointStateChangedEvent,
    DataPointValueChangedEvent,
    DeviceAvailabilityChangedEvent as LoomDeviceAvailabilityChangedEvent,
    DeviceCreatedEvent as LoomDeviceCreatedEvent,
    DeviceMetadataChangedEvent as LoomDeviceMetadataChangedEvent,
    DeviceRemovedEvent as LoomDeviceRemovedEvent,
    ProgramChangedEvent,
    ScheduleChangedEvent as LoomScheduleChangedEvent,
    SysvarChangedEvent,
)
from openccu_loom_client.events.types import (
    DataPointOptimisticRolledBackEvent,
    DeviceTriggerEvent as LoomDeviceTriggerEvent,
    data_point_event_key,
)

if TYPE_CHECKING:
    from openccu_loom_client.compat.aiohomematic._upstream import EventBus as AioEventBus
    from openccu_loom_client.events import SubscriptionGroup
    from openccu_loom_client.store import LoomStore

_LOGGER: Final = logging.getLogger(__name__)


def install_refresh_bridge(
    *,
    group: SubscriptionGroup,
    store: LoomStore,
    ha_bus: AioEventBus,
    central_name: str,
    event_group_resolver: Callable[..., Any] | None = None,
) -> None:
    """
    Bridge daemon wire events onto aiohomematic's own event bus.

    Daemon wire events are consumed on the loom bus (via ``group``); the
    HA-facing aiohomematic events are published on ``ha_bus`` so a consumer's
    ``type(event)``/``.key`` subscription matches. Value changes become
    :class:`DataPointStateChangedEvent` (keyed by the canonical ``unique_id``,
    supplied by the daemon or rebuilt via the shared contract); device triggers,
    optimistic rollbacks, central-state transitions and device create/remove
    become their aiohomematic equivalents. All subscriptions are tracked on
    ``group`` so the caller tears them down with a single ``group.cancel()``.
    """
    _wire_value_events(group=group, store=store, ha_bus=ha_bus)
    _wire_trigger_and_rollback(group=group, store=store, ha_bus=ha_bus, event_group_resolver=event_group_resolver)
    _wire_central_and_lifecycle(group=group, store=store, ha_bus=ha_bus, central_name=central_name)
    _wire_availability(group=group, store=store, ha_bus=ha_bus)
    _wire_alarm_events(group=group, store=store, ha_bus=ha_bus)
    _wire_schedules(group=group, store=store, ha_bus=ha_bus)


def _device_attrs(*, store: LoomStore, address: str) -> tuple[str, str, str | None]:
    """Return ``(interface_id, model, name)`` for a device, with safe fallbacks."""
    device = store.get_device(address=address)
    if device is None:
        return "", "", None
    return device.interface_id or "", device.model or "", device.name


def _trigger_type(*, token: str) -> DeviceTriggerEventType:
    """Map the daemon's short token (``keypress``) to the aiohomematic member."""
    for member in DeviceTriggerEventType:
        if token in (member.short, member.value):
            return member
    return DeviceTriggerEventType.KEYPRESS


def _wire_value_events(*, group: SubscriptionGroup, store: LoomStore, ha_bus: AioEventBus) -> None:
    """Bridge daemon value/custom/sysvar changes to ``DataPointStateChangedEvent``."""

    async def _emit(*, ts: Any, event_key: str, value: Any = None) -> None:
        await ha_bus.publish(event=DataPointStateChangedEvent(timestamp=ts, unique_id=event_key, new_value=value))

    async def on_value(event: DataPointValueChangedEvent) -> None:
        await _emit(
            ts=event.ts,
            event_key=event.payload.unique_id
            or data_point_event_key(
                serial_suffix=store.serial_suffix,
                device_address=event.payload.device_address,
                channel=event.payload.channel,
                parameter=event.payload.parameter,
            ),
            value=event.payload.value,
        )
        # aiohomematic re-renders a channel's custom data point on every
        # member field-DP event (a climate card updates when
        # ACTUAL_TEMPERATURE changes even though the CDP state dict does
        # not carry the temperature). Ping the channel's CDP too.
        if (
            store.get_custom_data_point_by_channel(
                address=event.payload.device_address, channel_no=event.payload.channel
            )
            is not None
        ):
            await _emit(
                ts=event.ts,
                event_key=custom_unique_id(
                    serial_suffix=store.serial_suffix,
                    device_address=event.payload.device_address,
                    channel_no=event.payload.channel,
                ),
            )

    async def on_custom(event: CustomDataPointStateChangedEvent) -> None:
        await _emit(
            ts=event.ts,
            event_key=event.payload.unique_id
            or custom_unique_id(
                serial_suffix=store.serial_suffix,
                device_address=event.payload.device_address,
                channel_no=event.payload.channel,
            ),
            # CustomDataPointStateChangedPayload carries a ``state`` dict, not a
            # scalar ``value`` — HA reads custom-DP state off the twin, so the
            # unified state-changed event has no value here.
            value=None,
        )

    async def on_sysvar(event: SysvarChangedEvent) -> None:
        await _emit(
            ts=event.ts,
            event_key=event.payload.unique_id
            or sysvar_unique_id(serial_suffix=store.serial_suffix, name=event.payload.name),
            value=event.payload.value,
        )

    async def on_program_changed(event: ProgramChangedEvent) -> None:
        """
        Re-render a program's two entities after its activity flag flipped.

        Both the switch and the button hang off the one canonical key — HA
        scopes unique_ids per platform — so a single emit reaches both. It
        carries the flag as the value because that is the switch's state; the
        button re-reads its own availability off the same summary.
        """
        if not event.payload.unique_id:
            return
        await _emit(ts=event.ts, event_key=event.payload.unique_id, value=event.payload.active)

    group.subscribe(event_type=DataPointValueChangedEvent, handler=on_value)
    group.subscribe(event_type=CustomDataPointStateChangedEvent, handler=on_custom)
    group.subscribe(event_type=SysvarChangedEvent, handler=on_sysvar)
    group.subscribe(event_type=ProgramChangedEvent, handler=on_program_changed)


def _wire_trigger_and_rollback(
    *,
    group: SubscriptionGroup,
    store: LoomStore,
    ha_bus: AioEventBus,
    event_group_resolver: Callable[..., Any] | None = None,
) -> None:
    """Bridge device triggers and optimistic rollbacks to their aiohomematic events."""

    async def on_trigger(event: LoomDeviceTriggerEvent) -> None:
        p = event.payload
        interface_id, model, device_name = _device_attrs(store=store, address=p.device_address)
        await ha_bus.publish(
            event=DeviceTriggerEvent(
                timestamp=datetime.now(tz=UTC),
                trigger_type=_trigger_type(token=p.event_type),
                model=model,
                interface_id=p.interface_id or interface_id,
                device_address=p.device_address,
                channel_no=p.channel,
                parameter=p.parameter,
                # The wire payload's value is optional; aiohomematic's frozen
                # dataclass carries it unvalidated, so the cast only documents
                # the contract without changing runtime behaviour.
                value=cast("str | int | float | bool", p.value),
                device_name=device_name,
            )
        )
        # Feed the matching event-group entity: record the member trigger
        # and ping its keyed state-changed subscription so HA fires the
        # event (the entity reads ``last_triggered_event.parameter``).
        if event_group_resolver is not None and (
            eg := event_group_resolver(
                device_address=p.device_address,
                channel_no=p.channel,
                event_type=_trigger_type(token=p.event_type),
            )
        ):
            eg.record_trigger(parameter=p.parameter, value=p.value)
            await ha_bus.publish(
                event=DataPointStateChangedEvent(
                    timestamp=datetime.now(tz=UTC),
                    unique_id=eg.unique_id,
                    new_value=p.parameter,
                )
            )

    async def on_rollback(event: DataPointOptimisticRolledBackEvent) -> None:
        # Translate the raw daemon broadcast into the public aiohomematic event
        # HA subscribes to (raw ``sent``/``present`` map to rolled_back/restored).
        p = event.payload
        interface_id, _model, device_name = _device_attrs(store=store, address=p.device_address)
        await ha_bus.publish(
            event=OptimisticRollbackEvent(
                timestamp=datetime.now(tz=UTC),
                dpk=DataPointKey(
                    interface_id=interface_id,
                    channel_address=f"{p.device_address}:{p.channel}",
                    paramset_key=ParamsetKey(p.paramset_key),
                    parameter=p.parameter,
                ),
                reason=p.reason,
                rolled_back_value=p.sent,
                restored_value=p.present,
                device_name=device_name,
            )
        )

    group.subscribe(event_type=LoomDeviceTriggerEvent, handler=on_trigger)
    group.subscribe(event_type=DataPointOptimisticRolledBackEvent, handler=on_rollback)


def _wire_availability(*, group: SubscriptionGroup, store: LoomStore, ha_bus: AioEventBus) -> None:
    """
    Fan a ``device.availability_changed`` push out to the device's entities.

    HA entities read ``available`` live off the store (twin → device
    summary), but they only re-render on a keyed
    :class:`DataPointStateChangedEvent` — availability carries no per-DP
    value push, so nothing else reaches them. Mirror aiohomematic's
    ``publish_device_updated_event(notify_data_points=True)``: ping every
    generic data point and every custom data point of the device, keyed
    by the daemon-supplied canonical ``unique_id`` each summary carries.
    (The store flip itself rides the wire→store bridge; this fan-out is
    ordering-independent because the pinged entities re-read the store.)
    """

    async def on_availability(event: LoomDeviceAvailabilityChangedEvent) -> None:
        address = event.payload.device_address
        for channel in store.channels_of(address=address):
            for dp in store.data_points_of(address=address, channel=channel.number):
                await ha_bus.publish(
                    event=DataPointStateChangedEvent(timestamp=event.ts, unique_id=dp.summary.unique_id, new_value=None)
                )
        for cdp in store.custom_data_points_of(address=address):
            await ha_bus.publish(
                event=DataPointStateChangedEvent(timestamp=event.ts, unique_id=cdp.summary.unique_id, new_value=None)
            )

    group.subscribe(event_type=LoomDeviceAvailabilityChangedEvent, handler=on_availability)


def _wire_alarm_events(*, group: SubscriptionGroup, store: LoomStore, ha_bus: AioEventBus) -> None:
    """
    Bridge the alarm pushes to keyed ``DataPointStateChangedEvent`` pings.

    The panel entity subscribes keyed by its daemon-computed
    ``unique_id`` (``openccu-loom_alarm_<zone>``). ``alarm.panel_changed``
    carries the key directly; the zone-scoped detail pushes (state,
    countdown tick, readiness, trigger) resolve zone → panel through the
    store. State itself travels on ``panel_changed`` — the detail pushes
    only refresh the entity's extra attributes.

    A ``panel_changed`` with ``removed`` (zone deleted, or the master
    dematerialising at < 2 zones) becomes aiohomematic's data-point
    flavour of :class:`DeviceRemovedEvent` (only ``unique_id`` set) —
    the generic hub entity base subscribes to exactly that key and
    removes itself from the entity registry.
    """

    async def _ping(*, ts: Any, unique_id: str, value: Any = None) -> None:
        await ha_bus.publish(event=DataPointStateChangedEvent(timestamp=ts, unique_id=unique_id, new_value=value))

    async def _ping_zone(*, ts: Any, zone_id: str) -> None:
        panel = store.get_alarm_panel_by_zone(zone_id=zone_id)
        if panel is not None:
            await _ping(ts=ts, unique_id=panel.unique_id)

    async def on_panel_changed(event: AlarmPanelChangedEvent) -> None:
        if event.payload.removed:
            await ha_bus.publish(
                event=AioDeviceRemovedEvent(timestamp=datetime.now(tz=UTC), unique_id=event.payload.unique_id)
            )
            return
        await _ping(ts=event.ts, unique_id=event.payload.unique_id, value=event.payload.state)

    async def on_alarm_state(event: AlarmStateChangedEvent) -> None:
        await _ping_zone(ts=event.ts, zone_id=event.payload.zone_id)

    async def on_countdown(event: AlarmCountdownEvent) -> None:
        await _ping_zone(ts=event.ts, zone_id=event.payload.zone_id)

    async def on_readiness(event: AlarmReadinessChangedEvent) -> None:
        await _ping_zone(ts=event.ts, zone_id=event.payload.zone_id)

    async def on_triggered(event: AlarmTriggeredEvent) -> None:
        await _ping_zone(ts=event.ts, zone_id=event.payload.zone_id)

    group.subscribe(event_type=AlarmPanelChangedEvent, handler=on_panel_changed)
    group.subscribe(event_type=AlarmStateChangedEvent, handler=on_alarm_state)
    group.subscribe(event_type=AlarmCountdownEvent, handler=on_countdown)
    group.subscribe(event_type=AlarmReadinessChangedEvent, handler=on_readiness)
    group.subscribe(event_type=AlarmTriggeredEvent, handler=on_triggered)


def _wire_schedules(*, group: SubscriptionGroup, store: LoomStore, ha_bus: AioEventBus) -> None:
    """
    Re-read a channel's week profile after a ``schedules.changed`` push.

    The broadcast deliberately carries no profile body — a week profile is
    large and a subscriber only needs to invalidate. The device's
    :class:`WeekProfileDp` is the one object holding the cached schedule
    (and the entry count HA renders), so it re-reads, and the entity is
    pinged afterwards so the new count reaches HA.

    A device without a schedule entity — most of them — is skipped
    without a fetch. The push also fires for a profile this client wrote
    itself, where the write path already refreshed the count; the re-read
    then merely confirms it.
    """

    async def on_schedule_changed(event: LoomScheduleChangedEvent) -> None:
        p = event.payload
        wp_dp = store.get_week_profile_data_point(address=p.device_address)
        if wp_dp is None or getattr(wp_dp, "channel_no", None) != p.channel:
            return
        try:
            await wp_dp.reload_schedule()
        except Exception:
            _LOGGER.debug("schedule reload failed for %s:%s", p.device_address, p.channel, exc_info=True)
            return
        await ha_bus.publish(
            event=DataPointStateChangedEvent(timestamp=event.ts, unique_id=wp_dp.unique_id, new_value=wp_dp.value)
        )

    group.subscribe(event_type=LoomScheduleChangedEvent, handler=on_schedule_changed)


def _wire_central_and_lifecycle(
    *, group: SubscriptionGroup, store: LoomStore, ha_bus: AioEventBus, central_name: str
) -> None:
    """Bridge central-state transitions and device create/remove/rename to lifecycle events."""

    async def on_central_state(event: LoomCentralStateChangedEvent) -> None:
        p = event.payload
        await ha_bus.publish(
            event=CentralStateChangedEvent(
                timestamp=datetime.now(tz=UTC),
                central_name=central_name,
                old_state=CentralState(p.old_state),
                new_state=CentralState(p.new_state),
                trigger=None,
            )
        )

    async def _emit_lifecycle(*, event_type: DeviceLifecycleEventType, device_address: str, interface_id: str) -> None:
        await ha_bus.publish(
            event=DeviceLifecycleEvent(
                timestamp=datetime.now(tz=UTC),
                event_type=event_type,
                device_addresses=(device_address,),
                interface_id=interface_id,
            )
        )

    async def on_device_created(event: LoomDeviceCreatedEvent) -> None:
        await _emit_lifecycle(
            event_type=DeviceLifecycleEventType.CREATED,
            device_address=event.payload.device_address,
            interface_id=event.payload.interface_id,
        )

    async def on_device_removed(event: LoomDeviceRemovedEvent) -> None:
        await _emit_lifecycle(
            event_type=DeviceLifecycleEventType.REMOVED,
            device_address=event.payload.device_address,
            interface_id=event.payload.interface_id,
        )

    async def on_device_metadata_changed(event: LoomDeviceMetadataChangedEvent) -> None:
        """
        Re-read a renamed / re-assigned device, then announce it as updated.

        The payload names the device but inlines none of the new values, so
        the store is refreshed first: the lifecycle event is what makes a
        consumer read the device back, and reading it before the refresh
        would hand out the old name. The address is always the DEVICE
        address, even when a channel was renamed.
        """
        address = event.payload.device_address
        try:
            await store.refresh_device(address=address)
        except Exception:
            _LOGGER.debug("metadata re-read failed for %s", address, exc_info=True)
            return
        await _emit_lifecycle(
            event_type=DeviceLifecycleEventType.UPDATED,
            device_address=address,
            interface_id=event.payload.interface_id,
        )

    group.subscribe(event_type=LoomCentralStateChangedEvent, handler=on_central_state)
    group.subscribe(event_type=LoomDeviceCreatedEvent, handler=on_device_created)
    group.subscribe(event_type=LoomDeviceRemovedEvent, handler=on_device_removed)
    group.subscribe(event_type=LoomDeviceMetadataChangedEvent, handler=on_device_metadata_changed)
