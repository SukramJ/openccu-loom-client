# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
End-to-end bootstrap of the LoomClient against an in-process mock daemon.

Covers the full setup sequence a Home-Assistant integration runs at
startup: connect (info handshake) → bootstrap (snapshot + per-device
detail + per-channel DPs) → DataPointsCreatedEvent fires. Plus the
WS-bridge wiring: a wire DataPointValueChangedEvent that lands on the
bus must mutate the store's DP in place.
"""

from __future__ import annotations

from datetime import UTC, datetime

from openccu_loom_types.rest import Kind
from openccu_loom_types.ws import DataPointValueChangedPayload

from openccu_loom_client import LoomClient
from openccu_loom_client.bridge import bind_ws_events_to_store
from openccu_loom_client.events import DataPointsCreatedEvent, DataPointValueChangedEvent
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

_SNAPSHOT = {
    "generated_at": "2026-05-24T08:00:00Z",
    "devices": [
        {
            "address": "VCU0001",
            "interface": "home:HmIP-RF",
            "interface_id": "home:HmIP-RF",
            "model": "HmIP-PSM",
            "name": "Lamp",
            "available": True,
            "channels_count": 1,
        }
    ],
    "interfaces": [
        {
            "id": "home:HmIP-RF",
            "name": "HmIP-RF",
            "connected": True,
            "interface": "HmIP-RF",
            "central_id": "home",
        }
    ],
}

_DEVICE_DETAIL = {
    "address": "VCU0001",
    "interface": "home:HmIP-RF",
    "interface_id": "home:HmIP-RF",
    "model": "HmIP-PSM",
    "name": "Lamp",
    "available": True,
    "channels_count": 1,
    "channels": [
        {
            "address": "VCU0001:1",
            "number": 1,
            "paramset_key": "VALUES",
            "data_points_count": 2,
        }
    ],
}

_DATA_POINTS = [
    {
        "parameter": "STATE",
        "value": False,
        "observed": True,
        "operations": {"read": True, "write": True, "event": True},
    },
    {
        "parameter": "LEVEL",
        "value": 0.0,
        "observed": True,
        "operations": {"read": True, "write": True, "event": True},
    },
]


def _wire_endpoints(mock_daemon: MockDaemon) -> None:
    """Set up the REST endpoints the bootstrap walks through."""
    mock_daemon.get("/api/v1/info", payload=_INFO)
    mock_daemon.get("/api/v1/snapshot", payload=_SNAPSHOT)
    mock_daemon.get("/api/v1/devices/VCU0001", payload=_DEVICE_DETAIL)
    mock_daemon.get(
        "/api/v1/devices/VCU0001/channels/1/data-points",
        payload=_DATA_POINTS,
    )


class TestConnectAndBootstrap:
    async def test_connect_only_runs_info_handshake(self, mock_daemon: MockDaemon) -> None:
        mock_daemon.get("/api/v1/info", payload=_INFO)
        async with LoomClient(mock_daemon.config) as client:
            # connect() ran via __aenter__; store is still empty.
            assert list(client.store.devices) == []

    async def test_bootstrap_populates_full_store(self, mock_daemon: MockDaemon) -> None:
        _wire_endpoints(mock_daemon)
        async with LoomClient(mock_daemon.config) as client:
            await client.bootstrap()
            devices = list(client.store.devices)
            assert len(devices) == 1
            device = devices[0]
            assert device.address == "VCU0001"
            channels = list(device.channels)
            assert len(channels) == 1
            dps = list(channels[0].data_points)
            assert {dp.parameter for dp in dps} == {"STATE", "LEVEL"}

    async def test_bootstrap_emits_data_points_created_event(self, mock_daemon: MockDaemon) -> None:
        captured: list[DataPointsCreatedEvent] = []

        async def h(e: DataPointsCreatedEvent) -> None:
            captured.append(e)

        _wire_endpoints(mock_daemon)
        async with LoomClient(mock_daemon.config) as client:
            client.events.subscribe(event_type=DataPointsCreatedEvent, handler=h)
            await client.bootstrap()

        assert len(captured) == 1
        event = captured[0]
        assert event.central == "home"  # inferred from snapshot.interfaces[0].central_id
        assert {d.address for d in event.devices} == {"VCU0001"}
        assert {dp.parameter for dp in event.data_points} == {"STATE", "LEVEL"}

    async def test_bootstrap_can_skip_data_points(self, mock_daemon: MockDaemon) -> None:
        mock_daemon.get("/api/v1/info", payload=_INFO)
        mock_daemon.get("/api/v1/snapshot", payload=_SNAPSHOT)
        mock_daemon.get("/api/v1/devices/VCU0001", payload=_DEVICE_DETAIL)
        # Note: /data-points endpoint NOT registered. If the
        # bootstrap calls it anyway, the mock daemon returns 404.
        async with LoomClient(mock_daemon.config) as client:
            await client.bootstrap(fetch_data_points=False)
            device = client.store.get_device(address="VCU0001")
            assert device is not None
            channels = list(device.channels)
            # Channels attached, DPs not.
            assert len(channels) == 1
            assert list(channels[0].data_points) == []


class TestWsBridge:
    async def test_value_changed_event_updates_store(self, mock_daemon: MockDaemon) -> None:
        """
        When a typed wire event arrives on the bus, the bridge forwards it to the store.

        The DP value changes in place.

        We bypass the actual WS connection here — the bridge listens
        on the bus, so publishing the event directly exercises the
        same code path that the dispatch loop would.
        """
        _wire_endpoints(mock_daemon)
        async with LoomClient(mock_daemon.config) as client:
            await client.bootstrap()
            # Manually wire the bridge (start_events would do this
            # via the WS path, but that requires a real WS server).
            group = client.events.create_subscription_group(name="test-bridge")
            bind_ws_events_to_store(bus=client.events, store=client.store, group=group)

            dp = client.store.get_data_point(address="VCU0001", channel=1, parameter="LEVEL")
            assert dp is not None
            assert dp.value == 0.0

            # Build a value-changed event identical to what the
            # WS-side dispatch_loop would emit.
            ev = DataPointValueChangedEvent(
                seq=1,
                kind=Kind.change,
                ts=datetime(2026, 5, 24, 8, 42, 13, tzinfo=UTC),
                topic="device.VCU0001.channels.1.data_points.LEVEL",
                type="datapoint.value_changed",
                payload=DataPointValueChangedPayload.model_validate(
                    {
                        "central": "home",
                        "device_address": "VCU0001",
                        "channel": 1,
                        "parameter": "LEVEL",
                        "paramset_key": "VALUES",
                        "value": 0.75,
                        "modified_at": "2026-05-24T08:42:13Z",
                    }
                ),
            )
            await client.events.publish(ev)

            # Store now reflects the new value.
            dp_after = client.store.get_data_point(address="VCU0001", channel=1, parameter="LEVEL")
            assert dp_after is not None
            assert dp_after.value == 0.75
            group.cancel()


class TestSendValueThroughClient:
    async def test_send_value_round_trips_via_transport(self, mock_daemon: MockDaemon) -> None:
        _wire_endpoints(mock_daemon)
        mock_daemon.put(
            "/api/v1/devices/VCU0001/channels/1/data-points/STATE/value",
            status=202,
        )
        async with LoomClient(mock_daemon.config) as client:
            await client.bootstrap()
            dp = client.store.get_data_point(address="VCU0001", channel=1, parameter="STATE")
            assert dp is not None
            await dp.send_value(True)
