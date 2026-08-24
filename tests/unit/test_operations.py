# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""REST operation modules — exercised against an in-process mock daemon."""

from __future__ import annotations

from collections.abc import AsyncIterator

from openccu_loom_types import DAEMON_API_VERSION
import pytest

from openccu_loom_client.operations import (
    ConfigOperations,
    DataPointsOperations,
    DevicesOperations,
    DiagnosticsOperations,
    HubOperations,
    SystemOperations,
)
from openccu_loom_client.transport import HttpTransport
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
async def http(mock_daemon: MockDaemon) -> AsyncIterator[HttpTransport]:
    t = HttpTransport(config=mock_daemon.config, backoff_sequence=(0.0,))
    mock_daemon.get("/api/v1/info", payload=_INFO)
    await t.connect()
    yield t
    await t.close()


class TestDevicesOperations:
    async def test_list_devices_paged(self, mock_daemon: MockDaemon, http: HttpTransport) -> None:
        mock_daemon.get(
            "/api/v1/devices",
            payload={"items": [], "page": 1, "per_page": 50, "total": 0},
        )
        result = await DevicesOperations(transport=http).list_devices()
        assert result.total == 0

    async def test_get_device_detail(self, mock_daemon: MockDaemon, http: HttpTransport) -> None:
        mock_daemon.get(
            "/api/v1/devices/VCU0001",
            payload={
                "address": "VCU0001",
                "interface": "home:HmIP-RF",
                "interface_id": "home:HmIP-RF",
                "model": "HmIP-PSM",
                "name": "Lamp",
                "available": True,
                "channels_count": 2,
                "updatable": False,
                "update_available": False,
                "master_pushes_config_pending": False,
                "has_sub_devices": False,
                "firmware": {},
                "availability": {},
                "channels": [
                    {
                        "address": "VCU0001:1",
                        "number": 1,
                        "paramset_key": "VALUES",
                        "data_points_count": 2,
                    },
                ],
            },
        )
        detail = await DevicesOperations(transport=http).get_device_detail(address="VCU0001")
        assert detail.address == "VCU0001"
        assert detail.channels is not None
        assert detail.channels[0].number == 1

    async def test_refresh_all_does_not_retry_post(self, mock_daemon: MockDaemon, http: HttpTransport) -> None:
        mock_daemon.post("/api/v1/devices/refresh", status=202)
        await DevicesOperations(transport=http).refresh_all()

    async def test_reload_device_config_posts_to_device(self, mock_daemon: MockDaemon, http: HttpTransport) -> None:
        mock_daemon.post("/api/v1/devices/VCU0001/reload", status=200, payload={})
        await DevicesOperations(transport=http).reload_device_config(address="VCU0001")
        req = next(r for r in mock_daemon.requests if r.method == "POST")
        assert req.path == "/api/v1/devices/VCU0001/reload"

    async def test_reload_channel_config_posts_to_channel(self, mock_daemon: MockDaemon, http: HttpTransport) -> None:
        mock_daemon.post("/api/v1/devices/VCU0001/channels/3/reload", status=200, payload={})
        await DevicesOperations(transport=http).reload_channel_config(address="VCU0001", channel=3)
        req = next(r for r in mock_daemon.requests if r.method == "POST")
        assert req.path == "/api/v1/devices/VCU0001/channels/3/reload"

    async def test_patch_device_sends_name(self, mock_daemon: MockDaemon, http: HttpTransport) -> None:
        mock_daemon.patch("/api/v1/devices/VCU0001", status=200, payload={})
        await DevicesOperations(transport=http).patch_device(address="VCU0001", name="Renamed")
        patch_req = next(r for r in mock_daemon.requests if r.method == "PATCH")
        assert patch_req.json() == {"name": "Renamed"}


class TestDataPointsOperations:
    async def test_set_value_with_priority(self, mock_daemon: MockDaemon, http: HttpTransport) -> None:
        mock_daemon.put("/api/v1/devices/VCU0001/channels/1/data-points/LEVEL/value", status=202)
        await DataPointsOperations(transport=http).set_value(
            address="VCU0001", channel=1, parameter="LEVEL", value=0.5, priority="high"
        )

    async def test_get_paramset(self, mock_daemon: MockDaemon, http: HttpTransport) -> None:
        mock_daemon.get(
            "/api/v1/devices/VCU0001/paramsets/MASTER",
            payload={"TRANSMIT_DUTY_CYCLE_LEVEL": 1, "PARAM_X": "hello"},
        )
        result = await DataPointsOperations(transport=http).get_paramset(address="VCU0001", paramset_key="MASTER")
        assert result == {"TRANSMIT_DUTY_CYCLE_LEVEL": 1, "PARAM_X": "hello"}


class TestHubOperations:
    async def test_list_programs(self, mock_daemon: MockDaemon, http: HttpTransport) -> None:
        mock_daemon.get(
            "/api/v1/programs",
            payload=[{"id": "p1", "name": "All off", "unique_id": "loom_test_p1"}],
        )
        programs = await HubOperations(transport=http).list_programs()
        assert len(programs) == 1
        assert programs[0].name == "All off"

    async def test_set_sysvar(self, mock_daemon: MockDaemon, http: HttpTransport) -> None:
        mock_daemon.put("/api/v1/sysvars/temp", status=202)
        await HubOperations(transport=http).set_sysvar(name="temp", value=21.5)

    async def test_list_alarm_messages(self, mock_daemon: MockDaemon, http: HttpTransport) -> None:
        mock_daemon.get("/api/v1/alarm-messages", payload=[])
        msgs = await HubOperations(transport=http).list_alarm_messages()
        assert msgs == []


class TestSystemOperations:
    async def test_get_snapshot(self, mock_daemon: MockDaemon, http: HttpTransport) -> None:
        mock_daemon.get(
            "/api/v1/snapshot",
            payload={
                "generated_at": "2026-05-24T08:00:00Z",
                "devices": [
                    {
                        "address": "VCU0001",
                        "interface": "home:HmIP-RF",
                        "interface_id": "home:HmIP-RF",
                        "model": "HmIP-PSM",
                        "name": "Lamp",
                        "available": True,
                        "channels_count": 2,
                        "updatable": False,
                        "update_available": False,
                        "master_pushes_config_pending": False,
                        "has_sub_devices": False,
                    }
                ],
            },
        )
        snap = await SystemOperations(transport=http).get_snapshot()
        assert len(snap.devices) == 1
        assert snap.devices[0].address == "VCU0001"

    async def test_list_interfaces(self, mock_daemon: MockDaemon, http: HttpTransport) -> None:
        mock_daemon.get(
            "/api/v1/interfaces",
            payload=[
                {
                    "id": "home:HmIP-RF",
                    "name": "HmIP-RF",
                    "connected": True,
                    "interface": "HmIP-RF",
                }
            ],
        )
        ifaces = await SystemOperations(transport=http).list_interfaces()
        assert ifaces[0].connected is True

    async def test_get_addon_update_status(self, mock_daemon: MockDaemon, http: HttpTransport) -> None:
        mock_daemon.get(
            "/api/v1/system/addon-update",
            payload={
                "supported": True,
                "current_version": "0.50.0",
                "latest_version": "0.50.1",
                "update_available": True,
                "release_url": "https://github.com/SukramJ/openccu-loom/releases/tag/v0.50.1",
                "state": "idle",
            },
        )
        status = await SystemOperations(transport=http).get_addon_update_status()
        assert status.supported is True
        assert status.current_version == "0.50.0"
        assert status.latest_version == "0.50.1"
        assert status.update_available is True
        assert status.state.value == "idle"

    async def test_check_addon_update_posts(self, mock_daemon: MockDaemon, http: HttpTransport) -> None:
        mock_daemon.post("/api/v1/system/addon-update/check", status=202)
        await SystemOperations(transport=http).check_addon_update()
        call = next(r for r in mock_daemon.requests if r.path.endswith("/addon-update/check"))
        assert call.method == "POST"

    async def test_install_addon_update_posts(self, mock_daemon: MockDaemon, http: HttpTransport) -> None:
        mock_daemon.post("/api/v1/system/addon-update/install", status=202)
        await SystemOperations(transport=http).install_addon_update()
        call = next(r for r in mock_daemon.requests if r.path.endswith("/addon-update/install"))
        assert call.method == "POST"


class TestWiringManifest:
    """``GET /diagnostics/wiring`` — what the daemon says it wired."""

    async def test_get_wiring_returns_the_declared_seams(self, mock_daemon: MockDaemon, http: HttpTransport) -> None:
        mock_daemon.get(
            "/api/v1/diagnostics/wiring",
            payload=[
                {
                    "name": "history.recorder",
                    "collaborator": "*history.Recorder",
                    "phase": "per-central",
                    "why": "no value change is ever recorded",
                },
                {
                    "name": "webhook.alarm_bus",
                    "collaborator": "*engine.Service alarm bus",
                    "phase": "ordered",
                    "before": ["northbridges.started"],
                    "why": "no alarm-panel event is ever forwarded",
                },
            ],
        )
        seams = await DiagnosticsOperations(transport=http).get_wiring()
        assert [s["name"] for s in seams] == ["history.recorder", "webhook.alarm_bus"]
        assert seams[1]["before"] == ["northbridges.started"]

    async def test_get_wiring_reports_a_violation_verbatim(self, mock_daemon: MockDaemon, http: HttpTransport) -> None:
        """
        A violated ordering constraint must survive the client untouched.

        It is the one field that says a wired-looking daemon is not: the
        collaborator IS attached, every other surface reports healthy,
        and only this list says the attach came too late.
        """
        mock_daemon.get(
            "/api/v1/diagnostics/wiring",
            payload=[
                {
                    "name": "webhook.alarm_bus",
                    "collaborator": "*engine.Service alarm bus",
                    "phase": "ordered",
                    "before": ["northbridges.started"],
                    "why": "no alarm-panel event is ever forwarded",
                    "violations": ['attached after "northbridges.started"'],
                }
            ],
        )
        seams = await DiagnosticsOperations(transport=http).get_wiring()
        assert seams[0]["violations"] == ['attached after "northbridges.started"']

    async def test_get_wiring_accepts_an_empty_ledger(self, mock_daemon: MockDaemon, http: HttpTransport) -> None:
        """Empty is a valid answer — "wired none of them", not an error."""
        mock_daemon.get("/api/v1/diagnostics/wiring", payload=[])
        assert await DiagnosticsOperations(transport=http).get_wiring() == []


class TestConfigSectionSave:
    """``PUT /config/sections/{section}`` — stored is not the same as in effect."""

    async def test_put_section_surfaces_applied(self, mock_daemon: MockDaemon, http: HttpTransport) -> None:
        mock_daemon.put(
            "/api/v1/config/sections/north.mqtt",
            payload={
                "section": "north.mqtt",
                "version": 3,
                "updated_at": "2026-08-24T08:00:00Z",
                "restart_required": False,
                "applied": True,
            },
        )
        ack = await ConfigOperations(transport=http).put_section(
            section="north.mqtt", values={"topic_base": "loomtest"}
        )
        assert ack["applied"] is True
        assert "apply_error" not in ack

    async def test_put_section_surfaces_a_failed_apply(self, mock_daemon: MockDaemon, http: HttpTransport) -> None:
        """
        Only ``apply_error`` separates the two outcomes.

        The section is stored either way; the field is what distinguishes
        "took effect now" from "the running daemon refused it". A caller
        that reports the second as a plain success repeats the defect the
        field exists to close.
        """
        mock_daemon.put(
            "/api/v1/config/sections/north.mqtt",
            payload={
                "section": "north.mqtt",
                "version": 4,
                "updated_at": "2026-08-24T08:01:00Z",
                "restart_required": False,
                "applied": False,
                "apply_error": "broker refused the connection",
            },
        )
        ack = await ConfigOperations(transport=http).put_section(
            section="north.mqtt", values={"broker_url": "tcp://nope:1883"}
        )
        assert ack["applied"] is False
        assert ack["apply_error"] == "broker refused the connection"

    async def test_put_section_against_an_older_daemon_omits_applied(
        self, mock_daemon: MockDaemon, http: HttpTransport
    ) -> None:
        """A daemon below api 7.8.0 sends neither key: unknown, not False."""
        mock_daemon.put(
            "/api/v1/config/sections/north.mqtt",
            payload={
                "section": "north.mqtt",
                "version": 5,
                "updated_at": "2026-08-24T08:02:00Z",
                "restart_required": False,
            },
        )
        ack = await ConfigOperations(transport=http).put_section(
            section="north.mqtt", values={"topic_base": "loomtest"}
        )
        assert "applied" not in ack
