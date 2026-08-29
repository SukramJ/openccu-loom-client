# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Week-profile and schedule-switch data points."""

from __future__ import annotations

from typing import Any

from aiohomematic.interfaces import model as aio_model

from openccu_loom_client.compat.aiohomematic._upstream import ScheduleProfile, WeekdayStr
from openccu_loom_client.compat.aiohomematic.model.week_profile import (
    ClimateWeekProfileDp,
    ScheduleChannelSwitch,
    WeekProfileDp,
)
from openccu_loom_client.model import Device
from openccu_loom_client.store import LoomStore
from openccu_loom_client.wire.rest import DeviceSummary, Schedule, WeekProfileResponse

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
            "unique_id": "loom_week_profile_vcu0000001_week_profile",
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
            "active_profile_index": 1,
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
    def __init__(self, *, schedule: Schedule | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.puts: list[Schedule] = []
        self._schedule = schedule

    async def set_channel_lock(self, *, address: str, channel: int, key: str, enabled: bool) -> None:
        self.calls.append({"address": address, "channel": channel, "key": key, "enabled": enabled})

    async def get_channel_schedule(self, *, address: str, channel: int) -> Schedule:
        assert self._schedule is not None
        return self._schedule

    async def put_channel_schedule(self, *, address: str, channel: int, schedule: Schedule) -> None:
        self.puts.append(schedule)
        self._schedule = schedule

    async def copy_schedule(self, *, src_address: str, dst_address: str) -> None:
        self.calls.append({"copy_schedule": (src_address, dst_address)})

    async def copy_climate_profile(
        self, *, src_channel_address: str, src_profile: int, dst_channel_address: str, dst_profile: int
    ) -> None:
        self.calls.append(
            {
                "src_channel_address": src_channel_address,
                "src_profile": src_profile,
                "dst_channel_address": dst_channel_address,
                "dst_profile": dst_profile,
            }
        )


def _climate_dp(store: LoomStore, device: Device, *, ops: _FakeSchedulesOps | None = None) -> ClimateWeekProfileDp:
    return ClimateWeekProfileDp(
        store=store,
        device=device,
        channel_no=1,
        week_profile=_week_profile(),
        schedules_ops=ops or _FakeSchedulesOps(),  # type: ignore[arg-type]
    )


class TestWeekProfileDp:
    def test_unique_id_carries_no_serial(self) -> None:
        store, device = _store_with_device()
        dp = _climate_dp(store, device)
        assert dp.unique_id == "loom_week_profile_vcu0000001_week_profile"

    def test_climate_entry_count_uses_active_profile(self) -> None:
        store, device = _store_with_device()
        dp = _climate_dp(store, device)
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
            schedules_ops=_FakeSchedulesOps(),  # type: ignore[arg-type]
        )
        dp.update_from(schedule=_simple_schedule())
        assert dp.value == 2
        assert dp.schedule_domain == "switch"
        assert dp.max_entries == 24

    def test_climate_metadata(self) -> None:
        store, device = _store_with_device()
        dp = _climate_dp(store, device)
        assert dp.schedule_type.value == "climate"
        assert dp.max_entries == 13 * 7 * 6
        assert dp.min_temp == 4.5
        assert dp.max_temp == 30.5
        # available_profiles are ScheduleProfile enums (climate.py/sensor.py read `.value`).
        assert dp.available_profiles == (ScheduleProfile.P1, ScheduleProfile.P2, ScheduleProfile.P3)
        assert dp.current_schedule_profile is ScheduleProfile.P1
        assert dp.schedule_channel_address == f"{_ADDRESS}:1"
        assert dp.enabled_default is True
        assert dp.schedule is None
        dp.update_from(schedule=_climate_schedule())
        assert dp.schedule is not None
        assert dp.schedule["kind"] == "climate"
        assert dp.device_active_profile_index == 1

    def test_default_type_has_no_temperatures(self) -> None:
        store, device = _store_with_device()
        dp = WeekProfileDp(
            store=store,
            device=device,
            channel_no=1,
            week_profile=_week_profile(schedule_type="default"),
            schedules_ops=_FakeSchedulesOps(),  # type: ignore[arg-type]
        )
        assert dp.min_temp is None
        assert dp.max_temp is None


class TestScheduleProtocolConformance:
    """The isinstance split the schedule facade + HA climate/sensor entities branch on."""

    def test_climate_dp_satisfies_climate_protocol(self) -> None:
        store, device = _store_with_device()
        dp = _climate_dp(store, device)
        assert isinstance(dp, aio_model.ClimateWeekProfileDataPointProtocol)
        assert isinstance(dp, aio_model.WeekProfileDataPointProtocol)

    def test_simple_dp_is_not_climate(self) -> None:
        store, device = _store_with_device()
        dp = WeekProfileDp(
            store=store,
            device=device,
            channel_no=1,
            week_profile=_week_profile(schedule_type="default"),
            schedules_ops=_FakeSchedulesOps(),  # type: ignore[arg-type]
        )
        assert isinstance(dp, aio_model.WeekProfileDataPointProtocol)
        assert not isinstance(dp, aio_model.ClimateWeekProfileDataPointProtocol)


class TestClimateScheduleData:
    """Profile/weekday read + write translate loom `start_time`/`end_time` to the cards' `starttime`/`endtime`."""

    async def test_get_schedule_profile_shape(self) -> None:
        store, device = _store_with_device()
        ops = _FakeSchedulesOps(schedule=_climate_schedule())
        dp = _climate_dp(store, device, ops=ops)
        profile = await dp.get_schedule_profile(profile=ScheduleProfile.P1, force_load=True)
        expected_weekday = {
            "base_temperature": 17.0,
            "periods": [
                {"starttime": "06:00", "endtime": "09:00", "temperature": 21.0},
                {"starttime": "17:00", "endtime": "22:00", "temperature": 22.0},
            ],
        }
        assert profile == {"MONDAY": expected_weekday, "TUESDAY": expected_weekday}

    async def test_current_profile_schedule_uses_pointer(self) -> None:
        store, device = _store_with_device()
        dp = _climate_dp(store, device)
        dp.update_from(schedule=_climate_schedule())
        assert dp.current_profile_schedule is not None
        assert set(dp.current_profile_schedule) == {"MONDAY", "TUESDAY"}
        # Switching the editor pointer to a profile absent from the payload → None.
        dp.set_current_schedule_profile(profile=ScheduleProfile.P6)
        assert dp.current_schedule_profile is ScheduleProfile.P6
        assert dp.current_profile_schedule is None

    async def test_set_schedule_weekday_round_trips_to_wire(self) -> None:
        store, device = _store_with_device()
        ops = _FakeSchedulesOps(schedule=_climate_schedule())
        dp = _climate_dp(store, device, ops=ops)
        await dp.set_schedule_weekday(
            profile=ScheduleProfile.P1,
            weekday=WeekdayStr.WEDNESDAY,
            weekday_data={
                "base_temperature": 18.0,
                "periods": [{"starttime": "07:00", "endtime": "09:00", "temperature": 22.0}],
            },
        )
        assert len(ops.puts) == 1
        wednesday = ops.puts[0].profiles["P1"].weekdays["WEDNESDAY"]
        assert wednesday.base_temperature == 18.0
        # Frontend `starttime`/`endtime` become wire `start_time`/`end_time`.
        assert wednesday.periods[0].start_time == "07:00"
        assert wednesday.periods[0].end_time == "09:00"
        assert wednesday.periods[0].temperature == 22.0

    async def test_copy_climate_profile_uses_1_based_index(self) -> None:
        store, device = _store_with_device()
        ops = _FakeSchedulesOps(schedule=_climate_schedule())
        dp = _climate_dp(store, device, ops=ops)
        await dp.copy_schedule_profile(source_profile=ScheduleProfile.P1, target_profile=ScheduleProfile.P3)
        assert ops.calls[-1] == {
            "src_channel_address": f"{_ADDRESS}:1",
            "src_profile": 1,
            "dst_channel_address": f"{_ADDRESS}:1",
            "dst_profile": 3,
        }


class TestDeviceScheduleData:
    """Simple-schedule read/write use the cards' `{entries: {slot: entry}}` shape."""

    async def test_get_schedule_slot_keyed_entries(self) -> None:
        store, device = _store_with_device()
        ops = _FakeSchedulesOps(schedule=_simple_schedule())
        dp = WeekProfileDp(
            store=store,
            device=device,
            channel_no=1,
            week_profile=_week_profile(schedule_type="default"),
            schedules_ops=ops,  # type: ignore[arg-type]
        )
        result = await dp.get_schedule(force_load=True)
        assert set(result["entries"]) == {"1", "2"}
        assert "slot_no" not in result["entries"]["1"]
        assert result["entries"]["1"]["time"] == "06:00"
        assert result["entries"]["2"]["time"] == "20:00"

    async def test_set_schedule_round_trips_slot_no(self) -> None:
        store, device = _store_with_device()
        ops = _FakeSchedulesOps(schedule=_simple_schedule())
        dp = WeekProfileDp(
            store=store,
            device=device,
            channel_no=1,
            week_profile=_week_profile(schedule_type="default"),
            schedules_ops=ops,  # type: ignore[arg-type]
        )
        payload = await dp.get_schedule(force_load=True)
        await dp.set_schedule(schedule_data=payload)
        assert len(ops.puts) == 1
        slots = {entry.slot_no: entry.time for entry in (ops.puts[0].simple_entries or [])}
        assert slots == {1: "06:00", 2: "20:00"}

    async def test_set_schedule_enabled_all_keys(self) -> None:
        store, device = _store_with_device()
        ops = _FakeSchedulesOps()
        dp = WeekProfileDp(
            store=store,
            device=device,
            channel_no=1,
            week_profile=_week_profile(schedule_type="default", schedule_enabled={"1_1": True, "1_2": True}),
            schedules_ops=ops,  # type: ignore[arg-type]
        )
        await dp.set_schedule_enabled(enabled=False)
        assert {call["key"] for call in ops.calls} == {"1_1", "1_2"}
        assert dp.schedule_enabled == {"1_1": False, "1_2": False}


class TestScheduleChannelSwitch:
    def _build(self) -> tuple[ScheduleChannelSwitch, WeekProfileDp, _FakeSchedulesOps]:
        store, device = _store_with_device()
        wp_dp = WeekProfileDp(
            store=store,
            device=device,
            channel_no=1,
            week_profile=_week_profile(schedule_type="default", schedule_enabled={"1_1": True, "1_2": False}),
            schedules_ops=_FakeSchedulesOps(),  # type: ignore[arg-type]
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

    def test_unique_id_falls_back_when_no_target_entry(self) -> None:
        # _build()'s week profile carries no available_target_channels for "1_1",
        # so the switch synthesises the canonical key (older-daemon fallback).
        switch, _wp_dp, _ops = self._build()
        assert switch.unique_id == "loom_schedule_channel_switch_vcu0000001_schedule_channel_lock_1_1"

    def test_unique_id_consumes_daemon_target_key(self) -> None:
        # J5: when the daemon ships the target entry, the switch consumes its
        # unique_id verbatim (here a sentinel distinct from the canonical key,
        # proving consumption rather than recomputation).
        store, device = _store_with_device()
        wp_dp = WeekProfileDp(
            store=store,
            device=device,
            channel_no=1,
            week_profile=WeekProfileResponse.model_validate(
                {
                    "address": f"{_ADDRESS}:1",
                    "unique_id": "loom_week_profile_vcu0000001_week_profile",
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
                            "unique_id": "loom_daemon_supplied_switch_key",
                        },
                    },
                }
            ),
            schedules_ops=_FakeSchedulesOps(),  # type: ignore[arg-type]
        )
        switch = ScheduleChannelSwitch(
            store=store,
            device=device,
            channel_no=1,
            channel_key="1_1",
            week_profile_dp=wp_dp,
            schedules_ops=_FakeSchedulesOps(),  # type: ignore[arg-type]
        )
        assert switch.unique_id == "loom_daemon_supplied_switch_key"

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
                    "unique_id": "loom_week_profile_vcu0000001_week_profile",
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
                            "unique_id": "loom_schedule_channel_switch_vcu0000001_schedule_channel_lock_1_1",
                        },
                    },
                }
            ),
            schedules_ops=_FakeSchedulesOps(),  # type: ignore[arg-type]
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
        # available_target_channels are exposed as aiohomematic TargetChannelInfo dataclasses
        # (the schedule facade calls dataclasses.asdict on them).
        import dataclasses

        targets = wp_dp.available_target_channels
        assert dataclasses.asdict(targets["1_1"]) == {
            "channel_no": 4,
            "channel_address": f"{_ADDRESS}:4",
            "name": "SHUTTER_VIRTUAL_RECEIVER",
            "channel_type": "primary",
        }

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
