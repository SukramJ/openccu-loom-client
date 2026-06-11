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

from openccu_loom_types.rest import CustomDPSummary, DataPointSummary

from openccu_loom_client.compat.aiohomematic.model.custom import (
    BaseCustomDpLock,
    BaseCustomDpSiren,
    CustomDpIpThermostat,
    make_custom_data_point,
)
from openccu_loom_client.compat.aiohomematic.model.generic import (
    DpBinarySensor,
    DpSensor,
    make_generic_data_point,
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


def _cdp_summary(**overrides: Any) -> CustomDPSummary:
    payload: dict[str, Any] = {
        "name": "SET_POINT_TEMPERATURE",
        "category": "climate",
        "channel_no": 1,
        "supported_operations": [],
        "kind": "climate_hmip",
    }
    payload.update(overrides)
    return CustomDPSummary.model_validate(payload)


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
        assert cdp.profiles == ("boost", "week_program_1")
        # HA reads .value off the members (climate.py preset_modes), so
        # the tuples must carry the aiohomematic enums, not bare strings.
        assert [m.value for m in cdp.modes] == ["auto", "heat", "off"]
        assert [p.value for p in cdp.profiles] == ["boost", "week_program_1"]
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
        assert [p.value for p in cdp.profiles] == ["boost"]

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
        cdp = _make_cdp(
            _cdp_summary(name="BUTTON_LOCK@0", category="lock", kind="lock", channel_no=0)
        )
        assert cdp.data_point_name_postfix == "BUTTON_LOCK"


class TestCustomTranslatedName:
    """Custom DPs derive their display name from the CCU channel name."""

    def _store_with_channels(self) -> LoomStore:
        from openccu_loom_types.rest import DeviceDetail, Snapshot

        store = LoomStore()
        store.set_custom_data_point_factory(make_custom_data_point)
        store.load_snapshot(
            Snapshot.model_validate(
                {
                    "generated_at": "2026-06-11T08:00:00Z",
                    "devices": [
                        {
                            "address": "VCU1",
                            "interface": "home:HmIP-RF",
                            "model": "HmIP-DRD3",
                            "name": "Küchenstrahler",
                            "available": True,
                            "channels_count": 7,
                        }
                    ],
                }
            )
        )
        store.attach_device_detail(
            DeviceDetail.model_validate(
                {
                    "address": "VCU1",
                    "interface": "home:HmIP-RF",
                    "model": "HmIP-DRD3",
                    "name": "Küchenstrahler",
                    "available": True,
                    "channels_count": 7,
                    "channels": [
                        {
                            "address": "VCU1:4",
                            "number": 4,
                            "name": "Küchenstrahler",
                            "paramset_key": "VALUES",
                            "data_points_count": 3,
                        },
                        {
                            "address": "VCU1:5",
                            "number": 5,
                            "name": "Küchenstrahler:vch5",
                            "paramset_key": "VALUES",
                            "data_points_count": 3,
                        },
                        {
                            "address": "VCU1:6",
                            "number": 6,
                            "name": "Küchenstrahler:vch6",
                            "paramset_key": "VALUES",
                            "data_points_count": 3,
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
        store.attach_custom_data_points(
            device_address="VCU1",
            cdps=[
                _cdp_summary(name="LEVEL@5", category="light", kind="light", channel_no=5),
                _cdp_summary(name="LEVEL@6", category="light", kind="light", channel_no=6),
            ],
        )
        assert store.get_custom_data_point(address="VCU1", name="LEVEL@5").translated_name == "vch5"
        assert store.get_custom_data_point(address="VCU1", name="LEVEL@6").translated_name == "vch6"
