# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
HA-parity regression tests — round 5 (2026-06-12 ccu-twin comparison).

Pins the six fixes from the live Otto-Rem vs ccu-twin diff: climate
temperature fallbacks (+ CDP refresh ping), the ``none`` preset, the
siren-only combined duration number, the schedule discovery (climate
channels, CDP requirement, no climate switches), the foreign-central
leak, and the aiohomematic display-name schema (ch/vch markers, channel
renames, `` chN`` postfixes).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from aiohomematic.async_support import Looper
from aiohomematic.central.events import DataPointStateChangedEvent, EventBus as AioEventBus
from openccu_loom_types.rest import (
    CustomDPSummary,
    DataPointSummary,
    DeviceDetail,
    Schedule,
    Snapshot,
    SysvarSummary,
    WeekProfileResponse,
)
from openccu_loom_types.ws import DataPointValueChangedPayload

from openccu_loom_client.compat.aiohomematic.central import CentralConfig
from openccu_loom_client.compat.aiohomematic.central.adapter import LoomCentralAdapter
from openccu_loom_client.compat.aiohomematic.central.refresh import install_refresh_bridge
from openccu_loom_client.compat.aiohomematic.model.combined import CombinedDurationDp
from openccu_loom_client.compat.aiohomematic.model.custom import BaseCustomDpClimate, make_custom_data_point
from openccu_loom_client.compat.aiohomematic.model.generic import make_generic_data_point
from openccu_loom_client.compat.aiohomematic.model.naming import custom_name_parts, generic_translated_name
from openccu_loom_client.compat.aiohomematic.model.week_profile import ScheduleChannelSwitch, WeekProfileDp
from openccu_loom_client.events import DataPointValueChangedEvent, EventBus
from openccu_loom_client.store import LoomStore

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _dp_summary(**overrides: Any) -> DataPointSummary:
    payload: dict[str, Any] = {
        "parameter": "STATE",
        "type": "FLOAT",
        "value": None,
        "observed": True,
        "operations": {"read": True, "write": False, "event": True},
        "category": "sensor",
    }
    payload.update(overrides)
    payload.setdefault("unique_id", f"loom_test_{str(payload['parameter']).lower()}")
    return DataPointSummary.model_validate(payload)


def _cdp_summary(**overrides: Any) -> CustomDPSummary:
    payload: dict[str, Any] = {
        "name": "SET_POINT_TEMPERATURE@1",
        "category": "climate",
        "channel_no": 1,
        "supported_operations": [],
        "kind": "climate_hmip",
    }
    payload.update(overrides)
    payload.setdefault("unique_id", f"loom_test_{str(payload['name']).lower()}")
    return CustomDPSummary.model_validate(payload)


def _store_with_device(
    *,
    address: str = "VCU1",
    model: str = "HmIP-eTRV-2",
    name: str = "Thermostat KU",
    channels: list[dict[str, Any]] | None = None,
) -> LoomStore:
    store = LoomStore()
    store.set_serial(serial="ABC1234567")
    store.set_central_name(central_name="home")
    store.set_data_point_factory(factory=make_generic_data_point)
    store.set_custom_data_point_factory(factory=make_custom_data_point)
    store.load_snapshot(
        snapshot=Snapshot.model_validate(
            {
                "generated_at": "2026-06-12T08:00:00Z",
                "devices": [
                    {
                        "address": address,
                        "interface": "home:HmIP-RF",
                        "model": model,
                        "name": name,
                        "available": True,
                        "channels_count": len(channels or []),
                    }
                ],
            }
        )
    )
    if channels:
        store.attach_device_detail(
            detail=DeviceDetail.model_validate(
                {
                    "address": address,
                    "interface": "home:HmIP-RF",
                    "model": model,
                    "name": name,
                    "available": True,
                    "channels_count": len(channels),
                    "channels": channels,
                }
            )
        )
    return store


def _channel(
    *,
    address: str,
    number: int,
    name: str,
    channel_type: str | None = None,
    is_custom_dp_primary: bool | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "address": f"{address}:{number}",
        "number": number,
        "name": name,
        "paramset_key": "VALUES",
        "data_points_count": 0,
    }
    if channel_type is not None:
        payload["type"] = channel_type
    if is_custom_dp_primary is not None:
        payload["is_custom_dp_primary"] = is_custom_dp_primary
    return payload


def _climate_store() -> LoomStore:
    store = _store_with_device(channels=[_channel(address="VCU1", number=1, name="Thermostat KU:1")])
    store.attach_channel_data_points(
        device_address="VCU1",
        channel_number=1,
        data_points=[
            _dp_summary(parameter="ACTUAL_TEMPERATURE", value=21.5),
            _dp_summary(parameter="SET_POINT_TEMPERATURE", value=19.0),
            _dp_summary(parameter="HUMIDITY", value=55, type="INTEGER"),
        ],
    )
    store.attach_custom_data_points(device_address="VCU1", cdps=[_cdp_summary()])
    return store


def _climate_cdp(store: LoomStore) -> BaseCustomDpClimate:
    cdp = store.get_custom_data_point(address="VCU1", name="SET_POINT_TEMPERATURE@1")
    assert isinstance(cdp, BaseCustomDpClimate)
    return cdp


# ---------------------------------------------------------------------------
# Fix A — climate temperatures fall back to the channel's generic DPs
# ---------------------------------------------------------------------------


class TestClimateGenericFallback:
    """The daemon climate state has no temperatures; field DPs back them."""

    def test_temperatures_from_generic_data_points(self) -> None:
        cdp = _climate_cdp(_climate_store())
        assert cdp.current_temperature == 21.5
        assert cdp.target_temperature == 19.0
        assert cdp.current_humidity == 55

    def test_state_keys_win_over_generic_dps(self) -> None:
        store = _climate_store()
        store.get_custom_data_point(address="VCU1", name="SET_POINT_TEMPERATURE@1")._replace_state(
            state={"current_temperature": 23.0, "set_temperature": 20.0, "current_humidity": 40}
        )
        cdp = _climate_cdp(store)
        assert cdp.current_temperature == 23.0
        assert cdp.target_temperature == 20.0
        assert cdp.current_humidity == 40

    def test_unobserved_generic_dp_reads_none(self) -> None:
        store = _store_with_device(channels=[_channel(address="VCU1", number=1, name="Thermostat KU:1")])
        store.attach_channel_data_points(
            device_address="VCU1",
            channel_number=1,
            data_points=[_dp_summary(parameter="ACTUAL_TEMPERATURE", value=0.0, observed=False)],
        )
        store.attach_custom_data_points(device_address="VCU1", cdps=[_cdp_summary()])
        cdp = _climate_cdp(store)
        assert cdp.current_temperature is None
        assert cdp.target_temperature is None
        assert cdp.current_humidity is None


def _switch_cdp_summary(**overrides: Any) -> CustomDPSummary:
    payload: dict[str, Any] = {
        "name": "STATE@3",
        "category": "switch",
        "channel_no": 3,
        "supported_operations": ["turn_on", "turn_off"],
        "kind": "switch",
    }
    payload.update(overrides)
    payload.setdefault("unique_id", f"loom_test_{str(payload['name']).lower()}")
    return CustomDPSummary.model_validate(payload)


def _switch_store(*, state_observed: bool = True, state_value: bool = True) -> LoomStore:
    """Channel-group switch device with a generic STATE DP on ch3."""
    store = _store_with_device(
        address="VCU9",
        model="HMIP-PS",
        name="Bücherregal",
        channels=[_channel(address="VCU9", number=3, name="Bücherregal:3")],
    )
    store.attach_channel_data_points(
        device_address="VCU9",
        channel_number=3,
        data_points=[
            _dp_summary(
                parameter="STATE",
                type="BOOL",
                value=state_value,
                observed=state_observed,
                operations={"read": True, "write": True, "event": True},
                category="switch",
            )
        ],
    )
    store.attach_custom_data_points(device_address="VCU9", cdps=[_switch_cdp_summary()])
    return store


def _switch_cdp(store: LoomStore) -> Any:
    cdp = store.get_custom_data_point(address="VCU9", name="STATE@3")
    assert cdp is not None
    return cdp


class TestSwitchGenericFallback:
    """A channel-group switch falls back to its generic STATE DP."""

    def test_value_from_generic_state_dp(self) -> None:
        # No CDP state delivered (the channel-group state_changed bug); the
        # generic STATE DP on ch3 backs the value.
        cdp = _switch_cdp(_switch_store(state_value=True))
        assert cdp.value is True
        assert cdp.is_on is True

    def test_off_from_generic_state_dp(self) -> None:
        cdp = _switch_cdp(_switch_store(state_value=False))
        assert cdp.value is False
        assert cdp.is_on is False

    def test_cdp_state_key_wins_over_generic_dp(self) -> None:
        store = _switch_store(state_value=False)
        _switch_cdp(store)._replace_state(state={"is_on": True})
        cdp = _switch_cdp(store)
        assert cdp.value is True
        assert cdp.is_on is True

    def test_unobserved_generic_dp_reads_none(self) -> None:
        cdp = _switch_cdp(_switch_store(state_observed=False))
        assert cdp.value is None
        assert cdp.is_on is False


class TestRefreshBridgePingsChannelCdp:
    """A field-DP value change re-renders the channel's custom data point."""

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

    async def _publish_value_changed(self, store: LoomStore) -> list[str]:
        bus = EventBus()
        group = bus.create_subscription_group(name="t")
        looper, ha_bus, seen = self._ha_setup()
        install_refresh_bridge(group=group, store=store, ha_bus=ha_bus, central_name="home")
        await bus.publish(
            event=DataPointValueChangedEvent(
                seq=1,
                kind="change",  # type: ignore[arg-type]
                ts="2026-06-12T08:00:00Z",
                payload=DataPointValueChangedPayload.model_validate(
                    {
                        "central": "home",
                        "device_address": "VCU1",
                        "channel": 1,
                        "parameter": "ACTUAL_TEMPERATURE",
                        "paramset_key": "VALUES",
                        "value": 22.0,
                        "modified_at": "2026-06-12T08:00:00Z",
                        "unique_id": "loom_vcu1_1_actual_temperature",
                    }
                ),
            )
        )
        await looper.block_till_done()
        return seen

    async def test_channel_with_cdp_gets_extra_ping(self) -> None:
        seen = await self._publish_value_changed(_climate_store())
        assert "loom_vcu1_1_actual_temperature" in seen
        assert "loom_vcu1_1" in seen

    async def test_channel_without_cdp_pings_generic_only(self) -> None:
        seen = await self._publish_value_changed(LoomStore())
        assert seen == ["loom_vcu1_1_actual_temperature"]


# ---------------------------------------------------------------------------
# Fix B — preset list always carries 'none'
# ---------------------------------------------------------------------------


class TestClimateProfilesIncludeNone:
    """aiohomematic always lists ClimateProfile.NONE; the daemon omits it."""

    def _cdp(self, preset_modes: list[str]) -> BaseCustomDpClimate:
        cdp = make_custom_data_point(
            summary=_cdp_summary(config={"preset_modes": preset_modes}),
            device_address="VCU1",
            store=LoomStore(),
        )
        assert isinstance(cdp, BaseCustomDpClimate)
        return cdp

    def test_none_inserted_after_control_block(self) -> None:
        profiles = self._cdp(["boost", "week_program_1", "week_program_2"]).profiles
        assert [p.value for p in profiles] == ["boost", "none", "week_program_1", "week_program_2"]

    def test_rf_control_block_order(self) -> None:
        profiles = self._cdp(["boost", "comfort", "eco", "week_program_1"]).profiles
        assert [p.value for p in profiles] == ["boost", "comfort", "eco", "none", "week_program_1"]

    def test_existing_none_not_duplicated(self) -> None:
        profiles = self._cdp(["boost", "none", "week_program_1"]).profiles
        assert [p.value for p in profiles] == ["boost", "none", "week_program_1"]

    def test_empty_list_yields_none_only(self) -> None:
        assert [p.value for p in self._cdp([]).profiles] == ["none"]


# ---------------------------------------------------------------------------
# adapter harness for Fix C / Fix D
# ---------------------------------------------------------------------------


class _FakeSchedulesOps:
    """Schedule operations backed by in-memory maps; missing keys raise (404)."""

    def __init__(
        self,
        *,
        week_profiles: dict[tuple[str, int], WeekProfileResponse] | None = None,
        schedules: dict[tuple[str, int], Schedule] | None = None,
    ) -> None:
        self.week_profiles = week_profiles or {}
        self.schedules = schedules or {}

    async def get_channel_week_profile(self, *, address: str, channel: int) -> WeekProfileResponse:
        if (key := (address, channel)) not in self.week_profiles:
            raise LookupError(f"no week profile on {address}:{channel}")
        return self.week_profiles[key]

    async def get_channel_schedule(self, *, address: str, channel: int) -> Schedule:
        if (key := (address, channel)) not in self.schedules:
            raise LookupError(f"no schedule on {address}:{channel}")
        return self.schedules[key]

    async def set_channel_lock(self, **_kwargs: Any) -> None:
        return None


def _adapter_for(store: LoomStore, *, schedules: _FakeSchedulesOps | None = None) -> Any:
    """Build a LoomCentralAdapter around a pre-populated store (no I/O)."""
    client = SimpleNamespace(
        store=store,
        schedules=schedules or _FakeSchedulesOps(),
        config=SimpleNamespace(http_base_url="http://loom.test"),
        events=None,
    )
    return LoomCentralAdapter(client=client, name="home")  # type: ignore[arg-type]


def _week_profile_response(
    *,
    address: str,
    channel: int,
    schedule_type: str = "default",
    schedule_enabled: dict[str, bool] | None = None,
) -> WeekProfileResponse:
    return WeekProfileResponse.model_validate(
        {
            "address": f"{address}:{channel}",
            "unique_id": f"loom_week_profile_{address.lower()}_week_profile",
            "schedule_type": schedule_type,
            "min_temp": 4.5 if schedule_type == "climate" else 0,
            "max_temp": 30.5 if schedule_type == "climate" else 0,
            "profile_count": 3 if schedule_type == "climate" else 1,
            "schedule_enabled": schedule_enabled,
            "has_climate_schedule": schedule_type == "climate",
        }
    )


# ---------------------------------------------------------------------------
# Fix C — combined duration spawns only on siren CDP channels
# ---------------------------------------------------------------------------


def _attach_duration_pair(store: LoomStore, *, address: str, channel: int) -> None:
    store.attach_channel_data_points(
        device_address=address,
        channel_number=channel,
        data_points=[
            _dp_summary(parameter="DURATION_VALUE", value=1.0, min=0, max=60),
            _dp_summary(parameter="DURATION_UNIT", value=0, type="ENUM"),
        ],
    )


class TestCombinedDurationSirenOnly:
    """aiohomematic's only visible combined timer sits on CustomDpIpSiren."""

    async def test_siren_channel_spawns_combined_number(self) -> None:
        store = _store_with_device(
            address="VCU1",
            model="HmIP-ASIR",
            name="Alarmsirene",
            channels=[_channel(address="VCU1", number=3, name="Alarmsirene:3")],
        )
        _attach_duration_pair(store, address="VCU1", channel=3)
        store.attach_custom_data_points(
            device_address="VCU1",
            cdps=[_cdp_summary(name="ACOUSTIC@3", category="siren", kind="siren", channel_no=3)],
        )
        adapter = _adapter_for(store)
        await adapter._bootstrap_combined_data_points()
        combined = [dp for dp in adapter._extra_data_points if isinstance(dp, CombinedDurationDp)]
        assert len(combined) == 1
        assert combined[0].unique_id == "loom_combined_vcu1_3_duration"

    async def test_duration_pair_without_siren_cdp_spawns_nothing(self) -> None:
        store = _store_with_device(
            address="VCU1",
            model="HmIP-MP3P",
            name="Türgong",
            channels=[_channel(address="VCU1", number=2, name="Türgong:2")],
        )
        _attach_duration_pair(store, address="VCU1", channel=2)
        adapter = _adapter_for(store)
        await adapter._bootstrap_combined_data_points()
        assert adapter._extra_data_points == []

    async def test_sound_player_cdp_spawns_nothing(self) -> None:
        # Sound players declare the DURATION pair too, but aiohomematic's
        # combined timer is invisible there — no number entity.
        store = _store_with_device(
            address="VCU1",
            model="HmIP-MP3P",
            name="Türgong",
            channels=[_channel(address="VCU1", number=2, name="Türgong:2")],
        )
        _attach_duration_pair(store, address="VCU1", channel=2)
        store.attach_custom_data_points(
            device_address="VCU1",
            cdps=[_cdp_summary(name="LEVEL@2", category="siren", kind="siren_sound", channel_no=2)],
        )
        adapter = _adapter_for(store)
        await adapter._bootstrap_combined_data_points()
        assert adapter._extra_data_points == []


# ---------------------------------------------------------------------------
# Fix D — schedule discovery
# ---------------------------------------------------------------------------


class TestScheduleDiscovery:
    """Week profiles need a CDP; climate probes the CDP channel, no switches."""

    async def test_non_climate_device_spawns_profile_and_switches(self) -> None:
        store = _store_with_device(
            address="VCU1",
            model="HMIP-PSM",
            name="Schalter",
            channels=[
                _channel(address="VCU1", number=3, name="Schalter:3"),
                _channel(
                    address="VCU1",
                    number=8,
                    name="Schalter Wochenprogramm",
                    channel_type="SWITCH_WEEK_PROFILE",
                ),
            ],
        )
        store.attach_custom_data_points(
            device_address="VCU1",
            cdps=[_cdp_summary(name="STATE@3", category="switch", kind="switch", channel_no=3)],
        )
        schedules = _FakeSchedulesOps(
            week_profiles={
                ("VCU1", 8): _week_profile_response(
                    address="VCU1", channel=8, schedule_enabled={"1_1": True, "1_2": False}
                )
            }
        )
        adapter = _adapter_for(store, schedules=schedules)
        await adapter._bootstrap_schedules()
        profiles = [dp for dp in adapter._extra_data_points if isinstance(dp, WeekProfileDp)]
        switches = [dp for dp in adapter._extra_data_points if isinstance(dp, ScheduleChannelSwitch)]
        assert len(profiles) == 1
        assert {sw.channel_key for sw in switches} == {"1_1", "1_2"}

    async def test_climate_device_probes_cdp_channel_without_switches(self) -> None:
        store = _store_with_device(channels=[_channel(address="VCU1", number=1, name="Thermostat KU:1")])
        store.attach_custom_data_points(device_address="VCU1", cdps=[_cdp_summary()])
        schedules = _FakeSchedulesOps(
            week_profiles={("VCU1", 1): _week_profile_response(address="VCU1", channel=1, schedule_type="climate")}
        )
        adapter = _adapter_for(store, schedules=schedules)
        await adapter._bootstrap_schedules()
        profiles = [dp for dp in adapter._extra_data_points if isinstance(dp, WeekProfileDp)]
        switches = [dp for dp in adapter._extra_data_points if isinstance(dp, ScheduleChannelSwitch)]
        assert len(profiles) == 1
        assert profiles[0].unique_id == "loom_week_profile_vcu1_week_profile"
        assert switches == []

    async def test_climate_device_with_week_profile_channel_spawns_switches(self) -> None:
        # HmIP-WGTC carries a climate CDP yet exposes its schedule on a
        # dedicated WEEK_PROFILE channel — the switches follow the channel,
        # not the mere presence of a climate CDP.
        store = _store_with_device(
            address="VCU1",
            model="HmIP-WGTC",
            name="Gefahrenmelder",
            channels=[
                _channel(address="VCU1", number=1, name="Gefahrenmelder:1"),
                _channel(
                    address="VCU1",
                    number=7,
                    name="Gefahrenmelder Wochenprogramm",
                    channel_type="SWITCH_WEEK_PROFILE",
                ),
            ],
        )
        store.attach_custom_data_points(
            device_address="VCU1",
            cdps=[_cdp_summary(name="SET_POINT_TEMPERATURE@1", category="climate", channel_no=1)],
        )
        schedules = _FakeSchedulesOps(
            week_profiles={
                ("VCU1", 7): _week_profile_response(
                    address="VCU1", channel=7, schedule_enabled={"1_1": True, "1_2": False}
                )
            }
        )
        adapter = _adapter_for(store, schedules=schedules)
        await adapter._bootstrap_schedules()
        profiles = [dp for dp in adapter._extra_data_points if isinstance(dp, WeekProfileDp)]
        switches = [dp for dp in adapter._extra_data_points if isinstance(dp, ScheduleChannelSwitch)]
        assert len(profiles) == 1
        assert {sw.channel_key for sw in switches} == {"1_1", "1_2"}

    async def test_device_without_cdp_spawns_nothing(self) -> None:
        # aiohomematic only initialises week profiles through a custom DP
        # (HmIP-MIO16-PCB: 24 surplus switches + 1 surplus sensor on loom).
        store = _store_with_device(
            address="VCU1",
            model="HmIP-MIO16-PCB",
            name="Steuerung",
            channels=[
                _channel(
                    address="VCU1",
                    number=49,
                    name="Steuerung Wochenprogramm",
                    channel_type="SWITCH_WEEK_PROFILE",
                )
            ],
        )
        schedules = _FakeSchedulesOps(
            week_profiles={
                ("VCU1", 49): _week_profile_response(address="VCU1", channel=49, schedule_enabled={"1_1": True})
            }
        )
        adapter = _adapter_for(store, schedules=schedules)
        await adapter._bootstrap_schedules()
        assert adapter._extra_data_points == []

    async def test_climate_404_is_tolerated(self) -> None:
        store = _store_with_device(channels=[_channel(address="VCU1", number=1, name="Thermostat KU:1")])
        store.attach_custom_data_points(device_address="VCU1", cdps=[_cdp_summary()])
        adapter = _adapter_for(store, schedules=_FakeSchedulesOps())
        await adapter._bootstrap_schedules()
        assert adapter._extra_data_points == []


# ---------------------------------------------------------------------------
# Fix E — foreign-central leak
# ---------------------------------------------------------------------------


def _snapshot_with_interfaces(centrals: list[str]) -> Snapshot:
    return Snapshot.model_validate(
        {
            "generated_at": "2026-06-12T08:00:00Z",
            "devices": [],
            "interfaces": [
                {
                    "id": f"{central}-HmIP-RF",
                    "name": f"{central}-HmIP-RF",
                    "connected": True,
                    "interface": "HmIP-RF",
                    "central_id": central,
                }
                for central in centrals
            ],
        }
    )


class TestCentralIdInference:
    """central_id must belong to the configured central, never a foreign one."""

    def test_multi_central_picks_configured_name(self) -> None:
        store = LoomStore()
        store.set_central_name(central_name="Otto-Rem")
        store.load_snapshot(snapshot=_snapshot_with_interfaces(["Kearney-Loc", "Otto-Rem"]))
        assert store.central_id == "Otto-Rem"

    def test_multi_central_without_match_stays_unset(self) -> None:
        store = LoomStore()
        store.set_central_name(central_name="Elsewhere")
        store.load_snapshot(snapshot=_snapshot_with_interfaces(["Kearney-Loc", "Otto-Rem"]))
        assert store.central_id == ""

    def test_single_central_adopts_daemon_name(self) -> None:
        store = LoomStore()
        store.set_central_name(central_name="My HA Instance")
        store.load_snapshot(snapshot=_snapshot_with_interfaces(["Kearney-Loc", "Kearney-Loc"]))
        assert store.central_id == "Kearney-Loc"

    async def test_foreign_sysvars_do_not_spawn(self) -> None:
        central = await CentralConfig(
            name="Otto-Rem", host="loom.test", port=8080, tls=False, token="tok-1"
        ).create_central()
        store = central._client.store
        store.load_snapshot(snapshot=_snapshot_with_interfaces(["Kearney-Loc", "Otto-Rem"]))
        for name, central_tag in (("svLocal", "Otto-Rem"), ("svForeign", "Kearney-Loc")):
            store._upsert_sysvar(
                summary=SysvarSummary.model_validate(
                    {
                        "name": name,
                        "type": "FLOAT",
                        "value_type": "FLOAT",
                        "value": 1.0,
                        "observed": True,
                        "central": central_tag,
                        "unique_id": f"loom_test_{name.lower()}",
                    }
                )
            )
        names = {dp.name for dp in central.hub_coordinator.get_hub_data_points() if hasattr(dp, "name")}
        assert "svLocal" in names
        assert "svForeign" not in names


# ---------------------------------------------------------------------------
# Fix F — aiohomematic display-name schema
# ---------------------------------------------------------------------------


class TestGenericTranslatedName:
    """Generic names follow get_data_point_name_data (channel + label + chN)."""

    def _store(self) -> LoomStore:
        store = _store_with_device(
            address="VCU1",
            model="HMIP-PSM",
            name="Belüftungsanlage",
            channels=[
                _channel(address="VCU1", number=0, name="Belüftungsanlage:0"),
                _channel(address="VCU1", number=2, name="Belüftungsanlage Schaltzustand"),
                _channel(address="VCU1", number=3, name="Belüftungsanlage:3"),
            ],
        )
        # STATE on two channels → the ch postfix applies.
        for channel in (2, 3):
            store.attach_channel_data_points(
                device_address="VCU1",
                channel_number=channel,
                data_points=[_dp_summary(parameter="STATE", value=True, label_omitted=True)],
            )
        return store

    def test_renamed_channel_with_omitted_label_keeps_channel_name(self) -> None:
        store = self._store()
        dp = store.get_data_point(address="VCU1", channel=2, parameter="STATE")
        assert dp.translated_name == "Schaltzustand ch2"

    def test_default_channel_with_omitted_label_collapses(self) -> None:
        store = self._store()
        dp = store.get_data_point(address="VCU1", channel=3, parameter="STATE")
        assert dp.translated_name == "ch3"

    def test_daemon_translation_passes_through(self) -> None:
        store = self._store()
        store.attach_channel_data_points(
            device_address="VCU1",
            channel_number=0,
            data_points=[_dp_summary(parameter="DUTY_CYCLE", value=False, translated_name="Duty Cycle")],
        )
        dp = store.get_data_point(address="VCU1", channel=0, parameter="DUTY_CYCLE")
        # Channel 0 never carries the chN postfix.
        assert dp.translated_name == "Duty Cycle"

    def test_renamed_channel_without_device_prefix(self) -> None:
        store = _store_with_device(
            address="VCU1",
            model="HmIP-MIO16-PCB",
            name="Steuerung",
            channels=[
                _channel(address="VCU1", number=18, name="Lüftung Hoch"),
                _channel(address="VCU1", number=22, name="Lüftung Normal"),
            ],
        )
        for channel in (18, 22):
            store.attach_channel_data_points(
                device_address="VCU1",
                channel_number=channel,
                data_points=[_dp_summary(parameter="STATE", value=False, label_omitted=True)],
            )
        dp = store.get_data_point(address="VCU1", channel=18, parameter="STATE")
        assert dp.translated_name == "Lüftung Hoch ch18"

    def test_missing_translation_is_suppressed_not_anglicised(self) -> None:
        store = self._store()
        store.attach_channel_data_points(
            device_address="VCU1",
            channel_number=0,
            data_points=[_dp_summary(parameter="WINDOW_OPEN", value=False)],
        )
        dp = store.get_data_point(address="VCU1", channel=0, parameter="WINDOW_OPEN")
        assert dp.translated_name is None


class TestCustomNameParts:
    """Custom names follow get_custom_data_point_name (ch/vch markers)."""

    def _psm_store(self) -> LoomStore:
        store = _store_with_device(
            address="VCU1",
            model="HMIP-PSM",
            name="Weinkühlschrank",
            channels=[
                _channel(address="VCU1", number=3, name="Weinkühlschrank:3", is_custom_dp_primary=True),
                _channel(address="VCU1", number=4, name="Weinkühlschrank:4", is_custom_dp_primary=False),
                _channel(address="VCU1", number=5, name="Weinkühlschrank:5", is_custom_dp_primary=False),
            ],
        )
        store.attach_custom_data_points(
            device_address="VCU1",
            cdps=[
                _cdp_summary(name=f"STATE@{no}", category="switch", kind="switch", channel_no=no) for no in (3, 4, 5)
            ],
        )
        return store

    def test_only_primary_channel_collapses_to_none(self) -> None:
        store = self._psm_store()
        cdp = store.get_custom_data_point(address="VCU1", name="STATE@3")
        assert cdp.translated_name is None

    def test_secondary_channels_get_vch_marker(self) -> None:
        store = self._psm_store()
        assert store.get_custom_data_point(address="VCU1", name="STATE@4").translated_name == "vch4"
        assert store.get_custom_data_point(address="VCU1", name="STATE@5").translated_name == "vch5"

    def test_multi_primary_devices_get_ch_marker(self) -> None:
        # HmIP-DRSI4 registers switch primaries on 6/10/14/18.
        store = _store_with_device(
            address="VCU1",
            model="HmIP-DRSI4",
            name="Schalter Dachboden",
            channels=[
                _channel(address="VCU1", number=6, name="Schalter Dachboden:6", is_custom_dp_primary=True),
                _channel(address="VCU1", number=10, name="Schalter Dachboden:10", is_custom_dp_primary=True),
            ],
        )
        store.attach_custom_data_points(
            device_address="VCU1",
            cdps=[_cdp_summary(name=f"STATE@{no}", category="switch", kind="switch", channel_no=no) for no in (6, 10)],
        )
        assert store.get_custom_data_point(address="VCU1", name="STATE@6").translated_name == "ch6"
        assert store.get_custom_data_point(address="VCU1", name="STATE@10").translated_name == "ch10"

    def test_renamed_channel_keeps_custom_name_with_marker(self) -> None:
        # HmIP-BSL: switch primary 4, secondaries 5/6; channel 5 renamed
        # 'Treppe:5' → 'Treppe vch5' (ccu twin: 'Signalleuchte FL Treppe vch5').
        store = _store_with_device(
            address="VCU1",
            model="HmIP-BSL",
            name="Signalleuchte FL",
            channels=[
                _channel(address="VCU1", number=4, name="Treppe"),
                _channel(address="VCU1", number=5, name="Treppe:5"),
            ],
        )
        store.attach_custom_data_points(
            device_address="VCU1",
            cdps=[_cdp_summary(name=f"STATE@{no}", category="switch", kind="switch", channel_no=no) for no in (4, 5)],
        )
        assert store.get_custom_data_point(address="VCU1", name="STATE@4").translated_name == "Treppe"
        assert store.get_custom_data_point(address="VCU1", name="STATE@5").translated_name == "Treppe vch5"

    def test_unknown_model_falls_back_to_lowest_channel_primary(self) -> None:
        store = _store_with_device(
            address="VCU1",
            model="Future-Device",
            name="Gerät",
            channels=[
                _channel(address="VCU1", number=1, name="Gerät:1"),
                _channel(address="VCU1", number=2, name="Gerät:2"),
            ],
        )
        store.attach_custom_data_points(
            device_address="VCU1",
            cdps=[_cdp_summary(name=f"STATE@{no}", category="switch", kind="switch", channel_no=no) for no in (1, 2)],
        )
        device = store.get_device(address="VCU1")
        assert custom_name_parts(store=store, device=device, channel_no=1, category_token="switch") == ("ch1", "ch1")
        assert custom_name_parts(store=store, device=device, channel_no=2, category_token="switch") == ("vch2", "vch2")

    def test_button_lock_postfix_renders_parameter_name(self) -> None:
        store = _store_with_device(
            address="VCU1",
            model="HmIP-WTH-2",
            name="Wandthermostat",
            channels=[_channel(address="VCU1", number=0, name="Wandthermostat:0")],
        )
        store.attach_custom_data_points(
            device_address="VCU1",
            cdps=[_cdp_summary(name="BUTTON_LOCK@0", category="lock", kind="lock", channel_no=0)],
        )
        cdp = store.get_custom_data_point(address="VCU1", name="BUTTON_LOCK@0")
        assert cdp.translated_name == "Button Lock"
        assert cdp.name_data.parameter_name == "Button Lock"


class TestGenericNamingHelper:
    """Direct checks of the shared naming helper edge cases."""

    def test_multi_channel_postfix_requires_second_channel(self) -> None:
        store = _store_with_device(channels=[_channel(address="VCU1", number=1, name="Thermostat KU:1")])
        store.attach_channel_data_points(
            device_address="VCU1",
            channel_number=1,
            data_points=[_dp_summary(parameter="HUMIDITY", value=55, translated_name="Luftfeuchtigkeit")],
        )
        device = store.get_device(address="VCU1")
        assert (
            generic_translated_name(
                store=store,
                device=device,
                channel_no=1,
                parameter="HUMIDITY",
                translation="Luftfeuchtigkeit",
                label_omitted=False,
            )
            == "Luftfeuchtigkeit"
        )


class TestEventGroupChannelName:
    """Event groups carry the channel-derived name (ChannelEventGroup.name)."""

    def _group(self, *, channel_name: str, channel_no: int = 1) -> Any:
        store = _store_with_device(
            address="VCU1",
            model="HmIP-BSM",
            name="Galerie",
            channels=[_channel(address="VCU1", number=channel_no, name=channel_name)],
        )
        store.attach_channel_data_points(
            device_address="VCU1",
            channel_number=channel_no,
            data_points=[
                _dp_summary(
                    parameter="PRESS_SHORT",
                    type="ACTION",
                    operations={"read": False, "write": True, "event": True},
                    category="button",
                )
            ],
        )
        from openccu_loom_client.compat.aiohomematic.model.event_group import build_event_groups

        groups = build_event_groups(store=store, central_id="abc1234567")
        assert len(groups) == 1
        return groups[0]

    def test_renamed_channel_keeps_custom_name(self) -> None:
        group = self._group(channel_name="Galerie aus")
        assert group.name == "aus"
        assert group.full_name == "Galerie aus"

    def test_default_channel_renders_ch_marker(self) -> None:
        group = self._group(channel_name="Galerie:1")
        assert group.name == "ch1"
        assert group.full_name == "Galerie ch1"


class TestLocalePlumbing:
    """The HA UI language reaches device.config_provider.config.locale."""

    async def test_central_config_threads_locale_to_devices(self) -> None:
        central = await CentralConfig(
            name="home",
            host="loom.test",
            port=8080,
            tls=False,
            token="tok-1",
            locale="de",
        ).create_central()
        store = central._client.store
        assert store.locale == "de"
        store.load_snapshot(
            snapshot=Snapshot.model_validate(
                {
                    "generated_at": "2026-06-12T08:00:00Z",
                    "devices": [
                        {
                            "address": "VCU1",
                            "interface": "home:HmIP-RF",
                            "model": "HmIP-eTRV-2",
                            "name": "Thermostat",
                            "available": True,
                            "channels_count": 1,
                        }
                    ],
                }
            )
        )
        device = store.get_device(address="VCU1")
        assert device.config_provider.config.locale == "de"

    def test_locale_defaults_to_english(self) -> None:
        assert LoomStore().locale == "en"


class TestSystemInformationCcuType:
    """The HA hub-update entity reads system_information.ccu_type."""

    def test_defaults_to_openccu(self) -> None:
        from aiohomematic.const import CCUType

        from openccu_loom_client.compat.aiohomematic.const import SystemInformation

        info = SystemInformation(serial="ABC", version="3.87")
        assert info.ccu_type == CCUType.OPENCCU


class TestCalculatedTranslatedName:
    """daemon api 1.5.0: calc DPs carry the locale-aware label."""

    def test_synthesized_summary_carries_translated_name(self) -> None:
        from openccu_loom_types.rest import CalculatedDPSummary

        from openccu_loom_client.compat.aiohomematic.model.calculated import synthesize_summary

        calc = CalculatedDPSummary.model_validate(
            {
                "name": "DEW_POINT",
                "category": "sensor",
                "value": 12.5,
                "observed": True,
                "translated_name": "Taupunkt",
                "unique_id": "loom_test_dew_point",
            }
        )
        assert synthesize_summary(calc=calc).translated_name == "Taupunkt"

    def test_calculated_name_uses_translated_label(self) -> None:
        """
        The calc DP's ``name`` is the daemon label, not the raw parameter.

        The HA integration builds the entity description name from
        ``name_data.name``; without this the generic fallback returns the raw
        parameter ("DEW_POINT") and the composed entity name reads "… DEW_POINT".
        """
        from openccu_loom_types.rest import CalculatedDPSummary

        from openccu_loom_client.compat.aiohomematic.model.calculated import make_calculated_data_point
        from openccu_loom_client.store import LoomStore

        calc = CalculatedDPSummary.model_validate(
            {
                "name": "DEW_POINT",
                "category": "sensor",
                "value": 12.5,
                "observed": True,
                "translated_name": "HEATING_CLIMATECONTROL_TRANSCEIVER Dew Point",
                "unique_id": "loom_test_dew_point",
            }
        )
        dp = make_calculated_data_point(summary=calc, device_address="VCU0000001", channel_number=1, store=LoomStore())
        assert dp.name == "HEATING_CLIMATECONTROL_TRANSCEIVER Dew Point"
        assert dp.name_data.name == "HEATING_CLIMATECONTROL_TRANSCEIVER Dew Point"

    def test_calculated_name_falls_back_to_parameter(self) -> None:
        """Without a daemon label, the calc DP name falls back to the parameter."""
        from openccu_loom_types.rest import CalculatedDPSummary

        from openccu_loom_client.compat.aiohomematic.model.calculated import make_calculated_data_point
        from openccu_loom_client.store import LoomStore

        calc = CalculatedDPSummary.model_validate(
            {
                "name": "ENTHALPY",
                "category": "sensor",
                "value": 1.0,
                "observed": True,
                "unique_id": "loom_test_enthalpy",
            }
        )
        dp = make_calculated_data_point(summary=calc, device_address="VCU0000001", channel_number=1, store=LoomStore())
        assert dp.name == "ENTHALPY"

    def test_combined_summary_carries_translated_name(self) -> None:
        from types import SimpleNamespace

        from openccu_loom_client.compat.aiohomematic.model.combined import _synthesize_summary

        value_dp = SimpleNamespace(min=0, max=600, summary=SimpleNamespace(unique_id="loom_test_duration"))
        summary = _synthesize_summary(value_dp=value_dp, translated_name="Zeitdauer")
        assert summary.translated_name == "Zeitdauer"
        assert summary.parameter == "DURATION"
