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

from openccu_loom_client.operations._base import _OperationsBase
from openccu_loom_client.wire.rest import (
    CopyProfileRequest,
    CopyScheduleRequest,
    Schedule,
    SetActiveProfileRequest,
    WeekProfileResponse,
)


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

    async def put_channel_schedule(self, *, address: str, channel: int, schedule: Schedule) -> None:
        """
        Replace a channel's schedule.

        Wire: ``PUT /devices/{addr}/channels/{n}/schedule``. The daemon
        validates the body against the channel's schedule type.
        Idempotent — replacing with the same body is a no-op on the CCU.
        """
        await self._transport.request(
            method="PUT",
            path=f"/devices/{address}/channels/{channel}/schedule",
            json_body=self._to_json_body(schedule),
            allow_retry=True,
        )

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

    async def put_device_schedule(self, *, address: str, schedule: Schedule) -> None:
        """
        Replace a device's schedule (auto-resolves the channel).

        Wire: ``PUT /devices/{addr}/schedule``.
        """
        await self._transport.request(
            method="PUT",
            path=f"/devices/{address}/schedule",
            json_body=self._to_json_body(schedule),
            allow_retry=True,
        )

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
