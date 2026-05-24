# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Refresh bridge: daemon value events → uniform ``DataPointStateChangedEvent``.

``homematicip_local`` entities (generic, custom and hub alike) subscribe
to :class:`DataPointStateChangedEvent` keyed by their ``unique_id`` to
know when to re-read state. The daemon instead emits three distinct
typed events. This bridge subscribes to all three on the same bus and
re-publishes one :class:`DataPointStateChangedEvent` per change, keyed
by the matching ``unique_id``:

* ``DataPointValueChangedEvent`` → generic DP unique id
  (``addr_channel_param``)
* ``CustomDataPointStateChangedEvent`` → custom DP unique id
  (``addr_cdp_name``)
* ``SysvarChangedEvent`` → sysvar unique id (``sysvar_name``)

The store→model bridge (value application) is unaffected — it keeps
consuming the original typed events without an ``event_key`` filter.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from openccu_loom_client.compat.aiohomematic.central.events import (
    DataPointStateChangedEvent,
)
from openccu_loom_client.compat.aiohomematic.model.custom import custom_unique_id
from openccu_loom_client.compat.aiohomematic.model.hub import sysvar_unique_id
from openccu_loom_client.events import (
    CustomDataPointStateChangedEvent,
    DataPointValueChangedEvent,
    SysvarChangedEvent,
)
from openccu_loom_client.events.types import data_point_event_key

if TYPE_CHECKING:
    from openccu_loom_client.events import EventBus, SubscriptionGroup


def install_refresh_bridge(*, bus: EventBus, group: SubscriptionGroup) -> None:
    """Wire the three daemon value events to ``DataPointStateChangedEvent``.

    All subscriptions are tracked on ``group`` so the caller tears them
    down with a single ``group.cancel()``.
    """

    async def _emit(*, seq: int, kind: Any, ts: Any, event_key: str) -> None:
        await bus.publish(
            DataPointStateChangedEvent(
                seq=seq,
                kind=kind,
                ts=ts,
                event_key=event_key,
            )
        )

    async def on_value(event: DataPointValueChangedEvent) -> None:
        await _emit(
            seq=event.seq,
            kind=event.kind,
            ts=event.ts,
            event_key=data_point_event_key(
                device_address=event.payload.device_address,
                channel=event.payload.channel,
                parameter=event.payload.parameter,
            ),
        )

    async def on_custom(event: CustomDataPointStateChangedEvent) -> None:
        await _emit(
            seq=event.seq,
            kind=event.kind,
            ts=event.ts,
            event_key=custom_unique_id(
                device_address=event.payload.device_address, name=event.payload.name
            ),
        )

    async def on_sysvar(event: SysvarChangedEvent) -> None:
        await _emit(
            seq=event.seq,
            kind=event.kind,
            ts=event.ts,
            event_key=sysvar_unique_id(event.payload.name),
        )

    group.subscribe(event_type=DataPointValueChangedEvent, handler=on_value)
    group.subscribe(event_type=CustomDataPointStateChangedEvent, handler=on_custom)
    group.subscribe(event_type=SysvarChangedEvent, handler=on_sysvar)
