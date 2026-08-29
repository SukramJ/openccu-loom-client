# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""REST operation modules — exercised against an in-process mock daemon."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from openccu_loom_client.exceptions import LoomNotFoundError
from openccu_loom_client.operations import (
    CustomDataPointsOperations,
    DataPointsOperations,
    DevicesOperations,
    DiagnosticsOperations,
    HubOperations,
    SystemOperations,
)
from openccu_loom_client.transport import HttpTransport
from openccu_loom_client.wire import DAEMON_API_VERSION
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
                        "firmware": {
                            "Current": "1.0.0",
                            "Available": "",
                            "Updatable": False,
                            "UpdateState": "UP_TO_DATE",
                        },
                        "availability": {
                            "IsReachable": True,
                            "LastUpdated": None,
                            "BatteryLevel": None,
                            "LowBattery": None,
                            "SignalStrength": None,
                        },
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


class TestOnboardingRelease:
    """`POST /devices/{addr}/release` — the second half of the onboarding wizard."""

    async def test_release_posts_to_the_device(self, mock_daemon: MockDaemon, http: HttpTransport) -> None:
        mock_daemon.post("/api/v1/devices/VCU0001/release", status=204)
        await DevicesOperations(transport=http).release_device(address="VCU0001")
        sent = mock_daemon.requests[-1]
        assert (sent.method, sent.path) == ("POST", "/api/v1/devices/VCU0001/release")

    async def test_release_is_not_retried(self, mock_daemon: MockDaemon, http: HttpTransport) -> None:
        """
        A blind repeat cannot be told from a genuine miss.

        The daemon answers 404 when nothing withholds the address — released
        already, or never in the wizard — so a retry would turn a stale view
        into what looks like a failure against a healthy daemon.
        """
        mock_daemon.post(
            "/api/v1/devices/VCU0001/release",
            status=404,
            payload={
                "type": "https://openccu-loom.dev/errors/not_found",
                "title": "Device not awaiting release",
                "status": 404,
            },
        )
        before = len(mock_daemon.requests)
        with pytest.raises(LoomNotFoundError):
            await DevicesOperations(transport=http).release_device(address="VCU0001")
        assert len(mock_daemon.requests) - before == 1


class TestCustomDataPointDetailShape:
    """
    `GET .../cdps/{name}` answers with a detail record, not a summary.

    This is a regression guard with a production traceback behind it. The
    façade validated `CustomDPSummary` and so raised against every real
    response — `supported_operations` and `unique_id` are simply not in it.
    Nothing called the façade, so nothing noticed, until the store began
    delegating to it in 2026.8.33 and 22 entities failed to be added on a
    live installation.

    The payload below is the one from that log.
    """

    async def test_validates_the_response_the_daemon_actually_sends(
        self, mock_daemon: MockDaemon, http: HttpTransport
    ) -> None:
        mock_daemon.get(
            "/api/v1/devices/VCU0001/cdps/STATE@12",
            payload={"name": "STATE@12", "category": "switch", "channel_no": 12, "state": {"is_on": False}},
        )
        detail = await CustomDataPointsOperations(transport=http).get(address="VCU0001", name="STATE@12")
        assert detail.name == "STATE@12"
        assert detail.category == "switch"
        assert detail.channel_no == 12
        assert detail.state == {"is_on": False}

    async def test_a_missing_state_is_not_an_error(self, mock_daemon: MockDaemon, http: HttpTransport) -> None:
        """The daemon may answer without a state; the caller decides what that means."""
        mock_daemon.get(
            "/api/v1/devices/VCU0001/cdps/SWITCH",
            payload={"name": "SWITCH", "category": "switch", "channel_no": 1},
        )
        detail = await CustomDataPointsOperations(transport=http).get(address="VCU0001", name="SWITCH")
        assert detail.state is None
