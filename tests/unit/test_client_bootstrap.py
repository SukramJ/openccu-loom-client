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

import asyncio
from datetime import UTC, datetime

from openccu_loom_types.rest import Kind1 as Kind
from openccu_loom_types.ws import DataPointValueChangedPayload, DeviceCreatedPayload

from openccu_loom_client import LoomClient
from openccu_loom_client.bridge import bind_ws_events_to_store
from openccu_loom_client.events import DataPointsCreatedEvent, DataPointValueChangedEvent, DeviceCreatedEvent
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
        async with LoomClient(config=mock_daemon.config) as client:
            # connect() ran via __aenter__; store is still empty.
            assert list(client.store.devices) == []

    async def test_bootstrap_populates_full_store(self, mock_daemon: MockDaemon) -> None:
        _wire_endpoints(mock_daemon)
        async with LoomClient(config=mock_daemon.config) as client:
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
        async with LoomClient(config=mock_daemon.config) as client:
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
        async with LoomClient(config=mock_daemon.config) as client:
            await client.bootstrap(fetch_data_points=False)
            device = client.store.get_device(address="VCU0001")
            assert device is not None
            channels = list(device.channels)
            # Channels attached, DPs not.
            assert len(channels) == 1
            assert list(channels[0].data_points) == []


# Nested snapshot (``?include=data_points``): channels + DPs arrive inline
# under ``device_channels``, so bootstrap needs no per-channel REST call.
_SNAPSHOT_NESTED = {
    **_SNAPSHOT,
    "device_channels": [
        {
            "device_address": "VCU0001",
            "channels": [
                {
                    "address": "VCU0001:1",
                    "number": 1,
                    "paramset_key": "VALUES",
                    "data_points_count": 2,
                    "data_points": _DATA_POINTS,
                }
            ],
        }
    ],
}


class TestNestedSnapshotBootstrap:
    """The nested snapshot fast-path attaches DPs without per-channel calls."""

    async def test_bootstrap_uses_nested_snapshot_no_per_channel_fetch(self, mock_daemon: MockDaemon) -> None:
        mock_daemon.get("/api/v1/info", payload=_INFO)
        mock_daemon.get("/api/v1/snapshot", payload=_SNAPSHOT_NESTED)
        mock_daemon.get("/api/v1/devices/VCU0001", payload=_DEVICE_DETAIL)
        # Deliberately NOT registering the /data-points endpoint: if the
        # bootstrap falls back to the per-channel fetch it would 404.

        async with LoomClient(config=mock_daemon.config) as client:
            await client.bootstrap()
            dps = list(client.store.get_device(address="VCU0001").channels)[0].data_points  # type: ignore[union-attr]
            assert {dp.parameter for dp in dps} == {"STATE", "LEVEL"}

        # The snapshot was requested with ?include=data_points …
        snapshot_reqs = [r for r in mock_daemon.requests if r.path == "/api/v1/snapshot"]
        assert snapshot_reqs and snapshot_reqs[0].query.get("include") == "data_points"
        # … and no per-channel data-points endpoint was ever called.
        assert not [r for r in mock_daemon.requests if r.path.endswith("/data-points")]


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
        async with LoomClient(config=mock_daemon.config) as client:
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
            await client.events.publish(event=ev)

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
        async with LoomClient(config=mock_daemon.config) as client:
            await client.bootstrap()
            dp = client.store.get_data_point(address="VCU0001", channel=1, parameter="STATE")
            assert dp is not None
            await dp.send_value(value=True)


# Detail + DP catalogue for a device that is NOT in the snapshot — it
# only appears via a live device.created push (B1 reconcile path).
_NEW_DEVICE_DETAIL = {
    "address": "VCU0002",
    "interface": "home:HmIP-RF",
    "interface_id": "home:HmIP-RF",
    "model": "HmIP-SWDO",
    "name": "Window",
    "available": True,
    "channels_count": 1,
    "channels": [
        {
            "address": "VCU0002:1",
            "number": 1,
            "paramset_key": "VALUES",
            "data_points_count": 1,
        }
    ],
}

_NEW_DEVICE_DATA_POINTS = [
    {
        "parameter": "STATE",
        "value": False,
        "observed": True,
        "operations": {"read": True, "write": False, "event": True},
    },
]


def _device_created_event(*, address: str) -> DeviceCreatedEvent:
    """Build the typed event the dispatch loop would emit for a device.created push."""
    return DeviceCreatedEvent(
        seq=99,
        kind=Kind.change,
        ts=datetime(2026, 5, 24, 9, 0, 0, tzinfo=UTC),
        topic=f"device.{address}.created",
        type="device.created",
        payload=DeviceCreatedPayload.model_validate(
            {
                "central": "home",
                "interface_id": "home:HmIP-RF",
                "device_address": address,
                "model": "HmIP-SWDO",
                "source": "pairing",
            }
        ),
    )


class TestDeviceCreatedReconcile:
    """B1: a live device.created push must spawn HA entities without a full re-bootstrap."""

    async def test_device_created_reconciles_graph_and_announces(self, mock_daemon: MockDaemon) -> None:
        _wire_endpoints(mock_daemon)
        mock_daemon.get("/api/v1/devices/VCU0002", payload=_NEW_DEVICE_DETAIL)
        mock_daemon.get(
            "/api/v1/devices/VCU0002/channels/1/data-points",
            payload=_NEW_DEVICE_DATA_POINTS,
        )

        captured: list[DataPointsCreatedEvent] = []

        async def on_created_dps(e: DataPointsCreatedEvent) -> None:
            captured.append(e)

        async with LoomClient(config=mock_daemon.config) as client:
            await client.bootstrap()
            # Mirror what start_events() wires (no real WS server here):
            # bridge seeds the stub, the client owns the reconcile.
            group = client.events.create_subscription_group(name="test-created")
            bind_ws_events_to_store(bus=client.events, store=client.store, group=group)
            group.subscribe(event_type=DeviceCreatedEvent, handler=client._on_device_created)
            # Subscribe AFTER bootstrap so we only capture the reconcile's event.
            client.events.subscribe(event_type=DataPointsCreatedEvent, handler=on_created_dps)

            assert client.store.get_device(address="VCU0002") is None

            await client.events.publish(event=_device_created_event(address="VCU0002"))
            # The reconcile runs off the dispatch loop as a tracked task.
            pending = list(client._bg_tasks)
            assert pending, "device.created should have spawned a reconcile task"
            await asyncio.gather(*pending)

            device = client.store.get_device(address="VCU0002")
            assert device is not None
            channels = list(device.channels)
            assert len(channels) == 1
            assert {dp.parameter for dp in channels[0].data_points} == {"STATE"}
            group.cancel()

        # Exactly one DataPointsCreatedEvent for the new device.
        assert len(captured) == 1
        assert {d.address for d in captured[0].devices} == {"VCU0002"}
        assert {dp.parameter for dp in captured[0].data_points} == {"STATE"}

    async def test_duplicate_device_created_is_idempotent(self, mock_daemon: MockDaemon) -> None:
        _wire_endpoints(mock_daemon)
        mock_daemon.get("/api/v1/devices/VCU0002", payload=_NEW_DEVICE_DETAIL)
        mock_daemon.get(
            "/api/v1/devices/VCU0002/channels/1/data-points",
            payload=_NEW_DEVICE_DATA_POINTS,
        )
        async with LoomClient(config=mock_daemon.config) as client:
            await client.bootstrap()
            group = client.events.create_subscription_group(name="test-created")
            bind_ws_events_to_store(bus=client.events, store=client.store, group=group)
            group.subscribe(event_type=DeviceCreatedEvent, handler=client._on_device_created)

            for _ in range(2):
                await client.events.publish(event=_device_created_event(address="VCU0002"))
                await asyncio.gather(*list(client._bg_tasks))

            device = client.store.get_device(address="VCU0002")
            assert device is not None
            # Still exactly one channel with one DP — no duplication.
            channels = list(device.channels)
            assert len(channels) == 1
            assert len(list(channels[0].data_points)) == 1
            group.cancel()


class _StubWs:
    """Minimal WsTransport stand-in that records subscribe() calls."""

    def __init__(self) -> None:
        self.started = False
        self.subscribed: list[list[str]] = []
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self.started = True

    async def subscribe(self, *, topics: list[str]) -> None:
        self.subscribed.append(list(topics))

    async def events(self):
        await self._stop.wait()
        for _ in ():  # generator that yields nothing once stopped
            yield

    async def stop(self) -> None:
        self._stop.set()


class TestExternalWsTransport:
    """B6: an injected ws_transport must still honour an explicit subscriptions list."""

    async def test_external_transport_receives_subscriptions(self, mock_daemon: MockDaemon) -> None:
        mock_daemon.get("/api/v1/info", payload=_INFO)
        stub = _StubWs()
        client = LoomClient(config=mock_daemon.config, ws_transport=stub)  # type: ignore[arg-type]
        await client.connect()
        await client.start_events(subscriptions=["device.*", "hub.*"])
        try:
            assert stub.started
            assert stub.subscribed == [["device.*", "hub.*"]]
        finally:
            await client.close()


class TestReplayLostRebootstrap:
    """B3: replay-lost re-bootstrap runs off the reader loop and de-duplicates."""

    async def test_replay_lost_reboots_once_off_loop(self, mock_daemon: MockDaemon) -> None:
        _wire_endpoints(mock_daemon)
        async with LoomClient(config=mock_daemon.config) as client:
            calls = 0
            started = asyncio.Event()
            release = asyncio.Event()

            async def slow_bootstrap(**_kwargs: object) -> None:
                nonlocal calls
                calls += 1
                started.set()
                await release.wait()

            client.bootstrap = slow_bootstrap  # type: ignore[method-assign]

            # First replay_lost schedules a background re-bootstrap and
            # returns immediately (does not block the reader).
            await client._on_replay_lost(901)
            assert client._rebootstrap_task is not None
            assert not client._rebootstrap_task.done()
            await asyncio.wait_for(started.wait(), timeout=1.0)

            # Second replay_lost while the first is in flight is deduped.
            await client._on_replay_lost(902)

            release.set()
            await asyncio.wait_for(client._rebootstrap_task, timeout=1.0)
            assert calls == 1
