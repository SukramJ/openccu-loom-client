# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
HA-parity regression tests for the live-state fix batch.

Pins the behaviours that diverged from aiohomematic against the live
OttoMac/Otto-Rem comparison (2026-06-10): binary-sensor ENUM→bool
mapping, unobserved-value semantics, CDP config-block reading, CDP
bootstrap state seeding, and the always-JSON invoke body.
"""

from __future__ import annotations

from typing import Any

from openccu_loom_types.rest import (
    CustomDPSummary,
    DataPointSummary,
    DeviceDetail,
    DeviceSummary,
    ProgramSummary,
    Snapshot,
    SysvarSummary,
)

from openccu_loom_client.compat.aiohomematic.central.adapter import _is_creatable
from openccu_loom_client.compat.aiohomematic.model.custom import (
    BaseCustomDpLock,
    BaseCustomDpSiren,
    CustomDpIpThermostat,
    make_custom_data_point,
)
from openccu_loom_client.compat.aiohomematic.model.generic import DpBinarySensor, DpSensor, make_generic_data_point
from openccu_loom_client.compat.aiohomematic.model.hub import (
    SysvarDpBinarySensor,
    SysvarDpNumber,
    SysvarDpSelect,
    SysvarDpSensor,
    SysvarDpSwitch,
    SysvarDpText,
    make_program_data_point,
    make_sysvar_data_point,
    resolve_sysvar_class,
)
from openccu_loom_client.store import LoomStore


def _dp_summary(**overrides: Any) -> DataPointSummary:
    payload: dict[str, Any] = {
        "parameter": "STATE",
        "value": None,
        "observed": True,
        "operations": {"read": True, "write": False, "event": True},
        "type": "ENUM",
        "category": "binary_sensor",
    }
    payload.update(overrides)
    payload.setdefault("unique_id", f"loom_test_{str(payload['parameter']).lower()}")
    return DataPointSummary.model_validate(payload)


def _make_binary_sensor(**overrides: Any) -> DpBinarySensor:
    dp = make_generic_data_point(
        summary=_dp_summary(**overrides),
        device_address="VCU1",
        channel_number=1,
        store=LoomStore(),
    )
    assert isinstance(dp, DpBinarySensor)
    return dp


class TestBinarySensorEnumMapping:
    """ENUM indices map to bool exactly like aiohomematic, never to strings."""

    def test_closed_open_list_closed_is_off(self) -> None:
        dp = _make_binary_sensor(value=0, value_list=["CLOSED", "OPEN"])
        assert dp.value is False

    def test_closed_open_list_open_is_on(self) -> None:
        dp = _make_binary_sensor(value=1, value_list=["CLOSED", "OPEN"])
        assert dp.value is True

    def test_dry_rain_list(self) -> None:
        assert _make_binary_sensor(value=1, value_list=["DRY", "RAIN"]).value is True
        assert _make_binary_sensor(value=0, value_list=["DRY", "RAIN"]).value is False

    def test_plain_bool_passes_through(self) -> None:
        dp = _make_binary_sensor(value=True, type="BOOL", value_list=None)
        assert dp.value is True

    def test_none_stays_none(self) -> None:
        dp = _make_binary_sensor(value=None, type="BOOL", value_list=None)
        assert dp.value is None

    def test_unknown_list_falls_back_to_truthiness(self) -> None:
        dp = _make_binary_sensor(value=0, value_list=["IDLE", "ACTIVE"])
        assert dp.value is False


class TestUnobservedValueSemantics:
    """observed=false reads None (unknown), never the wire default."""

    def test_unobserved_numeric_reads_none(self) -> None:
        dp = make_generic_data_point(
            summary=_dp_summary(
                parameter="ACTUAL_TEMPERATURE",
                value=0,
                observed=False,
                type="FLOAT",
                category="sensor",
            ),
            device_address="VCU1",
            channel_number=1,
            store=LoomStore(),
        )
        assert isinstance(dp, DpSensor)
        assert dp.value is None

    def test_observed_zero_is_a_real_value(self) -> None:
        dp = make_generic_data_point(
            summary=_dp_summary(
                parameter="ACTUAL_TEMPERATURE",
                value=0.0,
                observed=True,
                type="FLOAT",
                category="sensor",
            ),
            device_address="VCU1",
            channel_number=1,
            store=LoomStore(),
        )
        assert dp.value == 0.0


class _WireNamedCDPSummary(CustomDPSummary):
    """
    CustomDPSummary plus the naming fields daemon >= 0.45.0 ships.

    openccu-loom-types gains ``translated_name``/``parameter_name`` with
    the release generated from that daemon; the subclass lets fixtures
    inject the wire values until the types floor moves.
    """

    translated_name: str | None = None
    parameter_name: str | None = None


def _cdp_summary(**overrides: Any) -> CustomDPSummary:
    payload: dict[str, Any] = {
        "name": "SET_POINT_TEMPERATURE",
        "category": "climate",
        "channel_no": 1,
        "supported_operations": [],
        "kind": "climate_hmip",
    }
    payload.update(overrides)
    payload.setdefault("unique_id", f"loom_test_{str(payload['name']).lower()}")
    return _WireNamedCDPSummary.model_validate(payload)


def _make_cdp(summary: CustomDPSummary, *, initial_state: dict[str, Any] | None = None) -> Any:
    return make_custom_data_point(
        summary=summary,
        device_address="VCU1",
        store=LoomStore(),
        initial_state=initial_state,
    )


class TestClimateConfigBlock:
    """Static climate data comes from the CDP config payload."""

    def test_bounds_modes_profiles_from_config(self) -> None:
        cdp = _make_cdp(
            _cdp_summary(
                config={
                    "min_temp": 14.0,
                    "max_temp": 23.0,
                    "temp_step": 0.5,
                    "hvac_modes": ["auto", "heat", "off"],
                    "preset_modes": ["boost", "week_program_1"],
                },
                capabilities={"profile": True},
            )
        )
        assert isinstance(cdp, CustomDpIpThermostat)
        assert cdp.min_temp == 14.0
        assert cdp.max_temp == 23.0
        assert cdp.target_temperature_step == 0.5
        assert cdp.modes == ("auto", "heat", "off")
        # 'none' is inserted after the control-mode block, exactly where
        # aiohomematic's HmIP profiles list places it.
        assert cdp.profiles == ("boost", "none", "week_program_1")
        # HA reads .value off the members (climate.py preset_modes), so
        # the tuples must carry the aiohomematic enums, not bare strings.
        assert [m.value for m in cdp.modes] == ["auto", "heat", "off"]
        assert [p.value for p in cdp.profiles] == ["boost", "none", "week_program_1"]

    def test_capability_aliases(self) -> None:
        cdp = _make_cdp(
            _cdp_summary(
                name="LEVEL",
                category="light",
                kind="light",
                capabilities={"dimmable": True, "acoustic": True, "profile": True},
            )
        )
        # HA-side names map onto the daemon's flag names.
        assert cdp.capabilities.brightness is True
        assert cdp.capabilities.tones is True
        assert cdp.capabilities.profiles is True
        assert cdp.capabilities.color is False
        # HA checks capabilities.profiles (plural) for PRESET_MODE.
        assert cdp.capabilities.profiles is True

    def test_unknown_tokens_skipped(self) -> None:
        cdp = _make_cdp(
            _cdp_summary(
                config={
                    "hvac_modes": ["auto", "fancy_new_mode"],
                    "preset_modes": ["boost", "exotic"],
                }
            )
        )
        assert [m.value for m in cdp.modes] == ["auto"]
        assert [p.value for p in cdp.profiles] == ["boost", "none"]

    def test_defaults_without_config(self) -> None:
        cdp = _make_cdp(_cdp_summary())
        assert cdp.min_temp == 4.5
        assert cdp.max_temp == 30.5
        # At least HEAT so HA renders a usable climate card.
        assert cdp.modes == ("heat",)


class TestSirenConfigBlock:
    """Siren tone/light lists come from the CDP config payload."""

    def test_tones_from_config(self) -> None:
        cdp = _make_cdp(
            _cdp_summary(
                name="ACOUSTIC_ALARM_SELECTION",
                category="siren",
                kind="siren",
                config={
                    "available_tones": ["FREQUENCY_RISING", "FREQUENCY_FALLING"],
                    "available_lights": ["BLINKING_ALTERNATELY_REPEATING"],
                },
            )
        )
        assert isinstance(cdp, BaseCustomDpSiren)
        assert cdp.available_tones == ["FREQUENCY_RISING", "FREQUENCY_FALLING"]
        assert cdp.available_lights == ["BLINKING_ALTERNATELY_REPEATING"]


class TestBootstrapStateSeeding:
    """attach_custom_data_points seeds the live state from the summary."""

    def test_lock_state_seeded(self) -> None:
        store = LoomStore()
        store.attach_custom_data_points(
            device_address="VCU1",
            cdps=[
                _cdp_summary(
                    name="BUTTON_LOCK",
                    category="lock",
                    kind="lock",
                    state={"lock_state": "LOCKED", "is_locked": True},
                )
            ],
        )
        cdp = store.get_custom_data_point(address="VCU1", name="BUTTON_LOCK")
        assert cdp is not None
        assert cdp.state == {"lock_state": "LOCKED", "is_locked": True}


class TestButtonLockPostfix:
    """Button locks expose the BUTTON_LOCK postfix for HA's registry rule."""

    def test_button_lock_postfix(self) -> None:
        cdp = _make_cdp(_cdp_summary(name="BUTTON_LOCK", category="lock", kind="lock"))
        assert isinstance(cdp, BaseCustomDpLock)
        assert cdp.data_point_name_postfix == "BUTTON_LOCK"

    def test_real_lock_has_no_postfix(self) -> None:
        cdp = _make_cdp(_cdp_summary(name="LOCK_STATE", category="lock", kind="lock"))
        assert cdp.data_point_name_postfix == ""


class TestGenericTranslationKey:
    """translation_key mirrors aiohomematic's generate_translation_key."""

    def test_lowercased_parameter(self) -> None:
        dp = _make_binary_sensor(value=0, value_list=["CLOSED", "OPEN"])
        assert dp.translation_key == "state"


class TestChannelGroupWireNames:
    """Channel-group CDPs arrive with unique PARAM@ch wire names."""

    def test_group_members_keyed_separately(self) -> None:
        store = LoomStore()
        store.attach_custom_data_points(
            device_address="VCU1",
            cdps=[
                _cdp_summary(name="LEVEL@4", category="light", kind="light", channel_no=4),
                _cdp_summary(name="LEVEL@5", category="light", kind="light", channel_no=5),
                _cdp_summary(name="LEVEL@6", category="light", kind="light", channel_no=6),
            ],
        )
        cdps = store.custom_data_points_of(address="VCU1")
        assert len(cdps) == 3
        assert sorted(c.summary.channel_no for c in cdps) == [4, 5, 6]

    def test_button_lock_postfix_strips_channel_suffix(self) -> None:
        cdp = _make_cdp(_cdp_summary(name="BUTTON_LOCK@0", category="lock", kind="lock", channel_no=0))
        assert cdp.data_point_name_postfix == "BUTTON_LOCK"


class TestCustomTranslatedName:
    """Custom DPs derive their display name from the CCU channel name."""

    def _store_with_channels(self) -> LoomStore:
        from openccu_loom_types.rest import DeviceDetail, Snapshot

        store = LoomStore()
        store.set_custom_data_point_factory(factory=make_custom_data_point)
        store.load_snapshot(
            snapshot=Snapshot.model_validate(
                {
                    "generated_at": "2026-06-11T08:00:00Z",
                    "devices": [
                        {
                            "address": "VCU1",
                            "interface": "home:HmIP-RF",
                            "interface_id": "home:HmIP-RF",
                            "model": "HmIP-BDT",
                            "name": "Küchenstrahler",
                            "available": True,
                            "channels_count": 7,
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
        store.attach_device_detail(
            detail=DeviceDetail.model_validate(
                {
                    "address": "VCU1",
                    "interface": "home:HmIP-RF",
                    "interface_id": "home:HmIP-RF",
                    "model": "HmIP-BDT",
                    "name": "Küchenstrahler",
                    "available": True,
                    "channels_count": 7,
                    "updatable": False,
                    "update_available": False,
                    "master_pushes_config_pending": False,
                    "has_sub_devices": False,
                    "firmware": {},
                    "availability": {},
                    "channels": [
                        {
                            "address": "VCU1:4",
                            "number": 4,
                            "name": "Küchenstrahler:4",
                            "paramset_key": "VALUES",
                            "data_points_count": 3,
                            "is_custom_dp_primary": True,
                        },
                        {
                            "address": "VCU1:5",
                            "number": 5,
                            "name": "Küchenstrahler:5",
                            "paramset_key": "VALUES",
                            "data_points_count": 3,
                            "is_custom_dp_primary": False,
                        },
                        {
                            "address": "VCU1:6",
                            "number": 6,
                            "name": "Küchenstrahler:6",
                            "paramset_key": "VALUES",
                            "data_points_count": 3,
                            "is_custom_dp_primary": False,
                        },
                    ],
                }
            )
        )
        return store

    def test_primary_channel_collapses_to_none(self) -> None:
        store = self._store_with_channels()
        store.attach_custom_data_points(
            device_address="VCU1",
            cdps=[_cdp_summary(name="LEVEL@4", category="light", kind="light", channel_no=4)],
        )
        cdp = store.get_custom_data_point(address="VCU1", name="LEVEL@4")
        assert cdp.translated_name is None

    def test_secondary_channels_named_vch(self) -> None:
        store = self._store_with_channels()
        # Daemon >= 0.45.0 ships the composed vch markers on the wire.
        store.attach_custom_data_points(
            device_address="VCU1",
            cdps=[
                _cdp_summary(name="LEVEL@5", category="light", kind="light", channel_no=5, translated_name="vch5"),
                _cdp_summary(name="LEVEL@6", category="light", kind="light", channel_no=6, translated_name="vch6"),
            ],
        )
        assert store.get_custom_data_point(address="VCU1", name="LEVEL@5").translated_name == "vch5"
        assert store.get_custom_data_point(address="VCU1", name="LEVEL@6").translated_name == "vch6"


class TestUpdateDataPoint:
    """Per-device firmware-update data points (aiohomematic DpUpdate twin)."""

    def _device(
        self, store: LoomStore, firmware: dict[str, Any] | None = None, update_status: str | None = None
    ) -> Any:
        from openccu_loom_types.rest import DeviceDetail, Snapshot

        device_entry: dict[str, Any] = {
            "address": "VCU9",
            "interface": "HmIP-RF",
            "interface_id": "HmIP-RF",
            "model": "HmIP-PSM",
            "name": "Steckdose",
            "available": True,
            "channels_count": 1,
            "updatable": True,
            "update_available": False,
            "master_pushes_config_pending": False,
            "has_sub_devices": False,
            "firmware": {"Current": "1.0.0", "Available": "", "Updatable": False, "UpdateState": "UP_TO_DATE"},
            "availability": {
                "IsReachable": True,
                "LastUpdated": None,
                "BatteryLevel": None,
                "LowBattery": None,
                "SignalStrength": None,
            },
        }
        if update_status is not None:
            device_entry["update_status"] = update_status
        store.load_snapshot(
            snapshot=Snapshot.model_validate({"generated_at": "2026-06-11T08:00:00Z", "devices": [device_entry]})
        )
        detail = {
            "address": "VCU9",
            "interface": "HmIP-RF",
            "interface_id": "HmIP-RF",
            "model": "HmIP-PSM",
            "name": "Steckdose",
            "available": True,
            "channels_count": 1,
            "updatable": False,
            "update_available": False,
            "master_pushes_config_pending": False,
            "has_sub_devices": False,
            "firmware": {},
            "availability": {},
            "channels": [],
        }
        if firmware is not None:
            detail["firmware"] = firmware
        if update_status is not None:
            detail["update_status"] = update_status
        store.attach_device_detail(detail=DeviceDetail.model_validate(detail))
        return store.get_device(address="VCU9")

    def test_unique_id_and_versions(self) -> None:
        from openccu_loom_client.compat.aiohomematic.model.update import make_update_data_point

        store = LoomStore()
        store.set_serial(serial="3014F711A0001234")
        device = self._device(
            store,
            firmware={"Current": "1.2.3", "Available": "1.3.0", "UpdateState": "READY_FOR_UPDATE"},
            update_status="update_available",
        )
        dp = make_update_data_point(device=device, store=store)
        # Device addresses carry no central prefix (ccu reference:
        # ``<address>_update``); only the loom namespace is added.
        assert dp.unique_id == "loom_vcu9_update"
        assert dp.firmware == "1.2.3"
        # HmIP advertises the available version only in a ready state.
        assert dp.latest_firmware == "1.3.0"
        assert dp.in_progress is False
        assert dp.category.value == "update"
        assert dp.full_name == "Steckdose Update"

    def test_no_update_available_falls_back_to_installed(self) -> None:
        from openccu_loom_client.compat.aiohomematic.model.update import make_update_data_point

        store = LoomStore()
        store.set_serial(serial="3014F711A0001234")
        device = self._device(store, firmware={"Current": "1.2.3", "UpdateState": "UP_TO_DATE"})
        dp = make_update_data_point(device=device, store=store)
        assert dp.latest_firmware == "1.2.3"


class TestCalculatedDataPoints:
    """Daemon-calculated DPs spawn as sensors with the calculated key prefix."""

    def _store(self) -> LoomStore:
        from openccu_loom_client.compat.aiohomematic.model.calculated import make_calculated_data_point

        store = LoomStore()
        store.set_serial(serial="3014F711A0001234")
        store.set_calculated_data_point_factory(factory=make_calculated_data_point)
        return store

    def test_binary_calculated(self) -> None:
        from openccu_loom_types.rest import CalculatedDPSummary

        from openccu_loom_client.compat.aiohomematic.model.calculated import CalculatedDpBinarySensor

        store = self._store()
        store.attach_channel_calculated_data_points(
            device_address="VCU7",
            channel_number=1,
            calculated=[
                CalculatedDPSummary.model_validate(
                    {
                        "name": "WINDOW_OPEN",
                        "category": "binary_sensor",
                        "value": False,
                        "observed": True,
                        "available": True,
                        "unique_id": "loom_calculated_vcu7_1_window_open",
                    }
                )
            ],
        )
        dp = store.get_data_point(address="VCU7", channel=1, parameter="WINDOW_OPEN")
        assert isinstance(dp, CalculatedDpBinarySensor)
        # ccu twin: calculated_<address>_<channel>_<parameter>; loom adds its namespace.
        assert dp.unique_id == "loom_calculated_vcu7_1_window_open"
        assert dp.value is False
        assert dp.category.value == "binary_sensor"

    def test_sensor_calculated_unobserved_reads_none(self) -> None:
        from openccu_loom_types.rest import CalculatedDPSummary

        from openccu_loom_client.compat.aiohomematic.model.calculated import CalculatedDpSensor

        store = self._store()
        store.attach_channel_calculated_data_points(
            device_address="VCU7",
            channel_number=1,
            calculated=[
                CalculatedDPSummary.model_validate(
                    {
                        "name": "DEW_POINT",
                        "category": "sensor",
                        "value": 0,
                        "observed": False,
                        "available": False,
                        "unique_id": "loom_test_dew_point",
                    }
                )
            ],
        )
        dp = store.get_data_point(address="VCU7", channel=1, parameter="DEW_POINT")
        assert isinstance(dp, CalculatedDpSensor)
        assert dp.value is None  # unobserved reads unknown, not the wire default

    def test_value_changed_routes_to_calculated(self) -> None:
        from openccu_loom_types.rest import CalculatedDPSummary
        from openccu_loom_types.ws import DataPointValueChangedPayload

        store = self._store()
        store.attach_channel_calculated_data_points(
            device_address="VCU7",
            channel_number=1,
            calculated=[
                CalculatedDPSummary.model_validate(
                    {
                        "name": "WINDOW_OPEN",
                        "category": "binary_sensor",
                        "value": False,
                        "observed": True,
                        "available": True,
                        "unique_id": "loom_test_window_open",
                    }
                )
            ],
        )
        store.apply_value_changed(
            payload=DataPointValueChangedPayload.model_validate(
                {
                    "central": "home",
                    "device_address": "VCU7",
                    "channel": 1,
                    "parameter": "WINDOW_OPEN",
                    "paramset_key": "VALUES",
                    "value": True,
                    "modified_at": "2026-06-11T10:00:00Z",
                    "unique_id": "loom_test_window_open",
                    "available": True,
                }
            )
        )
        dp = store.get_data_point(address="VCU7", channel=1, parameter="WINDOW_OPEN")
        assert dp.value is True

    def _attach_dew_point(self, *, store: LoomStore, available: bool) -> Any:
        from openccu_loom_types.rest import CalculatedDPSummary

        store.attach_channel_calculated_data_points(
            device_address="VCU7",
            channel_number=1,
            calculated=[
                CalculatedDPSummary.model_validate(
                    {
                        "name": "DEW_POINT",
                        "category": "sensor",
                        "value": 9.3,
                        "observed": True,
                        "available": available,
                        "unique_id": "loom_test_dew_point",
                    }
                )
            ],
        )
        return store.get_data_point(address="VCU7", channel=1, parameter="DEW_POINT")

    def test_calculated_is_valid_follows_daemon_availability(self) -> None:
        """
        A derived value the daemon disowned must not read as valid.

        The generic rule ("a value is present") cannot see a source fault — the
        daemon keeps recomputing the number, only its `available` flag flips.
        Home Assistant restores an entity's previous state exactly when
        `is_valid` is False, so this is what keeps a dew point computed off a
        thermometer stuck at OVERFLOW off the dashboard.
        """
        store = self._store()

        healthy = self._attach_dew_point(store=store, available=True)
        assert healthy.is_valid is True

        faulted = self._attach_dew_point(store=store, available=False)
        assert faulted.value == 9.3  # the daemon still computes it …
        assert faulted.is_valid is False  # … but it is not a confirmed reading

    def test_calculated_is_valid_requires_a_value(self) -> None:
        """An available-but-unobserved calc DP is still not valid — no value to read."""
        from openccu_loom_types.rest import CalculatedDPSummary

        store = self._store()
        store.attach_channel_calculated_data_points(
            device_address="VCU7",
            channel_number=1,
            calculated=[
                CalculatedDPSummary.model_validate(
                    {
                        "name": "ENTHALPY",
                        "category": "sensor",
                        "value": None,
                        "observed": False,
                        "available": True,
                        "unique_id": "loom_test_enthalpy",
                    }
                )
            ],
        )
        dp = store.get_data_point(address="VCU7", channel=1, parameter="ENTHALPY")
        assert dp.is_valid is False


class TestUsageVerdictFilter:
    """Daemon usage verdicts gate generic entity creation (parity round 3)."""

    @staticmethod
    def _dp_with_usage(usage: str | None) -> Any:
        summary = _dp_summary(parameter="PRESS_SHORT", type="ACTION", category="button", usage=usage)

        class _Dp:
            def __init__(self) -> None:
                self.summary = summary

        return _Dp()

    def test_event_usage_is_not_creatable(self) -> None:
        assert _is_creatable(dp=self._dp_with_usage("event")) is False

    def test_no_create_and_ignored_are_not_creatable(self) -> None:
        assert _is_creatable(dp=self._dp_with_usage("no_create")) is False
        assert _is_creatable(dp=self._dp_with_usage("ignored")) is False

    def test_data_point_and_ce_verdicts_are_creatable(self) -> None:
        assert _is_creatable(dp=self._dp_with_usage("data_point")) is True
        assert _is_creatable(dp=self._dp_with_usage("ce_visible")) is True

    def test_missing_usage_defaults_to_creatable(self) -> None:
        assert _is_creatable(dp=self._dp_with_usage(None)) is True


def _sysvar_summary(**overrides: Any) -> SysvarSummary:
    payload: dict[str, Any] = {
        "name": "sv_alarm_messages",
        "description": "",
        "value_type": "FLOAT",
        "value": 1.0,
        "observed": True,
    }
    payload.update(overrides)
    payload.setdefault("unique_id", f"loom_test_{str(payload['name']).lower()}")
    return SysvarSummary.model_validate(payload)


class TestSysvarExtendedClasses:
    """Extended description marker unlocks the writable entity flavour."""

    def test_default_mapping_is_read_only(self) -> None:
        assert resolve_sysvar_class(value_type="ALARM", has_value_list=False) is SysvarDpBinarySensor
        assert resolve_sysvar_class(value_type="LOGIC", has_value_list=False) is SysvarDpBinarySensor
        assert resolve_sysvar_class(value_type="FLOAT", has_value_list=False) is SysvarDpSensor
        assert resolve_sysvar_class(value_type="LIST", has_value_list=True) is SysvarDpSensor

    def test_extended_mapping_is_writable(self) -> None:
        assert resolve_sysvar_class(value_type="ALARM", has_value_list=False, extended=True) is SysvarDpSwitch
        assert resolve_sysvar_class(value_type="LIST", has_value_list=True, extended=True) is SysvarDpSelect
        assert resolve_sysvar_class(value_type="FLOAT", has_value_list=False, extended=True) is SysvarDpNumber
        assert resolve_sysvar_class(value_type="STRING", has_value_list=False, extended=True) is SysvarDpText

    def test_factory_honours_is_extended_from_summary(self) -> None:
        dp = make_sysvar_data_point(
            summary=_sysvar_summary(value_type="LOGIC", is_extended=True),
            store=LoomStore(),
        )
        assert isinstance(dp, SysvarDpSwitch)

    def test_factory_defaults_to_read_only_without_flag(self) -> None:
        dp = make_sysvar_data_point(
            summary=_sysvar_summary(value_type="LOGIC"),
            store=LoomStore(),
        )
        assert isinstance(dp, SysvarDpBinarySensor)


class TestHubDeviceLinkRouting:
    """
    Hub data points linked to a device channel route device_info there.

    HA's hub entity reads ``dp.channel`` — ``None`` attaches the entity
    to the central hub device; a resolved channel routes it to the
    physical device via ``channel.device.identifier`` (mirrors
    aiohomematic's ``channel_lookup.identify_channel`` behaviour, fed by
    the daemon-resolved link on the wire summary).
    """

    @staticmethod
    def _store_with_channel() -> LoomStore:
        store = LoomStore()
        summary = DeviceSummary.model_validate(
            {
                "address": "VCU0001",
                "interface": "home:HmIP-RF",
                "interface_id": "home:HmIP-RF",
                "model": "HmIP-PSM",
                "name": "Lamp",
                "available": True,
                "channels_count": 1,
                "updatable": False,
                "update_available": False,
                "master_pushes_config_pending": False,
                "has_sub_devices": False,
                "firmware": {"Current": "1.0.0", "Available": "", "Updatable": False, "UpdateState": "UP_TO_DATE"},
                "availability": {
                    "IsReachable": True,
                    "LastUpdated": None,
                    "BatteryLevel": None,
                    "LowBattery": None,
                    "SignalStrength": None,
                },
            }
        )
        store.load_snapshot(
            snapshot=Snapshot.model_validate(
                {"generated_at": "2026-05-24T08:00:00Z", "devices": [summary.model_dump()]}
            )
        )
        store.attach_device_detail(
            detail=DeviceDetail.model_validate(
                {
                    **summary.model_dump(),
                    "firmware": {},
                    "availability": {},
                    "channels": [
                        {
                            "address": "VCU0001:1",
                            "number": 1,
                            "paramset_key": "VALUES",
                            "data_points_count": 0,
                        }
                    ],
                }
            )
        )
        return store

    def test_linked_sysvar_twin_resolves_channel(self) -> None:
        store = self._store_with_channel()
        dp = make_sysvar_data_point(
            summary=_sysvar_summary(channel="VCU0001:1", device_address="VCU0001"),
            store=store,
        )
        channel = dp.channel
        assert channel is not None
        # The exact hops homematicip_local's _get_device_info walks.
        assert channel.device is not None
        assert channel.device.identifier == "VCU0001@home:HmIP-RF"

    def test_unlinked_sysvar_twin_channel_is_none(self) -> None:
        dp = make_sysvar_data_point(summary=_sysvar_summary(), store=self._store_with_channel())
        assert dp.channel is None

    def test_linked_program_twin_resolves_channel(self) -> None:
        store = self._store_with_channel()
        program = ProgramSummary.model_validate(
            {
                "id": "p1",
                "name": "All off",
                "active": True,
                "unique_id": "loom_test_p1",
                "channel": "VCU0001:1",
                "device_address": "VCU0001",
            }
        )
        dp = make_program_data_point(summary=program, store=store)
        channel = dp.channel
        assert channel is not None
        assert channel.device is not None
        assert channel.device.identifier == "VCU0001@home:HmIP-RF"

    def test_link_to_unloaded_channel_falls_back_to_none(self) -> None:
        # A link the store cannot resolve (device not bootstrapped yet)
        # must degrade to the hub device, never raise.
        dp = make_sysvar_data_point(
            summary=_sysvar_summary(channel="GHOST:7", device_address="GHOST"),
            store=self._store_with_channel(),
        )
        assert dp.channel is None


class TestProgramControls:
    """A CCU program is two controls, and both have to track the CCU."""

    @staticmethod
    def _store() -> LoomStore:
        from openccu_loom_client.compat.aiohomematic.model.hub import make_program_data_points, make_sysvar_data_point

        store = LoomStore()
        store.set_serial(serial="3014F711A0001234")
        store.set_hub_data_point_factories(
            program_factory=make_program_data_points,
            sysvar_factory=make_sysvar_data_point,
        )
        return store

    @staticmethod
    def _program(*, active: bool, execute_available: bool) -> Any:
        from openccu_loom_types.rest import ProgramSummary

        return ProgramSummary.model_validate(
            {
                "id": "1234",
                "name": "Testprogramm",
                "active": active,
                "execute_available": execute_available,
                "unique_id": "loom_program_testprogramm",
                "central": "home",
            }
        )

    def test_switch_reports_the_activity_flag(self) -> None:
        """Home Assistant reads the switch's state off ``value``."""
        store = self._store()
        store._upsert_program(summary=self._program(active=True, execute_available=True))
        _button, switch = store.program_data_points(program_id="1234")
        assert switch.value is True

    def test_catalogue_refresh_reaches_both_controls(self) -> None:
        """
        A deactivation on the CCU takes the button down with it.

        This is the regression the whole change exists for: the twins used to
        be built beside the store's program, so a refresh updated a copy while
        Home Assistant kept reading the original.
        """
        store = self._store()
        store._upsert_program(summary=self._program(active=True, execute_available=True))
        button, switch = store.program_data_points(program_id="1234")
        assert button.available is True

        store._upsert_program(summary=self._program(active=False, execute_available=False))

        assert switch.value is False
        assert button.available is False

    def test_push_reaches_both_controls(self) -> None:
        """The live ``hub.program_changed`` push does the same without a poll."""
        from openccu_loom_types.ws import ProgramChangedPayload

        store = self._store()
        store._upsert_program(summary=self._program(active=False, execute_available=False))
        button, switch = store.program_data_points(program_id="1234")

        store.apply_program_changed(
            payload=ProgramChangedPayload.model_validate(
                {
                    "central": "home",
                    "program_id": "1234",
                    "active": True,
                    "execute_available": True,
                    "unique_id": "loom_program_testprogramm",
                }
            )
        )

        assert switch.value is True
        assert button.available is True

    def test_push_for_unknown_program_is_ignored(self) -> None:
        from openccu_loom_types.ws import ProgramChangedPayload

        store = self._store()
        store.apply_program_changed(
            payload=ProgramChangedPayload.model_validate(
                {"central": "home", "program_id": "ghost", "active": True, "execute_available": True}
            )
        )
        assert store.get_program(program_id="ghost") is None

    async def test_turn_off_writes_and_re_reads(self) -> None:
        """
        The switch writes the flag and settles the local view.

        The daemon accepts the write as scheduled and pushes the flip once the
        CCU confirms it; the re-read is what a consumer reading the program
        straight after the call sees.
        """
        calls: list[tuple[str, str, Any]] = []

        class _Transport:
            async def request(
                self,
                method: str,
                path: str,
                *,
                params: Any = None,
                json_body: Any = None,
                headers: Any = None,
                allow_retry: Any = None,
            ) -> Any:
                calls.append((method, path, json_body))
                if method == "GET":
                    return TestProgramControls._program(active=False, execute_available=False).model_dump(mode="json")
                return None

        store = self._store()
        store._upsert_program(summary=self._program(active=True, execute_available=True))
        store.set_transport(transport=_Transport())  # type: ignore[arg-type]
        button, switch = store.program_data_points(program_id="1234")

        await switch.turn_off()

        assert calls[0] == ("PATCH", "/programs/1234", {"active": False})
        assert calls[1][:2] == ("GET", "/programs/1234")
        assert switch.value is False
        assert button.available is False


class TestSysvarStaysLive:
    """A sysvar push has to reach the object Home Assistant holds."""

    def test_push_updates_the_categorised_twin(self) -> None:
        from openccu_loom_types.rest import SysvarSummary
        from openccu_loom_types.ws import SysvarChangedPayload

        from openccu_loom_client.compat.aiohomematic.model.hub import make_program_data_points, make_sysvar_data_point

        store = LoomStore()
        store.set_hub_data_point_factories(
            program_factory=make_program_data_points,
            sysvar_factory=make_sysvar_data_point,
        )
        store._upsert_sysvar(
            summary=SysvarSummary.model_validate(
                {
                    "name": "Testvar",
                    "value": 1.0,
                    "value_type": "FLOAT",
                    "observed": True,
                    "unique_id": "loom_sysvar_testvar",
                    "central": "home",
                }
            )
        )
        dp = store.get_sysvar(name="Testvar")
        assert dp is not None
        assert dp.value == 1.0

        store.apply_sysvar_changed(
            payload=SysvarChangedPayload.model_validate(
                {"central": "home", "name": "Testvar", "value": 42.0, "unique_id": "loom_sysvar_testvar"}
            )
        )

        # Same live object — not a copy the store updated beside it.
        assert dp.value == 42.0
        assert store.get_sysvar(name="Testvar") is dp


class TestHubPollAnnouncesResults:
    """The manual fetch service has to tell Home Assistant what it changed."""

    @staticmethod
    def _sysvar(value: float) -> Any:
        from openccu_loom_types.rest import SysvarSummary

        return SysvarSummary.model_validate(
            {
                "name": "Testvar",
                "value": value,
                "value_type": "FLOAT",
                "observed": True,
                "unique_id": "loom_sysvar_testvar",
                "central": "home",
            }
        )

    @staticmethod
    def _program(*, active: bool) -> Any:
        from openccu_loom_types.rest import ProgramSummary

        return ProgramSummary.model_validate(
            {
                "id": "1234",
                "name": "P",
                "active": active,
                "execute_available": active,
                "unique_id": "loom_program_p",
                "central": "home",
            }
        )

    def _coordinator(self, *, sysvars: list[Any], programs: list[Any], published: list[Any]) -> Any:
        from types import SimpleNamespace

        from openccu_loom_client.compat.aiohomematic.central.hub_coordinator import _HubCoordinator
        from openccu_loom_client.compat.aiohomematic.model.hub import make_program_data_points, make_sysvar_data_point

        store = LoomStore()
        store.set_central_name(central_name="home")
        store.set_hub_data_point_factories(
            program_factory=make_program_data_points,
            sysvar_factory=make_sysvar_data_point,
        )

        async def _list_sysvars() -> list[Any]:
            return sysvars

        async def _list_programs() -> list[Any]:
            return programs

        class _Bus:
            async def publish(self, *, event: Any) -> None:
                published.append(event)

        hub_ops = SimpleNamespace(list_sysvars=_list_sysvars, list_programs=_list_programs)
        return _HubCoordinator(client=SimpleNamespace(store=store, hub=hub_ops), ha_bus=_Bus()), store

    async def test_sysvar_fetch_emits_only_for_changes(self) -> None:
        published: list[Any] = []
        hc, store = self._coordinator(sysvars=[self._sysvar(99.0)], programs=[], published=published)
        store._upsert_sysvar(summary=self._sysvar(1.0))
        dp = store.get_sysvar(name="Testvar")
        assert dp is not None

        await hc.fetch_sysvar_data(scheduled=False)
        assert dp.value == 99.0
        assert len(published) == 1
        assert published[0].unique_id == "loom_sysvar_testvar"
        assert published[0].new_value == 99.0

        # A second fetch with the same catalogue announces nothing.
        published.clear()
        await hc.fetch_sysvar_data(scheduled=False)
        assert published == []

    async def test_program_fetch_emits_once_per_program(self) -> None:
        """
        One event per program, keyed on the shared canonical id.

        The execute button carries no ``value`` of its own — publishing per
        twin would read an attribute that does not exist.
        """
        published: list[Any] = []
        hc, store = self._coordinator(sysvars=[], programs=[self._program(active=False)], published=published)
        store._upsert_program(summary=self._program(active=True))

        await hc.fetch_program_data(scheduled=False)

        assert len(published) == 1
        assert published[0].unique_id == "loom_program_p"
        assert published[0].new_value is False

        published.clear()
        await hc.fetch_program_data(scheduled=False)
        assert published == []
