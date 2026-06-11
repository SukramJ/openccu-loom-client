# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Coverage for the operation modules added to close the wire-contract gaps.

These exercise the schedule, link, calculated-data-point, firmware,
message-ack, room/function and sysvar/program-lifecycle endpoints —
plus the ``hub.install_mode_changed`` broadcast binding — against an
in-process mock daemon so the request shapes are pinned to the daemon
contract.
"""

from __future__ import annotations

from openccu_loom_types.rest import Schedule, ScheduleChannelRef
from openccu_loom_types.ws import WsEnvelope
import pytest

from openccu_loom_client.events import InstallModeChangedEvent
from openccu_loom_client.events.types import event_from_envelope
from openccu_loom_client.operations import (
    DevicesOperations,
    HubOperations,
    LinksOperations,
    SchedulesOperations,
)
from openccu_loom_client.transport import HttpTransport
from tests.helpers import MockDaemon

_INFO = {
    "version": "1.2.3",
    "api_version": "1.0.0",
    "commit": "deadbeef",
    "build_date": "2026-05-24T10:00:00Z",
    "started_at": "2026-05-24T10:01:00Z",
    "uptime": "PT60S",
    "capabilities": ["rest.v1", "ws.broadcasts.v1"],
    "schema_digest": "sha256:test",
}


@pytest.fixture
async def http(mock_daemon: MockDaemon):
    t = HttpTransport(mock_daemon.config, backoff_sequence=(0.0,))
    mock_daemon.get("/api/v1/info", payload=_INFO)
    await t.connect()
    yield t, mock_daemon
    await t.close()


def _find_call(mock: MockDaemon, method: str):
    """Return the first recorded request for ``method``."""
    return next(r for r in mock.requests if r.method == method)


class TestSchedulesOperations:
    async def test_get_channel_week_profile(self, http) -> None:
        t, mock = http
        mock.get(
            "/api/v1/devices/VCU1/channels/1/week_profile",
            payload={
                "address": "VCU1:1",
                "schedule_type": "climate",
                "min_temp": 5.0,
                "max_temp": 30.5,
                "profile_count": 3,
                "has_climate_schedule": True,
            },
        )
        result = await SchedulesOperations(transport=t).get_channel_week_profile(
            address="VCU1", channel=1
        )
        assert result.schedule_type == "climate"
        assert result.profile_count == 3

    async def test_put_channel_schedule_sends_body(self, http) -> None:
        t, mock = http
        mock.put("/api/v1/devices/VCU1/channels/1/schedule", status=202)
        schedule = Schedule(
            channel=ScheduleChannelRef(address="VCU1:1", number=1, device_address="VCU1"),
            kind="climate",
        )
        await SchedulesOperations(transport=t).put_channel_schedule(
            address="VCU1", channel=1, schedule=schedule
        )
        body = _find_call(mock, "PUT").json()
        assert body["kind"] == "climate"
        assert body["channel"]["device_address"] == "VCU1"

    async def test_set_device_active_profile(self, http) -> None:
        t, mock = http
        mock.post("/api/v1/devices/VCU1/schedule/active-profile", status=202)
        await SchedulesOperations(transport=t).set_device_active_profile(
            address="VCU1", profile="P2"
        )
        assert _find_call(mock, "POST").json() == {"profile": "P2"}


class TestLinksOperations:
    async def test_add_link_sends_sender_receiver(self, http) -> None:
        t, mock = http
        mock.post("/api/v1/devices/VCU1/links", status=202)
        await LinksOperations(transport=t).add_link(
            address="VCU1",
            sender_address="VCU1:1",
            receiver_address="VCU2:1",
            name="kitchen",
        )
        assert _find_call(mock, "POST").json() == {
            "sender_address": "VCU1:1",
            "receiver_address": "VCU2:1",
            "name": "kitchen",
        }

    async def test_remove_link_passes_query(self, http) -> None:
        t, mock = http
        mock.delete(
            "/api/v1/devices/VCU1/links",
            status=202,
        )
        await LinksOperations(transport=t).remove_link(
            address="VCU1", sender="VCU1:1", receiver="VCU2:1"
        )

    async def test_enable_central_links(self, http) -> None:
        t, mock = http
        mock.post("/api/v1/devices/VCU1/central-links", status=202)
        await LinksOperations(transport=t).enable_central_links(address="VCU1")

    async def test_get_link_paramset(self, http) -> None:
        t, mock = http
        mock.get(
            "/api/v1/devices/VCU1/link-ps/VCU2:1",
            payload={"SHORT_ACTION_TYPE": 1},
        )
        result = await LinksOperations(transport=t).get_link_paramset(address="VCU1", peer="VCU2:1")
        assert result == {"SHORT_ACTION_TYPE": 1}


class TestDevicesOperationsGaps:
    async def test_update_firmware_does_not_retry(self, http) -> None:
        t, mock = http
        mock.post("/api/v1/devices/VCU1/firmware/update", status=202)
        await DevicesOperations(transport=t).update_firmware(address="VCU1")

    async def test_list_calculated_data_points(self, http) -> None:
        t, mock = http
        mock.get(
            "/api/v1/devices/VCU1/channels/1/calc-dps",
            payload=[{"name": "DEW_POINT", "value": 12.3, "observed": True}],
        )
        result = await DevicesOperations(transport=t).list_calculated_data_points(
            address="VCU1", channel=1
        )
        assert result[0].name == "DEW_POINT"
        assert result[0].value == 12.3


class TestHubOperationsGaps:
    async def test_ack_alarm_message(self, http) -> None:
        t, mock = http
        mock.post("/api/v1/alarm-messages/42/ack", status=202)
        await HubOperations(transport=t).ack_alarm_message(message_id="42")

    async def test_ack_service_message(self, http) -> None:
        t, mock = http
        mock.post("/api/v1/service-messages/7/ack", status=202)
        await HubOperations(transport=t).ack_service_message(message_id="7")

    async def test_list_rooms(self, http) -> None:
        t, mock = http
        mock.get(
            "/api/v1/rooms",
            payload=[{"name": "Kitchen", "device_count": 4}],
        )
        rooms = await HubOperations(transport=t).list_rooms()
        assert rooms[0].name == "Kitchen"
        assert rooms[0].device_count == 4

    async def test_list_functions(self, http) -> None:
        t, mock = http
        mock.get(
            "/api/v1/functions",
            payload=[{"name": "Light", "device_count": 9}],
        )
        functions = await HubOperations(transport=t).list_functions()
        assert functions[0].name == "Light"

    async def test_set_program_enabled(self, http) -> None:
        t, mock = http
        mock.patch("/api/v1/programs/p1", status=202)
        await HubOperations(transport=t).set_program_enabled(program_id="p1", active=False)
        assert _find_call(mock, "PATCH").json() == {"active": False}

    async def test_create_sysvar_only_sends_supplied_fields(self, http) -> None:
        t, mock = http
        mock.post("/api/v1/sysvars", status=202)
        await HubOperations(transport=t).create_sysvar(name="my_var", value=1.0, value_type="FLOAT")
        assert _find_call(mock, "POST").json() == {
            "name": "my_var",
            "value": 1.0,
            "value_type": "FLOAT",
        }

    async def test_update_sysvar_metadata(self, http) -> None:
        t, mock = http
        mock.patch("/api/v1/sysvars/my_var", status=202)
        await HubOperations(transport=t).update_sysvar_metadata(
            name="my_var", description="Living room", unit="°C"
        )
        assert _find_call(mock, "PATCH").json() == {
            "description": "Living room",
            "unit": "°C",
        }


class TestInstallModeChangedEvent:
    def test_broadcast_binds_to_typed_event(self) -> None:
        env = WsEnvelope.model_validate(
            {
                "topic": "hub.home.install_mode",
                "type": "hub.install_mode_changed",
                "ts": "2026-05-24T08:42:13Z",
                "seq": 5,
                "kind": "change",
                "payload": {"central": "home", "enabled": True, "remaining_s": 60},
            }
        )
        ev = event_from_envelope(env)
        assert isinstance(ev, InstallModeChangedEvent)
        assert ev.payload.enabled is True
        assert ev.payload.remaining_s == 60
        # Routing key derives from the central name.
        assert ev.event_key == "home"
