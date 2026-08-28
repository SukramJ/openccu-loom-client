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
from openccu_loom_types.rest import (
    AddonUpdateStatus,
    CustomDPSummary,
    DataPointSummary,
    DeviceDetail,
    HubDataPoints,
    Kind2 as Kind,
    ProgramSummary,
    SecuritySnapshot,
    Snapshot,
)
from openccu_loom_types.ws import (
    CentralStateChangedPayload,
    CustomDataPointStateChangedPayload,
    DaemonStatusPayload,
    DataPointValueChangedPayload,
    DeviceAvailabilityChangedPayload,
    DeviceCreatedPayload,
    DeviceMetadataChangedPayload,
    DeviceRemovedPayload,
    DeviceTriggerPayload,
    HubConnectivityChangedPayload,
    HubCountChangedPayload,
    HubMetricChangedPayload,
    HubSystemUpdateChangedPayload,
    InstallModeChangedPayload,
    OptimisticRollbackPayload,
    ScheduleChangedPayload,
    SecurityClassChangedPayload,
    SecurityFaultChangedPayload,
    SecurityNotificationPayload,
    SecurityStateChangedPayload,
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
    custom_unique_id,
    make_custom_data_point,
)
from openccu_loom_client.compat.aiohomematic.model.generic import (
    DpBinarySensor,
    DpSensor,
    DpSwitch,
    make_generic_data_point,
)
from openccu_loom_client.compat.aiohomematic.model.hub import make_program_data_points
from openccu_loom_client.events import (
    AddonUpdateStateChangedEvent,
    CentralStateChangedEvent as LoomCentralStateChangedEvent,
    CustomDataPointStateChangedEvent,
    DaemonStatusChangedEvent,
    DataPointValueChangedEvent,
    DeviceAvailabilityChangedEvent,
    DeviceCreatedEvent,
    DeviceMetadataChangedEvent,
    DeviceRemovedEvent,
    EventBus,
    HubAlarmMessageCountChangedEvent,
    HubConnectivityChangedEvent,
    HubInboxChangedEvent,
    HubMetricsChangedEvent,
    HubSystemUpdateChangedEvent,
    InstallModeChangedEvent,
    ScheduleChangedEvent,
    SecurityClassChangedEvent,
    SecurityFaultChangedEvent,
    SecurityNotificationEvent,
    SecurityStateChangedEvent,
    SysvarChangedEvent,
)
from openccu_loom_client.events.types import (
    DataPointOptimisticRolledBackEvent,
    DeviceTriggerEvent as LoomDeviceTriggerEvent,
)
from openccu_loom_client.store import LoomStore


async def _adapter():
    return await CentralConfig(name="home", host="loom.test", port=8080, tls=False, token="tok-1").create_central()


def _cdp(*, name: str, category: str, kind: str, unique_id: str | None = None) -> CustomDPSummary:
    return CustomDPSummary.model_validate(
        {
            "name": name,
            "category": category,
            "channel_no": 1,
            "supported_operations": ["open", "close", "set_position"],
            "kind": kind,
            "unique_id": unique_id or f"loom_test_{name}",
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
                            "interface_id": "home:HmIP-RF",
                            "updatable": False,
                            "update_available": False,
                            "master_pushes_config_pending": False,
                            "has_sub_devices": False,
                            "firmware": {
                                "Current": "1.0.0",
                                "Available": "",
                                "Updatable": False,
                                "UpdateState": "UP_TO_DATE",
                            },
                            "availability": {
                                "IsReachable": True,
                                "LastUpdated": None,
                                "BatteryLevel": None,
                                "LowBattery": None,
                                "SignalStrength": None,
                            },
                        }
                    ],
                }
            )
        )
        store.attach_custom_data_points(
            device_address="VCU1",
            cdps=[_cdp(name="cover", category="cover", kind="cover_blind", unique_id="loom_vcu1_1")],
        )
        store.apply_custom_data_point_state_changed(
            payload=CustomDataPointStateChangedPayload.model_validate(
                {
                    "central": "home",
                    "device_address": "VCU1",
                    "channel": 1,
                    "name": "cover",
                    "state": {"state": "open", "current_position": 42},
                    "unique_id": "loom_test_vcu1_1_cover",
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

    async def test_cover_motion_follows_the_daemon_not_the_raw_direction(self) -> None:
        """
        The daemon's ``state`` token decides, not the ``direction`` field.

        ``direction`` carries the CCU's raw travel direction; the daemon's
        token already accounts for a channel wired with inverted control,
        where "up" on the wire means closing. Deriving motion from
        ``direction`` reported the opposite of what the daemon determined.
        """
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
                            "interface_id": "home:HmIP-RF",
                            "updatable": False,
                            "update_available": False,
                            "master_pushes_config_pending": False,
                            "has_sub_devices": False,
                            "firmware": {
                                "Current": "1.0.0",
                                "Available": "",
                                "Updatable": False,
                                "UpdateState": "UP_TO_DATE",
                            },
                            "availability": {
                                "IsReachable": True,
                                "LastUpdated": None,
                                "BatteryLevel": None,
                                "LowBattery": None,
                                "SignalStrength": None,
                            },
                        }
                    ],
                }
            )
        )
        store.attach_custom_data_points(
            device_address="VCU1",
            cdps=[_cdp(name="cover", category="cover", kind="cover_blind", unique_id="loom_vcu1_1")],
        )

        def _apply(state: dict[str, object]) -> CustomDpCover:
            store.apply_custom_data_point_state_changed(
                payload=CustomDataPointStateChangedPayload.model_validate(
                    {
                        "central": "home",
                        "device_address": "VCU1",
                        "channel": 1,
                        "name": "cover",
                        "state": state,
                        "unique_id": "loom_test_vcu1_1_cover",
                    }
                )
            )
            cover = central.query_facade.get_data_points(data_point_type=DataPointType.Cover)[0]
            assert isinstance(cover, CustomDpCover)
            return cover

        # Inverted control: the wire says "up" while the daemon, which knows
        # the channel is inverted, reports closing.
        cover = _apply({"state": "closing", "direction": "opening", "current_position": 60})
        assert cover.is_closing is True
        assert cover.is_opening is False

        # And the mirror image.
        cover = _apply({"state": "opening", "direction": "closing", "current_position": 60})
        assert cover.is_opening is True
        assert cover.is_closing is False

        # Closed comes from the token too; the daemon derives it from
        # position 0, so the two agree.
        cover = _apply({"state": "closed", "current_position": 0})
        assert cover.is_closed is True
        assert cover.is_opening is False

        # A payload without a token still answers from the position.
        cover = _apply({"current_position": 0})
        assert cover.is_closed is True

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
                    "unique_id": "loom_test_vcu2_1_climate",
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
                    "unique_id": f"loom_test_{category}",
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
                    "unique_id": f"loom_test_{category}",
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

    def test_ha_capability_names_resolve_to_the_daemons_vocabulary(self) -> None:
        """
        homematicip_local spells two light capabilities differently to the daemon.

        `light.py:211/213` read `capabilities.hs_color` and
        `capabilities.color_temperature`; the daemon publishes `color` and
        `color_temp` (its `CustomDPCapability` vocabulary). Without the alias
        both read False, which is indistinguishable from a device that lacks
        the feature.
        """
        dp, _ = _cdp_instance(
            kind="light_color",
            category="light",
            capabilities={"color": True, "color_temp": True, "dimmable": True},
            state={"state": "ON", "brightness": 255},
        )
        assert dp.capabilities.hs_color is True
        assert dp.capabilities.color_temperature is True
        assert dp.capabilities.brightness is True
        # An unknown name still answers False rather than raising.
        assert dp.capabilities.nonexistent is False

    def test_brightness_pct_truncates_like_aiohomematic(self) -> None:
        """Aiohomematic uses `int(level * 100)`; the daemon sends 0-255 instead."""
        dp, _ = _cdp_instance(
            kind="light", category="light", capabilities={"dimmable": True}, state={"state": "ON", "brightness": 128}
        )
        assert dp.brightness == 128
        assert dp.brightness_pct == 50
        assert dp.group_brightness_pct is None

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
            state={"state": "ON", "color_mode": "hs", "color": {"h": 210, "s": 80}},
        )
        assert isinstance(dp, CustomDpDimmer)
        # Both values pass through: the custom-data-point plane already
        # reports HA's units (hue in degrees, saturation 0..100).
        assert dp.hs_color == (210.0, 80.0)

    def test_light_hs_color_does_not_rescale_saturation(self) -> None:
        """
        Saturation arrives on HA's scale and must not be multiplied again.

        The read path used to scale for the 0..1 wire fraction, which lives on
        the raw SATURATION data point rather than here: the daemon's
        custom-data-point plane normalises to 0..100 once
        (`internal/model/custom/light/color.go:159`) and divides back on write
        (`:212`). The double scaling turned this 50 into 5000, and HA clamps to
        100 — so every colour rendered fully saturated.

        Deliberately at a middle saturation. At 100 the bug is invisible: the
        clamp lands on the value the correct code returns anyway.
        """
        dp, _ = _cdp_instance(
            kind="light_color",
            category="light",
            capabilities={"color": True, "brightness": True},
            state={"state": "ON", "color_mode": "hs", "color": {"h": 30, "s": 50}},
        )
        assert dp.hs_color == (30.0, 50.0)

    def test_light_hs_color_ignores_flat_keys(self) -> None:
        """
        Flat ``hue``/``saturation`` keys are not a colour source.

        They were one for daemons before 0.8.0, and the fallback passed them
        through *unscaled* — so a payload carrying both shapes would have
        answered on a different saturation scale depending on which branch
        ran. The supported daemon emits the nested ``color`` object.
        """
        dp, _ = _cdp_instance(
            kind="light_color",
            category="light",
            capabilities={"color": True},
            state={"state": "ON", "hue": 120, "saturation": 50},
        )
        assert dp.hs_color is None

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
                    "unique_id": "loom_test_state",
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
                        "unique_id": "loom_test_on_time",
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


class TestProtocolSurfacePresentation:
    """The generic twin surfaces the channel's daemon-supplied room / function / value labels."""

    def _store(
        self,
        *,
        room: str | None = None,
        functions: list[str] | None = None,
        value_translations: dict[str, str] | None = None,
    ) -> LoomStore:
        store = LoomStore(transport=_FakeTransport())  # type: ignore[arg-type]
        store.set_data_point_factory(factory=make_generic_data_point)
        channel: dict[str, Any] = {
            "address": "VCU1:1",
            "number": 1,
            "paramset_key": "VALUES",
            "data_points_count": 1,
        }
        if room is not None:
            channel["room"] = room
        if functions is not None:
            channel["functions"] = functions
        store.attach_device_detail(
            detail=DeviceDetail.model_validate(
                {
                    "address": "VCU1",
                    "interface": "home:HmIP-RF",
                    "interface_id": "home:HmIP-RF",
                    "model": "HmIP-PSM",
                    "name": "Lamp",
                    "available": True,
                    "channels_count": 1,
                    "channels": [channel],
                    "updatable": False,
                    "update_available": False,
                    "master_pushes_config_pending": False,
                    "has_sub_devices": False,
                    "firmware": {},
                    "availability": {},
                }
            )
        )
        dp: dict[str, Any] = {
            "parameter": "STATE",
            "type": "BOOL",
            "value": False,
            "observed": True,
            "operations": {"read": True, "write": True, "event": True},
            "unique_id": "loom_test_state",
        }
        if value_translations is not None:
            dp["value_translations"] = value_translations
        store.attach_channel_data_points(
            device_address="VCU1",
            channel_number=1,
            data_points=[DataPointSummary.model_validate(dp)],
        )
        return store

    def test_room_and_rooms_populate_from_channel(self) -> None:
        store = self._store(room="Wohnzimmer")
        dp = store.get_data_point(address="VCU1", channel=1, parameter="STATE")
        assert isinstance(dp, DpSwitch)
        assert dp.room == "Wohnzimmer"
        assert dp.rooms == {"Wohnzimmer"}

    def test_room_none_when_channel_has_no_room(self) -> None:
        store = self._store(room=None)
        dp = store.get_data_point(address="VCU1", channel=1, parameter="STATE")
        assert isinstance(dp, DpSwitch)
        assert dp.room is None
        assert dp.rooms == set()

    def test_function_resolves_first_channel_function(self) -> None:
        store = self._store(functions=["Licht", "Zentrale"])
        dp = store.get_data_point(address="VCU1", channel=1, parameter="STATE")
        assert isinstance(dp, DpSwitch)
        assert dp.function == "Licht"

    def test_function_none_when_channel_has_no_function(self) -> None:
        store = self._store(functions=None)
        dp = store.get_data_point(address="VCU1", channel=1, parameter="STATE")
        assert isinstance(dp, DpSwitch)
        assert dp.function is None

    def test_value_translations_populate_from_summary(self) -> None:
        store = self._store(value_translations={"OPEN": "Offen", "CLOSED": "Geschlossen"})
        dp = store.get_data_point(address="VCU1", channel=1, parameter="STATE")
        assert isinstance(dp, DpSwitch)
        assert dp.value_translations == {"OPEN": "Offen", "CLOSED": "Geschlossen"}

    def test_value_translations_empty_when_absent(self) -> None:
        store = self._store(value_translations=None)
        dp = store.get_data_point(address="VCU1", channel=1, parameter="STATE")
        assert isinstance(dp, DpSwitch)
        assert dp.value_translations == {}


class TestGenericMultiplier:
    """
    ``multiplier`` scales min/max/step/value for HA's number & sensor platforms.

    types 0.5.0 adds ``DataPointSummary.multiplier`` and omits it when the
    factor is the trivial 1, so absent must resolve to 1.0 — not 0.0 (which
    would zero every scaled value) and not ``None`` (which HA would reject
    as a multiplier).
    """

    def _dp(self, *, multiplier: float | None) -> DpSensor:
        store = LoomStore(transport=_FakeTransport())  # type: ignore[arg-type]
        store.set_data_point_factory(factory=make_generic_data_point)
        store.attach_device_detail(
            detail=DeviceDetail.model_validate(
                {
                    "address": "VCU1",
                    "interface": "home:HmIP-RF",
                    "interface_id": "home:HmIP-RF",
                    "model": "HmIP-PSM",
                    "name": "Lamp",
                    "available": True,
                    "channels_count": 1,
                    "channels": [
                        {
                            "address": "VCU1:1",
                            "number": 1,
                            "paramset_key": "VALUES",
                            "data_points_count": 1,
                        }
                    ],
                    "updatable": False,
                    "update_available": False,
                    "master_pushes_config_pending": False,
                    "has_sub_devices": False,
                    "firmware": {},
                    "availability": {},
                }
            )
        )
        dp: dict[str, Any] = {
            "parameter": "LEVEL",
            "type": "FLOAT",
            "value": 0.42,
            "observed": True,
            # read-only FLOAT resolves to DpSensor (resolve_generic_class).
            "operations": {"read": True, "write": False, "event": True},
            "unique_id": "loom_test_level",
        }
        if multiplier is not None:
            dp["multiplier"] = multiplier
        store.attach_channel_data_points(
            device_address="VCU1",
            channel_number=1,
            data_points=[DataPointSummary.model_validate(dp)],
        )
        result = store.get_data_point(address="VCU1", channel=1, parameter="LEVEL")
        assert isinstance(result, DpSensor)
        return result

    def test_multiplier_from_summary(self) -> None:
        dp = self._dp(multiplier=100.0)
        assert dp.multiplier == 100.0

    def test_multiplier_defaults_to_one_when_absent(self) -> None:
        dp = self._dp(multiplier=None)
        assert dp.multiplier == 1.0


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
    """Stand-in for ``client.system`` returning a fixed interface list + aggregate."""

    def __init__(
        self,
        *,
        interfaces: list[Any] | None = None,
        aggregate: list[Any] | None = None,
        update: list[Any] | None = None,
        addon: AddonUpdateStatus | None = None,
    ) -> None:
        self._interfaces = list(interfaces or ())
        self._aggregate = list(aggregate or ())
        self._update = list(update or ())
        self._addon = addon
        self.aggregate_calls = 0

    async def list_interfaces(self) -> list[Any]:
        return list(self._interfaces)

    async def get_hub_data_points(self) -> list[Any]:
        self.aggregate_calls += 1
        return list(self._aggregate)

    async def get_system_update(self) -> list[Any]:
        return list(self._update)

    async def get_addon_update_status(self) -> AddonUpdateStatus:
        if self._addon is None:
            # Mirrors a pre-3.3.0 daemon: the endpoint answers 404.
            msg = "404 not found"
            raise RuntimeError(msg)
        return self._addon


class _FakeI18nOps:
    """
    Stand-in for ``client.i18n``.

    ``entries=None`` reproduces a daemon older than api 5.2.0: the read
    raises and every entity keeps its own token.
    """

    def __init__(self, *, entries: dict[str, str] | None = None, locale: str = "de") -> None:
        self._entries = entries
        self._locale = locale
        self.calls = 0
        self.locales_requested: list[str | None] = []

    async def get_entity_names(self, *, locale: str | None = None) -> Any:
        self.calls += 1
        self.locales_requested.append(locale)
        if self._entries is None:
            msg = "404 not found"
            raise RuntimeError(msg)
        return SimpleNamespace(locale=locale or self._locale, entries=dict(self._entries))


class _FakeSecurityOps:
    """
    Stand-in for ``client.security``.

    ``snapshot=None`` reproduces a daemon without the Security & Safety
    domain (no persistence tier, or older than api 5.0.0): the read
    raises and no security entity is ever built.
    """

    def __init__(self, *, snapshot: Any = None, faults: list[Any] | None = None) -> None:
        self._snapshot = snapshot
        self._faults = list(faults or ())
        self.snapshot_calls = 0
        self.fault_calls = 0

    async def get_snapshot(self) -> Any:
        self.snapshot_calls += 1
        if self._snapshot is None:
            msg = "503 security domain unavailable"
            raise RuntimeError(msg)
        return self._snapshot

    async def list_faults(self) -> list[Any]:
        self.fault_calls += 1
        return list(self._faults)


class _FakeHubClient:
    def __init__(
        self,
        *,
        store: LoomStore,
        hub: _FakeHubOps,
        system: _FakeSystemOps,
        security: _FakeSecurityOps | None = None,
        i18n: _FakeI18nOps | None = None,
    ) -> None:
        self.store = store
        self.hub = hub
        self.system = system
        self.security = security or _FakeSecurityOps()
        self.i18n = i18n or _FakeI18nOps()


def _iface(*, ident: str = "HmIP-RF", central_id: str = "home", connected: bool = True) -> SimpleNamespace:
    # ``id`` is the wire id ``<central>-<interface>`` — the form
    # GET /interfaces reports and (since api 6.1.0) the connectivity
    # snapshot + push carry, so the fixtures pin the documented contract.
    return SimpleNamespace(id=f"{central_id}-{ident}", interface=ident, central_id=central_id, connected=connected)


def _addon_status(**overrides: Any) -> AddonUpdateStatus:
    payload: dict[str, Any] = {
        "supported": True,
        "current_version": "0.50.0",
        "latest_version": "0.50.1",
        "update_available": True,
        "state": "idle",
    }
    payload.update(overrides)
    return AddonUpdateStatus.model_validate(payload)


class TestHubPushRouting:
    """G6: hub push broadcasts route onto the singletons and emit HA state-changed."""

    def _coordinator(
        self,
        *,
        hub: _FakeHubOps | None = None,
        interfaces: list[Any] | None = None,
        aggregate: list[Any] | None = None,
        update: list[Any] | None = None,
        addon: AddonUpdateStatus | None = None,
        security: _FakeSecurityOps | None = None,
        i18n: _FakeI18nOps | None = None,
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
            client=_FakeHubClient(
                store=store,
                hub=hub or _FakeHubOps(),
                system=_FakeSystemOps(interfaces=interfaces, aggregate=aggregate, update=update, addon=addon),
                security=security,
                i18n=i18n,
            ),
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
                    central="home", interface_id="home-HmIP-RF", reachable=False, latency_ms=None
                ),
            )
        )
        await looper.block_till_done()
        entry = coord._connectivity_dps["home-HmIP-RF"]
        assert entry.sensor.value is False
        assert seen == [entry.sensor.unique_id]

    async def test_system_update_push_updates_singleton_and_emits(self) -> None:
        coord, loom_bus, group, looper, seen = self._coordinator()
        await coord._ensure_singletons()
        coord.install_push_routing(group=group)
        await loom_bus.publish(
            event=HubSystemUpdateChangedEvent(
                seq=1,
                kind=Kind.change,
                ts="2026-06-21T08:00:00Z",
                payload=HubSystemUpdateChangedPayload(
                    central="home",
                    current_firmware="1.2",
                    available_firmware="1.3",
                    update_available=True,
                    in_progress=False,
                ),
            )
        )
        await looper.block_till_done()
        assert coord._update_dp is not None
        assert coord._update_dp.update_available is True
        assert coord._update_dp.current_firmware == "1.2"
        assert coord._update_dp.available_firmware == "1.3"
        assert seen == [coord._update_dp.unique_id]

    async def test_addon_update_push_updates_singleton_and_emits(self) -> None:
        coord, loom_bus, group, looper, seen = self._coordinator(addon=_addon_status())
        await coord._ensure_singletons()
        coord.install_push_routing(group=group)
        await loom_bus.publish(
            event=AddonUpdateStateChangedEvent(
                seq=1,
                kind=Kind.change,
                ts="2026-06-21T08:00:00Z",
                payload=_addon_status(state="downloading"),
            )
        )
        await looper.block_till_done()
        dp = coord.addon_update_dp
        assert dp is not None
        assert dp.in_progress is True
        assert seen == [dp.unique_id]

    async def test_addon_update_dp_gated_on_supported(self) -> None:
        # Pre-3.3.0 daemon: GET /system/addon-update answers 404 → no singleton.
        coord, _loom_bus, _group, _looper, _seen = self._coordinator()
        await coord._ensure_singletons()
        assert coord.addon_update_dp is None
        # Platform without the firmware installer: supported=False → no singleton.
        coord2, _loom_bus2, _group2, _looper2, _seen2 = self._coordinator(addon=_addon_status(supported=False))
        await coord2._ensure_singletons()
        assert coord2.addon_update_dp is None

    async def test_addon_update_dp_spawns_through_hub_update_category(self) -> None:
        # The HA update platform asks for HmUpdate.default_category() —
        # aiohomematic's HUB_UPDATE member. Both update twins must answer.
        coord, _loom_bus, _group, _looper, _seen = self._coordinator(addon=_addon_status())
        await coord._ensure_singletons()
        dps = coord.get_hub_data_points(category=AioDataPointCategory.HUB_UPDATE, registered=False)
        assert coord.update_dp is not None
        assert coord.addon_update_dp is not None
        assert {dp.unique_id for dp in dps} == {coord.update_dp.unique_id, coord.addon_update_dp.unique_id}
        # The initial status is applied at build time.
        assert coord.addon_update_dp.current_firmware == "0.50.0"
        assert coord.addon_update_dp.update_available is True

    async def test_alarm_count_push_refetches_then_skips_when_unchanged(self) -> None:
        hub = _FakeHubOps(
            alarms=[SimpleNamespace(central="home", name="LOW_BAT", display_name="Low battery", timestamp=None)]
        )
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

    async def test_install_mode_push_applies_to_all_interface_sensors(self) -> None:
        coord, loom_bus, group, looper, seen = self._coordinator(interfaces=[_iface()])
        await coord._ensure_singletons()
        coord.install_push_routing(group=group)
        await loom_bus.publish(
            event=InstallModeChangedEvent(
                seq=1,
                kind=Kind.change,
                ts="2026-06-21T08:00:00Z",
                payload=InstallModeChangedPayload(central="home", enabled=True, remaining_s=45),
            )
        )
        await looper.block_till_done()
        pair = coord._install_pair_for(interface_id="home-HmIP-RF")
        assert pair is not None
        assert pair.sensor.value == 45
        assert seen == [pair.sensor.unique_id]


class TestHubAggregateFetch:
    """G4(a): one GET /hub/data-points seeds every singleton in a single call."""

    def _aggregate(self) -> HubDataPoints:
        return HubDataPoints.model_validate(
            {
                "central": "home",
                "alarm_messages": {"legacy_name": "alarm_messages", "value": 0},
                "service_messages": {"legacy_name": "service_messages", "value": 0},
                "inbox": {"legacy_name": "inbox", "value": 2},
                "update": {"legacy_name": "system_update", "update_available": False, "in_progress": False},
                "daemon_connection": {"legacy_name": "daemon_connection", "connected": True},
                "metrics": [{"legacy_name": "system_health", "value": 95, "unit": "%"}],
                "connectivity": [{"interface_id": "home-HmIP-RF", "reachable": True}],
                "install_mode": [
                    {"interface_id": "home-HmIP-RF", "enabled": True, "remaining_s": 30, "observed": True}
                ],
            }
        )

    async def test_aggregate_seeds_all_singletons_in_one_call(self) -> None:
        coord, _loom_bus, _group, looper, _seen = TestHubPushRouting()._coordinator(
            interfaces=[_iface()], aggregate=[self._aggregate()]
        )
        await coord.fetch_hub_singleton_data()
        await looper.block_till_done()

        assert coord._inbox_dp.value == 2
        assert coord._metrics_dps.system_health.value == 95
        assert coord._connectivity_dps["home-HmIP-RF"].sensor.value is True
        install = coord._install_pair_for(interface_id="home-HmIP-RF")
        assert install is not None
        assert install.sensor.value == 30
        assert coord.daemon_connection_dp is not None
        assert coord.daemon_connection_dp.value is True
        # …and it is announced, not merely built: an unlisted singleton
        # spawns no entity at all.
        assert "daemon_connection" in {dp.name for dp in coord.get_hub_data_points()}
        # The per-endpoint fan-out collapsed to a single aggregate call.
        assert coord._client.system.aggregate_calls == 1

    async def test_shutdown_broadcast_flips_the_daemon_connection_sensor(self) -> None:
        """A stopping daemon reaches the sensor; the next poll re-arms it."""
        coord, loom_bus, group, looper, seen = TestHubPushRouting()._coordinator(
            interfaces=[_iface()], aggregate=[self._aggregate()]
        )
        await coord.fetch_hub_singleton_data()
        await looper.block_till_done()
        coord.install_push_routing(group=group)
        seen.clear()

        await loom_bus.publish(
            event=DaemonStatusChangedEvent(
                seq=1,
                kind=Kind.change,
                ts="2026-06-21T08:00:00Z",
                payload=DaemonStatusPayload.model_validate(
                    {"status": "offline", "reason": "shutdown", "event_at": "2026-06-21T08:00:00Z"}
                ),
            )
        )
        await looper.block_till_done()
        assert coord.daemon_connection_dp.value is False
        assert seen == [coord.daemon_connection_dp.unique_id]

        # The aggregate can only ever report connected — that is what makes
        # the poll after a reconnect the re-arm path.
        await coord.fetch_hub_singleton_data()
        await looper.block_till_done()
        assert coord.daemon_connection_dp.value is True


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
                        "unique_id": "",
                        "available": True,
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
                        "available": True,
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
                        "unique_id": "",
                    }
                ),
            )
        )
        await bus.publish(
            event=SysvarChangedEvent(
                seq=3,
                kind=Kind.change,
                ts="2026-05-24T08:00:00Z",
                payload=SysvarChangedPayload.model_validate(
                    {"central": "home", "name": "My Var", "value": 1.0, "unique_id": ""}
                ),
            )
        )
        # Rebuilt canonical keys: a custom DP keys on its primary channel
        # address (no serial prefix for the non-virtual VCU1); a sysvar on
        # ``loom_<serial>_sysvar_<hub_slug(name)>`` (space folds to a dash).
        await looper.block_till_done()
        assert seen == ["loom_vcu1_1", "loom_11a0001234_sysvar_my-var"]

    async def test_custom_wire_unique_id_is_channel_level(self) -> None:
        # Daemon ≥ 0.48.9 stamps the *channel-level* key on the custom-DP
        # summary and the state_changed push (it briefly stamped the
        # parameter-level routing key, e.g. ``…_1_level``). The twin takes
        # its unique_id from the summary while the bridge falls back to
        # ``custom_unique_id`` — so the wire form must equal the rebuild,
        # or an HA entity would never see its own state pings.
        bus = EventBus()
        group = bus.create_subscription_group(name="t")
        store = LoomStore()
        store.set_serial(serial="3014F711A0001234")
        looper, ha_bus, seen = self._ha_setup()
        install_refresh_bridge(group=group, store=store, ha_bus=ha_bus, central_name="home")
        wire_unique_id = custom_unique_id(serial_suffix=store.serial_suffix, device_address="VCU1", channel_no=1)
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
                        "unique_id": wire_unique_id,
                    }
                ),
            )
        )
        await looper.block_till_done()
        assert seen == ["loom_vcu1_1"]

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
                        "unique_id": "loom_test_vcu1_1_level",
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

    async def test_availability_change_pings_every_entity_of_the_device(self) -> None:
        # An availability flip carries no per-DP value push, so the bridge
        # must fan it out itself: one keyed state-changed ping per generic
        # DP and per custom DP of the device (each re-reads ``available``
        # off the store). Mirrors aiohomematic's
        # ``publish_device_updated_event(notify_data_points=True)``.
        bus = EventBus()
        group = bus.create_subscription_group(name="t")
        store = LoomStore()
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
                            "interface_id": "home:HmIP-RF",
                            "updatable": False,
                            "update_available": False,
                            "master_pushes_config_pending": False,
                            "has_sub_devices": False,
                            "firmware": {
                                "Current": "1.0.0",
                                "Available": "",
                                "Updatable": False,
                                "UpdateState": "UP_TO_DATE",
                            },
                            "availability": {
                                "IsReachable": True,
                                "LastUpdated": None,
                                "BatteryLevel": None,
                                "LowBattery": None,
                                "SignalStrength": None,
                            },
                        }
                    ],
                }
            )
        )
        detail = store.get_device(address="VCU1").summary.model_dump()
        store.attach_device_detail(
            detail=DeviceDetail.model_validate(
                {
                    **detail,
                    "firmware": {},
                    "availability": {},
                    "channels": [
                        {
                            "number": 1,
                            "address": "VCU1:1",
                            "paramset_key": "VALUES",
                            "data_points_count": 1,
                        }
                    ],
                }
            )
        )
        store.attach_channel_data_points(
            device_address="VCU1",
            channel_number=1,
            data_points=[
                DataPointSummary.model_validate(
                    {
                        "parameter": "LEVEL",
                        "type": "FLOAT",
                        "value": 0.0,
                        "observed": True,
                        "operations": {"read": True, "write": True, "event": True},
                        "unique_id": "loom_vcu1_1_level",
                    }
                )
            ],
        )
        store.attach_custom_data_points(
            device_address="VCU1",
            cdps=[_cdp(name="cover", category="cover", kind="cover_blind", unique_id="loom_vcu1_1")],
        )
        looper, ha_bus, seen = self._ha_setup()
        install_refresh_bridge(group=group, store=store, ha_bus=ha_bus, central_name="home")
        await bus.publish(
            event=DeviceAvailabilityChangedEvent(
                seq=9,
                kind=Kind.change,
                ts="2026-05-24T08:00:00Z",
                payload=DeviceAvailabilityChangedPayload.model_validate(
                    {
                        "central": "home",
                        "interface_id": "home:HmIP-RF",
                        "device_address": "VCU1",
                        "available": False,
                    }
                ),
            )
        )
        await looper.block_till_done()
        assert sorted(seen) == ["loom_vcu1_1", "loom_vcu1_1_level"]


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
                        "unique_id": "loom_test_vcu1_1_press_short",
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

    async def test_metadata_change_refreshes_the_device_before_announcing_it(self) -> None:
        """A rename re-reads the device, and the announce sees the new name."""

        def _detail(*, name: str) -> dict[str, Any]:
            return {
                "address": "VCU1",
                "interface": "home:HmIP-RF",
                "interface_id": "home:HmIP-RF",
                "model": "HmIP-PSM",
                "name": name,
                "available": True,
                "channels_count": 0,
                "channels": [],
                "updatable": False,
                "update_available": False,
                "master_pushes_config_pending": False,
                "has_sub_devices": False,
                "firmware": {},
                "availability": {},
            }

        class _Transport:
            def __init__(self) -> None:
                self.paths: list[str] = []

            async def request(self, *, method: str, path: str, **_: Any) -> Any:
                self.paths.append(f"{method} {path}")
                return _detail(name="Stehlampe Flur")

        store = LoomStore()
        store.attach_device_detail(detail=DeviceDetail.model_validate(_detail(name="Lamp")))
        transport = _Transport()
        store.set_transport(transport=transport)  # type: ignore[arg-type]
        bus = EventBus()
        group = bus.create_subscription_group(name="t")
        looper = Looper()
        ha_bus = AioEventBus(task_scheduler=looper)
        install_refresh_bridge(group=group, store=store, ha_bus=ha_bus, central_name="home")
        # Record the name the store carried at announce time: an announce
        # that raced the re-read would hand the consumer the old one.
        seen: list[tuple[DeviceLifecycleEventType, str | None]] = []
        ha_bus.create_subscription_group(name="x").subscribe(
            event_type=DeviceLifecycleEvent,
            event_key=None,
            handler=lambda *, event: seen.append(
                (event.event_type, getattr(store.get_device(address="VCU1"), "name", None))
            ),
        )

        await bus.publish(
            event=DeviceMetadataChangedEvent(
                seq=1,
                kind=Kind.change,
                ts="2026-05-24T08:00:00Z",
                payload=DeviceMetadataChangedPayload.model_validate(
                    {"central": "home", "interface_id": "home:HmIP-RF", "device_address": "VCU1"}
                ),
            )
        )
        await looper.block_till_done()

        assert transport.paths == ["GET /devices/VCU1"]
        assert seen == [(DeviceLifecycleEventType.UPDATED, "Stehlampe Flur")]

    async def test_schedule_change_reloads_the_week_profile_and_pings_it(self) -> None:
        """``schedules.changed`` invalidates the cached profile; the entity re-renders."""

        class _WeekProfileDp:
            unique_id = "loom_vcu1_week_profile"
            channel_no = 1

            def __init__(self) -> None:
                self.reloads = 0
                self.value = 3

            async def reload_schedule(self) -> None:
                self.reloads += 1
                self.value = 4

        store = LoomStore()
        wp_dp = _WeekProfileDp()
        store.set_week_profile_data_point(address="VCU1", data_point=wp_dp)
        bus = EventBus()
        group = bus.create_subscription_group(name="t")
        looper = Looper()
        ha_bus = AioEventBus(task_scheduler=looper)
        install_refresh_bridge(group=group, store=store, ha_bus=ha_bus, central_name="home")
        seen: list[tuple[str, Any]] = []
        ha_bus.create_subscription_group(name="x").subscribe(
            event_type=DataPointStateChangedEvent,
            event_key=None,
            handler=lambda *, event: seen.append((event.unique_id, event.new_value)),
        )

        def _push(*, address: str, channel: int, seq: int) -> ScheduleChangedEvent:
            return ScheduleChangedEvent(
                seq=seq,
                kind=Kind.change,
                ts="2026-05-24T08:00:00Z",
                payload=ScheduleChangedPayload.model_validate(
                    {
                        "central": "home",
                        "interface_id": "home:HmIP-RF",
                        "device_address": address,
                        "channel": channel,
                    }
                ),
            )

        await bus.publish(event=_push(address="VCU1", channel=1, seq=1))
        await looper.block_till_done()
        assert wp_dp.reloads == 1
        assert seen == [("loom_vcu1_week_profile", 4)]

        # A push for another channel of the same device, and one for a device
        # without a schedule entity, must not reach this profile.
        await bus.publish(event=_push(address="VCU1", channel=2, seq=2))
        await bus.publish(event=_push(address="VCU9", channel=1, seq=3))
        await looper.block_till_done()
        assert wp_dp.reloads == 1
        assert len(seen) == 1

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
                        "unique_id": "loom_test_state",
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
                        "unique_id": "loom_test_state",
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
                        "unique_id": "loom_test_temperature",
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
                        "unique_id": "loom_test_low_bat",
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
                        "unique_id": "loom_test_state_2",
                    }
                ),
            ],
        )
        by_param = {dp.parameter: dp for dp in central._client.store.data_points}
        assert by_param["LOW_BAT"].translated_name == "Batterie"
        assert by_param["STATE"].translated_name is None


class TestProgramControlAvailability:
    """api 3.12.0: only the execute button is gated on the daemon's answer."""

    @staticmethod
    def _summary(**extra: Any) -> Any:
        return ProgramSummary.model_validate(
            {
                "id": "p1",
                "name": "All off",
                "description": "",
                "active": True,
                "unique_id": "loom_test_p1",
                **extra,
            }
        )

    def _twins(self, **extra: Any) -> tuple[Any, Any]:
        store = LoomStore()
        return make_program_data_points(summary=self._summary(**extra), store=store)

    def test_button_goes_unavailable_when_the_ccu_would_refuse(self) -> None:
        button, _switch = self._twins(active=False, execute_available=False)
        assert button.available is False

    def test_switch_stays_available_on_a_deactivated_program(self) -> None:
        # Gating the switch too would strip out the only control that can
        # turn the program back on — the CCU never refuses this write.
        _button, switch = self._twins(active=False, execute_available=False)
        assert switch.available is True

    def test_both_available_while_the_program_is_active(self) -> None:
        button, switch = self._twins(execute_available=True)
        assert (button.available, switch.available) == (True, True)

    def test_button_stays_pressable_when_the_daemon_omits_the_field(self) -> None:
        # Fail-open: a pre-3.12.0 daemon, or a CCU whose flag has not been
        # observed yet, must not present a dead button.
        button, _switch = self._twins()
        assert button.available is True


def _security_snapshot(**overrides: Any) -> SecuritySnapshot:
    payload: dict[str, Any] = {
        "severity": "ok",
        "engine_healthy": True,
        "classes": [
            {"class": "smoke", "active": False, "severity": "ok", "known": 2, "sources": []},
            {"class": "water", "active": False, "severity": "ok", "known": 1, "sources": []},
        ],
    }
    payload.update(overrides)
    return SecuritySnapshot.model_validate(payload)


class TestSecurityHubEntities:
    """
    The Security & Safety entities are built from the snapshot and live on push.

    Before daemon 0.54.0 the domain had no WebSocket push at all, so
    everything here would have needed a poll loop. These tests drive the
    real ``install_push_routing`` rather than calling the handlers, because
    a handler that works while nothing subscribes it is the failure mode
    that produces a permanently "ok" smoke sensor.
    """

    def _coordinator(self, *, security: _FakeSecurityOps) -> tuple[_HubCoordinator, EventBus, Any, Looper, list[str]]:
        return TestHubPushRouting._coordinator(TestHubPushRouting(), security=security)

    async def test_entities_are_built_from_the_snapshot(self) -> None:
        security = _FakeSecurityOps(snapshot=_security_snapshot(severity="warning"))
        coord, _, _, _, _ = self._coordinator(security=security)
        await coord._ensure_singletons()

        names = {dp.name for dp in coord.get_hub_data_points()}
        assert {"security_severity", "security_faults", "security_last_alarm", "security_last_fault"} <= names
        # One binary sensor per class the installation actually has sources
        # for — never a permanently-off gas alarm for a home without gas.
        assert {"security_smoke", "security_water"} <= names
        assert "security_gas" not in names
        severity = next(dp for dp in coord.get_hub_data_points() if dp.name == "security_severity")
        assert severity.value == "warning"

    async def test_no_security_domain_builds_no_entities(self) -> None:
        """A daemon without the domain answers 503; the entities must simply not exist."""
        coord, _, _, _, _ = self._coordinator(security=_FakeSecurityOps(snapshot=None))
        await coord._ensure_singletons()
        assert not [dp for dp in coord.get_hub_data_points() if dp.name.startswith("security_")]

    async def test_class_push_flips_the_binary_sensor_and_names_the_detector(self) -> None:
        security = _FakeSecurityOps(snapshot=_security_snapshot())
        coord, loom_bus, group, looper, seen = self._coordinator(security=security)
        await coord._ensure_singletons()
        coord.install_push_routing(group=group)

        await loom_bus.publish(
            event=SecurityClassChangedEvent(
                seq=1,
                kind=Kind.change,
                ts="2026-08-05T08:00:00Z",
                payload=SecurityClassChangedPayload.model_validate(
                    {
                        "class": "smoke",
                        "active": True,
                        "sources": [{"ref": "r1", "name": "Rauchmelder Flur", "at": "2026-08-05T08:00:00Z"}],
                    }
                ),
            )
        )
        await looper.block_till_done()

        smoke = next(dp for dp in coord.get_hub_data_points() if dp.name == "security_smoke")
        assert smoke.value is True
        assert smoke.device_class == "smoke"
        # The names are what an automation writes into a message; the full
        # objects carry the ref REST needs to reach the same source back.
        assert smoke.attributes["source_names"] == ["Rauchmelder Flur"]
        assert smoke.attributes["sources"][0]["ref"] == "r1"
        assert smoke.attributes["count"] == 1
        assert smoke.attributes["truncated"] is False
        assert smoke.unique_id in seen

    async def test_state_push_moves_the_severity(self) -> None:
        security = _FakeSecurityOps(snapshot=_security_snapshot())
        coord, loom_bus, group, looper, seen = self._coordinator(security=security)
        await coord._ensure_singletons()
        coord.install_push_routing(group=group)

        await loom_bus.publish(
            event=SecurityStateChangedEvent(
                seq=1,
                kind=Kind.change,
                ts="2026-08-05T08:00:00Z",
                payload=SecurityStateChangedPayload.model_validate(
                    {"severity": "alarm", "previous_severity": "ok", "open_faults": 0}
                ),
            )
        )
        await looper.block_till_done()

        severity = next(dp for dp in coord.get_hub_data_points() if dp.name == "security_severity")
        assert severity.value == "alarm"
        assert severity.unique_id in seen

    async def test_fault_push_refetches_the_ledger(self) -> None:
        """The count rides the push; the per-fault attribution needs the read."""
        fault = SimpleNamespace(
            reason=SimpleNamespace(value="low_battery"),
            source=SimpleNamespace(name="Fenster Küche", channel_address="ABC:1"),
        )
        security = _FakeSecurityOps(snapshot=_security_snapshot(), faults=[fault])
        coord, loom_bus, group, looper, seen = self._coordinator(security=security)
        await coord._ensure_singletons()
        coord.install_push_routing(group=group)

        await loom_bus.publish(
            event=SecurityFaultChangedEvent(
                seq=1,
                kind=Kind.change,
                ts="2026-08-05T08:00:00Z",
                payload=SecurityFaultChangedPayload.model_validate(
                    {
                        "fault_id": "f1",
                        "class": "battery",
                        "reason": "low_battery",
                        "severity": "warning",
                        "source": {"ref": "r1", "at": "2026-08-05T08:00:00Z"},
                        "open": True,
                        "acknowledged": False,
                        "open_count": 1,
                    }
                ),
            )
        )
        await looper.block_till_done()

        faults = next(dp for dp in coord.get_hub_data_points() if dp.name == "security_faults")
        assert faults.value == 1
        assert faults.attributes["fault_1"] == "Fenster Küche: low_battery"
        assert faults.unique_id in seen

    async def test_notification_push_routes_hazard_and_fault_apart(self) -> None:
        security = _FakeSecurityOps(snapshot=_security_snapshot())
        coord, loom_bus, group, looper, _ = self._coordinator(security=security)
        await coord._ensure_singletons()
        coord.install_push_routing(group=group)

        for fault_flag, subject in ((False, "Rauchalarm"), (True, "Melder nicht erreichbar")):
            await loom_bus.publish(
                event=SecurityNotificationEvent(
                    seq=1,
                    kind=Kind.change,
                    ts="2026-08-05T08:00:00Z",
                    payload=SecurityNotificationPayload.model_validate(
                        {
                            "class": "smoke",
                            "severity": "alarm",
                            "verb": "triggered",
                            "subject": subject,
                            "message": f"{subject}.",
                            "i18n_key": "security.smoke.triggered",
                            "at": "2026-08-05T08:00:00Z",
                            "fault": fault_flag,
                        }
                    ),
                )
            )
        await looper.block_till_done()

        by_name = {dp.name: dp for dp in coord.get_hub_data_points()}
        assert by_name["security_last_alarm"].value == "Rauchalarm"
        assert by_name["security_last_fault"].value == "Melder nicht erreichbar"
        assert by_name["security_last_alarm"].attributes["i18n_key"] == "security.smoke.triggered"

    async def test_a_class_appearing_mid_session_gets_its_sensor(self) -> None:
        """A newly-paired detector introduces a class the snapshot did not have."""
        security = _FakeSecurityOps(snapshot=_security_snapshot())
        coord, loom_bus, group, looper, _ = self._coordinator(security=security)
        await coord._ensure_singletons()
        coord.install_push_routing(group=group)
        assert "security_gas" not in {dp.name for dp in coord.get_hub_data_points()}

        await loom_bus.publish(
            event=SecurityClassChangedEvent(
                seq=1,
                kind=Kind.change,
                ts="2026-08-05T08:00:00Z",
                payload=SecurityClassChangedPayload.model_validate({"class": "gas", "active": True}),
            )
        )
        await looper.block_till_done()

        gas = next(dp for dp in coord.get_hub_data_points() if dp.name == "security_gas")
        assert gas.value is True


_ENTITY_NAMES_DE = {
    "discovery.alarm_messages": "Alarmmeldungen",
    "discovery.service_messages": "Servicemeldungen",
    "discovery.inbox": "Posteingang",
    "discovery.system_health": "Systemzustand",
    "discovery.connection_latency": "Verbindungslatenz",
    "discovery.last_event_age": "Alter letztes Ereignis",
    "discovery.connectivity": "Konnektivität {iface}",
    "discovery.install_mode_duration": "Anlernmodus {iface} Dauer",
    "discovery.install_mode_activate": "Anlernmodus {iface} aktivieren",
    "security.entity.state": "Sicherheitsstatus",
    "security.entity.problem": "Sicherheitsstörung",
    "security.entity.last_alarm": "Letzte Gefahrenmeldung",
    "security.entity.last_fault": "Letzte Störungsmeldung",
    "security.entity.class.smoke": "Rauch",
    "security.entity.class.water": "Wasser",
    "security.entity.class.gas": "Gas",
}


class TestEntityNamesFromTheDaemon:
    """
    The daemon names its own entities; this layer renders those names.

    The words lived in the daemon's catalogue and again in the Home
    Assistant integration's strings.json, with nothing comparing them.
    Reading the daemon's copy removes the second one — but `name` has to
    stay the English token, because HA matches its entity descriptions
    (icon, device class, category) against it with `var_name_contains`.
    """

    def _coordinator(
        self, *, i18n: _FakeI18nOps, security: _FakeSecurityOps | None = None
    ) -> tuple[_HubCoordinator, EventBus, Any, Looper, list[str]]:
        return TestHubPushRouting._coordinator(
            TestHubPushRouting(),
            interfaces=[_iface(ident="HmIP-RF")],
            security=security or _FakeSecurityOps(snapshot=_security_snapshot()),
            i18n=i18n,
        )

    async def test_the_catalogue_is_read_in_home_assistants_language(self) -> None:
        """
        The entity names must follow Home Assistant's UI language.

        HA's language and the daemon's configured locale are separate
        choices and often disagree.
        """
        i18n = _FakeI18nOps(entries=_ENTITY_NAMES_DE)
        coord, _, _, _, _ = self._coordinator(i18n=i18n)
        coord._client.store.set_locale(locale="de")
        await coord._ensure_singletons()

        assert i18n.locales_requested == ["de"]

    async def test_singletons_adopt_the_daemon_names(self) -> None:
        coord, _, _, _, _ = self._coordinator(i18n=_FakeI18nOps(entries=_ENTITY_NAMES_DE))
        await coord._ensure_singletons()

        by_name = {dp.name: dp for dp in coord.get_hub_data_points()}
        assert by_name["alarm_messages"].resolved_name == "Alarmmeldungen"
        assert by_name["inbox"].resolved_name == "Posteingang"
        assert by_name["system_health"].resolved_name == "Systemzustand"
        assert by_name["security_severity"].resolved_name == "Sicherheitsstatus"
        assert by_name["security_smoke"].resolved_name == "Rauch"
        assert by_name["security_last_fault"].resolved_name == "Letzte Störungsmeldung"

    async def test_the_match_token_never_changes(self) -> None:
        """
        `name` is HA's entity-description key, not a display name.

        homematicip_local matches `var_name_contains="ALARM_MESSAGES"` and
        friends against it; a localized token there would cost the entity
        its icon, device class and category.
        """
        coord, _, _, _, _ = self._coordinator(i18n=_FakeI18nOps(entries=_ENTITY_NAMES_DE))
        await coord._ensure_singletons()

        names = {dp.name for dp in coord.get_hub_data_points()}
        assert {"alarm_messages", "service_messages", "inbox", "install_mode_hmip"} <= names
        for dp in coord.get_hub_data_points():
            assert dp.name == dp.name.strip()
            assert "ä" not in dp.name and "ö" not in dp.name and "ü" not in dp.name

    async def test_templates_are_filled_per_interface(self) -> None:
        """`Konnektivität {iface}` can only be completed by this side."""
        coord, _, _, _, _ = self._coordinator(i18n=_FakeI18nOps(entries=_ENTITY_NAMES_DE))
        await coord._ensure_singletons()

        by_name = {dp.name: dp for dp in coord.get_hub_data_points()}
        assert by_name["Connectivity HmIP-RF"].resolved_name == "Konnektivität HmIP-RF"
        assert by_name["install_mode_hmip"].resolved_name == "Anlernmodus HmIP-RF Dauer"
        assert by_name["install_mode_hmip_button"].resolved_name == "Anlernmodus HmIP-RF aktivieren"

    async def test_an_old_daemon_leaves_every_name_unset(self) -> None:
        """A 404 is not an error: the consumer falls back to its own rendering."""
        coord, _, _, _, _ = self._coordinator(i18n=_FakeI18nOps(entries=None))
        await coord._ensure_singletons()

        assert all(dp.resolved_name is None for dp in coord.get_hub_data_points())
        # Nothing reached the store either, so a panel names its
        # companions the same way a singleton names itself: not at all.
        assert coord._client.store.entity_names == {}

    async def test_the_catalogue_reaches_the_store(self) -> None:
        """
        Not every reader of these names is a singleton.

        An alarm panel is rebuilt by a catalogue reconcile and seeded
        from a bare push, so pushing names onto the instance would have
        to be repeated on both paths. The store holds the catalogue and
        the panel reads it back — see the alarm-panel tests for what it
        composes out of it.
        """
        coord, _, _, _, _ = self._coordinator(i18n=_FakeI18nOps(entries=_ENTITY_NAMES_DE))
        await coord._ensure_singletons()

        assert coord._client.store.entity_names["discovery.alarm_messages"] == "Alarmmeldungen"

    async def test_a_key_the_catalogue_omits_leaves_that_name_unset(self) -> None:
        coord, _, _, _, _ = self._coordinator(i18n=_FakeI18nOps(entries={"discovery.inbox": "Posteingang"}))
        await coord._ensure_singletons()

        by_name = {dp.name: dp for dp in coord.get_hub_data_points()}
        assert by_name["inbox"].resolved_name == "Posteingang"
        assert by_name["alarm_messages"].resolved_name is None

    async def test_a_class_appearing_later_is_named_like_its_siblings(self) -> None:
        i18n = _FakeI18nOps(entries=_ENTITY_NAMES_DE)
        coord, loom_bus, group, looper, _ = self._coordinator(i18n=i18n)
        await coord._ensure_singletons()
        coord.install_push_routing(group=group)

        await loom_bus.publish(
            event=SecurityClassChangedEvent(
                seq=1,
                kind=Kind.change,
                ts="2026-08-05T08:00:00Z",
                payload=SecurityClassChangedPayload.model_validate({"class": "gas", "active": True}),
            )
        )
        await looper.block_till_done()

        gas = next(dp for dp in coord.get_hub_data_points() if dp.name == "security_gas")
        assert gas.resolved_name == "Gas"
        # One read at build time, not one per lazily-created sensor.
        assert i18n.calls == 1
