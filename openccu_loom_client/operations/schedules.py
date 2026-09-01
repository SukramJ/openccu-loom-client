# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Climate / week-profile schedule REST operations.

Maps to the ``schedules``-tagged endpoints in the daemon's OpenAPI
surface. These cover the thermostat week-program feature that
``homematicip_local`` exposes as HA climate schedules.

Wire types come from ``openccu_loom_client.wire.rest`` (:class:`Schedule`,
:class:`WeekProfileResponse`, :class:`SetActiveProfileRequest`), so
callers get end-to-end typing for the thermostat week-program feature
``homematicip_local`` exposes as HA climate schedules.
"""

from __future__ import annotations

from typing import Any

from openccu_loom_client.operations._base import _OperationsBase
from openccu_loom_client.wire.rest import (
    CopyProfileRequest,
    CopyScheduleRequest,
    Schedule,
    ScheduleWriteResult,
    SetActiveProfileRequest,
    WeekProfileResponse,
)


def _write_result(*, payload: Any) -> ScheduleWriteResult:
    """
    Read a schedule write's 202 body, tolerating a daemon that sends none.

    The write used to answer 202 with an empty body; since daemon api 10.1.0
    it carries a :class:`ScheduleWriteResult`. An empty body therefore means
    an older daemon, and decodes to a result with no corrections.

    That collapses two states this layer cannot tell apart, which is worth
    knowing before acting on the answer: ``corrections`` is also absent when a
    10.1.0 daemon stored the schedule exactly as submitted, because the field
    is omitted when empty. So a falsy ``corrections`` means "nothing was
    reported", not "nothing was corrected" -- a caller that needs the stronger
    reading has to establish the daemon's api version first.
    """
    if not payload:
        return ScheduleWriteResult()
    return ScheduleWriteResult.model_validate(payload)


def apply_corrections(*, schedule: Schedule, result: ScheduleWriteResult) -> Schedule:
    """
    Return ``schedule`` with the daemon's corrections applied.

    A caller that keeps a local copy of what it just wrote has to reconcile it
    with what was actually stored, or the copy quietly describes a schedule the
    device does not hold -- the same defect the daemon's correction report
    exists to prevent, one layer out. This applies the reported rewrites so the
    local copy matches the device.

    Corrections whose coordinates do not resolve (an unknown profile, weekday
    or period index) are skipped rather than guessed at: they would mean the
    daemon and this copy disagree about the payload's shape, and inventing a
    target would hide that instead of leaving it visible in a later read.
    """
    corrections = result.corrections
    if not corrections or schedule.profiles is None:
        return schedule
    profiles = {key: profile.model_copy(deep=True) for key, profile in schedule.profiles.items()}
    changed = False
    for correction in corrections:
        profile = profiles.get(correction.profile)
        if profile is None:
            continue
        weekday = profile.weekdays.get(correction.weekday)
        if weekday is None or not 0 <= correction.period < len(weekday.periods):
            continue
        period = weekday.periods[correction.period]
        setattr(period, correction.field.value, correction.applied)
        changed = True
    if not changed:
        return schedule
    return schedule.model_copy(update={"profiles": profiles})


class SchedulesOperations(_OperationsBase):
    """Wraps the daemon's week-profile / schedule REST surface."""

    # ---- channel-scoped ----

    async def get_channel_week_profile(self, *, address: str, channel: int) -> WeekProfileResponse:
        """
        Week-profile descriptor (type, temp range, available profiles).

        Wire: ``GET /devices/{addr}/channels/{n}/week_profile``. Returns
        404 when the channel has no attached week profile.
        """
        payload = await self._transport.request(
            method="GET",
            path=f"/devices/{address}/channels/{channel}/week_profile",
        )
        return WeekProfileResponse.model_validate(payload)

    async def get_channel_schedule(self, *, address: str, channel: int) -> Schedule:
        """
        Climate or simple schedule of one channel.

        Wire: ``GET /devices/{addr}/channels/{n}/schedule``. ``kind``
        discriminates ``climate`` (``profiles``) from ``simple``
        (``simple_entries``).
        """
        payload = await self._transport.request(
            method="GET",
            path=f"/devices/{address}/channels/{channel}/schedule",
        )
        return Schedule.model_validate(payload)

    async def put_channel_schedule(self, *, address: str, channel: int, schedule: Schedule) -> ScheduleWriteResult:
        """
        Replace a channel's schedule.

        Wire: ``PUT /devices/{addr}/channels/{n}/schedule``. The daemon
        validates the body against the channel's schedule type.
        Idempotent — replacing with the same body is a no-op on the CCU.

        Returns what the daemon actually stored where it differs from what
        was submitted. A climate end time with an hour of 24 is stored as
        23:55 rather than refused -- which is what the CCU's own editor does
        with the same input -- and each such rewrite comes back as a
        :class:`ScheduleTimeCorrection` naming the profile, weekday, period
        and field. Discarding this is how a consumer ends up showing a
        schedule the device does not hold; see :func:`_write_result` for what
        an empty answer does and does not mean.
        """
        payload = await self._transport.request(
            method="PUT",
            path=f"/devices/{address}/channels/{channel}/schedule",
            json_body=self._to_json_body(schedule),
            allow_retry=True,
        )
        return _write_result(payload=payload)

    async def set_channel_active_profile(self, *, address: str, channel: int, profile: str) -> None:
        """
        Pick the active climate profile (P1..P6) for a channel.

        Wire: ``POST /devices/{addr}/channels/{n}/schedule/active-profile``.
        """
        body = SetActiveProfileRequest(profile=profile)
        await self._transport.request(
            method="POST",
            path=f"/devices/{address}/channels/{channel}/schedule/active-profile",
            json_body=body.model_dump(mode="json"),
            allow_retry=False,
        )

    async def set_channel_lock(self, *, address: str, channel: int, key: str, enabled: bool) -> None:
        """
        Enable/disable one target channel's week-program participation.

        Wire: ``PUT /devices/{addr}/channels/{n}/week_profile/channel-locks/{key}``
        with ``{"enabled": bool}``. ``key`` is the schedule channel key
        (e.g. ``"1_1"``) from ``WeekProfileResponse.schedule_enabled``.
        Idempotent — re-applying the same flag is a no-op on the CCU.
        """
        await self._transport.request(
            method="PUT",
            path=f"/devices/{address}/channels/{channel}/week_profile/channel-locks/{key}",
            json_body={"enabled": enabled},
            allow_retry=True,
        )

    # ---- device-scoped (auto-resolves the schedule channel) ----

    async def get_device_schedule(self, *, address: str) -> Schedule:
        """
        Schedule of a device, auto-resolving the schedule channel.

        Wire: ``GET /devices/{addr}/schedule``.
        """
        payload = await self._transport.request(
            method="GET",
            path=f"/devices/{address}/schedule",
        )
        return Schedule.model_validate(payload)

    async def put_device_schedule(self, *, address: str, schedule: Schedule) -> ScheduleWriteResult:
        """
        Replace a device's schedule (auto-resolves the channel).

        Wire: ``PUT /devices/{addr}/schedule``. Carries the same corrections
        as :meth:`put_channel_schedule`.
        """
        payload = await self._transport.request(
            method="PUT",
            path=f"/devices/{address}/schedule",
            json_body=self._to_json_body(schedule),
            allow_retry=True,
        )
        return _write_result(payload=payload)

    async def set_device_active_profile(self, *, address: str, profile: str) -> None:
        """
        Pick the active climate profile (auto-resolves the channel).

        Wire: ``POST /devices/{addr}/schedule/active-profile``.
        """
        body = SetActiveProfileRequest(profile=profile)
        await self._transport.request(
            method="POST",
            path=f"/devices/{address}/schedule/active-profile",
            json_body=body.model_dump(mode="json"),
            allow_retry=False,
        )

    # ---- copy (clone a whole schedule / a single climate profile) ----

    async def copy_schedule(self, *, src_address: str, dst_address: str) -> None:
        """
        Copy a device's whole week schedule onto another device.

        Wire: ``POST /devices/{src}/schedules/copy`` with a
        :class:`CopyScheduleRequest` (``{target_device_address}``).
        Not retried — replaying the copy after a partial CCU write can
        clobber an edit the operator made between attempts.
        """
        body = CopyScheduleRequest(target_device_address=dst_address)
        await self._transport.request(
            method="POST",
            path=f"/devices/{src_address}/schedules/copy",
            json_body=body.model_dump(mode="json"),
            allow_retry=False,
        )

    async def copy_climate_profile(
        self,
        *,
        src_channel_address: str,
        src_profile: int,
        dst_channel_address: str,
        dst_profile: int,
    ) -> None:
        """
        Copy a single climate profile (P1..P6) to another channel/profile.

        Wire: ``POST /devices/{addr}/channels/{n}/week_profile/copy`` with
        a :class:`CopyProfileRequest` (``{source_profile,
        target_channel_address, target_profile}``). The source channel is
        derived from ``src_channel_address`` (``"<device>:<channel>"``).
        Profiles are 1-based (1..6). Not retried.
        """
        device_address, _, channel = src_channel_address.rpartition(":")
        body = CopyProfileRequest(
            source_profile=src_profile,
            target_channel_address=dst_channel_address,
            target_profile=dst_profile,
        )
        await self._transport.request(
            method="POST",
            path=f"/devices/{device_address}/channels/{channel}/week_profile/copy",
            json_body=body.model_dump(mode="json"),
            allow_retry=False,
        )
