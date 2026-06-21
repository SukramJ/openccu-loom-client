# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Custom-DP categorisation + the uniform refresh bridge."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from aiohomematic.async_support import Looper
from aiohomematic.central.events import (
    CentralStateChangedEvent as AioCentralStateChangedEvent,
    DataPointsCreatedEvent as AioDataPointsCreatedEvent,
    DataPointStateChangedEvent,
    DeviceLifecycleEvent,
    DeviceLifecycleEventType,
    DeviceTriggerEvent as AioDeviceTriggerEvent,
    EventBus as AioEventBus,
    OptimisticRollbackEvent,
)
from aiohomematic.const import (
    CentralState,
    DataPointCategory as AioDataPointCategory,
    DeviceTriggerEventType,
    ParamsetKey,
)
from openccu_loom_types.enums import DataPointType
from openccu_loom_types.rest import CustomDPSummary, DataPointSummary, Kind1 as Kind, Snapshot
from openccu_loom_types.ws import (
    CentralStateChangedPayload,
    CustomDataPointStateChangedPayload,
    DataPointValueChangedPayload,
    DeviceCreatedPayload,
    DeviceRemovedPayload,
    DeviceTriggerPayload,
    HubConnectivityChangedPayload,
    HubCountChangedPayload,
    HubMetricChangedPayload,
    OptimisticRollbackPayload,
    SysvarChangedPayload,
)

from openccu_loom_client.compat.aiohomematic.central import CentralConfig
from openccu_loom_client.compat.aiohomematic.central.adapter import _HubCoordinator
from openccu_loom_client.compat.aiohomematic.central.refresh import install_refresh_bridge
from openccu_loom_client.compat.aiohomematic.model.custom import (
    CustomDpCover,
    CustomDpDimmer,
    CustomDpIpBlind,
    CustomDpIpThermostat,
    CustomDpTextDisplay,
    make_custom_data_point,
)
from openccu_loom_client.compat.aiohomematic.model.generic import (
    DpBinarySensor,
    DpSensor,
    DpSwitch,
    make_generic_data_point,
)
from openccu_loom_client.events import (
    CentralStateChangedEvent as LoomCentralStateChangedEvent,
    CustomDataPointStateChangedEvent,
    DataPointValueChangedEvent,
    DeviceCreatedEvent,
    DeviceRemovedEvent,
    EventBus,
    HubAlarmMessageCountChangedEvent,
    HubConnectivityChangedEvent,
    HubInboxChangedEvent,
    HubMetricsChangedEvent,
    SysvarChangedEvent,
)
from openccu_loom_client.events.types import (
    DataPointOptimisticRolledBackEvent,
    DeviceTriggerEvent as LoomDeviceTriggerEvent,
)
from openccu_loom_client.store import LoomStore


async def _adapter():
    return await CentralConfig(name="home", host="loom.test", port=8080, tls=False, token="tok-1").create_central()


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
            snapshot=Snapshot.model_validate(
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
            payload=CustomDataPointStateChangedPayload.model_validate(
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
            payload=CustomDataPointStateChangedPayload.model_validate(
                {
                    "central": "home",
                    "device_address": "VCU2",
                    "channel": 1,
                    "name": "climate",
                    "state": {"hvac_mode": "heat", "set_temperature": 21.5},
                }
            )
        )
        climates = central.query_facade.get_data_points(data_point_type=DataPointType.Climate)
        assert len(climates) == 1
        assert isinstance(climates[0], CustomDpIpThermostat)
        assert climates[0].hvac_mode == "heat"
        assert climates[0].target_temperature == 21.5


class _FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []

    async def request(self, method, path, *, params=None, json_body=None, headers=None, allow_retry=None):
        self.calls.append((method, path, json_body))


def _cdp_instance(*, kind, category, capabilities=None, state=None, supported=("turn_on", "turn_off")):
    transport = _FakeTransport()
    store = LoomStore(transport=transport)  # type: ignore[arg-type]
    store.set_custom_data_point_factory(factory=make_custom_data_point)
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
            payload=CustomDataPointStateChangedPayload.model_validate(
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
        dp, transport = _cdp_instance(kind="light", category="light", state={"state": "OFF"})
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

    # ---- G1: light HS colour read-back ----

    def test_light_hs_color_reads_nested_color_object(self) -> None:
        dp, _ = _cdp_instance(
            kind="light_color",
            category="light",
            capabilities={"color": True, "brightness": True},
            state={"state": "ON", "color_mode": "hs", "color": {"h": 210, "s": 0.8}},
        )
        assert isinstance(dp, CustomDpDimmer)
        # hue passthrough (degrees); daemon saturation [0,1] → HA [0,100].
        assert dp.hs_color == (210.0, 80.0)

    def test_light_hs_color_falls_back_to_flat_keys(self) -> None:
        dp, _ = _cdp_instance(
            kind="light_color",
            category="light",
            capabilities={"color": True},
            state={"state": "ON", "hue": 120, "saturation": 50},
        )
        # Legacy flat keys (pre-0.8.0 daemon) pass through unscaled.
        assert dp.hs_color == (120.0, 50.0)

    def test_light_hs_color_none_without_colour(self) -> None:
        dp, _ = _cdp_instance(kind="light", category="light", state={"state": "ON"})
        assert dp.hs_color is None

    # ---- G2: text-display option lists ----

    def test_text_display_option_lists_read_from_state(self) -> None:
        dp, _ = _cdp_instance(
            kind="text_display",
            category="text_display",
            supported=("write", "clear"),
            state={
                "available_icons": ["icon1", "icon2"],
                "available_background_colors": ["WHITE", "BLACK"],
                "available_text_colors": ["RED"],
                "available_alignments": ["LEFT", "CENTER"],
            },
        )
        assert isinstance(dp, CustomDpTextDisplay)
        assert dp.available_icons == ("icon1", "icon2")
        assert dp.available_background_colors == ("WHITE", "BLACK")
        assert dp.available_text_colors == ("RED",)
        assert dp.available_alignments == ("LEFT", "CENTER")
        assert dp.available_sounds == ()  # omitted from state → empty
        assert dp.has_icons is True
        assert dp.has_sounds is False


class TestGenericSetOnTime:
    """G7: generic switch set_on_time writes the sibling ON_TIME parameter."""

    def _switch_store(self, *, with_on_time: bool) -> tuple[LoomStore, _FakeTransport]:
        transport = _FakeTransport()
        store = LoomStore(transport=transport)  # type: ignore[arg-type]
        store.set_data_point_factory(factory=make_generic_data_point)
        dps = [
            DataPointSummary.model_validate(
                {
                    "parameter": "STATE",
                    "type": "BOOL",
                    "value": False,
                    "observed": True,
                    "operations": {"read": True, "write": True, "event": True},
                }
            )
        ]
        if with_on_time:
            dps.append(
                DataPointSummary.model_validate(
                    {
                        "parameter": "ON_TIME",
                        "type": "FLOAT",
                        "value": 0.0,
                        "observed": True,
                        "operations": {"read": True, "write": True, "event": True},
                    }
                )
            )
        store.attach_channel_data_points(device_address="VCU1", channel_number=1, data_points=dps)
        return store, transport

    async def test_set_on_time_writes_on_time_value(self) -> None:
        store, transport = self._switch_store(with_on_time=True)
        dp = store.get_data_point(address="VCU1", channel=1, parameter="STATE")
        assert isinstance(dp, DpSwitch)
        await dp.set_on_time(on_time=5)
        method, path, body = transport.calls[-1]
        assert method == "PUT"
        assert path.endswith("/channels/1/data-points/ON_TIME/value")
        assert body == {"value": 5}

    async def test_set_on_time_noop_when_channel_lacks_on_time(self) -> None:
        store, transport = self._switch_store(with_on_time=False)
        dp = store.get_data_point(address="VCU1", channel=1, parameter="STATE")
        assert isinstance(dp, DpSwitch)
        await dp.set_on_time(on_time=5)
        assert transport.calls == []  # no write attempted


class _FakeHubOps:
    """Stand-in for ``client.hub`` recording how often the message lists are fetched."""

    def __init__(self, *, alarms: list[Any] | None = None, services: list[Any] | None = None) -> None:
        self._alarms = list(alarms or ())
        self._services = list(services or ())
        self.alarm_calls = 0
        self.service_calls = 0

    async def list_alarm_messages(self) -> list[Any]:
        self.alarm_calls += 1
        return list(self._alarms)

    async def list_service_messages(self) -> list[Any]:
        self.service_calls += 1
        return list(self._services)


class _FakeSystemOps:
    """Stand-in for ``client.system`` returning a fixed interface list."""

    def __init__(self, *, interfaces: list[Any] | None = None) -> None:
        self._interfaces = list(interfaces or ())

    async def list_interfaces(self) -> list[Any]:
        return list(self._interfaces)


class _FakeHubClient:
    def __init__(self, *, store: LoomStore, hub: _FakeHubOps, system: _FakeSystemOps) -> None:
        self.store = store
        self.hub = hub
        self.system = system


def _iface(*, ident: str = "HmIP-RF", central_id: str = "home", connected: bool = True) -> SimpleNamespace:
    return SimpleNamespace(id=ident, interface=ident, central_id=central_id, connected=connected)


class TestHubPushRouting:
    """G6: hub push broadcasts route onto the singletons and emit HA state-changed."""

    def _coordinator(
        self, *, hub: _FakeHubOps | None = None, interfaces: list[Any] | None = None
    ) -> tuple[_HubCoordinator, EventBus, Any, Looper, list[str]]:
        store = LoomStore()
        store.set_serial(serial="ABC1234567")
        store.set_central_name(central_name="home")
        looper = Looper()
        ha_bus = AioEventBus(task_scheduler=looper)
        seen: list[str] = []
        ha_bus.create_subscription_group(name="entity").subscribe(
            event_type=DataPointStateChangedEvent,
            event_key=None,
            handler=lambda *, event: seen.append(event.unique_id),
        )
        coord = _HubCoordinator(
            client=_FakeHubClient(store=store, hub=hub or _FakeHubOps(), system=_FakeSystemOps(interfaces=interfaces)),
            ha_bus=ha_bus,
        )
        loom_bus = EventBus()
        group = loom_bus.create_subscription_group(name="push")
        return coord, loom_bus, group, looper, seen

    async def test_inbox_push_updates_singleton_and_emits(self) -> None:
        coord, loom_bus, group, looper, seen = self._coordinator()
        await coord._ensure_singletons()
        coord.install_push_routing(group=group)
        await loom_bus.publish(
            event=HubInboxChangedEvent(
                seq=1,
                kind=Kind.change,
                ts="2026-06-21T08:00:00Z",
                payload=HubCountChangedPayload(central="home", count=3),
            )
        )
        await looper.block_till_done()
        assert coord._inbox_dp.value == 3
        assert seen == [coord._inbox_dp.unique_id]

    async def test_metrics_push_routes_by_legacy_name(self) -> None:
        coord, loom_bus, group, looper, seen = self._coordinator()
        await coord._ensure_singletons()
        coord.install_push_routing(group=group)
        await loom_bus.publish(
            event=HubMetricsChangedEvent(
                seq=1,
                kind=Kind.change,
                ts="2026-06-21T08:00:00Z",
                payload=HubMetricChangedPayload(central="home", metric="connection_latency_ms", value=12, unit="ms"),
            )
        )
        await looper.block_till_done()
        assert coord._metrics_dps.connection_latency.value == 12
        assert seen == [coord._metrics_dps.connection_latency.unique_id]

    async def test_connectivity_push_updates_interface_sensor(self) -> None:
        coord, loom_bus, group, looper, seen = self._coordinator(interfaces=[_iface()])
        await coord._ensure_singletons()
        coord.install_push_routing(group=group)
        await loom_bus.publish(
            event=HubConnectivityChangedEvent(
                seq=1,
                kind=Kind.change,
                ts="2026-06-21T08:00:00Z",
                payload=HubConnectivityChangedPayload(
                    central="home", interface_id="HmIP-RF", reachable=False, latency_ms=None
                ),
            )
        )
        await looper.block_till_done()
        entry = coord._connectivity_dps["HmIP-RF"]
        assert entry.sensor.value is False
        assert seen == [entry.sensor.unique_id]

    async def test_alarm_count_push_refetches_then_skips_when_unchanged(self) -> None:
        hub = _FakeHubOps(alarms=[SimpleNamespace(central="home", name="Low battery", device_name="Sensor")])
        coord, loom_bus, group, looper, seen = self._coordinator(hub=hub)
        await coord._ensure_singletons()
        coord.install_push_routing(group=group)

        def _push() -> HubAlarmMessageCountChangedEvent:
            return HubAlarmMessageCountChangedEvent(
                seq=1,
                kind=Kind.change,
                ts="2026-06-21T08:00:00Z",
                payload=HubCountChangedPayload(central="home", count=1),
            )

        await loom_bus.publish(event=_push())
        await looper.block_till_done()
        assert hub.alarm_calls == 1  # None → 1 delta: list refetched
        assert coord._alarm_messages_dp.value == 1
        assert seen == [coord._alarm_messages_dp.unique_id]

        await loom_bus.publish(event=_push())
        await looper.block_till_done()
        assert hub.alarm_calls == 1  # 1 == 1: no refetch, no new emit
        assert seen == [coord._alarm_messages_dp.unique_id]

    async def test_push_for_other_central_is_ignored(self) -> None:
        coord, loom_bus, group, looper, seen = self._coordinator()
        await coord._ensure_singletons()
        coord.install_push_routing(group=group)
        await loom_bus.publish(
            event=HubInboxChangedEvent(
                seq=1,
                kind=Kind.change,
                ts="2026-06-21T08:00:00Z",
                payload=HubCountChangedPayload(central="other-ccu", count=9),
            )
        )
        await looper.block_till_done()
        assert coord._inbox_dp.value is None  # foreign central → not applied
        assert seen == []


class TestRefreshBridge:
    def _ha_setup(self) -> tuple[Looper, AioEventBus, list[str]]:
        """Build a real aiohomematic bus and collect the ``unique_id`` of each published state event."""
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

    async def test_value_change_becomes_state_changed(self) -> None:
        # Payload without unique_id → the bridge rebuilds the canonical key
        # from the store's serial suffix. A normal device carries no prefix.
        bus = EventBus()
        group = bus.create_subscription_group(name="t")
        looper, ha_bus, seen = self._ha_setup()
        install_refresh_bridge(group=group, store=LoomStore(), ha_bus=ha_bus, central_name="home")
        await bus.publish(
            event=DataPointValueChangedEvent(
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
        await looper.block_till_done()
        assert seen == ["loom_vcu1_1_state"]

    async def test_value_change_consumes_payload_unique_id(self) -> None:
        # When the daemon supplies unique_id, the bridge uses it verbatim
        # (no rebuild) — the drift-free path.
        bus = EventBus()
        group = bus.create_subscription_group(name="t")
        looper, ha_bus, seen = self._ha_setup()
        install_refresh_bridge(group=group, store=LoomStore(), ha_bus=ha_bus, central_name="home")
        await bus.publish(
            event=DataPointValueChangedEvent(
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
        await looper.block_till_done()
        assert seen == ["loom_vcu1_1_state"]

    async def test_custom_and_sysvar_changes_become_state_changed(self) -> None:
        bus = EventBus()
        group = bus.create_subscription_group(name="t")
        store = LoomStore()
        store.set_serial(serial="3014F711A0001234")  # serial suffix → 11a0001234
        looper, ha_bus, seen = self._ha_setup()
        install_refresh_bridge(group=group, store=store, ha_bus=ha_bus, central_name="home")
        await bus.publish(
            event=CustomDataPointStateChangedEvent(
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
            event=SysvarChangedEvent(
                seq=3,
                kind=Kind.change,
                ts="2026-05-24T08:00:00Z",
                payload=SysvarChangedPayload.model_validate({"central": "home", "name": "My Var", "value": 1.0}),
            )
        )
        # Rebuilt canonical keys: a custom DP keys on its primary channel
        # address (no serial prefix for the non-virtual VCU1); a sysvar on
        # ``loom_<serial>_sysvar_<hub_slug(name)>`` (space folds to a dash).
        await looper.block_till_done()
        assert seen == ["loom_vcu1_1", "loom_11a0001234_sysvar_my-var"]

    async def test_optimistic_rollback_broadcast_becomes_public_event(self) -> None:
        bus = EventBus()
        group = bus.create_subscription_group(name="t")
        looper, ha_bus, _ = self._ha_setup()
        install_refresh_bridge(group=group, store=LoomStore(), ha_bus=ha_bus, central_name="home")
        seen: list[OptimisticRollbackEvent] = []
        ha_group = ha_bus.create_subscription_group(name="rollback")
        ha_group.subscribe(
            event_type=OptimisticRollbackEvent,
            event_key=None,
            handler=lambda *, event: seen.append(event),
        )
        await bus.publish(
            event=DataPointOptimisticRolledBackEvent(
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
        await looper.block_till_done()
        assert len(seen) == 1
        ev = seen[0]
        # Raw daemon broadcast → real aiohomematic OptimisticRollbackEvent;
        # sent→rolled_back, present→restored, addressed via the DataPointKey.
        assert ev.dpk.channel_address == "VCU1:1"
        assert ev.dpk.parameter == "LEVEL"
        assert ev.dpk.paramset_key == ParamsetKey.VALUES
        assert ev.rolled_back_value == 0.8
        assert ev.restored_value == 0.5
        assert ev.reason == "timeout"


class TestEventBridge:
    """The bridge republishes daemon wire events as real aiohomematic events."""

    @staticmethod
    def _wire() -> tuple[EventBus, Looper, AioEventBus]:
        bus = EventBus()
        group = bus.create_subscription_group(name="t")
        looper = Looper()
        ha_bus = AioEventBus(task_scheduler=looper)
        install_refresh_bridge(group=group, store=LoomStore(), ha_bus=ha_bus, central_name="home")
        return bus, looper, ha_bus

    async def test_device_trigger_becomes_aiohomematic_event(self) -> None:
        bus, looper, ha_bus = self._wire()
        seen: list[AioDeviceTriggerEvent] = []
        ha_bus.create_subscription_group(name="x").subscribe(
            event_type=AioDeviceTriggerEvent,
            event_key=None,
            handler=lambda *, event: seen.append(event),
        )
        await bus.publish(
            event=LoomDeviceTriggerEvent(
                seq=1,
                kind=Kind.change,
                ts="2026-05-24T08:00:00Z",
                payload=DeviceTriggerPayload.model_validate(
                    {
                        "central": "home",
                        "interface_id": "home:HmIP-RF",
                        "device_address": "VCU1",
                        "channel": 1,
                        "event_type": "keypress",  # daemon short token
                        "parameter": "PRESS_SHORT",
                        "value": True,
                    }
                ),
            )
        )
        await looper.block_till_done()
        assert len(seen) == 1
        ev = seen[0]
        assert ev.trigger_type == DeviceTriggerEventType.KEYPRESS
        assert ev.interface_id == "home:HmIP-RF"
        assert ev.device_address == "VCU1"
        assert ev.channel_no == 1
        assert ev.parameter == "PRESS_SHORT"
        assert ev.value is True

    async def test_central_state_change_becomes_aiohomematic_event(self) -> None:
        bus, looper, ha_bus = self._wire()
        seen: list[AioCentralStateChangedEvent] = []
        # Routed by the central name, exactly as control_unit subscribes.
        ha_bus.create_subscription_group(name="x").subscribe(
            event_type=AioCentralStateChangedEvent,
            event_key="home",
            handler=lambda *, event: seen.append(event),
        )
        await bus.publish(
            event=LoomCentralStateChangedEvent(
                seq=1,
                kind=Kind.change,
                ts="2026-05-24T08:00:00Z",
                payload=CentralStateChangedPayload.model_validate(
                    {"central": "home", "old_state": "starting", "new_state": "running"}
                ),
            )
        )
        await looper.block_till_done()
        assert len(seen) == 1
        assert seen[0].new_state == CentralState.RUNNING
        assert seen[0].central_name == "home"

    async def test_device_create_remove_become_lifecycle_events(self) -> None:
        bus, looper, ha_bus = self._wire()
        seen: list[DeviceLifecycleEvent] = []
        ha_bus.create_subscription_group(name="x").subscribe(
            event_type=DeviceLifecycleEvent,
            event_key=None,
            handler=lambda *, event: seen.append(event),
        )
        await bus.publish(
            event=DeviceCreatedEvent(
                seq=1,
                kind=Kind.change,
                ts="2026-05-24T08:00:00Z",
                payload=DeviceCreatedPayload.model_validate(
                    {
                        "central": "home",
                        "interface_id": "home:HmIP-RF",
                        "device_address": "VCU1",
                        "model": "HmIP-PSM",
                    }
                ),
            )
        )
        await bus.publish(
            event=DeviceRemovedEvent(
                seq=2,
                kind=Kind.change,
                ts="2026-05-24T08:00:00Z",
                payload=DeviceRemovedPayload.model_validate(
                    {"central": "home", "interface_id": "home:HmIP-RF", "device_address": "VCU1"}
                ),
            )
        )
        await looper.block_till_done()
        assert [e.event_type for e in seen] == [
            DeviceLifecycleEventType.CREATED,
            DeviceLifecycleEventType.REMOVED,
        ]
        assert seen[0].device_addresses == ("VCU1",)

    async def test_data_points_created_groups_by_aiohomematic_category(self) -> None:
        # Regression: the loom DataPointCategory StrEnum's ``str()`` yields its
        # repr (``DataPointCategory.BinarySensor``), so the bootstrap must map
        # to the aiohomematic category by ``.value`` — not ``str()``.
        central = await _adapter()
        central._client.store.attach_channel_data_points(
            device_address="VCU1",
            channel_number=1,
            data_points=[
                DataPointSummary.model_validate(
                    {
                        "parameter": "STATE",
                        "type": "BOOL",
                        "category": "binary_sensor",
                        "data_point_type": "binary_sensor",
                        "value": True,
                        "observed": True,
                        "operations": {"read": True, "write": False, "event": True},
                    }
                )
            ],
        )
        seen: list[AioDataPointsCreatedEvent] = []
        central.event_bus.create_subscription_group(name="x").subscribe(
            event_type=AioDataPointsCreatedEvent,
            event_key=None,
            handler=lambda *, event: seen.append(event),
        )
        await central._emit_data_points_created()
        await central._looper.block_till_done()
        assert len(seen) == 1
        assert AioDataPointCategory.BINARY_SENSOR in seen[0].new_data_points


class TestGenericDataPointFactory:
    """The generic factory trusts the daemon's category + translated name."""

    async def test_uses_daemon_category_over_heuristic_resolver(self) -> None:
        # A read-only ENUM with a value_list: the heuristic resolver would
        # pick a sensor (only BOOL → binary_sensor), but the daemon
        # authoritatively classifies the door state as a binary_sensor.
        # The factory must trust summary.category.
        central = await _adapter()
        central._client.store.attach_channel_data_points(
            device_address="VCU1",
            channel_number=1,
            data_points=[
                DataPointSummary.model_validate(
                    {
                        "parameter": "STATE",
                        "type": "ENUM",
                        "category": "binary_sensor",
                        "value_list": ["CLOSED", "OPEN", "TILTED"],
                        "value": "CLOSED",
                        "observed": True,
                        "operations": {"read": True, "write": False, "event": True},
                    }
                )
            ],
        )
        dps = [dp for dp in central._client.store.data_points if dp.parameter == "STATE"]
        assert len(dps) == 1
        assert isinstance(dps[0], DpBinarySensor)
        assert dps[0].category.value == "binary_sensor"

    async def test_falls_back_to_resolver_when_category_absent(self) -> None:
        # No daemon category → the heuristic resolver applies (read-only
        # non-BOOL → sensor).
        central = await _adapter()
        central._client.store.attach_channel_data_points(
            device_address="VCU1",
            channel_number=1,
            data_points=[
                DataPointSummary.model_validate(
                    {
                        "parameter": "TEMPERATURE",
                        "type": "FLOAT",
                        "value": 21.5,
                        "observed": True,
                        "operations": {"read": True, "write": False, "event": True},
                    }
                )
            ],
        )
        dps = [dp for dp in central._client.store.data_points if dp.parameter == "TEMPERATURE"]
        assert len(dps) == 1
        assert isinstance(dps[0], DpSensor)

    async def test_translated_name_from_summary_and_label_omitted(self) -> None:
        # The daemon supplies the locale-aware name; a label_omitted
        # "primary" parameter collapses to the device name (None).
        central = await _adapter()
        central._client.store.attach_channel_data_points(
            device_address="VCU1",
            channel_number=1,
            data_points=[
                DataPointSummary.model_validate(
                    {
                        "parameter": "LOW_BAT",
                        "type": "BOOL",
                        "category": "binary_sensor",
                        "translated_name": "Batterie",
                        "value": False,
                        "observed": True,
                        "operations": {"read": True, "write": False, "event": True},
                    }
                ),
                DataPointSummary.model_validate(
                    {
                        "parameter": "STATE",
                        "type": "ENUM",
                        "category": "binary_sensor",
                        "label_omitted": True,
                        "value_list": ["CLOSED", "OPEN"],
                        "value": "CLOSED",
                        "observed": True,
                        "operations": {"read": True, "write": False, "event": True},
                    }
                ),
            ],
        )
        by_param = {dp.parameter: dp for dp in central._client.store.data_points}
        assert by_param["LOW_BAT"].translated_name == "Batterie"
        assert by_param["STATE"].translated_name is None
