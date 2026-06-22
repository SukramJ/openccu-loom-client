# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
``aiohomematic.model.week_profile_data_point`` twins — schedule data points.

Two per-device schedule entities mirror aiohomematic's week-profile
layer:

- :class:`WeekProfileDp` (category ``week_profile`` → HA sensor): the
  number of active schedule entries plus the schedule metadata the HA
  sensor renders as attributes. Backed by the daemon's
  ``GET …/week_profile`` descriptor and ``GET …/schedule`` payload.
- :class:`ScheduleChannelSwitch` (category ``schedule_switch`` → HA
  switch, disabled by default): one switch per
  ``schedule_enabled`` channel key, toggling the channel's week-program
  participation via ``PUT …/week_profile/channel-locks/{key}``.

Unique ids match aiohomematic's registry exactly
(``loom_week_profile_<addr>_week_profile`` and
``loom_schedule_channel_switch_<addr>_schedule_channel_lock_<key>`` —
device addresses carry no serial slot).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar, Final

from openccu_loom_types.enums import DataPointCategory

from openccu_loom_client.canonical import canonical_unique_id
from openccu_loom_client.compat.aiohomematic.model._protocol_surface import _CommonProtocolSurface, _NameData
from openccu_loom_client.compat.aiohomematic.model.hub._surface import _HubEntitySurface

if TYPE_CHECKING:
    from openccu_loom_types.rest import Schedule, WeekProfileResponse

    from openccu_loom_client.model import Channel, Device
    from openccu_loom_client.operations.schedules import SchedulesOperations
    from openccu_loom_client.store import LoomStore

# Maximum schedule entries, mirroring aiohomematic's week-profile
# constants: 13 climate slots/day × 7 weekdays × 6 profiles, and 24
# entries for simple (non-climate) schedules.
_MAX_CLIMATE_ENTRIES: Final = 13 * 7 * 6
_MAX_SIMPLE_ENTRIES: Final = 24

_SCHEDULE_KIND_CLIMATE: Final = "climate"


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
    """Week-profile data point: active-entry count + schedule metadata."""

    _category: ClassVar[DataPointCategory] = DataPointCategory.WeekProfile

    def __init__(
        self,
        *,
        store: LoomStore,
        device: Device,
        channel_no: int,
        week_profile: WeekProfileResponse,
    ) -> None:
        """Bind the data point to the daemon's week-profile descriptor."""
        super().__init__(store=store, device=device, channel_no=channel_no)
        self._week_profile = week_profile
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
    def translation_key(self) -> str:
        """Return the HA translation key."""
        return "week_profile"

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
    def available_profiles(self) -> tuple[str, ...]:
        """Return the available climate profiles (P1…P6)."""
        return tuple(self._week_profile.available_profiles or ())

    @property
    def current_profile(self) -> str | None:
        """Return the active climate profile, or ``None``."""
        return self._week_profile.current_profile

    @property
    def available_target_channels(self) -> dict[str, Any]:
        """Return the target-channel map (not modelled on the loom backend)."""
        return {}


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


__all__ = ["ScheduleChannelSwitch", "WeekProfileDp"]
