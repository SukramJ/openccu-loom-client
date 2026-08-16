# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Client-synthetic events — emitted locally, not by the daemon.

Two events live here:

1. :class:`DataPointsCreatedEvent` — fires after snapshot bootstrap
   and after per-device reconcile when a new batch of data-points
   becomes addressable in the store. The Home-Assistant integration
   subscribes to this to spawn HA entities for the new DPs (mirrors
   ``aiohomematic.central.events.DataPointsCreatedEvent``).
2. :class:`OptimisticRollbackEvent` — the aiohomematic-shaped,
   HA-facing rollback event. The daemon now emits the raw
   ``datapoint.optimistic_rolled_back`` broadcast
   (:class:`~openccu_loom_client.events.types.DataPointOptimisticRolledBackEvent`),
   which the compat refresh bridge translates into this event so the
   HA-side subscriber surface matches aiohomematic. The
   :func:`new_optimistic_rollback_event` factory also lets callers
   synthesize it locally (e.g. from a REST ``set_value`` failure) when
   no broadcast is in flight.

Both carry envelope-shaped metadata so the :class:`EventBus` can route
them with the same machinery as wire events.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

from openccu_loom_types.rest import Kind2 as Kind

from openccu_loom_client.events.types import LoomEvent

if TYPE_CHECKING:
    from openccu_loom_client.model import DataPoint, Device


def _now() -> datetime:
    return datetime.now(tz=UTC)


@dataclass(slots=True, kw_only=True)
class DataPointsCreatedEvent(LoomEvent):
    """
    A batch of data-points became addressable in the store.

    Fired by the store after :meth:`LoomStore.load_snapshot` finishes
    populating, after :meth:`LoomStore.attach_device_detail` resolves
    a brand-new device, and after a daemon-side
    :class:`DeviceCreatedEvent` triggered a reconcile that produced
    new DPs.

    ``devices`` carries the parent Devices for grouping; ``data_points``
    is the flat list of DPs the subscriber should act on. Both are
    populated together so consumers can pick whichever granularity
    matches their wiring.
    """

    devices: list[Device] = field(default_factory=list)
    data_points: list[DataPoint] = field(default_factory=list)
    central: str | None = None
    type_id: ClassVar[str] = "client.data_points_created"

    def __post_init__(self) -> None:
        """Default the routing key to the central name when one is set."""
        if self.event_key is None and self.central is not None:
            self.event_key = self.central


@dataclass(slots=True, kw_only=True)
class OptimisticRollbackEvent(LoomEvent):
    """
    A previously-optimistic write was rolled back.

    The aiohomematic-shaped, HA-facing event. It is produced by the
    compat refresh bridge from the daemon's raw
    ``datapoint.optimistic_rolled_back`` broadcast
    (:class:`~openccu_loom_client.events.types.DataPointOptimisticRolledBackEvent`),
    and can also be synthesized locally from a REST ``set_value``
    failure via :func:`new_optimistic_rollback_event` when no broadcast
    is in flight.
    """

    device_address: str
    channel: int
    parameter: str
    rolled_back_value: Any = None
    restored_value: Any = None
    central: str | None = None
    reason: str | None = None
    type_id: ClassVar[str] = "client.optimistic_rollback"

    def __post_init__(self) -> None:
        """Default the routing key to the central name when one is set."""
        if self.event_key is None and self.central is not None:
            self.event_key = self.central


def new_data_points_created_event(
    *,
    devices: list[Device],
    data_points: list[DataPoint],
    central: str | None = None,
) -> DataPointsCreatedEvent:
    """
    Construct a ready-to-publish DataPointsCreatedEvent.

    Wraps the envelope-metadata defaults so the store doesn't have
    to know about ``seq=0`` / ``kind=initial`` conventions.
    """
    return DataPointsCreatedEvent(
        seq=0,
        kind=Kind.initial,
        ts=_now(),
        topic=None,
        type=DataPointsCreatedEvent.type_id,
        devices=devices,
        data_points=data_points,
        central=central,
    )


def new_optimistic_rollback_event(
    *,
    device_address: str,
    channel: int,
    parameter: str,
    rolled_back_value: Any = None,
    restored_value: Any = None,
    central: str | None = None,
    reason: str | None = None,
) -> OptimisticRollbackEvent:
    """
    Construct a ready-to-publish OptimisticRollbackEvent.

    Wraps the envelope-metadata defaults so a caller synthesizing the
    rollback locally (e.g. from a ``set_value`` failure) need not know
    about the ``seq=0`` / ``kind=change`` conventions. The refresh
    bridge builds the event directly when translating the daemon's
    broadcast.
    """
    return OptimisticRollbackEvent(
        seq=0,
        kind=Kind.change,
        ts=_now(),
        topic=None,
        type=OptimisticRollbackEvent.type_id,
        device_address=device_address,
        channel=channel,
        parameter=parameter,
        rolled_back_value=rolled_back_value,
        restored_value=restored_value,
        central=central,
        reason=reason,
    )
