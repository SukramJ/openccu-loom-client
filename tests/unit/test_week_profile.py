# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Week-profile and schedule-switch data points."""

from __future__ import annotations

from typing import Any

from openccu_loom_types.rest import DeviceSummary, Schedule, WeekProfileResponse

from openccu_loom_client.compat.aiohomematic.model.week_profile import ScheduleChannelSwitch, WeekProfileDp
from openccu_loom_client.model import Device
from openccu_loom_client.store import LoomStore

_ADDRESS = "VCU0000001"


def _store_with_device() -> tuple[LoomStore, Device]:
    store = LoomStore()
    store.set_serial(serial="ABC1234567")
    store.set_central_name(central_name="home")
    summary = DeviceSummary.model_validate(
        {
            "address": _ADDRESS,
            "interface": "HmIP-RF",
            "interface_id": "home-HmIP-RF",
            "model": "HmIP-eTRV-2",
            "name": "Thermostat",
            "available": True,
            "channels_count": 1,
        }
    )
    device = store._upsert_device_summary(summary=summary)
    return store, device


def _week_profile(
    *,
    schedule_type: str = "climate",
    schedule_enabled: dict[str, bool] | None = None,
) -> WeekProfileResponse:
    return WeekProfileResponse.model_validate(
        {
            "address": f"{_ADDRESS}:1",
            "schedule_type": schedule_type,
            "min_temp": 4.5,
            "max_temp": 30.5,
            "profile_count": 3,
            "current_profile": "P1",
            "available_profiles": ["P1", "P2", "P3"],
            "schedule_enabled": schedule_enabled,
            "has_climate_schedule": schedule_type == "climate",
        }
    )


def _climate_schedule() -> Schedule:
    weekday = {
        "base_temperature": 17.0,
        "periods": [
            {"start_time": "06:00", "end_time": "09:00", "temperature": 21.0},
            {"start_time": "17:00", "end_time": "22:00", "temperature": 22.0},
        ],
    }
    return Schedule.model_validate(
        {
            "channel": {"address": f"{_ADDRESS}:1", "number": 1, "device_address": _ADDRESS},
            "kind": "climate",
            "active_profile": "P1",
            "profiles": {
                "P1": {"weekdays": {"MONDAY": weekday, "TUESDAY": weekday}},
                # A foreign profile must not count towards the active one.
                "P2": {"weekdays": {"MONDAY": weekday}},
            },
        }
    )


def _simple_schedule() -> Schedule:
    entry = {
        "slot_no": 1,
        "weekdays": ["MONDAY"],
        "time": "06:00",
        "level": 1.0,
        "target_channels": [f"{_ADDRESS}:2"],
    }
    return Schedule.model_validate(
        {
            "channel": {"address": f"{_ADDRESS}:1", "number": 1, "device_address": _ADDRESS},
            "kind": "simple",
            "domain": "switch",
            "simple_entries": [entry, {**entry, "slot_no": 2, "time": "20:00"}],
        }
    )


class _FakeSchedulesOps:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def set_channel_lock(self, *, address: str, channel: int, key: str, enabled: bool) -> None:
        self.calls.append({"address": address, "channel": channel, "key": key, "enabled": enabled})


class TestWeekProfileDp:
    def test_unique_id_carries_no_serial(self) -> None:
        store, device = _store_with_device()
        dp = WeekProfileDp(store=store, device=device, channel_no=1, week_profile=_week_profile())
        assert dp.unique_id == "loom_week_profile_vcu0000001_week_profile"

    def test_climate_entry_count_uses_active_profile(self) -> None:
        store, device = _store_with_device()
        dp = WeekProfileDp(store=store, device=device, channel_no=1, week_profile=_week_profile())
        assert dp.value is None
        assert dp.is_valid is False
        dp.update_from(schedule=_climate_schedule())
        # Active profile P1: 2 weekdays × 2 periods.
        assert dp.value == 4
        assert dp.is_valid is True

    def test_simple_entry_count(self) -> None:
        store, device = _store_with_device()
        dp = WeekProfileDp(
            store=store,
            device=device,
            channel_no=1,
            week_profile=_week_profile(schedule_type="default"),
        )
        dp.update_from(schedule=_simple_schedule())
        assert dp.value == 2
        assert dp.schedule_domain == "switch"
        assert dp.max_entries == 24

    def test_climate_metadata(self) -> None:
        store, device = _store_with_device()
        dp = WeekProfileDp(store=store, device=device, channel_no=1, week_profile=_week_profile())
        assert dp.schedule_type.value == "climate"
        assert dp.max_entries == 13 * 7 * 6
        assert dp.min_temp == 4.5
        assert dp.max_temp == 30.5
        assert dp.available_profiles == ("P1", "P2", "P3")
        assert dp.current_profile == "P1"
        assert dp.schedule_channel_address == f"{_ADDRESS}:1"
        assert dp.enabled_default is True
        assert dp.schedule is None
        dp.update_from(schedule=_climate_schedule())
        assert dp.schedule is not None
        assert dp.schedule["kind"] == "climate"

    def test_default_type_has_no_temperatures(self) -> None:
        store, device = _store_with_device()
        dp = WeekProfileDp(
            store=store,
            device=device,
            channel_no=1,
            week_profile=_week_profile(schedule_type="default"),
        )
        assert dp.min_temp is None
        assert dp.max_temp is None


class TestScheduleChannelSwitch:
    def _build(self) -> tuple[ScheduleChannelSwitch, WeekProfileDp, _FakeSchedulesOps]:
        store, device = _store_with_device()
        wp_dp = WeekProfileDp(
            store=store,
            device=device,
            channel_no=1,
            week_profile=_week_profile(schedule_type="default", schedule_enabled={"1_1": True, "1_2": False}),
        )
        ops = _FakeSchedulesOps()
        switch = ScheduleChannelSwitch(
            store=store,
            device=device,
            channel_no=1,
            channel_key="1_1",
            week_profile_dp=wp_dp,
            schedules_ops=ops,  # type: ignore[arg-type]
        )
        return switch, wp_dp, ops

    def test_unique_id(self) -> None:
        switch, _wp_dp, _ops = self._build()
        assert switch.unique_id == "loom_schedule_channel_switch_vcu0000001_schedule_channel_lock_1_1"

    def test_value_reads_schedule_enabled(self) -> None:
        switch, _wp_dp, _ops = self._build()
        assert switch.value is True
        assert switch.channel_key == "1_1"
        assert switch.is_valid is True

    def test_name_data_carries_target_channel_name(self) -> None:
        store, device = _store_with_device()
        wp_dp = WeekProfileDp(
            store=store,
            device=device,
            channel_no=1,
            week_profile=WeekProfileResponse.model_validate(
                {
                    "address": f"{_ADDRESS}:1",
                    "schedule_type": "default",
                    "min_temp": 0,
                    "max_temp": 0,
                    "profile_count": 1,
                    "has_climate_schedule": False,
                    "schedule_enabled": {"1_1": True},
                    "available_target_channels": {
                        "1_1": {
                            "channel_no": 4,
                            "channel_address": f"{_ADDRESS}:4",
                            "name": "SHUTTER_VIRTUAL_RECEIVER",
                            "channel_type": "primary",
                        },
                    },
                }
            ),
        )
        switch = ScheduleChannelSwitch(
            store=store,
            device=device,
            channel_no=1,
            channel_key="1_1",
            week_profile_dp=wp_dp,
            schedules_ops=_FakeSchedulesOps(),  # type: ignore[arg-type]
        )
        # The HA switch composes "<Schedule> <channel_name>" from this.
        assert switch.name_data.channel_name == "SHUTTER_VIRTUAL_RECEIVER"
        assert wp_dp.target_channel_name(channel_key="1_1") == "SHUTTER_VIRTUAL_RECEIVER"
        # Unknown key (older daemon / no mapping) -> bare schedule name fallback.
        assert wp_dp.target_channel_name(channel_key="9_9") is None

    def test_enabled_default_is_false(self) -> None:
        switch, wp_dp, _ops = self._build()
        assert switch.enabled_default is False
        assert wp_dp.enabled_default is True

    async def test_turn_off_writes_channel_lock_and_updates_state(self) -> None:
        switch, wp_dp, ops = self._build()
        await switch.turn_off()
        assert ops.calls == [{"address": _ADDRESS, "channel": 1, "key": "1_1", "enabled": False}]
        assert switch.value is False
        assert wp_dp.schedule_enabled == {"1_1": False, "1_2": False}

    async def test_turn_on_writes_channel_lock(self) -> None:
        switch, _wp_dp, ops = self._build()
        await switch.turn_on()
        assert ops.calls == [{"address": _ADDRESS, "channel": 1, "key": "1_1", "enabled": True}]
        assert switch.value is True

    def test_name_data_has_no_channel_name(self) -> None:
        switch, _wp_dp, _ops = self._build()
        assert switch.name_data.channel_name is None
        assert switch.name_data.parameter_name == "SCHEDULE_CHANNEL_LOCK_1_1"
