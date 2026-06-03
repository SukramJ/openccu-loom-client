# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Custom-DP categorisation + the uniform refresh bridge."""

from __future__ import annotations

from openccu_loom_types.enums import DataPointType
from openccu_loom_types.rest import CustomDPSummary, Kind, Snapshot
from openccu_loom_types.ws import (
    CustomDataPointStateChangedPayload,
    DataPointValueChangedPayload,
    OptimisticRollbackPayload,
    SysvarChangedPayload,
)

from openccu_loom_client.compat.aiohomematic.central import CentralConfig
from openccu_loom_client.compat.aiohomematic.central.events import (
    DataPointStateChangedEvent,
)
from openccu_loom_client.compat.aiohomematic.central.refresh import install_refresh_bridge
from openccu_loom_client.compat.aiohomematic.model.custom import (
    CustomDpCover,
    CustomDpDimmer,
    CustomDpIpBlind,
    CustomDpIpThermostat,
    make_custom_data_point,
)
from openccu_loom_client.events import (
    CustomDataPointStateChangedEvent,
    DataPointValueChangedEvent,
    EventBus,
    OptimisticRollbackEvent,
    SysvarChangedEvent,
)
from openccu_loom_client.events.types import DataPointOptimisticRolledBackEvent
from openccu_loom_client.store import LoomStore


async def _adapter():
    return await CentralConfig(
        name="home", host="loom.test", port=8080, tls=False, token="tok-1"
    ).create_central()


def _cdp(*, name: str, category: str, kind: str) -> CustomDPSummary:
    return CustomDPSummary.model_validate(
        {
            "name": name,
            "category": category,
            "channel_no": 1,
            "supported_operations": ["open", "close", "set_position"],
            "kind": kind,
        }
    )


class TestCustomDataPointModel:
    async def test_cover_categorised_with_state(self) -> None:
        central = await _adapter()
        store = central._client.store
        store.load_snapshot(
            Snapshot.model_validate(
                {
                    "generated_at": "2026-05-24T08:00:00Z",
                    "devices": [
                        {
                            "address": "VCU1",
                            "interface": "home:HmIP-RF",
                            "model": "HmIP-BROLL",
                            "name": "Shutter",
                            "available": True,
                            "channels_count": 1,
                        }
                    ],
                }
            )
        )
        store.attach_custom_data_points(
            device_address="VCU1",
            cdps=[_cdp(name="cover", category="cover", kind="cover_blind")],
        )
        store.apply_custom_data_point_state_changed(
            CustomDataPointStateChangedPayload.model_validate(
                {
                    "central": "home",
                    "device_address": "VCU1",
                    "channel": 1,
                    "name": "cover",
                    "state": {"state": "open", "current_position": 42},
                }
            )
        )
        covers = central.query_facade.get_data_points(data_point_type=DataPointType.Cover)
        assert len(covers) == 1
        cover = covers[0]
        assert isinstance(cover, CustomDpCover)
        assert cover.current_position == 42
        assert cover.is_closed is False
        # Custom DP keys on its primary channel address; canonical
        # ``loom_`` namespace, and VCU1 (a normal device) carries no
        # serial prefix.
        assert cover.unique_id == "loom_vcu1_1"

    async def test_climate_kind_maps_to_thermostat(self) -> None:
        central = await _adapter()
        store = central._client.store
        store.attach_custom_data_points(
            device_address="VCU2",
            cdps=[_cdp(name="climate", category="climate", kind="climate_hmip")],
        )
        store.apply_custom_data_point_state_changed(
            CustomDataPointStateChangedPayload.model_validate(
                {
                    "central": "home",
                    "device_address": "VCU2",
                    "channel": 1,
                    "name": "climate",
                    "state": {"hvac_mode": "heat", "set_temperature": 21.5},
                }
            )
        )
        climates = central.query_facade.get_data_points(
            data_point_type=DataPointType.Climate
        )
        assert len(climates) == 1
        assert isinstance(climates[0], CustomDpIpThermostat)
        assert climates[0].hvac_mode == "heat"
        assert climates[0].target_temperature == 21.5


class _FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []

    async def request(
        self, method, path, *, params=None, json_body=None, headers=None, allow_retry=None
    ):
        self.calls.append((method, path, json_body))


def _cdp_instance(
    *, kind, category, capabilities=None, state=None, supported=("turn_on", "turn_off")
):
    transport = _FakeTransport()
    store = LoomStore(transport=transport)  # type: ignore[arg-type]
    store.set_custom_data_point_factory(make_custom_data_point)
    store.attach_custom_data_points(
        device_address="VCU1",
        cdps=[
            CustomDPSummary.model_validate(
                {
                    "name": category,
                    "category": category,
                    "channel_no": 1,
                    "supported_operations": list(supported),
                    "kind": kind,
                    "capabilities": capabilities or {},
                }
            )
        ],
    )
    if state is not None:
        store.apply_custom_data_point_state_changed(
            CustomDataPointStateChangedPayload.model_validate(
                {
                    "central": "home",
                    "device_address": "VCU1",
                    "channel": 1,
                    "name": category,
                    "state": state,
                }
            )
        )
    return store.get_custom_data_point(address="VCU1", name=category), transport


class TestCustomDeepParity:
    def test_capabilities_attribute_access(self) -> None:
        dp, _ = _cdp_instance(
            kind="light_color_temp",
            category="light",
            capabilities={"color_temp": True, "brightness": True},
            state={"state": "ON", "brightness": 128},
        )
        assert isinstance(dp, CustomDpDimmer)
        assert dp.is_on is True
        assert dp.brightness == 128
        # attribute access — present True, absent False (never raises)
        assert dp.capabilities.brightness is True
        assert dp.capabilities.color_temp is True
        assert dp.capabilities.effects is False
        assert dp.has_color_temperature is True
        assert dp.level_to_brightness(1.0) == 255

    async def test_light_turn_on_with_brightness_invokes_set_level(self) -> None:
        dp, transport = _cdp_instance(
            kind="light", category="light", state={"state": "OFF"}
        )
        await dp.turn_on(brightness=200)
        method, path, body = transport.calls[-1]
        assert method == "POST"
        assert path.endswith("/cdps/light/set_level")
        assert body == {"params": {"brightness": 200}}

    async def test_blind_set_position_with_tilt(self) -> None:
        dp, transport = _cdp_instance(
            kind="cover_blind",
            category="cover",
            state={"current_position": 50, "current_tilt_position": 10},
        )
        assert isinstance(dp, CustomDpIpBlind)
        assert dp.current_tilt_position == 10
        await dp.set_position(position=60, tilt_position=30)
        paths = [c[1] for c in transport.calls]
        assert any(p.endswith("/set_position") for p in paths)
        assert any(p.endswith("/set_tilt") for p in paths)


class TestRefreshBridge:
    async def _collect(self, bus: EventBus) -> list[str]:
        seen: list[str] = []

        async def recorder(event: DataPointStateChangedEvent) -> None:
            seen.append(event.event_key or "")

        bus.subscribe(event_type=DataPointStateChangedEvent, handler=recorder)
        return seen

    async def test_value_change_becomes_state_changed(self) -> None:
        # Payload without unique_id → the bridge rebuilds the canonical key
        # from the store's serial suffix. A normal device carries no prefix.
        bus = EventBus()
        group = bus.create_subscription_group(name="t")
        install_refresh_bridge(bus=bus, group=group, store=LoomStore())
        seen = await self._collect(bus)
        await bus.publish(
            DataPointValueChangedEvent(
                seq=1,
                kind=Kind.change,
                ts="2026-05-24T08:00:00Z",
                payload=DataPointValueChangedPayload.model_validate(
                    {
                        "central": "home",
                        "device_address": "VCU1",
                        "channel": 1,
                        "parameter": "STATE",
                        "paramset_key": "VALUES",
                        "value": True,
                        "modified_at": "2026-05-24T08:00:00Z",
                    }
                ),
            )
        )
        assert seen == ["loom_vcu1_1_state"]

    async def test_value_change_consumes_payload_unique_id(self) -> None:
        # When the daemon supplies unique_id, the bridge uses it verbatim
        # (no rebuild) — the drift-free path.
        bus = EventBus()
        group = bus.create_subscription_group(name="t")
        install_refresh_bridge(bus=bus, group=group, store=LoomStore())
        seen = await self._collect(bus)
        await bus.publish(
            DataPointValueChangedEvent(
                seq=1,
                kind=Kind.change,
                ts="2026-05-24T08:00:00Z",
                payload=DataPointValueChangedPayload.model_validate(
                    {
                        "central": "home",
                        "device_address": "VCU1",
                        "channel": 1,
                        "parameter": "STATE",
                        "paramset_key": "VALUES",
                        "value": True,
                        "modified_at": "2026-05-24T08:00:00Z",
                        "unique_id": "loom_vcu1_1_state",
                    }
                ),
            )
        )
        assert seen == ["loom_vcu1_1_state"]

    async def test_custom_and_sysvar_changes_become_state_changed(self) -> None:
        bus = EventBus()
        group = bus.create_subscription_group(name="t")
        store = LoomStore()
        store.set_serial("3014F711A0001234")  # serial suffix → 11a0001234
        install_refresh_bridge(bus=bus, group=group, store=store)
        seen = await self._collect(bus)
        await bus.publish(
            CustomDataPointStateChangedEvent(
                seq=2,
                kind=Kind.change,
                ts="2026-05-24T08:00:00Z",
                payload=CustomDataPointStateChangedPayload.model_validate(
                    {
                        "central": "home",
                        "device_address": "VCU1",
                        "channel": 1,
                        "name": "cover",
                        "state": {"current_position": 10},
                    }
                ),
            )
        )
        await bus.publish(
            SysvarChangedEvent(
                seq=3,
                kind=Kind.change,
                ts="2026-05-24T08:00:00Z",
                payload=SysvarChangedPayload.model_validate(
                    {"central": "home", "name": "My Var", "value": 1.0}
                ),
            )
        )
        # Rebuilt canonical keys: a custom DP keys on its primary channel
        # address (no serial prefix for the non-virtual VCU1); a sysvar on
        # ``loom_<serial>_sysvar_<hub_slug(name)>`` (space folds to a dash).
        assert seen == ["loom_vcu1_1", "loom_11a0001234_sysvar_my-var"]

    async def test_optimistic_rollback_broadcast_becomes_public_event(self) -> None:
        bus = EventBus()
        group = bus.create_subscription_group(name="t")
        install_refresh_bridge(bus=bus, group=group, store=LoomStore())
        seen: list[OptimisticRollbackEvent] = []

        async def recorder(event: OptimisticRollbackEvent) -> None:
            seen.append(event)

        bus.subscribe(event_type=OptimisticRollbackEvent, handler=recorder)
        await bus.publish(
            DataPointOptimisticRolledBackEvent(
                seq=7,
                kind=Kind.change,
                ts="2026-05-24T08:00:00Z",
                payload=OptimisticRollbackPayload.model_validate(
                    {
                        "central": "home",
                        "device_address": "VCU1",
                        "channel": 1,
                        "parameter": "LEVEL",
                        "paramset_key": "VALUES",
                        "reason": "timeout",
                        "sent": 0.8,
                        "present": 0.5,
                    }
                ),
            )
        )
        assert len(seen) == 1
        ev = seen[0]
        # Raw daemon broadcast → public aiohomematic-shaped event, with
        # field mapping (sent→rolled_back, present→restored) and the
        # envelope seq preserved (not reset to 0 by local synthesis).
        assert ev.device_address == "VCU1"
        assert ev.parameter == "LEVEL"
        assert ev.rolled_back_value == 0.8
        assert ev.restored_value == 0.5
        assert ev.reason == "timeout"
        assert ev.seq == 7
