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
import contextlib
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from openccu_loom_client import LoomClient
from openccu_loom_client.bridge import bind_ws_events_to_store
import openccu_loom_client.client as client_module
from openccu_loom_client.events import (
    AuthFailedEvent,
    ConnectionStateChangedEvent,
    DataPointsCreatedEvent,
    DataPointValueChangedEvent,
    DeviceAvailabilityChangedEvent,
    DeviceCreatedEvent,
    DeviceReleasedEvent,
)
from openccu_loom_client.exceptions import LoomIncompatibleVersionError, LoomTransportError
from openccu_loom_client.wire import DAEMON_API_VERSION
from openccu_loom_client.wire.rest import Kind2 as Kind
from openccu_loom_client.wire.ws import (
    DataPointValueChangedPayload,
    DeviceAvailabilityChangedPayload,
    DeviceCreatedPayload,
    DeviceReleasedPayload,
)
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
            "updatable": False,
            "update_available": False,
            "master_pushes_config_pending": False,
            "has_sub_devices": False,
            "firmware": {"Current": "1.0.0", "Available": "", "Updatable": False, "UpdateState": "UP_TO_DATE"},
            "availability": {
                "IsReachable": True,
                "LastUpdated": None,
                "BatteryLevel": None,
                "LowBattery": None,
                "SignalStrength": None,
            },
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
        }
    ],
}

_DATA_POINTS = [
    {
        "parameter": "STATE",
        "value": False,
        "observed": True,
        "operations": {"read": True, "write": True, "event": True},
        "unique_id": "loom_test_vcu0001_1_state",
    },
    {
        "parameter": "LEVEL",
        "value": 0.0,
        "observed": True,
        "operations": {"read": True, "write": True, "event": True},
        "unique_id": "loom_test_vcu0001_1_level",
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

        # The snapshot was requested with both nested expansions …
        snapshot_reqs = [r for r in mock_daemon.requests if r.path == "/api/v1/snapshot"]
        assert snapshot_reqs and snapshot_reqs[0].query.get("include") == "channels,data_points"
        # … no per-channel data-points endpoint was ever called …
        assert not [r for r in mock_daemon.requests if r.path.endswith("/data-points")]
        # … and no per-device detail call either. Since daemon api 7.23.0 the
        # summary carries `firmware` and `availability`, and the snapshot's
        # `Channel` is a `ChannelSummary` with data points attached, so the
        # whole graph comes out of the one snapshot response.
        assert not [r for r in mock_daemon.requests if r.path.startswith("/api/v1/devices/")]
        assert len(snapshot_reqs) == 1


class TestTheSnapshotIsScopedToThisCentral:
    """
    A daemon may mediate several CCUs; this consumer is bound to one.

    Unscoped, every Home Assistant entry pulls, parses and then discards the
    other CCUs' whole device tree — measured on a maintainer's own two-CCU
    installation, which is what moved this from a footnote to a defect.
    `GET /snapshot` and `GET /devices` both take `?central=` (openapi.yaml),
    and the client did not pass it.

    The resolution rule is `LoomStore._infer_central_id`'s, on purpose: the
    configured name when the daemon knows it, the sole entry when there is
    only one, nothing otherwise. Scoping the request by one rule and filtering
    the store by another is how a consumer ends up with half a fleet.
    """

    @staticmethod
    def _ccu_entries(*names: str) -> dict[str, object]:
        """Build `GET /system/ccu` entries with every field the model requires."""
        return {
            "entries": [
                {
                    "name": name,
                    "host": f"{name}.local",
                    "available": True,
                    "is_ha_app": False,
                    "configured_interfaces": ["BidCos-RF"],
                    "readiness": {
                        "phase": "ready",
                        "ready": True,
                        "interfaces_loaded": 1,
                        "interfaces_total": 1,
                    },
                }
                for name in names
            ]
        }

    @staticmethod
    def _snapshot_query(mock_daemon: MockDaemon) -> dict[str, str]:
        requests = [r for r in mock_daemon.requests if r.path == "/api/v1/snapshot"]
        assert len(requests) == 1, f"expected exactly one snapshot request, got {len(requests)}"
        return requests[0].query

    async def test_the_configured_central_scopes_the_request(self, mock_daemon: MockDaemon) -> None:
        """With several CCUs, the configured name is the only safe pick — and it is sent."""
        mock_daemon.get("/api/v1/info", payload=_INFO)
        mock_daemon.get("/api/v1/system/ccu", payload=self._ccu_entries("ccu-attic", "ccu-cellar"))
        mock_daemon.get("/api/v1/snapshot", payload=_SNAPSHOT_NESTED)

        async with LoomClient(config=mock_daemon.config) as client:
            client.store.set_central_name(central_name="ccu-cellar")
            await client.bootstrap()

        assert self._snapshot_query(mock_daemon).get("central") == "ccu-cellar"

    async def test_the_daemon_central_count_is_recorded(self, mock_daemon: MockDaemon) -> None:
        """
        How many centrals the daemon mediates, kept for a diagnostics dump.

        Whether multi-CCU deployments exist was an open question no repository
        could answer. This is the one place that already knows, and recording
        it lets a consumer surface it in a diagnostics dump the user attaches
        to a bug report — no reporting, no telemetry.
        """
        mock_daemon.get("/api/v1/info", payload=_INFO)
        mock_daemon.get("/api/v1/system/ccu", payload=self._ccu_entries("ccu-attic", "ccu-cellar"))
        mock_daemon.get("/api/v1/snapshot", payload=_SNAPSHOT_NESTED)

        async with LoomClient(config=mock_daemon.config) as client:
            assert client.store.daemon_central_count == 0, "nothing is known before the lookup"
            client.store.set_central_name(central_name="ccu-cellar")
            await client.bootstrap()
            assert client.store.daemon_central_count == 2

    async def test_a_single_central_scopes_without_being_configured(self, mock_daemon: MockDaemon) -> None:
        """One CCU is unambiguous, so the scope is free even with no name set."""
        mock_daemon.get("/api/v1/info", payload=_INFO)
        mock_daemon.get("/api/v1/system/ccu", payload=self._ccu_entries("ccu-only"))
        mock_daemon.get("/api/v1/snapshot", payload=_SNAPSHOT_NESTED)

        async with LoomClient(config=mock_daemon.config) as client:
            await client.bootstrap()

        assert self._snapshot_query(mock_daemon).get("central") == "ccu-only"

    async def test_an_unresolvable_central_still_bootstraps_unscoped(self, mock_daemon: MockDaemon) -> None:
        """
        Several CCUs and no match: the old behaviour, not an error.

        Sending a name the daemon does not know would filter the snapshot to
        nothing and leave the consumer with no entities at all — strictly worse
        than carrying a foreign device tree.
        """
        mock_daemon.get("/api/v1/info", payload=_INFO)
        mock_daemon.get("/api/v1/system/ccu", payload=self._ccu_entries("ccu-attic", "ccu-cellar"))
        mock_daemon.get("/api/v1/snapshot", payload=_SNAPSHOT_NESTED)

        async with LoomClient(config=mock_daemon.config) as client:
            client.store.set_central_name(central_name="ccu-that-moved-away")
            await client.bootstrap()
            assert client.store.get_device(address="VCU0001") is not None

        assert "central" not in self._snapshot_query(mock_daemon)

    async def test_a_daemon_without_the_ccu_endpoint_bootstraps_unscoped(self, mock_daemon: MockDaemon) -> None:
        """An older daemon 404s on GET /system/ccu; that must not fail the bootstrap."""
        mock_daemon.get("/api/v1/info", payload=_INFO)
        mock_daemon.get("/api/v1/snapshot", payload=_SNAPSHOT_NESTED)

        async with LoomClient(config=mock_daemon.config) as client:
            await client.bootstrap()
            assert client.store.get_device(address="VCU0001") is not None

        assert "central" not in self._snapshot_query(mock_daemon)


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
                        "unique_id": "loom_test_vcu0001_1_level",
                        "available": True,
                    }
                ),
            )
            await client.events.publish(event=ev)

            # Store now reflects the new value.
            dp_after = client.store.get_data_point(address="VCU0001", channel=1, parameter="LEVEL")
            assert dp_after is not None
            assert dp_after.value == 0.75
            group.cancel()

    async def test_availability_changed_event_updates_store(self, mock_daemon: MockDaemon) -> None:
        """The bridge forwards ``device.availability_changed`` to the store."""
        _wire_endpoints(mock_daemon)
        async with LoomClient(config=mock_daemon.config) as client:
            await client.bootstrap()
            group = client.events.create_subscription_group(name="test-bridge")
            bind_ws_events_to_store(bus=client.events, store=client.store, group=group)

            assert client.store.get_device(address="VCU0001").available is True
            await client.events.publish(
                event=DeviceAvailabilityChangedEvent(
                    seq=2,
                    kind=Kind.change,
                    ts=datetime(2026, 8, 16, 10, 0, 0, tzinfo=UTC),
                    topic="device.VCU0001.lifecycle",
                    type="device.availability_changed",
                    payload=DeviceAvailabilityChangedPayload.model_validate(
                        {
                            "central": "home",
                            "interface_id": "home:HmIP-RF",
                            "device_address": "VCU0001",
                            "available": False,
                        }
                    ),
                )
            )
            assert client.store.get_device(address="VCU0001").available is False
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
    "updatable": False,
    "update_available": False,
    "master_pushes_config_pending": False,
    "has_sub_devices": False,
    "firmware": {},
    "availability": {},
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
        "unique_id": "loom_test_vcu0002_1_state",
    },
]


def _device_created_event(*, address: str, source: str = "NEW") -> DeviceCreatedEvent:
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
                "source": source,
            }
        ),
    )


def _device_released_event(*, address: str) -> DeviceReleasedEvent:
    """Build the typed event the dispatch loop emits for a device.released push."""
    return DeviceReleasedEvent(
        seq=101,
        kind=Kind.change,
        ts=datetime(2026, 8, 28, 9, 0, 0, tzinfo=UTC),
        topic=f"device.{address}.lifecycle",
        type="device.released",
        payload=DeviceReleasedPayload.model_validate(
            {"central": "home", "interface_id": "home:HmIP-RF", "device_address": address}
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


class TestReconcileFanOutIsBounded:
    """G2: a batch of device.created events must not become a REST storm."""

    async def test_cache_restore_is_not_reconciled(self, mock_daemon: MockDaemon) -> None:
        """
        source=CACHE is the daemon restoring its description cache at boot.

        That is a whole fleet at once, and every device in it is already covered
        by the snapshot walk — reconciling each one is pure duplicate load, sent
        exactly when the daemon is busiest.
        """
        _wire_endpoints(mock_daemon)
        async with LoomClient(config=mock_daemon.config) as client:
            await client.bootstrap()
            before = len(mock_daemon.requests)
            await client._on_device_created(_device_created_event(address="VCU0404", source="CACHE"))
            await asyncio.sleep(0.05)
            assert len(mock_daemon.requests) == before

    async def test_genuine_arrival_is_still_reconciled(self, mock_daemon: MockDaemon) -> None:
        """The filter must not swallow the case the reconcile exists for."""
        _wire_endpoints(mock_daemon)
        mock_daemon.get("/api/v1/devices/VCU0002", payload=_NEW_DEVICE_DETAIL)
        mock_daemon.get(
            "/api/v1/devices/VCU0002/channels/1/data-points",
            payload=_NEW_DEVICE_DATA_POINTS,
        )
        async with LoomClient(config=mock_daemon.config) as client:
            await client.bootstrap()
            before = len(mock_daemon.requests)
            await client._on_device_created(_device_created_event(address="VCU0002", source="NEW"))
            await asyncio.sleep(0.1)
            assert len(mock_daemon.requests) > before
            assert client.store.get_device(address="VCU0002") is not None

    async def test_missing_source_still_reconciles(self, mock_daemon: MockDaemon) -> None:
        """An older daemon sends no source; behaviour must be unchanged there."""
        _wire_endpoints(mock_daemon)
        mock_daemon.get("/api/v1/devices/VCU0002", payload=_NEW_DEVICE_DETAIL)
        mock_daemon.get(
            "/api/v1/devices/VCU0002/channels/1/data-points",
            payload=_NEW_DEVICE_DATA_POINTS,
        )
        event = _device_created_event(address="VCU0002")
        event.payload.source = None
        async with LoomClient(config=mock_daemon.config) as client:
            await client.bootstrap()
            before = len(mock_daemon.requests)
            await client._on_device_created(event)
            await asyncio.sleep(0.1)
            assert len(mock_daemon.requests) > before

    async def test_device_already_complete_is_not_refetched(self, mock_daemon: MockDaemon) -> None:
        """A device the snapshot walk just loaded needs no second round trip."""
        _wire_endpoints(mock_daemon)
        async with LoomClient(config=mock_daemon.config) as client:
            await client.bootstrap()
            # VCU0001 came from the snapshot complete with channels + DPs.
            assert client.store.get_device(address="VCU0001") is not None
            before = len(mock_daemon.requests)
            await client._on_device_created(_device_created_event(address="VCU0001"))
            await asyncio.sleep(0.05)
            assert len(mock_daemon.requests) == before

    async def test_stub_device_is_reconciled(self, mock_daemon: MockDaemon) -> None:
        """
        The completeness check must not mistake the bridge's stub for the real thing.

        The wire bridge seeds a channel-less stub before this handler runs; that
        is precisely the state a reconcile has to fill in.
        """
        _wire_endpoints(mock_daemon)
        mock_daemon.get("/api/v1/devices/VCU0002", payload=_NEW_DEVICE_DETAIL)
        mock_daemon.get(
            "/api/v1/devices/VCU0002/channels/1/data-points",
            payload=_NEW_DEVICE_DATA_POINTS,
        )
        async with LoomClient(config=mock_daemon.config) as client:
            await client.bootstrap()
            event = _device_created_event(address="VCU0002")
            client.store.apply_device_created(payload=event.payload)  # seed the stub
            assert client.store.get_device(address="VCU0002") is not None
            before = len(mock_daemon.requests)
            await client._on_device_created(event)
            await asyncio.sleep(0.1)
            assert len(mock_daemon.requests) > before

    async def test_reconcile_concurrency_is_capped(self, mock_daemon: MockDaemon) -> None:
        """Whatever survives the filters is still paced, never fired all at once."""
        _wire_endpoints(mock_daemon)
        async with LoomClient(config=mock_daemon.config) as client:
            await client.bootstrap()
            live = 0
            peak = 0

            async def slow_fetch(**_kwargs: object) -> None:
                nonlocal live, peak
                live += 1
                peak = max(peak, live)
                await asyncio.sleep(0.05)
                live -= 1

            client._fetch_device_into_store = slow_fetch  # type: ignore[method-assign]
            for i in range(20):
                await client._on_device_created(_device_created_event(address=f"VCU9{i:03d}"))
            await asyncio.sleep(0.6)
            assert peak <= client_module._MAX_CONCURRENT_RECONCILES


class TestStartEventsIsRestartable:
    """G8: a second start_events() must not leave the previous wiring on the bus."""

    async def test_restart_after_dispatch_died_does_not_double_apply(self, mock_daemon: MockDaemon) -> None:
        """
        The idempotence guard only holds while the dispatch task lives.

        The one path that ends it without close() is the WS transport giving up
        on a rejected credential — and calling start_events() again is the
        natural recovery. Before the fix that re-assigned `_wire_group` without
        cancelling, so both generations of wire handlers stayed subscribed and
        every event was applied to the store twice.
        """
        mock_daemon.get("/api/v1/info", payload=_INFO)
        stub = _StubWs()
        client = LoomClient(config=mock_daemon.config, ws_transport=stub)  # type: ignore[arg-type]
        await client.connect()
        try:
            await client.start_events()
            first = client.events.subscription_count()
            assert first > 0

            # Simulate the transport having given up: the dispatch task is
            # done, so the guard lets a retry through.
            client._dispatch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await client._dispatch_task

            await client.start_events()
            assert client.events.subscription_count() == first
        finally:
            await client.close()


class TestReleasedOnly:
    """Onboarding release state (daemon 0.66.1+): adopt a device only once released."""

    async def test_bootstrap_asks_the_daemon_to_filter(self, mock_daemon: MockDaemon) -> None:
        _wire_endpoints(mock_daemon)
        async with LoomClient(config=mock_daemon.config) as client:
            await client.bootstrap()
        snap = next(r for r in mock_daemon.requests if r.path.endswith("/snapshot"))
        assert snap.query.get("released_only") == "true"

    async def test_config_can_turn_the_filter_off(self, mock_daemon: MockDaemon) -> None:
        """A consumer building a configuration surface needs to see everything."""
        _wire_endpoints(mock_daemon)
        cfg = replace(mock_daemon.config, released_only=False)
        async with LoomClient(config=cfg) as client:
            await client.bootstrap()
        snap = next(r for r in mock_daemon.requests if r.path.endswith("/snapshot"))
        assert "released_only" not in snap.query

    async def test_device_released_adopts_the_device(self, mock_daemon: MockDaemon) -> None:
        """
        The release frame is what lifts the filter, so it must load the device.

        With released_only on there was no device.created for it and no store
        stub — the whole graph has to be fetched, exactly as for a new pairing.
        """
        _wire_endpoints(mock_daemon)
        mock_daemon.get("/api/v1/devices/VCU0002", payload=_NEW_DEVICE_DETAIL)
        mock_daemon.get(
            "/api/v1/devices/VCU0002/channels/1/data-points",
            payload=_NEW_DEVICE_DATA_POINTS,
        )
        async with LoomClient(config=mock_daemon.config) as client:
            await client.bootstrap()
            assert client.store.get_device(address="VCU0002") is None
            await client._on_device_released(_device_released_event(address="VCU0002"))
            await asyncio.sleep(0.1)
            device = client.store.get_device(address="VCU0002")
            assert device is not None
            assert [dp.summary.parameter for ch in device.channels for dp in ch.data_points]

    async def test_device_released_for_a_known_device_is_a_no_op(self, mock_daemon: MockDaemon) -> None:
        """Without the filter the device was adopted at device.created already."""
        _wire_endpoints(mock_daemon)
        async with LoomClient(config=mock_daemon.config) as client:
            await client.bootstrap()
            before = len(mock_daemon.requests)
            await client._on_device_released(_device_released_event(address="VCU0001"))
            await asyncio.sleep(0.05)
            assert len(mock_daemon.requests) == before


class TestReadinessGate:
    """G9: bootstrapping before the daemon reached the CCU yields an empty model."""

    @staticmethod
    def _ccu_entry(*, phase: str, ready: bool) -> dict[str, object]:
        return {
            "entries": [
                {
                    "name": "home",
                    "host": "ccu.example.lan",
                    "available": True,
                    "is_ha_app": False,
                    "configured_interfaces": ["HmIP-RF"],
                    "serial": "ABC123",
                    "readiness": {
                        "phase": phase,
                        "ready": ready,
                        "interfaces_loaded": 1 if ready else 0,
                        "interfaces_total": 1,
                    },
                }
            ]
        }

    async def test_readiness_is_read_from_system_ccu(self, mock_daemon: MockDaemon) -> None:
        mock_daemon.get("/api/v1/info", payload=_INFO)
        mock_daemon.get("/api/v1/system/ccu", payload=self._ccu_entry(phase="waiting_for_ccu", ready=False))
        async with LoomClient(config=mock_daemon.config) as client:
            readiness = await client.get_readiness()
            assert readiness is not None
            assert readiness.ready is False
            assert str(getattr(readiness.phase, "value", readiness.phase)) == "waiting_for_ccu"

    async def test_wait_returns_once_ready_latches(self, mock_daemon: MockDaemon) -> None:
        mock_daemon.get("/api/v1/info", payload=_INFO)
        mock_daemon.get("/api/v1/system/ccu", payload=self._ccu_entry(phase="loading_devices", ready=False))
        mock_daemon.get("/api/v1/system/ccu", payload=self._ccu_entry(phase="ready", ready=True))
        async with LoomClient(config=mock_daemon.config) as client:
            with patch.object(client_module, "_READINESS_POLL_SECONDS", 0.01):
                assert await client.wait_until_ready(timeout_seconds=2.0) is True

    async def test_timeout_comes_from_the_config(self, mock_daemon: MockDaemon) -> None:
        """How long a caller can afford to block is the caller's question."""
        mock_daemon.get("/api/v1/info", payload=_INFO)
        mock_daemon.get("/api/v1/system/ccu", payload=self._ccu_entry(phase="waiting_for_ccu", ready=False))
        cfg = replace(mock_daemon.config, readiness_wait_seconds=0.05)
        async with LoomClient(config=cfg) as client:
            with patch.object(client_module, "_READINESS_POLL_SECONDS", 0.01):
                assert await client.wait_until_ready() is False

    async def test_zero_timeout_skips_the_wait(self, mock_daemon: MockDaemon) -> None:
        """A caller that wants a fast, possibly-empty start sets 0."""
        mock_daemon.get("/api/v1/info", payload=_INFO)
        mock_daemon.get("/api/v1/system/ccu", payload=self._ccu_entry(phase="waiting_for_ccu", ready=False))
        cfg = replace(mock_daemon.config, readiness_wait_seconds=0.0)
        async with LoomClient(config=cfg) as client:
            assert await client.wait_until_ready() is False

    async def test_wait_gives_up_without_blocking_forever(self, mock_daemon: MockDaemon) -> None:
        """
        A daemon whose CCU never appears must not hold a consumer's setup open.

        Giving up is not fatal — the walk runs anyway and the daemon's resync
        push re-bootstraps once the CCU arrives.
        """
        mock_daemon.get("/api/v1/info", payload=_INFO)
        mock_daemon.get("/api/v1/system/ccu", payload=self._ccu_entry(phase="waiting_for_ccu", ready=False))
        async with LoomClient(config=mock_daemon.config) as client:
            with patch.object(client_module, "_READINESS_POLL_SECONDS", 0.01):
                assert await client.wait_until_ready(timeout_seconds=0.05) is False

    async def test_daemon_without_readiness_never_blocks(self, mock_daemon: MockDaemon) -> None:
        """An older daemon reports no readiness; that must not stall anybody."""
        mock_daemon.get("/api/v1/info", payload=_INFO)
        mock_daemon.get("/api/v1/system/ccu", payload={"entries": []})
        async with LoomClient(config=mock_daemon.config) as client:
            assert await client.get_readiness() is None
            assert await client.wait_until_ready(timeout_seconds=0.05) is True

    async def test_health_probe_is_reachable_and_never_raises(self, mock_daemon: MockDaemon) -> None:
        mock_daemon.get("/api/v1/info", payload=_INFO)
        mock_daemon.get("/api/v1/health", payload={"status": "degraded", "components": []})
        async with LoomClient(config=mock_daemon.config) as client:
            health = await client.get_health()
            assert health is not None
            assert str(getattr(health.status, "value", health.status)) == "degraded"

    async def test_health_probe_returns_none_when_unreadable(self, mock_daemon: MockDaemon) -> None:
        mock_daemon.get("/api/v1/info", payload=_INFO)  # no /health stub registered
        async with LoomClient(config=mock_daemon.config) as client:
            assert await client.get_health() is None


class TestRebootstrapHook:
    """G4: a layer built on top of the store must learn that the store was re-walked."""

    async def test_hook_runs_after_the_walk(self, mock_daemon: MockDaemon) -> None:
        _wire_endpoints(mock_daemon)
        async with LoomClient(config=mock_daemon.config) as client:
            order: list[str] = []

            async def fake_bootstrap(**_kwargs: object) -> None:
                order.append("walk")

            async def hook() -> None:
                order.append("hook")

            client.bootstrap = fake_bootstrap  # type: ignore[method-assign]
            client.set_rebootstrap_hook(hook)
            await client._run_rebootstrap()
            # Order matters: the hook builds on what the walk just put there.
            assert order == ["walk", "hook"]

    async def test_hook_failure_does_not_break_the_walk(self, mock_daemon: MockDaemon) -> None:
        _wire_endpoints(mock_daemon)
        async with LoomClient(config=mock_daemon.config) as client:

            async def hook() -> None:
                raise RuntimeError("consumer bug")

            client.set_rebootstrap_hook(hook)
            await client._run_rebootstrap()  # must not raise
            assert client._last_rebootstrap_finished is not None

    async def test_hook_can_be_cleared(self, mock_daemon: MockDaemon) -> None:
        _wire_endpoints(mock_daemon)
        async with LoomClient(config=mock_daemon.config) as client:
            calls = 0

            async def hook() -> None:
                nonlocal calls
                calls += 1

            client.set_rebootstrap_hook(hook)
            await client._run_rebootstrap()
            assert calls == 1
            client.set_rebootstrap_hook(None)
            await client._run_rebootstrap()
            assert calls == 1


class TestConnectionStateAndAuthFailure:
    """G3 + G5: a dropped stream and a rejected credential must both be visible."""

    async def test_connection_transitions_are_published(self, mock_daemon: MockDaemon) -> None:
        mock_daemon.get("/api/v1/info", payload=_INFO)
        async with LoomClient(config=mock_daemon.config) as client:
            seen: list[bool] = []

            async def on_state(e: ConnectionStateChangedEvent) -> None:
                seen.append(e.connected)

            client.events.subscribe(event_type=ConnectionStateChangedEvent, handler=on_state)
            assert client.connected is False

            await client._on_connection_state(True)
            assert client.connected is True
            await client._on_connection_state(False)
            assert client.connected is False
            await asyncio.sleep(0.05)
            assert seen == [True, False]

    async def test_auth_failure_is_published(self, mock_daemon: MockDaemon) -> None:
        """
        The transport stops its reconnect loop on a rejected credential.

        That is right — retrying a dead credential can only hammer the daemon —
        but it used to leave the stream silent with nobody told.
        """
        mock_daemon.get("/api/v1/info", payload=_INFO)
        async with LoomClient(config=mock_daemon.config) as client:
            seen: list[AuthFailedEvent] = []

            async def on_auth(e: AuthFailedEvent) -> None:
                seen.append(e)

            client.events.subscribe(event_type=AuthFailedEvent, handler=on_auth)
            await client._on_auth_failed()
            await asyncio.sleep(0.05)
            assert len(seen) == 1
            assert seen[0].reason == "credential_rejected"


class TestContractRecheck:
    """G6: a daemon upgraded under a live connection must be noticed."""

    async def test_a_major_version_difference_alone_connects(self, mock_daemon: MockDaemon) -> None:
        """
        A different major is a report, not a refusal.

        It used to be a refusal, and that was the wrong question to ask: this
        daemon's major has moved three times in a single release window, every
        time removing surface no generated client referenced. Refusing on the
        number locked callers out of daemons that served them perfectly.
        """
        major = int(DAEMON_API_VERSION.split(".")[0])
        mock_daemon.get("/api/v1/info", payload={**_INFO, "api_version": f"{major + 1}.0.0"})
        client = LoomClient(config=mock_daemon.config)
        await client.connect()
        await client.close()

    async def test_a_missing_capability_is_typed(self, mock_daemon: MockDaemon) -> None:
        """
        "Host unreachable" and "will never work" must not arrive as one class.

        A caller retrying a failed setup needs to tell a condition that clears
        on its own from one that clears only when somebody upgrades. That
        distinction survived the gate's move from the version to the capability
        set — it is what the caller declared it cannot work without.
        """
        mock_daemon.get("/api/v1/info", payload={**_INFO, "capabilities": ["rest.v1"]})
        client = LoomClient(config=mock_daemon.config)
        with pytest.raises(LoomIncompatibleVersionError):
            await client.connect(required_capabilities=("ws.broadcasts.v1",))
        await client.close()

    async def test_recheck_notices_a_swapped_daemon(self, mock_daemon: MockDaemon) -> None:
        """
        A daemon swapped under a live connection is caught at its cause.

        The cause is a capability the caller declared and the new peer does not
        advertise. A moved version number is not one: the re-check reports it
        and carries on, for the same reason connect() does.
        """
        # Both queued up front: the stub repeats its last entry, so registering
        # the second one after connect() would leave the first still on the
        # queue and the re-check would read the compatible answer again.
        mock_daemon.get("/api/v1/info", payload=_INFO)
        mock_daemon.get("/api/v1/info", payload={**_INFO, "capabilities": ["rest.v1"]})
        client = LoomClient(config=mock_daemon.config)
        await client.connect(required_capabilities=("ws.broadcasts.v1",))
        try:
            assert client.info is not None
            with pytest.raises(LoomIncompatibleVersionError):
                await client._http.recheck_contract()
            # The refused contract must not have half-replaced the standing one.
            assert client.info is not None
            assert "ws.broadcasts.v1" in (client.info.capabilities or [])
        finally:
            await client.close()

    async def test_recheck_survives_a_transient_failure(self, mock_daemon: MockDaemon) -> None:
        """A /info that cannot be read is not evidence of incompatibility."""
        mock_daemon.get("/api/v1/info", payload=_INFO)
        async with LoomClient(config=mock_daemon.config) as client:
            before = client.info

            async def boom(**_kwargs: object) -> None:
                raise LoomTransportError("connection refused")

            client._http.request = boom  # type: ignore[method-assign]
            assert await client._http.recheck_contract() is False
            # The previous handshake is kept rather than discarded.
            assert client.info is before

    async def test_reconnect_triggers_the_recheck(self, mock_daemon: MockDaemon) -> None:
        mock_daemon.get("/api/v1/info", payload=_INFO)
        async with LoomClient(config=mock_daemon.config) as client:
            calls = 0

            async def counting_recheck() -> bool:
                nonlocal calls
                calls += 1
                return True

            client._http.recheck_contract = counting_recheck  # type: ignore[method-assign]
            await client._on_connection_state(True)
            await asyncio.sleep(0.05)
            assert calls == 1
            # A disconnect must not re-check anything — there is nothing to ask.
            await client._on_connection_state(False)
            await asyncio.sleep(0.05)
            assert calls == 1


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

    async def test_replay_lost_cooldown_skips_back_to_back_reboots(
        self, mock_daemon: MockDaemon, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A second loss inside the cooldown window after a completed walk is
        # dropped — the just-taken snapshot is still authoritative.
        monkeypatch.setattr("openccu_loom_client.client._REBOOTSTRAP_COOLDOWN_SECONDS", 1000.0)
        _wire_endpoints(mock_daemon)
        async with LoomClient(config=mock_daemon.config) as client:
            calls = 0

            async def fast_bootstrap(**_kwargs: object) -> None:
                nonlocal calls
                calls += 1

            client.bootstrap = fast_bootstrap  # type: ignore[method-assign]

            await client._on_replay_lost(901)
            await asyncio.wait_for(client._rebootstrap_task, timeout=1.0)
            assert calls == 1

            # Within cooldown → dropped, no new walk scheduled.
            await client._on_replay_lost(902)
            assert calls == 1

    async def test_replay_lost_reboots_again_after_cooldown(
        self, mock_daemon: MockDaemon, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # With the cooldown elapsed, a fresh loss walks the snapshot again.
        monkeypatch.setattr("openccu_loom_client.client._REBOOTSTRAP_COOLDOWN_SECONDS", 0.0)
        _wire_endpoints(mock_daemon)
        async with LoomClient(config=mock_daemon.config) as client:
            calls = 0

            async def fast_bootstrap(**_kwargs: object) -> None:
                nonlocal calls
                calls += 1

            client.bootstrap = fast_bootstrap  # type: ignore[method-assign]

            await client._on_replay_lost(901)
            await asyncio.wait_for(client._rebootstrap_task, timeout=1.0)
            assert calls == 1

            await client._on_replay_lost(902)
            assert client._rebootstrap_task is not None
            await asyncio.wait_for(client._rebootstrap_task, timeout=1.0)
            assert calls == 2
