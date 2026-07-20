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
                }
            )
        )
        dp = store.get_data_point(address="VCU7", channel=1, parameter="WINDOW_OPEN")
        assert dp.value is True


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
