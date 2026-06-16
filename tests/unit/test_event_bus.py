# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""EventBus and SubscriptionGroup semantics."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from openccu_loom_types.rest import Kind
from openccu_loom_types.ws import CentralStateChangedPayload, DataPointValueChangedPayload
import pytest

from openccu_loom_client.events import (
    CentralStateChangedEvent,
    DataPointValueChangedEvent,
    DeviceCreatedEvent,
    EventBus,
)


def _ts() -> datetime:
    return datetime(2026, 5, 24, 8, 42, 13, tzinfo=UTC)


def _dpv_event(*, seq: int = 1, value: Any = 1.0) -> DataPointValueChangedEvent:
    return DataPointValueChangedEvent(
        seq=seq,
        kind=Kind.change,
        ts=_ts(),
        topic="device.0001.channels.1.data_points.LEVEL",
        type="datapoint.value_changed",
        payload=DataPointValueChangedPayload.model_validate(
            {
                "central": "home",
                "device_address": "0001",
                "channel": 1,
                "parameter": "LEVEL",
                "paramset_key": "VALUES",
                "value": value,
                "modified_at": "2026-05-24T08:42:13Z",
            }
        ),
    )


def _central_event(*, central: str = "home") -> CentralStateChangedEvent:
    # __post_init__ on the subclass auto-derives event_key from
    # payload.central — no manual pin needed.
    return CentralStateChangedEvent(
        seq=1,
        kind=Kind.change,
        ts=_ts(),
        topic=f"central.{central}.state",
        type="central.state_changed",
        payload=CentralStateChangedPayload.model_validate(
            {"central": central, "old_state": "INIT", "new_state": "RUNNING"}
        ),
    )


class TestSubscribeAndPublish:
    async def test_handler_fires_on_matching_type(self) -> None:
        bus = EventBus()
        captured: list[DataPointValueChangedEvent] = []

        async def h(e: DataPointValueChangedEvent) -> None:
            captured.append(e)

        bus.subscribe(event_type=DataPointValueChangedEvent, handler=h)
        await bus.publish(event=_dpv_event())
        assert len(captured) == 1

    async def test_handler_does_not_fire_for_other_type(self) -> None:
        bus = EventBus()
        seen: list[Any] = []

        async def h(e: DataPointValueChangedEvent) -> None:
            seen.append(e)

        bus.subscribe(event_type=DataPointValueChangedEvent, handler=h)
        await bus.publish(event=_central_event())
        assert seen == []

    async def test_publish_with_no_subscribers_is_noop(self) -> None:
        bus = EventBus()
        await bus.publish(event=_dpv_event())  # must not raise

    async def test_multiple_handlers_all_called_in_order(self) -> None:
        bus = EventBus()
        order: list[str] = []

        async def h1(_e: Any) -> None:
            order.append("h1")

        async def h2(_e: Any) -> None:
            order.append("h2")

        bus.subscribe(event_type=DataPointValueChangedEvent, handler=h1)
        bus.subscribe(event_type=DataPointValueChangedEvent, handler=h2)
        await bus.publish(event=_dpv_event())
        assert order == ["h1", "h2"]

    async def test_handler_exception_does_not_break_fanout(self, caplog) -> None:
        bus = EventBus()
        survivors: list[Any] = []

        async def boom(_e: Any) -> None:
            raise RuntimeError("kaboom")

        async def good(_e: Any) -> None:
            survivors.append("yes")

        bus.subscribe(event_type=DataPointValueChangedEvent, handler=boom)
        bus.subscribe(event_type=DataPointValueChangedEvent, handler=good)
        await bus.publish(event=_dpv_event())
        assert survivors == ["yes"]
        assert "kaboom" in caplog.text or any("handler raised" in r.message for r in caplog.records)

    async def test_handler_can_unsubscribe_peer_mid_fanout(self) -> None:
        """
        A handler unsubscribing a sibling within the same fan-out must take effect immediately.

        The peer is skipped in this publish call, not the next one.

        Rationale: when h1's logic concludes "h2 should never see this
        event again", h2 receiving this very event would violate the
        intent and is the kind of bug that's nearly untestable later.
        """
        bus = EventBus()
        survivors: list[Any] = []

        async def h2(_e: Any) -> None:
            survivors.append("h2")

        async def h1(_e: Any) -> None:
            survivors.append("h1")
            unsub_h2()

        unsub_h1 = bus.subscribe(event_type=DataPointValueChangedEvent, handler=h1)
        unsub_h2 = bus.subscribe(event_type=DataPointValueChangedEvent, handler=h2)
        await bus.publish(event=_dpv_event())
        assert survivors == ["h1"]
        survivors.clear()
        await bus.publish(event=_dpv_event())
        assert survivors == ["h1"]
        unsub_h1()


class TestEventKey:
    async def test_event_key_filters_to_matching_subscribers(self) -> None:
        bus = EventBus()
        home: list[Any] = []
        cabin: list[Any] = []

        async def home_h(_e: Any) -> None:
            home.append("home")

        async def cabin_h(_e: Any) -> None:
            cabin.append("cabin")

        bus.subscribe(
            event_type=CentralStateChangedEvent,
            event_key="home",
            handler=home_h,
        )
        bus.subscribe(
            event_type=CentralStateChangedEvent,
            event_key="cabin",
            handler=cabin_h,
        )
        await bus.publish(event=_central_event(central="home"))
        assert home == ["home"]
        assert cabin == []

    async def test_subscriber_with_none_key_receives_everything(self) -> None:
        bus = EventBus()
        any_central: list[Any] = []

        async def h(_e: Any) -> None:
            any_central.append("x")

        bus.subscribe(event_type=CentralStateChangedEvent, handler=h)  # event_key=None
        await bus.publish(event=_central_event(central="home"))
        await bus.publish(event=_central_event(central="cabin"))
        assert len(any_central) == 2


class TestUnsubscribe:
    async def test_unsubscribe_stops_further_calls(self) -> None:
        bus = EventBus()
        received: list[Any] = []

        async def h(_e: Any) -> None:
            received.append("x")

        unsub = bus.subscribe(event_type=DataPointValueChangedEvent, handler=h)
        await bus.publish(event=_dpv_event())
        unsub()
        await bus.publish(event=_dpv_event())
        assert len(received) == 1
        assert bus.subscription_count() == 0

    async def test_unsubscribe_is_idempotent(self) -> None:
        bus = EventBus()

        async def h(_e: Any) -> None: ...

        unsub = bus.subscribe(event_type=DataPointValueChangedEvent, handler=h)
        unsub()
        unsub()  # must not raise
        assert bus.subscription_count() == 0


class TestSubscriptionGroup:
    async def test_group_cancel_unsubscribes_all_members(self) -> None:
        bus = EventBus()
        received: list[Any] = []

        async def h(_e: Any) -> None:
            received.append("x")

        group = bus.create_subscription_group(name="g1")
        group.subscribe(event_type=DataPointValueChangedEvent, handler=h)
        group.subscribe(event_type=DeviceCreatedEvent, handler=h)
        assert group.size == 2
        assert bus.subscription_count() == 2

        group.cancel()
        assert group.size == 0
        assert bus.subscription_count() == 0

        await bus.publish(event=_dpv_event())
        assert received == []

    async def test_group_cancel_is_idempotent(self) -> None:
        bus = EventBus()

        async def h(_e: Any) -> None: ...

        group = bus.create_subscription_group(name="g")
        group.subscribe(event_type=DataPointValueChangedEvent, handler=h)
        group.cancel()
        group.cancel()  # must not raise

    async def test_groups_are_independent(self) -> None:
        bus = EventBus()
        a_received: list[Any] = []
        b_received: list[Any] = []

        async def ah(_e: Any) -> None:
            a_received.append("a")

        async def bh(_e: Any) -> None:
            b_received.append("b")

        group_a = bus.create_subscription_group(name="A")
        group_b = bus.create_subscription_group(name="B")
        group_a.subscribe(event_type=DataPointValueChangedEvent, handler=ah)
        group_b.subscribe(event_type=DataPointValueChangedEvent, handler=bh)

        group_a.cancel()
        await bus.publish(event=_dpv_event())
        assert a_received == []
        assert b_received == ["b"]


@pytest.fixture(autouse=True)
def caplog(caplog):
    return caplog
