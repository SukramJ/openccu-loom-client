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

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from aiohomematic.central.events import (
    CentralStateChangedEvent,
    DataPointStateChangedEvent,
    DeviceLifecycleEvent,
    DeviceLifecycleEventType,
    DeviceTriggerEvent,
    OptimisticRollbackEvent,
)
from aiohomematic.const import CentralState, DataPointKey, DeviceTriggerEventType, ParamsetKey

from openccu_loom_client.compat.aiohomematic.model.custom import custom_unique_id
from openccu_loom_client.compat.aiohomematic.model.hub import sysvar_unique_id
from openccu_loom_client.events import (
    CentralStateChangedEvent as LoomCentralStateChangedEvent,
    CustomDataPointStateChangedEvent,
    DataPointValueChangedEvent,
    DeviceCreatedEvent as LoomDeviceCreatedEvent,
    DeviceRemovedEvent as LoomDeviceRemovedEvent,
    SysvarChangedEvent,
)
from openccu_loom_client.events.types import (
    DataPointOptimisticRolledBackEvent,
    DeviceTriggerEvent as LoomDeviceTriggerEvent,
    data_point_event_key,
)

if TYPE_CHECKING:
    from aiohomematic.central.events import EventBus as AioEventBus

    from openccu_loom_client.events import SubscriptionGroup
    from openccu_loom_client.store import LoomStore


def install_refresh_bridge(
    *,
    group: SubscriptionGroup,
    store: LoomStore,
    ha_bus: AioEventBus,
    central_name: str,
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
    _wire_trigger_and_rollback(group=group, store=store, ha_bus=ha_bus)
    _wire_central_and_lifecycle(group=group, ha_bus=ha_bus, central_name=central_name)


def _device_attrs(store: LoomStore, address: str) -> tuple[str, str, str | None]:
    """Return ``(interface_id, model, name)`` for a device, with safe fallbacks."""
    device = store.get_device(address=address)
    if device is None:
        return "", "", None
    return device.interface_id or "", device.model or "", device.name


def _trigger_type(token: str) -> DeviceTriggerEventType:
    """Map the daemon's short token (``keypress``) to the aiohomematic member."""
    for member in DeviceTriggerEventType:
        if token in (member.short, member.value):
            return member
    return DeviceTriggerEventType.KEYPRESS


def _wire_value_events(*, group: SubscriptionGroup, store: LoomStore, ha_bus: AioEventBus) -> None:
    """Bridge daemon value/custom/sysvar changes to ``DataPointStateChangedEvent``."""

    async def _emit(*, ts: Any, event_key: str, value: Any = None) -> None:
        await ha_bus.publish(
            event=DataPointStateChangedEvent(timestamp=ts, unique_id=event_key, new_value=value)
        )

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
            value=getattr(event.payload, "value", None),
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
            value=getattr(event.payload, "value", None),
        )

    async def on_sysvar(event: SysvarChangedEvent) -> None:
        await _emit(
            ts=event.ts,
            event_key=event.payload.unique_id
            or sysvar_unique_id(serial_suffix=store.serial_suffix, name=event.payload.name),
            value=getattr(event.payload, "value", None),
        )

    group.subscribe(event_type=DataPointValueChangedEvent, handler=on_value)
    group.subscribe(event_type=CustomDataPointStateChangedEvent, handler=on_custom)
    group.subscribe(event_type=SysvarChangedEvent, handler=on_sysvar)


def _wire_trigger_and_rollback(
    *, group: SubscriptionGroup, store: LoomStore, ha_bus: AioEventBus
) -> None:
    """Bridge device triggers and optimistic rollbacks to their aiohomematic events."""

    async def on_trigger(event: LoomDeviceTriggerEvent) -> None:
        p = event.payload
        interface_id, model, device_name = _device_attrs(store, p.device_address)
        await ha_bus.publish(
            event=DeviceTriggerEvent(
                timestamp=datetime.now(tz=UTC),
                trigger_type=_trigger_type(p.event_type),
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

    async def on_rollback(event: DataPointOptimisticRolledBackEvent) -> None:
        # Translate the raw daemon broadcast into the public aiohomematic event
        # HA subscribes to (raw ``sent``/``present`` map to rolled_back/restored).
        p = event.payload
        interface_id, _model, device_name = _device_attrs(store, p.device_address)
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


def _wire_central_and_lifecycle(
    *, group: SubscriptionGroup, ha_bus: AioEventBus, central_name: str
) -> None:
    """Bridge central-state transitions and device create/remove to lifecycle events."""

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

    async def _emit_lifecycle(
        *, event_type: DeviceLifecycleEventType, device_address: str, interface_id: str
    ) -> None:
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

    group.subscribe(event_type=LoomCentralStateChangedEvent, handler=on_central_state)
    group.subscribe(event_type=LoomDeviceCreatedEvent, handler=on_device_created)
    group.subscribe(event_type=LoomDeviceRemovedEvent, handler=on_device_removed)
