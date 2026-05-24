# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""REST operation modules — exercised against aioresponses."""

from __future__ import annotations

import pytest
from aioresponses import aioresponses

from openccu_loom_client import LoomConfig
from openccu_loom_client.operations import (
    DataPointsOperations,
    DevicesOperations,
    HubOperations,
    SystemOperations,
)
from openccu_loom_client.transport import HttpTransport

_INFO = {
    "version": "1.2.3",
    "api_version": "1.0.0",
    "commit": "deadbeef",
    "build_date": "2026-05-24T10:00:00Z",
    "started_at": "2026-05-24T10:01:00Z",
    "uptime": "PT60S",
    "capabilities": ["rest.v1", "ws.broadcasts.v1"],
}


@pytest.fixture
async def http(config: LoomConfig):
    t = HttpTransport(config, backoff_sequence=(0.0,))
    with aioresponses() as mock:
        mock.get("http://loom.test:8080/api/v1/info", payload=_INFO)
        await t.connect()
        yield t, mock
    await t.close()


class TestDevicesOperations:
    async def test_list_devices_paged(self, http) -> None:
        t, mock = http
        mock.get(
            "http://loom.test:8080/api/v1/devices?page=1&per_page=50",
            payload={"items": [], "page": 1, "per_page": 50, "total": 0},
        )
        result = await DevicesOperations(transport=t).list_devices()
        assert result.total == 0

    async def test_get_device_detail(self, http) -> None:
        t, mock = http
        mock.get(
            "http://loom.test:8080/api/v1/devices/VCU0001",
            payload={
                "address": "VCU0001",
                "interface": "home:HmIP-RF",
                "model": "HmIP-PSM",
                "name": "Lamp",
                "available": True,
                "channels_count": 2,
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
        detail = await DevicesOperations(transport=t).get_device_detail(address="VCU0001")
        assert detail.address == "VCU0001"
        assert detail.channels is not None
        assert detail.channels[0].number == 1

    async def test_refresh_all_does_not_retry_post(self, http) -> None:
        t, mock = http
        mock.post("http://loom.test:8080/api/v1/devices/refresh", status=202)
        await DevicesOperations(transport=t).refresh_all()

    async def test_patch_device_sends_name(self, http) -> None:
        t, mock = http
        mock.patch("http://loom.test:8080/api/v1/devices/VCU0001", status=200, payload={})
        await DevicesOperations(transport=t).patch_device(
            address="VCU0001", name="Renamed"
        )
        # aioresponses keys requests by (method, URL); pick the PATCH out
        # by walking the requests map. The exact key shape isn't part of
        # its public API.
        patch_call = next(
            calls[0]
            for (method, _url), calls in mock.requests.items()
            if method == "PATCH"
        )
        assert patch_call.kwargs["json"] == {"name": "Renamed"}


class TestDataPointsOperations:
    async def test_set_value_with_priority(self, http) -> None:
        t, mock = http
        url = "http://loom.test:8080/api/v1/devices/VCU0001/channels/1/data-points/LEVEL/value"
        mock.put(url, status=202)
        await DataPointsOperations(transport=t).set_value(
            address="VCU0001", channel=1, parameter="LEVEL", value=0.5, priority="high"
        )

    async def test_get_paramset(self, http) -> None:
        t, mock = http
        mock.get(
            "http://loom.test:8080/api/v1/devices/VCU0001/paramsets/MASTER",
            payload={"TRANSMIT_DUTY_CYCLE_LEVEL": 1, "PARAM_X": "hello"},
        )
        result = await DataPointsOperations(transport=t).get_paramset(
            address="VCU0001", paramset_key="MASTER"
        )
        assert result == {"TRANSMIT_DUTY_CYCLE_LEVEL": 1, "PARAM_X": "hello"}


class TestHubOperations:
    async def test_list_programs(self, http) -> None:
        t, mock = http
        mock.get(
            "http://loom.test:8080/api/v1/programs",
            payload=[{"id": "p1", "name": "All off"}],
        )
        programs = await HubOperations(transport=t).list_programs()
        assert len(programs) == 1
        assert programs[0].name == "All off"

    async def test_set_sysvar(self, http) -> None:
        t, mock = http
        mock.put("http://loom.test:8080/api/v1/sysvars/temp", status=202)
        await HubOperations(transport=t).set_sysvar(name="temp", value=21.5)

    async def test_list_alarm_messages(self, http) -> None:
        t, mock = http
        mock.get("http://loom.test:8080/api/v1/alarm-messages", payload=[])
        msgs = await HubOperations(transport=t).list_alarm_messages()
        assert msgs == []


class TestSystemOperations:
    async def test_get_snapshot(self, http) -> None:
        t, mock = http
        mock.get(
            "http://loom.test:8080/api/v1/snapshot",
            payload={
                "generated_at": "2026-05-24T08:00:00Z",
                "devices": [
                    {
                        "address": "VCU0001",
                        "interface": "home:HmIP-RF",
                        "model": "HmIP-PSM",
                        "name": "Lamp",
                        "available": True,
                        "channels_count": 2,
                    }
                ],
            },
        )
        snap = await SystemOperations(transport=t).get_snapshot()
        assert len(snap.devices) == 1
        assert snap.devices[0].address == "VCU0001"

    async def test_list_interfaces(self, http) -> None:
        t, mock = http
        mock.get(
            "http://loom.test:8080/api/v1/interfaces",
            payload=[
                {
                    "id": "home:HmIP-RF",
                    "name": "HmIP-RF",
                    "connected": True,
                    "interface": "HmIP-RF",
                }
            ],
        )
        ifaces = await SystemOperations(transport=t).list_interfaces()
        assert ifaces[0].connected is True
