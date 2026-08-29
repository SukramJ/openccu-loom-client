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

import pytest

from openccu_loom_client.events import InstallModeChangedEvent
from openccu_loom_client.events.types import event_from_envelope
from openccu_loom_client.operations import (
    DevicesOperations,
    HubOperations,
    I18nOperations,
    LinksOperations,
    SchedulesOperations,
)
from openccu_loom_client.transport import HttpTransport
from openccu_loom_client.wire import DAEMON_API_VERSION
from openccu_loom_client.wire.rest import Area, AreaRoomRef, Schedule, ScheduleChannelRef
from openccu_loom_client.wire.ws import WsEnvelope
from tests.helpers import MockDaemon

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
async def http(mock_daemon: MockDaemon):
    t = HttpTransport(config=mock_daemon.config, backoff_sequence=(0.0,))
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
                "unique_id": "loom_week_profile_vcu1_week_profile",
                "schedule_type": "climate",
                "min_temp": 5.0,
                "max_temp": 30.5,
                "profile_count": 3,
                "has_climate_schedule": True,
            },
        )
        result = await SchedulesOperations(transport=t).get_channel_week_profile(address="VCU1", channel=1)
        assert result.schedule_type == "climate"
        assert result.profile_count == 3

    async def test_put_channel_schedule_sends_body(self, http) -> None:
        t, mock = http
        mock.put("/api/v1/devices/VCU1/channels/1/schedule", status=202)
        schedule = Schedule(
            channel=ScheduleChannelRef(address="VCU1:1", number=1, device_address="VCU1"),
            kind="climate",
        )
        await SchedulesOperations(transport=t).put_channel_schedule(address="VCU1", channel=1, schedule=schedule)
        body = _find_call(mock, "PUT").json()
        assert body["kind"] == "climate"
        assert body["channel"]["device_address"] == "VCU1"

    async def test_set_device_active_profile(self, http) -> None:
        t, mock = http
        mock.post("/api/v1/devices/VCU1/schedule/active-profile", status=202)
        await SchedulesOperations(transport=t).set_device_active_profile(address="VCU1", profile="P2")
        assert _find_call(mock, "POST").json() == {"profile": "P2"}

    async def test_copy_schedule_sends_target_device(self, http) -> None:
        t, mock = http
        mock.post("/api/v1/devices/VCU1/schedules/copy", status=202)
        await SchedulesOperations(transport=t).copy_schedule(src_address="VCU1", dst_address="VCU2")
        assert _find_call(mock, "POST").json() == {"target_device_address": "VCU2"}

    async def test_copy_climate_profile_derives_source_channel(self, http) -> None:
        t, mock = http
        mock.post("/api/v1/devices/VCU1/channels/1/week_profile/copy", status=202)
        await SchedulesOperations(transport=t).copy_climate_profile(
            src_channel_address="VCU1:1",
            src_profile=1,
            dst_channel_address="VCU2:1",
            dst_profile=3,
        )
        post = _find_call(mock, "POST")
        assert post.path == "/api/v1/devices/VCU1/channels/1/week_profile/copy"
        assert post.json() == {
            "source_profile": 1,
            "target_channel_address": "VCU2:1",
            "target_profile": 3,
        }


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
        await LinksOperations(transport=t).remove_link(address="VCU1", sender="VCU1:1", receiver="VCU2:1")

    async def test_enable_central_links(self, http) -> None:
        t, mock = http
        mock.post("/api/v1/devices/VCU1/central-links", status=202)
        await LinksOperations(transport=t).enable_central_links(address="VCU1")

    async def test_create_central_links_alias(self, http) -> None:
        t, mock = http
        mock.post("/api/v1/devices/VCU1/central-links", status=202)
        await LinksOperations(transport=t).create_central_links(address="VCU1")
        assert _find_call(mock, "POST").path == "/api/v1/devices/VCU1/central-links"

    async def test_remove_central_links_alias(self, http) -> None:
        t, mock = http
        mock.delete("/api/v1/devices/VCU1/central-links", status=202)
        await LinksOperations(transport=t).remove_central_links(address="VCU1")
        assert _find_call(mock, "DELETE").path == "/api/v1/devices/VCU1/central-links"

    async def test_central_links_status_alias(self, http) -> None:
        t, mock = http
        mock.get(
            "/api/v1/devices/VCU1/central-links",
            payload={"supported": True, "eligible_channels": 2},
        )
        status = await LinksOperations(transport=t).central_links_status(address="VCU1")
        assert status.supported is True
        assert status.eligible_channels == 2

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

    async def test_device_icon_returns_the_png_bytes(self, http) -> None:
        # api 7.6.0 put the icon route behind authentication, so a
        # bearer-only consumer fetches the image through the client
        # instead of handing the URL to a browser.
        t, mock = http
        mock.get("/api/v1/devices/VCU1/icon", body=b"\x89PNG\r\n", content_type="image/png")
        assert await DevicesOperations(transport=t).get_device_icon(address="VCU1") == b"\x89PNG\r\n"

    async def test_device_icon_without_artwork_is_none_not_an_error(self, http) -> None:
        # 404 is the daemon's ordinary "no artwork for this model" answer;
        # raising it would make every icon-less device an exception. The
        # route answers with a bare http.NotFound — no problem document —
        # so only the status can decide.
        t, mock = http
        mock.get("/api/v1/devices/VCU1/icon", status=404, body=b"404 page not found\n", content_type="text/plain")
        assert await DevicesOperations(transport=t).get_device_icon(address="VCU1") is None

    async def test_list_calculated_data_points(self, http) -> None:
        t, mock = http
        mock.get(
            "/api/v1/devices/VCU1/channels/1/calc-dps",
            payload=[
                {
                    "name": "DEW_POINT",
                    "value": 12.3,
                    "observed": True,
                    "available": True,
                    "unique_id": "loom_test_dew_point",
                }
            ],
        )
        result = await DevicesOperations(transport=t).list_calculated_data_points(address="VCU1", channel=1)
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

    async def test_list_areas(self, http) -> None:
        t, mock = http
        mock.get(
            "/api/v1/areas",
            payload=[
                {
                    "id": "a1",
                    "name": "Erdgeschoss",
                    "position": 1,
                    "rooms": [{"central": "home", "room": "Kitchen"}],
                }
            ],
        )
        areas = await HubOperations(transport=t).list_areas()
        assert areas[0].name == "Erdgeschoss"
        assert areas[0].rooms is not None
        assert areas[0].rooms[0].central == "home"
        assert areas[0].rooms[0].room == "Kitchen"

    async def test_create_area_returns_server_generated_id(self, http) -> None:
        t, mock = http
        mock.post(
            "/api/v1/areas",
            status=201,
            payload={"id": "srv-1", "name": "Schuppen"},
        )
        created = await HubOperations(transport=t).create_area(area=Area.model_validate({"id": "", "name": "Schuppen"}))
        assert created.id == "srv-1"
        assert _find_call(mock, "POST").json() == {"id": "", "name": "Schuppen"}

    async def test_update_area(self, http) -> None:
        t, mock = http
        mock.put("/api/v1/areas/a1", status=204)
        await HubOperations(transport=t).update_area(
            area_id="a1", area=Area.model_validate({"id": "a1", "name": "Obergeschoss", "position": 2})
        )
        assert _find_call(mock, "PUT").json() == {"id": "a1", "name": "Obergeschoss", "position": 2}

    async def test_delete_area(self, http) -> None:
        t, mock = http
        # The id is path-quoted on the way out (``a%201``); the server
        # decodes it back, so an id with a space stays one path segment.
        mock.delete("/api/v1/areas/a 1", status=204)
        await HubOperations(transport=t).delete_area(area_id="a 1")

    async def test_replace_area_rooms_sends_full_set(self, http) -> None:
        t, mock = http
        mock.put("/api/v1/areas/a1/rooms", status=204)
        await HubOperations(transport=t).replace_area_rooms(
            area_id="a1",
            rooms=[
                AreaRoomRef.model_validate({"central": "home", "room": "Kitchen"}),
                AreaRoomRef.model_validate({"central": "home", "room": "Bath"}),
            ],
        )
        assert _find_call(mock, "PUT").json() == [
            {"central": "home", "room": "Kitchen"},
            {"central": "home", "room": "Bath"},
        ]

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
        await HubOperations(transport=t).update_sysvar_metadata(name="my_var", description="Living room", unit="°C")
        assert _find_call(mock, "PATCH").json() == {
            "description": "Living room",
            "unit": "°C",
        }

    async def test_fetch_system_variables_all_centrals(self, http) -> None:
        t, mock = http
        mock.post("/api/v1/sysvars/fetch", status=202)
        await HubOperations(transport=t).fetch_system_variables()
        post = _find_call(mock, "POST")
        assert post.path == "/api/v1/sysvars/fetch"
        assert "central" not in post.query

    async def test_fetch_system_variables_scoped_central(self, http) -> None:
        t, mock = http
        mock.post("/api/v1/sysvars/fetch", status=202)
        await HubOperations(transport=t).fetch_system_variables(central_name="home")
        assert _find_call(mock, "POST").query == {"central": "home"}


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
        ev = event_from_envelope(envelope=env)
        assert isinstance(ev, InstallModeChangedEvent)
        assert ev.payload.enabled is True
        assert ev.payload.remaining_s == 60
        # Routing key derives from the central name.
        assert ev.event_key == "home"


class TestI18nOperations:
    """The daemon's entity-name catalogue (daemon ≥ 0.54.0, api 5.2.0)."""

    async def test_get_entity_names_defaults_to_the_daemon_locale(self, http) -> None:
        transport, mock_daemon = http
        mock_daemon.get(
            "/api/v1/i18n/entities",
            payload={"locale": "de", "entries": {"discovery.inbox": "Posteingang"}},
        )
        catalogue = await I18nOperations(transport=transport).get_entity_names()
        assert catalogue.locale == "de"
        assert catalogue.entries["discovery.inbox"] == "Posteingang"
        # No locale asked for means no locale sent: the daemon's own
        # configured language answers.
        assert mock_daemon.requests[-1].query == {}

    async def test_get_entity_names_passes_the_requested_locale(self, http) -> None:
        transport, mock_daemon = http
        mock_daemon.get("/api/v1/i18n/entities", payload={"locale": "en", "entries": {}})
        await I18nOperations(transport=transport).get_entity_names(locale="en")
        assert mock_daemon.requests[-1].query == {"locale": "en"}
