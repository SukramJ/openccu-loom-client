# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
The daemon may store a schedule time in a corrected form rather than refuse it.

These cover the two places that has to survive: the operation must hand the
report to its caller, and a caller that caches what it wrote must reconcile
that cache with what was actually stored.
"""

from __future__ import annotations

import pytest

from openccu_loom_client.operations.schedules import SchedulesOperations, apply_corrections
from openccu_loom_client.transport import HttpTransport
from openccu_loom_client.wire import DAEMON_API_VERSION
from openccu_loom_client.wire.rest import (
    ClimatePeriod,
    ClimateProfile,
    ClimateWeekday,
    Schedule,
    ScheduleChannelRef,
    ScheduleTimeCorrection,
    ScheduleWriteResult,
)


def _schedule(*, end_time: str = "24:30") -> Schedule:
    return Schedule(
        channel=ScheduleChannelRef(address="VCU1:1", number=1, device_address="VCU1"),
        kind="climate",
        profiles={
            "P1": ClimateProfile(
                weekdays={
                    "MONDAY": ClimateWeekday(
                        base_temperature=17.0,
                        periods=[ClimatePeriod(start_time="06:00", end_time=end_time, temperature=21.0)],
                    )
                }
            )
        },
    )


def _correction(*, period: int = 0, profile: str = "P1", weekday: str = "MONDAY") -> ScheduleTimeCorrection:
    return ScheduleTimeCorrection(
        profile=profile,
        weekday=weekday,
        period=period,
        field="end_time",
        requested="24:30",
        applied="23:55",
    )


class TestApplyCorrections:
    def test_applies_the_reported_rewrite(self) -> None:
        result = ScheduleWriteResult(corrections=[_correction()])
        corrected = apply_corrections(schedule=_schedule(), result=result)
        assert corrected.profiles is not None
        period = corrected.profiles["P1"].weekdays["MONDAY"].periods[0]
        assert period.end_time == "23:55"
        # The untouched field is untouched.
        assert period.start_time == "06:00"

    def test_leaves_the_original_alone(self) -> None:
        """The caller's own copy must not mutate under it -- it may still be in use."""
        original = _schedule()
        apply_corrections(schedule=original, result=ScheduleWriteResult(corrections=[_correction()]))
        assert original.profiles is not None
        assert original.profiles["P1"].weekdays["MONDAY"].periods[0].end_time == "24:30"

    def test_no_corrections_returns_the_schedule_unchanged(self) -> None:
        """The negative control: without a correction nothing may be rewritten."""
        original = _schedule(end_time="22:00")
        assert apply_corrections(schedule=original, result=ScheduleWriteResult()) is original
        assert apply_corrections(schedule=original, result=ScheduleWriteResult(corrections=[])) is original

    def test_unresolvable_coordinates_are_skipped_not_guessed(self) -> None:
        """
        Skip a correction whose coordinates this copy cannot resolve.

        A correction naming a profile, weekday or period this copy does not have
        means the two disagree about the payload's shape. Inventing a target
        would hide that; skipping leaves it visible on the next read.
        """
        original = _schedule()
        for bad in (
            _correction(profile="P6"),
            _correction(weekday="SUNDAY"),
            _correction(period=7),
            _correction(period=-1),
        ):
            out = apply_corrections(schedule=original, result=ScheduleWriteResult(corrections=[bad]))
            assert out is original, f"{bad!r} should not have resolved"


_INFO = {
    "version": "1.2.3",
    "api_version": DAEMON_API_VERSION,
    "commit": "deadbeef",
    "build_date": "2026-05-24T10:00:00Z",
    "addon_build": False,
    "started_at": "2026-05-24T10:01:00Z",
    "uptime": "PT60S",
    "capabilities": ["rest.v1", "ws.broadcasts.v1"],
    "schema_digest": "sha256:test",
    "config_ui_url": "",
}


@pytest.fixture
async def http(mock_daemon):
    t = HttpTransport(config=mock_daemon.config, backoff_sequence=(0.0,))
    mock_daemon.get("/api/v1/info", payload=_INFO)
    await t.connect()
    yield t, mock_daemon
    await t.close()


class TestPutChannelScheduleReturnsCorrections:
    async def test_reports_what_the_daemon_stored(self, http) -> None:
        """The report has to reach the caller; swallowing it is the whole defect."""
        t, mock = http
        mock.put(
            "/api/v1/devices/VCU1/channels/1/schedule",
            status=202,
            payload={
                "corrections": [
                    {
                        "profile": "P1",
                        "weekday": "MONDAY",
                        "period": 0,
                        "field": "end_time",
                        "requested": "24:30",
                        "applied": "23:55",
                    }
                ]
            },
        )
        result = await SchedulesOperations(transport=t).put_channel_schedule(
            address="VCU1", channel=1, schedule=_schedule()
        )
        assert result.corrections is not None
        assert len(result.corrections) == 1
        assert result.corrections[0].applied == "23:55"
        assert result.corrections[0].requested == "24:30"

    async def test_an_empty_body_is_not_an_error(self, http) -> None:
        """
        Decode an empty 202 body instead of raising.

        A daemon before api 10.1.0 answers 202 with no body at all. That must
        decode, not raise -- and it reports nothing, which is a weaker
        statement than "nothing was corrected".
        """
        t, mock = http
        mock.put("/api/v1/devices/VCU1/channels/1/schedule", status=202)
        result = await SchedulesOperations(transport=t).put_channel_schedule(
            address="VCU1", channel=1, schedule=_schedule()
        )
        assert not result.corrections

    async def test_device_level_write_reports_too(self, http) -> None:
        t, mock = http
        mock.put(
            "/api/v1/devices/VCU1/schedule",
            status=202,
            payload={
                "corrections": [
                    {
                        "profile": "P1",
                        "weekday": "MONDAY",
                        "period": 0,
                        "field": "end_time",
                        "requested": "24:01",
                        "applied": "23:55",
                    }
                ]
            },
        )
        result = await SchedulesOperations(transport=t).put_device_schedule(address="VCU1", schedule=_schedule())
        assert result.corrections is not None
        assert result.corrections[0].applied == "23:55"
