# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Compat alarm-control-panel: categorised twin, adapter wiring, refresh keys.

The alarm panel is loom-native (aiohomematic has no alarm engine), so
the compat class carries no aio twin — ``homematicip_local``'s future
``alarm_control_panel`` platform dispatches on it alone. These tests pin
the entity surface (category / registration / attributes), the query
facade + announce paths in the adapter, and the refresh-bridge fan-in
keyed by the daemon-computed panel ``unique_id``.
"""

from __future__ import annotations

from aiohomematic.async_support import Looper
from aiohomematic.central.events import DataPointStateChangedEvent, EventBus as AioEventBus
from openccu_loom_types.enums import DataPointCategory
from openccu_loom_types.rest import AlarmPanelEntity, Kind1 as Kind
from openccu_loom_types.ws import AlarmCountdownPayload, AlarmPanelChangedPayload

from openccu_loom_client.compat.aiohomematic.central.adapter import _category_for_type
from openccu_loom_client.compat.aiohomematic.central.refresh import install_refresh_bridge
from openccu_loom_client.compat.aiohomematic.model.alarm_panel import (
    LoomDpAlarmControlPanel,
    make_alarm_panel_data_point,
)
from openccu_loom_client.events import AlarmCountdownEvent, AlarmPanelChangedEvent, EventBus
from openccu_loom_client.store import LoomStore


def _panel_entity(
    *,
    area_id: str = "eg",
    state: str = "disarmed",
    master: bool = False,
) -> AlarmPanelEntity:
    return AlarmPanelEntity.model_validate(
        {
            "unique_id": f"openccu-loom_alarm_{area_id}",
            "area_id": area_id,
            "name": area_id.upper(),
            "category": "alarm_control_panel",
            "state": state,
            "available": True,
            "master": master,
            "supported_modes": ["perimeter", "full"],
        }
    )


def _store_with_compat_panels(*entities: AlarmPanelEntity) -> LoomStore:
    store = LoomStore()
    store.set_alarm_panel_factory(factory=make_alarm_panel_data_point)
    store.attach_alarm_panels(panels=list(entities))
    return store


class TestLoomDpAlarmControlPanel:
    def test_entity_surface(self) -> None:
        store = _store_with_compat_panels(_panel_entity())
        panel = store.get_alarm_panel_by_area(area_id="eg")
        assert isinstance(panel, LoomDpAlarmControlPanel)
        assert panel.category is DataPointCategory.AlarmControlPanel
        assert panel.default_category() is DataPointCategory.AlarmControlPanel
        # The daemon-computed unique_id is consumed verbatim.
        assert panel.unique_id == "openccu-loom_alarm_eg"
        assert panel.enabled_default is True
        assert panel.channel is None
        assert panel.value == "disarmed"
        assert panel.is_valid is True
        assert panel.state_uncertain is False

    def test_registration_lifecycle(self) -> None:
        store = _store_with_compat_panels(_panel_entity())
        panel = store.get_alarm_panel_by_area(area_id="eg")
        assert isinstance(panel, LoomDpAlarmControlPanel)
        assert panel.is_registered is False
        panel.register()
        assert panel.is_registered is True
        panel.unregister()
        assert panel.is_registered is False

    def test_attributes_carry_live_detail(self) -> None:
        store = _store_with_compat_panels(_panel_entity())
        panel = store.get_alarm_panel_by_area(area_id="eg")
        assert isinstance(panel, LoomDpAlarmControlPanel)
        store.apply_alarm_countdown(
            payload=AlarmCountdownPayload.model_validate(
                {
                    "area_id": "eg",
                    "kind": "exit_delay",
                    "remaining_s": 25,
                    "total_s": 30,
                    "remaining_ms": 25000,
                    "total_ms": 30000,
                }
            )
        )
        attrs = panel.attributes
        assert attrs["area_id"] == "eg"
        assert attrs["supported_modes"] == ["perimeter", "full"]
        assert attrs["countdown_kind"] == "exit_delay"
        assert attrs["countdown_remaining_s"] == 25
        assert panel.additional_information == attrs


class TestCategoryForType:
    def test_maps_aiohomematic_screaming_case_by_value(self) -> None:
        # homematicip_local passes aiohomematic's enum (SCREAMING_CASE
        # members); the mapping must match on the shared string value.
        from aiohomematic.const import DataPointType as AioDataPointType

        assert _category_for_type(data_point_type=AioDataPointType.SIREN) is DataPointCategory.Siren

    def test_maps_loom_pascal_case_by_value(self) -> None:
        from openccu_loom_types.enums import DataPointType as LoomDataPointType

        assert (
            _category_for_type(data_point_type=LoomDataPointType.AlarmControlPanel)
            is DataPointCategory.AlarmControlPanel
        )

    def test_none_and_unknown_yield_none(self) -> None:
        assert _category_for_type(data_point_type=None) is None
        assert _category_for_type(data_point_type="no_such_platform") is None


class TestAdapterAlarmPanels:
    async def _central_with_panels(self):
        from openccu_loom_client.compat.aiohomematic.central import CentralConfig

        central = await CentralConfig(
            name="home", host="loom.test", port=8080, tls=False, token="tok-123456"
        ).create_central()
        # The adapter constructor installed the compat factory on the store.
        central._client.store.attach_alarm_panels(panels=[_panel_entity()])
        return central

    async def test_query_facade_serves_panels_by_category(self) -> None:
        central = await self._central_with_panels()
        panels = central.query_facade.get_data_points(category=DataPointCategory.AlarmControlPanel)
        assert len(panels) == 1
        assert isinstance(panels[0], LoomDpAlarmControlPanel)
        # Registered filter applies to panels like to any other DP.
        panels[0].register()
        assert (
            central.query_facade.get_data_points(category=DataPointCategory.AlarmControlPanel, registered=False) == ()
        )

    async def test_batch_announce_gates_on_missing_aio_category(self) -> None:
        # The installed aiohomematic (2026.7.x) does not know
        # alarm_control_panel yet — the announce must skip the panels
        # instead of crashing, and other categories must still land.
        central = await self._central_with_panels()
        seen: list[dict] = []
        group = central.event_bus.create_subscription_group(name="spawn")
        from aiohomematic.central.events import DataPointsCreatedEvent as AioDataPointsCreatedEvent

        group.subscribe(
            event_type=AioDataPointsCreatedEvent,
            event_key=None,
            handler=lambda *, event: seen.append(dict(event.new_data_points)),
        )
        await central._emit_data_points_created()
        await central._looper.block_till_done()
        flat = [dp for grouped in seen for dps in grouped.values() for dp in dps]
        assert all(not isinstance(dp, LoomDpAlarmControlPanel) for dp in flat)

    async def test_runtime_panel_announce_gates_without_crash(self) -> None:
        central = await self._central_with_panels()
        event = AlarmPanelChangedEvent(
            seq=1,
            kind=Kind.change,
            ts="2026-07-16T08:00:00Z",
            payload=AlarmPanelChangedPayload.model_validate(
                {
                    "unique_id": "openccu-loom_alarm_eg",
                    "area_id": "eg",
                    "name": "EG",
                    "state": "armed_away",
                    "available": True,
                }
            ),
        )
        # Gate path (aiohomematic lacks the category): no announce, no crash,
        # and the id stays un-announced so a future capable announce can fire.
        await central._on_alarm_panel_changed(event)
        assert central._announced_alarm_panel_ids == set()


class TestRefreshBridgeAlarm:
    def _ha_setup(self) -> tuple[Looper, AioEventBus, list[str]]:
        looper = Looper()
        ha_bus = AioEventBus(task_scheduler=looper)
        seen: list[str] = []
        group = ha_bus.create_subscription_group(name="entity")
        group.subscribe(
            event_type=DataPointStateChangedEvent,
            event_key=None,
            handler=lambda *, event: seen.append(event.unique_id),
        )
        return looper, ha_bus, seen

    async def test_panel_changed_pings_by_unique_id(self) -> None:
        bus = EventBus()
        group = bus.create_subscription_group(name="t")
        looper, ha_bus, seen = self._ha_setup()
        install_refresh_bridge(group=group, store=LoomStore(), ha_bus=ha_bus, central_name="home")
        await bus.publish(
            event=AlarmPanelChangedEvent(
                seq=1,
                kind=Kind.change,
                ts="2026-07-16T08:00:00Z",
                payload=AlarmPanelChangedPayload.model_validate(
                    {
                        "unique_id": "openccu-loom_alarm_eg",
                        "area_id": "eg",
                        "name": "EG",
                        "state": "armed_away",
                        "available": True,
                    }
                ),
            )
        )
        await looper.block_till_done()
        assert seen == ["openccu-loom_alarm_eg"]

    async def test_area_scoped_event_resolves_panel_key(self) -> None:
        store = _store_with_compat_panels(_panel_entity())
        bus = EventBus()
        group = bus.create_subscription_group(name="t")
        looper, ha_bus, seen = self._ha_setup()
        install_refresh_bridge(group=group, store=store, ha_bus=ha_bus, central_name="home")
        await bus.publish(
            event=AlarmCountdownEvent(
                seq=1,
                kind=Kind.change,
                ts="2026-07-16T08:00:00Z",
                payload=AlarmCountdownPayload.model_validate(
                    {
                        "area_id": "eg",
                        "kind": "entry_delay",
                        "remaining_s": 10,
                        "total_s": 30,
                        "remaining_ms": 10000,
                        "total_ms": 30000,
                    }
                ),
            )
        )
        await looper.block_till_done()
        assert seen == ["openccu-loom_alarm_eg"]

    async def test_area_event_without_panel_is_silent(self) -> None:
        bus = EventBus()
        group = bus.create_subscription_group(name="t")
        looper, ha_bus, seen = self._ha_setup()
        install_refresh_bridge(group=group, store=LoomStore(), ha_bus=ha_bus, central_name="home")
        await bus.publish(
            event=AlarmCountdownEvent(
                seq=1,
                kind=Kind.change,
                ts="2026-07-16T08:00:00Z",
                payload=AlarmCountdownPayload.model_validate(
                    {
                        "area_id": "ghost",
                        "kind": "exit_delay",
                        "remaining_s": 1,
                        "total_s": 2,
                        "remaining_ms": 1000,
                        "total_ms": 2000,
                    }
                ),
            )
        )
        await looper.block_till_done()
        assert seen == []
