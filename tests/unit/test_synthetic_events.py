# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Synthetic-event constructors + their behaviour on the bus."""

from __future__ import annotations

from openccu_loom_types.rest import Kind

from openccu_loom_client.events import (
    DataPointsCreatedEvent,
    EventBus,
    OptimisticRollbackEvent,
    new_data_points_created_event,
    new_optimistic_rollback_event,
)


class TestDataPointsCreatedEvent:
    def test_constructor_sets_envelope_defaults(self) -> None:
        ev = new_data_points_created_event(devices=[], data_points=[], central="home")
        assert ev.type == "client.data_points_created"
        assert ev.seq == 0
        assert ev.kind == Kind.initial
        assert ev.event_key == "home"
        assert ev.topic is None

    def test_no_central_means_no_event_key(self) -> None:
        ev = new_data_points_created_event(devices=[], data_points=[])
        assert ev.event_key is None

    async def test_routes_through_bus_like_a_wire_event(self) -> None:
        bus = EventBus()
        seen: list[DataPointsCreatedEvent] = []

        async def h(e: DataPointsCreatedEvent) -> None:
            seen.append(e)

        bus.subscribe(event_type=DataPointsCreatedEvent, handler=h)
        await bus.publish(event=new_data_points_created_event(devices=[], data_points=[], central="home"))
        assert len(seen) == 1
        assert seen[0].central == "home"


class TestOptimisticRollbackEvent:
    def test_constructor_carries_diagnostic_payload(self) -> None:
        ev = new_optimistic_rollback_event(
            device_address="VCU0001",
            channel=1,
            parameter="LEVEL",
            rolled_back_value=0.9,
            restored_value=0.5,
            central="home",
            reason="ccu_rejected",
        )
        assert ev.type == "client.optimistic_rollback"
        assert ev.kind == Kind.change
        assert ev.device_address == "VCU0001"
        assert ev.rolled_back_value == 0.9
        assert ev.restored_value == 0.5
        assert ev.reason == "ccu_rejected"

    async def test_routes_through_bus(self) -> None:
        bus = EventBus()
        seen: list[OptimisticRollbackEvent] = []

        async def h(e: OptimisticRollbackEvent) -> None:
            seen.append(e)

        bus.subscribe(event_type=OptimisticRollbackEvent, handler=h)
        await bus.publish(
            event=new_optimistic_rollback_event(
                device_address="VCU0001",
                channel=1,
                parameter="LEVEL",
                rolled_back_value=1.0,
            )
        )
        assert len(seen) == 1
