# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
``aiohomematic.model.week_profile_data_point`` twins — schedule data points.

Three per-device schedule entities mirror aiohomematic's week-profile
layer:

- :class:`WeekProfileDp` (category ``week_profile`` → HA sensor): the
  number of active schedule entries plus the schedule metadata the HA
  sensor renders as attributes, and the *device*-schedule read/write
  surface (``get_schedule`` / ``set_schedule`` / ``set_schedule_enabled``)
  the config panel's simple-schedule editor and the HACS schedule cards
  drive. Satisfies :class:`WeekProfileDataPointProtocol`. Backed by the
  daemon's ``GET …/week_profile`` descriptor and ``…/schedule`` payload.
- :class:`ClimateWeekProfileDp` (subclass): adds the profile-/weekday-level
  climate surface (``get_schedule_profile`` / ``set_schedule_weekday`` /
  ``current_schedule_profile`` / …) so it satisfies the
  ``@runtime_checkable`` :class:`ClimateWeekProfileDataPointProtocol`. Only
  climate schedules get this class, so ``isinstance(…, Climate…Protocol)``
  distinguishes climate from simple schedules exactly like the reference
  (``ClimateWeekProfile`` vs ``DefaultWeekProfile``) — the config-panel
  schedule facade and the HA climate/sensor entities branch on that check.
- :class:`ScheduleChannelSwitch` (category ``schedule_switch`` → HA
  switch, disabled by default): one switch per ``schedule_enabled``
  channel key, toggling the channel's week-program participation.

The frontend consumes profile data as ``{weekday: {base_temperature,
periods: [{starttime, endtime, temperature}]}}`` and simple schedules as
``{entries: {slot: SimpleScheduleEntry}}`` — the loom wire models
(``ClimatePeriod.start_time``/``end_time``, ``SimpleScheduleEntry.slot_no``)
are translated to those key names here so the shape matches the aiohomematic
backend the cards were written against.

Unique ids match aiohomematic's registry exactly
(``loom_week_profile_<addr>_week_profile`` and
``loom_schedule_channel_switch_<addr>_schedule_channel_lock_<key>`` —
device addresses carry no serial slot).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar, Final

from openccu_loom_client.canonical import canonical_unique_id
from openccu_loom_client.compat.aiohomematic._upstream import (
    CallSource,
    DataPointUsage,
    ScheduleField,
    ScheduleProfile,
    TargetChannelInfo,
    WeekdayStr,
)
from openccu_loom_client.compat.aiohomematic.model._protocol_surface import _CommonProtocolSurface, _NameData
from openccu_loom_client.compat.aiohomematic.model.hub._surface import _HubEntitySurface
from openccu_loom_client.wire.enums import DataPointCategory
from openccu_loom_client.wire.rest import ClimatePeriod, ClimateProfile, ClimateWeekday, SimpleScheduleEntry

if TYPE_CHECKING:
    from openccu_loom_client.model import Channel, Device
    from openccu_loom_client.operations.schedules import SchedulesOperations
    from openccu_loom_client.store import LoomStore
    from openccu_loom_client.wire.rest import Schedule, WeekProfileResponse

# Maximum schedule entries, mirroring aiohomematic's week-profile
# constants: 13 climate slots/day × 7 weekdays × 6 profiles, and 24
# entries for simple (non-climate) schedules.
_MAX_CLIMATE_ENTRIES: Final = 13 * 7 * 6
_MAX_SIMPLE_ENTRIES: Final = 24

_SCHEDULE_KIND_CLIMATE: Final = "climate"

# aiohomematic's per-frontend key names (``starttime``/``endtime``) differ
# from the loom wire model's (``start_time``/``end_time``); the cards were
# written against the former.
_PERIOD_START: Final = "starttime"
_PERIOD_END: Final = "endtime"
_PERIOD_TEMP: Final = "temperature"
_WEEKDAY_BASE_TEMP: Final = "base_temperature"
_WEEKDAY_PERIODS: Final = "periods"


def _periods_to_frontend(*, periods: list[ClimatePeriod]) -> list[dict[str, Any]]:
    """Render wire climate periods as the ``{starttime, endtime, temperature}`` dicts the cards read."""
    return [
        {_PERIOD_START: period.start_time, _PERIOD_END: period.end_time, _PERIOD_TEMP: period.temperature}
        for period in periods
    ]


def _weekday_to_frontend(*, weekday: ClimateWeekday) -> dict[str, Any]:
    """Render a wire climate weekday as ``{base_temperature, periods: [...]}``."""
    return {
        _WEEKDAY_BASE_TEMP: weekday.base_temperature,
        _WEEKDAY_PERIODS: _periods_to_frontend(periods=weekday.periods),
    }


def _profile_to_frontend(*, profile: ClimateProfile) -> dict[str, Any]:
    """Render a wire climate profile as ``{weekday: {base_temperature, periods}}``."""
    return {weekday_key: _weekday_to_frontend(weekday=weekday) for weekday_key, weekday in profile.weekdays.items()}


def _weekday_from_frontend(*, weekday_data: dict[str, Any]) -> ClimateWeekday:
    """Parse a ``{base_temperature, periods: [{starttime, endtime, temperature}]}`` dict into the wire model."""
    return ClimateWeekday(
        base_temperature=weekday_data[_WEEKDAY_BASE_TEMP],
        periods=[
            ClimatePeriod(
                start_time=period[_PERIOD_START],
                end_time=period[_PERIOD_END],
                temperature=period[_PERIOD_TEMP],
            )
            for period in weekday_data.get(_WEEKDAY_PERIODS, [])
        ],
    )


class _ScheduleEntityBase(_HubEntitySurface, _CommonProtocolSurface):
    """Shared device/channel surface of the schedule data points."""

    def __init__(self, *, store: LoomStore, device: Device, channel_no: int) -> None:
        """Bind the data point to its store, device and schedule channel."""
        self._store = store
        self._device = device
        self._channel_no = channel_no

    @property
    def device(self) -> Device:
        """Return the owning device."""
        return self._device

    @property
    def channel(self) -> Channel | None:
        """Return the schedule channel from the store."""
        return self._store.get_channel(address=self._device.address, number=self._channel_no)

    @property
    def channel_no(self) -> int:
        """Return the schedule channel number."""
        return self._channel_no

    @property
    def available(self) -> bool:
        """Return the owning device's availability."""
        return bool(self._device.available)

    @property
    def is_valid(self) -> bool:
        """Return whether the data point carries a value."""
        return True

    @property
    def modified_at(self) -> datetime | None:
        """Return when the value last changed."""
        return getattr(self, "_modified", None)

    @property
    def refreshed_at(self) -> datetime | None:
        """Return when the value was last fetched."""
        return getattr(self, "_modified", None)


class WeekProfileDp(_ScheduleEntityBase):
    """
    Week-profile data point: active-entry count + simple-schedule surface.

    Satisfies ``WeekProfileDataPointProtocol`` (device-level schedule access
    for non-climate schedules). Climate schedules use the
    :class:`ClimateWeekProfileDp` subclass, which additionally satisfies
    ``ClimateWeekProfileDataPointProtocol``.
    """

    _category: ClassVar[DataPointCategory] = DataPointCategory.WeekProfile

    def __init__(
        self,
        *,
        store: LoomStore,
        device: Device,
        channel_no: int,
        week_profile: WeekProfileResponse,
        schedules_ops: SchedulesOperations,
    ) -> None:
        """Bind the data point to the daemon's week-profile descriptor and schedule operations."""
        super().__init__(store=store, device=device, channel_no=channel_no)
        self._week_profile = week_profile
        self._schedules_ops = schedules_ops
        self._schedule: Schedule | None = None
        self._schedule_enabled: dict[str, bool] | None = (
            dict(week_profile.schedule_enabled) if week_profile.schedule_enabled else None
        )
        self._value: int | None = None
        self._modified: datetime | None = None
        self._enabled_default = True

    # ---- identity ----

    @property
    def unique_id(self) -> str:
        """Return the daemon-owned canonical key ``loom_week_profile_<addr>_week_profile`` (J5)."""
        return self._week_profile.unique_id

    @property
    def name(self) -> str:
        """Return the data-point name."""
        return "Week Profile"

    @property
    def full_name(self) -> str:
        """Return the display name ``<device> Week Profile``."""
        return f"{self._device.name} Week Profile"

    @property
    def translated_name(self) -> str:
        """Return the translated data-point name (loom has no translation table → raw name)."""
        return self.name

    @property
    def translated_full_name(self) -> str:
        """Return the translated display name (loom has no translation table → raw full name)."""
        return self.full_name

    @property
    def translation_key(self) -> str:
        """Return the HA translation key."""
        return "week_profile"

    @property
    def name_data(self) -> _NameData:
        """Return the name data the HA sensor entity reads."""
        return _NameData(parameter_name=self.name, name=self.name, full_name=self.full_name)

    # ---- base-protocol tail (neutral, mirrors aiohomematic's defaults for a schedule DP) ----

    @property
    def function(self) -> str | None:
        """Return the channel function (schedule DPs carry none)."""
        return None

    @property
    def is_in_multiple_channels(self) -> bool:
        """Return whether the parameter spans channels (never, for a schedule DP)."""
        return False

    @property
    def timer_on_time(self) -> float | None:
        """Return the on-time (schedule DPs have none)."""
        return None

    @property
    def timer_on_time_running(self) -> bool:
        """Return whether an on-time is running (never, for a schedule DP)."""
        return False

    def force_usage(self, *, forced_usage: DataPointUsage) -> None:
        """Force the data-point usage (no-op: schedule DPs are always data points)."""

    def reset_timer_on_time(self) -> None:
        """Reset the on-time (no-op: schedule DPs have no timer)."""

    def set_timer_on_time(self, *, on_time: float) -> None:
        """Set the on-time (no-op: schedule DPs have no timer)."""

    async def load_data_point_value(self, *, call_source: CallSource, direct_call: bool = False) -> None:
        """Load the data-point value (schedule data is populated during bootstrap / reload_schedule)."""

    def fire_schedule_updated(self) -> None:
        """Notify subscribers that the schedule changed (no-op: no per-DP event bus on the loom path)."""

    # ---- value ----

    @property
    def value(self) -> int | None:
        """Return the number of active schedule entries (``None`` until loaded)."""
        return self._value

    @property
    def is_valid(self) -> bool:
        """Return whether the entry count has been loaded."""
        return self._value is not None

    def target_channel_name(self, *, channel_key: str) -> str | None:
        """
        Return the display name of the actuator channel a lock key controls.

        Sourced from the daemon's ``available_target_channels`` (api 1.7.0);
        ``None`` on older daemons that do not ship the mapping.
        """
        # Field present since types 0.1.19; None when the daemon (api < 1.7.0)
        # does not populate it, so the switch falls back to the bare schedule name.
        targets = self._week_profile.available_target_channels
        if targets and (info := targets.get(channel_key)) is not None:
            return info.name
        return None

    def target_channel_unique_id(self, *, channel_key: str) -> str:
        """
        Return the schedule-channel-switch ``unique_id`` for a lock key (J5).

        Prefers the daemon-owned key from ``available_target_channels`` (required
        per entry since types 0.1.29 / daemon 0.11.0). Switches are spawned per
        ``schedule_enabled`` key, which can lack a target entry (older daemon /
        partial mapping); there the canonical key is synthesised as a fallback,
        so the entity identity stays stable either way (mirrors the refresh
        bridge's prefer-daemon-key/rebuild pattern).
        """
        targets = self._week_profile.available_target_channels or {}
        if (info := targets.get(channel_key)) is not None:
            return info.unique_id
        return canonical_unique_id(
            serial_suffix=self._store.serial_suffix,
            address=self._device.address,
            parameter=f"SCHEDULE_CHANNEL_LOCK_{channel_key}",
            prefix="schedule_channel_switch",
        )

    def update_from(self, *, schedule: Schedule) -> None:
        """Recompute the entry count from a fetched channel schedule."""
        self._schedule = schedule
        self._value = self._count_entries(schedule=schedule)
        self._modified = datetime.now(tz=UTC)

    @staticmethod
    def _count_entries(*, schedule: Schedule) -> int:
        """
        Count the active schedule entries.

        Climate schedules count the periods over all weekdays of the
        active profile (falling back to all profiles when the active one
        is not in the payload); simple schedules count their entries.
        """
        if str(getattr(schedule.kind, "value", schedule.kind)) == _SCHEDULE_KIND_CLIMATE:
            profiles = schedule.profiles or {}
            if (active := schedule.active_profile) and active in profiles:
                profiles = {active: profiles[active]}
            return sum(len(weekday.periods) for profile in profiles.values() for weekday in profile.weekdays.values())
        return len(schedule.simple_entries or [])

    async def _reload_schedule(self, *, force_load: bool = False) -> Schedule:
        """Fetch (or return the cached) channel schedule, refreshing the entry count."""
        if force_load or self._schedule is None:
            schedule = await self._schedules_ops.get_channel_schedule(
                address=self._device.address, channel=self._channel_no
            )
            self.update_from(schedule=schedule)
        assert self._schedule is not None  # noqa: S101 - update_from always sets it
        return self._schedule

    # ---- device (simple) schedule read/write (WeekProfileDataPointProtocol) ----

    async def get_schedule(self, *, force_load: bool = False) -> dict[str, Any]:
        """
        Fetch and return the simple schedule as ``{"entries": {slot: entry}}``.

        Mirrors aiohomematic's ``DefaultWeekProfile`` dump: the frontend's
        ``ScheduleData.entries`` is a slot-keyed map of ``SimpleScheduleEntry``.
        """
        schedule = await self._reload_schedule(force_load=force_load)
        entries = {
            str(entry.slot_no): entry.model_dump(mode="json", exclude={"slot_no"})
            for entry in (schedule.simple_entries or [])
        }
        return {"entries": entries}

    async def set_schedule(self, *, schedule_data: dict[str, Any]) -> None:
        """Write a simple schedule (``{"entries": {slot: entry}}``) back to the daemon."""
        raw_entries = schedule_data.get("entries", schedule_data)
        entries = [
            SimpleScheduleEntry.model_validate({**entry, "slot_no": int(slot)}) for slot, entry in raw_entries.items()
        ]
        base = await self._reload_schedule()
        new_schedule = base.model_copy(update={"simple_entries": entries})
        await self._schedules_ops.put_channel_schedule(
            address=self._device.address, channel=self._channel_no, schedule=new_schedule
        )
        self.update_from(schedule=new_schedule)

    async def set_schedule_enabled(self, *, enabled: bool, channel_key: str | None = None) -> None:
        """Enable or disable the weekly program for one channel key, or all known keys when ``None``."""
        keys = [channel_key] if channel_key is not None else list(self._schedule_enabled or {})
        for key in keys:
            await self._schedules_ops.set_channel_lock(
                address=self._device.address, channel=self._channel_no, key=key, enabled=enabled
            )
            self.set_channel_enabled(channel_key=key, enabled=enabled)

    async def reload_schedule(self) -> None:
        """Reload the schedule from the daemon and refresh the entry count."""
        await self._reload_schedule(force_load=True)

    # ---- schedule metadata (read by the HA week-profile sensor) ----

    @property
    def schedule(self) -> dict[str, Any] | None:
        """Return the cached schedule as a JSON-serialisable dict, or ``None``."""
        if self._schedule is None:
            return None
        return self._schedule.model_dump(mode="json", exclude_none=True)

    @property
    def schedule_type(self) -> Any:
        """Return the schedule type (``climate`` / ``default``, ``.value`` readable)."""
        return self._week_profile.schedule_type

    @property
    def schedule_domain(self) -> str | None:
        """Return the schedule domain of a non-climate schedule, or ``None``."""
        if self._schedule is None:
            return None
        return self._schedule.domain

    @property
    def schedule_channel_address(self) -> str:
        """Return the schedule channel address (``<device>:<channel>``)."""
        return f"{self._device.address}:{self._channel_no}"

    @property
    def schedule_enabled(self) -> dict[str, bool] | None:
        """Return the per-channel schedule enabled map, or ``None``."""
        return dict(self._schedule_enabled) if self._schedule_enabled is not None else None

    @property
    def supported_schedule_fields(self) -> frozenset[ScheduleField]:
        """
        Return the schedule fields the device advertises.

        The daemon does not expose the device's MASTER-paramset schedule-field
        descriptor, so this is empty; the simple-schedule editor then renders
        its per-domain default field set (mirrors aiohomematic when the
        descriptor is absent).
        """
        return frozenset()

    def set_channel_enabled(self, *, channel_key: str, enabled: bool) -> None:
        """Record an optimistic per-channel enabled flag (after a lock write)."""
        if self._schedule_enabled is None:
            self._schedule_enabled = {}
        self._schedule_enabled[channel_key] = enabled
        self._modified = datetime.now(tz=UTC)

    @property
    def max_entries(self) -> int:
        """Return the maximum number of schedule entries."""
        if str(getattr(self._week_profile.schedule_type, "value", "")) == _SCHEDULE_KIND_CLIMATE:
            return _MAX_CLIMATE_ENTRIES
        return _MAX_SIMPLE_ENTRIES

    @property
    def min_temp(self) -> float | None:
        """Return the minimum temperature (climate only)."""
        if str(getattr(self._week_profile.schedule_type, "value", "")) == _SCHEDULE_KIND_CLIMATE:
            return self._week_profile.min_temp
        return None

    @property
    def max_temp(self) -> float | None:
        """Return the maximum temperature (climate only)."""
        if str(getattr(self._week_profile.schedule_type, "value", "")) == _SCHEDULE_KIND_CLIMATE:
            return self._week_profile.max_temp
        return None

    @property
    def available_target_channels(self) -> dict[str, TargetChannelInfo]:
        """Return the actuator channels the schedule targets, as aiohomematic ``TargetChannelInfo`` dataclasses."""
        targets = self._week_profile.available_target_channels or {}
        return {
            key: TargetChannelInfo(
                channel_no=info.channel_no,
                channel_address=info.channel_address,
                name=info.name,
                channel_type=info.channel_type,
            )
            for key, info in targets.items()
        }


class ClimateWeekProfileDp(WeekProfileDp):
    """
    Climate week-profile data point: adds the profile-/weekday-level surface.

    Satisfies the ``@runtime_checkable`` ``ClimateWeekProfileDataPointProtocol``
    so the schedule facade's ``isinstance`` gate and the HA climate/sensor
    entities recognise it as climate-capable. Simple (non-climate) schedules
    use the plain :class:`WeekProfileDp`, which does *not* satisfy the climate
    protocol — mirroring aiohomematic's ``ClimateWeekProfile`` /
    ``DefaultWeekProfile`` split.
    """

    def __init__(
        self,
        *,
        store: LoomStore,
        device: Device,
        channel_no: int,
        week_profile: WeekProfileResponse,
        schedules_ops: SchedulesOperations,
    ) -> None:
        """Bind the climate data point and seed the editor's current-profile pointer from the descriptor."""
        super().__init__(
            store=store,
            device=device,
            channel_no=channel_no,
            week_profile=week_profile,
            schedules_ops=schedules_ops,
        )
        self._current_schedule_profile: ScheduleProfile = (
            _to_schedule_profile(value=week_profile.current_profile, default=None) or ScheduleProfile.P1
        )

    # ---- profile metadata ----

    @property
    def available_profiles(self) -> tuple[ScheduleProfile, ...]:
        """Return the available climate profiles (P1…P6) as ``ScheduleProfile`` enums."""
        return tuple(
            profile
            for name in (self._week_profile.available_profiles or ())
            if (profile := _to_schedule_profile(value=name, default=None)) is not None
        )

    @property
    def current_schedule_profile(self) -> ScheduleProfile:
        """Return the profile the editor is currently viewing (local pointer, like the reference)."""
        return self._current_schedule_profile

    @property
    def schedule_profile_nos(self) -> int:
        """Return the number of supported profiles."""
        return self._week_profile.profile_count or len(self.available_profiles)

    @property
    def device_active_profile_index(self) -> int | None:
        """Return the 1-based active-profile index the device reports, or ``None``."""
        if self._schedule is not None:
            return self._schedule.active_profile_index
        return None

    @property
    def current_profile_schedule(self) -> dict[str, Any] | None:
        """Return the cached schedule of the current profile as ``{weekday: {...}}``, or ``None``."""
        if self._schedule is None or not self._schedule.profiles:
            return None
        profile = self._schedule.profiles.get(self._current_schedule_profile.value)
        if profile is None:
            return None
        return _profile_to_frontend(profile=profile)

    def set_current_schedule_profile(self, *, profile: ScheduleProfile) -> None:
        """Set the profile the editor views (local pointer only; the device's active profile is a separate DP)."""
        self._current_schedule_profile = profile
        self._modified = datetime.now(tz=UTC)

    # ---- profile / weekday read ----

    async def get_schedule(self, *, force_load: bool = False) -> dict[str, Any]:
        """Return the whole climate schedule as ``{profile: {weekday: {...}}}``."""
        schedule = await self._reload_schedule(force_load=force_load)
        return {name: _profile_to_frontend(profile=profile) for name, profile in (schedule.profiles or {}).items()}

    async def get_schedule_profile(self, *, profile: ScheduleProfile, force_load: bool = False) -> dict[str, Any]:
        """Return a single profile as ``{weekday: {base_temperature, periods: [...]}}``."""
        schedule = await self._reload_schedule(force_load=force_load)
        profile_obj = (schedule.profiles or {}).get(profile.value)
        if profile_obj is None:
            return {}
        return _profile_to_frontend(profile=profile_obj)

    async def get_schedule_weekday(
        self, *, profile: ScheduleProfile, weekday: WeekdayStr, force_load: bool = False
    ) -> dict[str, Any]:
        """Return a single weekday as ``{base_temperature, periods: [...]}``."""
        schedule = await self._reload_schedule(force_load=force_load)
        profile_obj = (schedule.profiles or {}).get(profile.value)
        if profile_obj is None or (weekday_obj := profile_obj.weekdays.get(weekday.value)) is None:
            return {}
        return _weekday_to_frontend(weekday=weekday_obj)

    # ---- profile / weekday write ----

    async def set_schedule_weekday(
        self, *, profile: ScheduleProfile, weekday: WeekdayStr, weekday_data: dict[str, Any]
    ) -> None:
        """Write a single weekday of a profile back to the daemon."""
        schedule = await self._reload_schedule()
        profiles = dict(schedule.profiles or {})
        profile_obj = profiles.get(profile.value)
        weekdays = dict(profile_obj.weekdays) if profile_obj is not None else {}
        weekdays[weekday.value] = _weekday_from_frontend(weekday_data=weekday_data)
        profiles[profile.value] = (
            profile_obj.model_copy(update={"weekdays": weekdays})
            if profile_obj is not None
            else ClimateProfile(weekdays=weekdays)
        )
        await self._put_profiles(schedule=schedule, profiles=profiles)

    async def set_schedule_profile(self, *, profile: ScheduleProfile, profile_data: dict[str, Any]) -> None:
        """Write a whole profile (``{weekday: {...}}``) back to the daemon."""
        schedule = await self._reload_schedule()
        profiles = dict(schedule.profiles or {})
        profiles[profile.value] = ClimateProfile(
            weekdays={
                weekday_key: _weekday_from_frontend(weekday_data=weekday_data)
                for weekday_key, weekday_data in profile_data.items()
            }
        )
        await self._put_profiles(schedule=schedule, profiles=profiles)

    async def set_schedule(self, *, schedule_data: dict[str, Any]) -> None:
        """Write the whole climate schedule (``{profile: {weekday: {...}}}``) back to the daemon."""
        schedule = await self._reload_schedule()
        profiles = {
            profile_key: ClimateProfile(
                weekdays={
                    weekday_key: _weekday_from_frontend(weekday_data=weekday_data)
                    for weekday_key, weekday_data in profile_data.items()
                }
            )
            for profile_key, profile_data in schedule_data.items()
        }
        await self._put_profiles(schedule=schedule, profiles=profiles)

    async def _put_profiles(self, *, schedule: Schedule, profiles: dict[str, ClimateProfile]) -> None:
        """Persist an updated profile map to the daemon and refresh the cache."""
        new_schedule = schedule.model_copy(update={"profiles": profiles})
        await self._schedules_ops.put_channel_schedule(
            address=self._device.address, channel=self._channel_no, schedule=new_schedule
        )
        self.update_from(schedule=new_schedule)

    # ---- cross-device copy ----

    async def copy_schedule(self, *, target_data_point: ClimateWeekProfileDp) -> None:
        """Copy the whole schedule to another climate device."""
        await self._schedules_ops.copy_schedule(
            src_address=self._device.address, dst_address=target_data_point.device.address
        )

    async def copy_schedule_profile(
        self,
        *,
        source_profile: ScheduleProfile,
        target_profile: ScheduleProfile,
        target_data_point: ClimateWeekProfileDp | None = None,
    ) -> None:
        """Copy one profile to another profile (on this or a target device)."""
        target = target_data_point or self
        await self._schedules_ops.copy_climate_profile(
            src_channel_address=self.schedule_channel_address,
            src_profile=_profile_index(profile=source_profile),
            dst_channel_address=target.schedule_channel_address,
            dst_profile=_profile_index(profile=target_profile),
        )


def _to_schedule_profile(*, value: Any, default: ScheduleProfile | None) -> ScheduleProfile | None:
    """Map a wire profile string (``"P1"``…) to a ``ScheduleProfile`` enum, falling back to ``default``."""
    if value is None:
        return default
    try:
        return ScheduleProfile(str(value))
    except ValueError:
        return default


def _profile_index(*, profile: ScheduleProfile) -> int:
    """Return the 1-based profile index (``P1`` → ``1``) the daemon copy endpoint expects."""
    return int(profile.value[1:])


class ScheduleChannelSwitch(_ScheduleEntityBase):
    """Per-channel switch toggling week-program participation."""

    _category: ClassVar[DataPointCategory] = DataPointCategory.ScheduleSwitch

    def __init__(
        self,
        *,
        store: LoomStore,
        device: Device,
        channel_no: int,
        channel_key: str,
        week_profile_dp: WeekProfileDp,
        schedules_ops: SchedulesOperations,
    ) -> None:
        """Bind the switch to its channel key, parent week profile and operations."""
        super().__init__(store=store, device=device, channel_no=channel_no)
        self._channel_key = channel_key
        self._week_profile_dp = week_profile_dp
        self._schedules_ops = schedules_ops
        self._modified: datetime | None = None
        # aiohomematic registers schedule switches disabled by default.
        self._enabled_default = False

    # ---- identity ----

    @property
    def unique_id(self) -> str:
        """Return the daemon-owned ``loom_schedule_channel_switch_<addr>_…`` key (J5)."""
        return self._week_profile_dp.target_channel_unique_id(channel_key=self._channel_key)

    @property
    def channel_key(self) -> str:
        """Return the target channel key (e.g. ``"1_1"``)."""
        return self._channel_key

    @property
    def name(self) -> str:
        """Return the data-point name."""
        return f"SCHEDULE_CHANNEL_LOCK_{self._channel_key}"

    @property
    def full_name(self) -> str:
        """Return the display name ``<device> Schedule <key>``."""
        return f"{self._device.name} Schedule {self._channel_key}"

    @property
    def name_data(self) -> _NameData:
        """
        Return the name data the HA switch entity reads.

        ``channel_name`` carries the actuator channel the lock key controls
        (from the daemon's ``available_target_channels``, api 1.7.0), so the HA
        switch composes ``<Schedule> <channel name>`` like the reference twin.
        It stays ``None`` on older daemons, where the entity falls back to the
        bare translated schedule name.
        """
        return _NameData(
            parameter_name=self.name,
            name=self.name,
            full_name=self.full_name,
            channel_name=self._week_profile_dp.target_channel_name(channel_key=self._channel_key),
        )

    # ---- value / actions ----

    @property
    def value(self) -> bool | None:
        """Return whether the schedule is enabled for this channel."""
        if (schedule_enabled := self._week_profile_dp.schedule_enabled) is None:
            return None
        return schedule_enabled.get(self._channel_key)

    @property
    def is_valid(self) -> bool:
        """Return whether the enabled state is known."""
        return self.value is not None

    async def turn_on(self) -> None:
        """Enable the schedule for this channel."""
        await self._set_enabled(enabled=True)

    async def turn_off(self) -> None:
        """Disable the schedule for this channel."""
        await self._set_enabled(enabled=False)

    async def _set_enabled(self, *, enabled: bool) -> None:
        """Write the channel lock and record the optimistic state."""
        await self._schedules_ops.set_channel_lock(
            address=self._device.address,
            channel=self._channel_no,
            key=self._channel_key,
            enabled=enabled,
        )
        self._week_profile_dp.set_channel_enabled(channel_key=self._channel_key, enabled=enabled)
        self._modified = datetime.now(tz=UTC)


__all__ = ["ClimateWeekProfileDp", "ScheduleChannelSwitch", "WeekProfileDp"]
