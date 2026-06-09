# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Refresh bridge: daemon value events → uniform ``DataPointStateChangedEvent``.

``homematicip_local`` entities (generic, custom and hub alike) subscribe
to :class:`DataPointStateChangedEvent` keyed by their ``unique_id`` to
know when to re-read state. The daemon instead emits distinct typed
events. This bridge subscribes to them on the same bus and re-publishes
one :class:`DataPointStateChangedEvent` per change, keyed by the matching
``unique_id``. Each key is built by the shared ``aiohomematic_contract``
reference (bit-identical to aiohomematic), using the ``central`` id the
daemon stamps on every payload:

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

from typing import TYPE_CHECKING, Any

from aiohomematic.event_types import DataPointStateChangedEvent

from openccu_loom_client.compat.aiohomematic.model.custom import custom_unique_id
from openccu_loom_client.compat.aiohomematic.model.hub import sysvar_unique_id
from openccu_loom_client.events import (
    CustomDataPointStateChangedEvent,
    DataPointValueChangedEvent,
    OptimisticRollbackEvent,
    SysvarChangedEvent,
)
from openccu_loom_client.events.types import (
    DataPointOptimisticRolledBackEvent,
    data_point_event_key,
)

if TYPE_CHECKING:
    from aiohomematic.central.events import EventBus as AioEventBus

    from openccu_loom_client.events import EventBus, SubscriptionGroup
    from openccu_loom_client.store import LoomStore


def install_refresh_bridge(
    *, bus: EventBus, group: SubscriptionGroup, store: LoomStore, ha_bus: AioEventBus
) -> None:
    """
    Wire the daemon value events to aiohomematic's ``DataPointStateChangedEvent``.

    Daemon wire events are consumed on the loom ``bus`` (via ``group``); the
    HA-facing :class:`DataPointStateChangedEvent` is published on ``ha_bus``,
    aiohomematic's own event bus, so a HA entity's ``type(event)``/``.key``
    subscription matches. Each event's routing key is the daemon-supplied
    canonical ``payload.unique_id`` when present; otherwise it is rebuilt from
    the raw payload fields and ``store.serial_suffix`` via the shared contract
    (so it stays bit-identical). All subscriptions are tracked on ``group`` so
    the caller tears them down with a single ``group.cancel()``.
    """

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

    async def on_rollback(event: DataPointOptimisticRolledBackEvent) -> None:
        # Translate the raw daemon broadcast into the public,
        # aiohomematic-shaped event HA subscribes to, preserving the
        # envelope's seq/kind/ts (the local-synthesis factory would
        # reset them to seq=0).
        p = event.payload
        await bus.publish(
            OptimisticRollbackEvent(
                seq=event.seq,
                kind=event.kind,
                ts=event.ts,
                type=OptimisticRollbackEvent.type_id,
                device_address=p.device_address,
                channel=p.channel,
                parameter=p.parameter,
                rolled_back_value=p.sent,
                restored_value=p.present,
                central=p.central,
                reason=p.reason,
            )
        )

    group.subscribe(event_type=DataPointValueChangedEvent, handler=on_value)
    group.subscribe(event_type=CustomDataPointStateChangedEvent, handler=on_custom)
    group.subscribe(event_type=SysvarChangedEvent, handler=on_sysvar)
    group.subscribe(event_type=DataPointOptimisticRolledBackEvent, handler=on_rollback)
