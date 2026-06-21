# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
The ``Device.client`` shim + the device-level compat surface.

The HA integration's service handlers reach the raw interface client via
``hm_device.client.*`` and the device's ``channels`` mapping,
``week_profile_data_point`` and ``set_forced_availability``. These tests
pin the request shapes against an in-process mock daemon and the
store-backed behaviour of the bare :class:`Device`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from openccu_loom_types.rest import ChannelSummary, DeviceDetail, DeviceSummary, Snapshot
import pytest

from openccu_loom_client.model.device_client import DeviceClient
from openccu_loom_client.operations.datapoints import DataPointsOperations
from openccu_loom_client.store import LoomStore
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

# aiohomematic-only knobs the integration passes; the shim must accept+ignore them.
_AIOHM_KNOBS: dict[str, Any] = {
    "wait_for_callback": True,
    "rx_mode": "BURST",
    "check_against_pd": True,
    "retry": True,
}


@pytest.fixture
async def http(mock_daemon: MockDaemon):
    t = HttpTransport(config=mock_daemon.config, backoff_sequence=(0.0,))
    mock_daemon.get("/api/v1/info", payload=_INFO)
    await t.connect()
    yield t, mock_daemon
    await t.close()


def _find_call(mock: MockDaemon, method: str):
    return next(r for r in mock.requests if r.method == method)


def _find_get(mock: MockDaemon):
    """Return the last GET that is not the connect-time ``/info`` handshake."""
    return next(r for r in reversed(mock.requests) if r.method == "GET" and r.path != "/api/v1/info")


def _device(*, address: str, name: str = "Dev", available: bool = True) -> DeviceSummary:
    return DeviceSummary.model_validate(
        {
            "address": address,
            "interface": "home:HmIP-RF",
            "interface_id": "home:HmIP-RF",
            "model": "HmIP-BROLL",
            "name": name,
            "available": available,
            "channels_count": 0,
        }
    )


def _channel(*, address: str, number: int) -> ChannelSummary:
    return ChannelSummary.model_validate(
        {"address": f"{address}:{number}", "number": number, "paramset_key": "VALUES", "data_points_count": 1}
    )


def _store_with(*, device: DeviceSummary, channels: list[ChannelSummary]) -> LoomStore:
    store = LoomStore()
    store.load_snapshot(
        snapshot=Snapshot.model_validate({"generated_at": "2026-06-12T08:00:00Z", "devices": [device.model_dump()]})
    )
    detail = DeviceDetail.model_validate({**device.model_dump(), "channels": [c.model_dump() for c in channels]})
    store.attach_device_detail(detail=detail)
    return store


class TestDeviceClientValues:
    """Single-value and paramset reads/writes route to the data-point surface."""

    async def test_set_value_puts_value_body(self, http) -> None:
        t, mock = http
        mock.put("/api/v1/devices/VCU1/channels/1/data-points/LEVEL/value", status=202)
        await DeviceClient(transport=t, device_address="VCU1").set_value(
            channel_address="VCU1:1", paramset_key="VALUES", parameter="LEVEL", value=0.5, **_AIOHM_KNOBS
        )
        put = _find_call(mock, "PUT")
        assert put.path == "/api/v1/devices/VCU1/channels/1/data-points/LEVEL/value"
        assert put.json() == {"value": 0.5}

    async def test_get_value_reads_via_batch(self, http) -> None:
        t, mock = http
        mock.post(
            "/api/v1/devices/values:batch",
            payload={"results": [{"address": "VCU1", "channel": 1, "parameter": "LEVEL", "summary": {"value": 0.42}}]},
        )
        value = await DeviceClient(transport=t, device_address="VCU1").get_value(
            channel_address="VCU1:1", paramset_key="VALUES", parameter="LEVEL", convert_from_pd=True
        )
        assert value == 0.42

    async def test_get_value_returns_none_on_per_item_error(self, http) -> None:
        """B4: a per-item batch error must not leak back as the value."""
        t, mock = http
        mock.post(
            "/api/v1/devices/values:batch",
            payload={"results": [{"address": "VCU1", "channel": 1, "parameter": "LEVEL", "error": "ccu timeout"}]},
        )
        value = await DeviceClient(transport=t, device_address="VCU1").get_value(
            channel_address="VCU1:1", paramset_key="VALUES", parameter="LEVEL"
        )
        assert value is None

    async def test_get_paramset_reads_channel_paramset(self, http) -> None:
        t, mock = http
        mock.get("/api/v1/devices/VCU1:1/paramsets/MASTER", payload={"TEMPERATURE_OFFSET": 1})
        result = await DeviceClient(transport=t, device_address="VCU1").get_paramset(
            channel_address="VCU1:1", paramset_key=SimpleNamespace(value="MASTER"), convert_from_pd=True
        )
        assert result == {"TEMPERATURE_OFFSET": 1}
        assert _find_get(mock).path == "/api/v1/devices/VCU1:1/paramsets/MASTER"

    async def test_put_paramset_writes_channel_paramset(self, http) -> None:
        t, mock = http
        mock.put("/api/v1/devices/VCU1:1/paramsets/MASTER", status=202)
        await DeviceClient(transport=t, device_address="VCU1").put_paramset(
            channel_address="VCU1:1", paramset_key=SimpleNamespace(value="MASTER"), values={"X": 2}, **_AIOHM_KNOBS
        )
        put = _find_call(mock, "PUT")
        assert put.path == "/api/v1/devices/VCU1:1/paramsets/MASTER"
        assert put.json() == {"X": 2}


class TestBatchReadParsing:
    """B7: batch_read tolerates both wire shapes and never iterates dict keys."""

    async def test_dict_without_results_yields_empty(self, http) -> None:
        t, mock = http
        # A dict that lacks "results" must yield no items — not iterate its keys.
        mock.post("/api/v1/devices/values:batch", payload={"unexpected": "shape"})
        out = await DataPointsOperations(transport=t).batch_read(queries=[("VCU1", 1, "LEVEL")])
        assert out == {}

    async def test_bare_list_payload_is_parsed(self, http) -> None:
        t, mock = http
        mock.post(
            "/api/v1/devices/values:batch",
            payload=[{"address": "VCU1", "channel": 1, "parameter": "LEVEL", "summary": {"value": 1.5}}],
        )
        out = await DataPointsOperations(transport=t).batch_read(queries=[("VCU1", 1, "LEVEL")])
        assert out == {("VCU1", 1, "LEVEL"): 1.5}


class TestDeviceClientLinks:
    """A peer-address paramset key routes to the link surface; links CRUD too."""

    async def test_get_paramset_with_peer_reads_link_paramset(self, http) -> None:
        t, mock = http
        mock.get("/api/v1/devices/VCU1:1/link-ps/VCU2:3", payload={"SHORT_ON_TIME": 5})
        result = await DeviceClient(transport=t, device_address="VCU1").get_paramset(
            channel_address="VCU1:1", paramset_key="VCU2:3"
        )
        assert result == {"SHORT_ON_TIME": 5}
        assert _find_get(mock).path == "/api/v1/devices/VCU1:1/link-ps/VCU2:3"

    async def test_put_paramset_with_peer_writes_link_paramset(self, http) -> None:
        t, mock = http
        mock.put("/api/v1/devices/VCU1:1/link-ps/VCU2:3", status=202)
        await DeviceClient(transport=t, device_address="VCU1").put_paramset(
            channel_address="VCU1:1", paramset_key="VCU2:3", values={"SHORT_ON_TIME": 9}
        )
        put = _find_call(mock, "PUT")
        assert put.path == "/api/v1/devices/VCU1:1/link-ps/VCU2:3"
        assert put.json() == {"SHORT_ON_TIME": 9}

    async def test_get_link_peers_filters_both_directions(self, http) -> None:
        t, mock = http
        mock.get(
            "/api/v1/devices/VCU1/links",
            payload=[
                {
                    "sender_address": "VCU1:1",
                    "receiver_address": "VCU2:1",
                    "peer_address": "VCU2:1",
                    "direction": "SENDER",
                },
                {
                    "sender_address": "VCU3:2",
                    "receiver_address": "VCU1:1",
                    "peer_address": "VCU3:2",
                    "direction": "RECEIVER",
                },
                {
                    "sender_address": "VCU9:9",
                    "receiver_address": "VCU8:8",
                    "peer_address": "VCU8:8",
                    "direction": "SENDER",
                },
            ],
        )
        peers = await DeviceClient(transport=t, device_address="VCU1").get_link_peers(channel_address="VCU1:1")
        assert peers == ("VCU2:1", "VCU3:2")

    async def test_add_link_posts_sender_receiver(self, http) -> None:
        t, mock = http
        mock.post("/api/v1/devices/VCU1/links", status=201)
        await DeviceClient(transport=t, device_address="VCU1").add_link(
            sender_address="VCU1:1", receiver_address="VCU2:1", name="n", description="d"
        )
        post = _find_call(mock, "POST")
        assert post.path == "/api/v1/devices/VCU1/links"
        assert post.json() == {
            "sender_address": "VCU1:1",
            "receiver_address": "VCU2:1",
            "name": "n",
            "description": "d",
        }

    async def test_remove_link_passes_query(self, http) -> None:
        t, mock = http
        mock.delete("/api/v1/devices/VCU1/links", status=204)
        await DeviceClient(transport=t, device_address="VCU1").remove_link(
            sender_address="VCU1:1", receiver_address="VCU2:1"
        )
        delete = _find_call(mock, "DELETE")
        assert delete.path == "/api/v1/devices/VCU1/links"
        assert delete.query == {"sender": "VCU1:1", "receiver": "VCU2:1"}


class TestDeviceChannelsView:
    """``device.channels`` is a mapping-like view (dict-style get + iteration)."""

    def test_get_by_address(self) -> None:
        store = _store_with(device=_device(address="VCU1"), channels=[_channel(address="VCU1", number=1)])
        device = store.get_device(address="VCU1")
        assert device is not None
        channel = device.channels.get("VCU1:1")
        assert channel is not None
        assert channel.number == 1

    def test_get_missing_returns_none(self) -> None:
        store = _store_with(device=_device(address="VCU1"), channels=[_channel(address="VCU1", number=1)])
        device = store.get_device(address="VCU1")
        assert device is not None
        assert device.channels.get("VCU1:9") is None
        assert device.channels.get("VCU1") is None

    def test_iteration_yields_channels(self) -> None:
        store = _store_with(
            device=_device(address="VCU1"),
            channels=[_channel(address="VCU1", number=0), _channel(address="VCU1", number=1)],
        )
        device = store.get_device(address="VCU1")
        assert device is not None
        assert [c.number for c in device.channels] == [0, 1]


class TestDeviceWeekProfile:
    """The store-registered week-profile data point surfaces on the device."""

    def test_none_by_default(self) -> None:
        store = _store_with(device=_device(address="VCU1"), channels=[])
        device = store.get_device(address="VCU1")
        assert device is not None
        assert device.week_profile_data_point is None

    def test_returns_registered_dp(self) -> None:
        store = _store_with(device=_device(address="VCU1"), channels=[])
        sentinel = object()
        store.set_week_profile_data_point(address="VCU1", data_point=sentinel)
        device = store.get_device(address="VCU1")
        assert device is not None
        assert device.week_profile_data_point is sentinel


class TestDeviceForcedAvailability:
    """``set_forced_availability`` overrides the reported availability."""

    def test_force_true_overrides_unavailable(self) -> None:
        store = _store_with(device=_device(address="VCU1", available=False), channels=[])
        device = store.get_device(address="VCU1")
        assert device is not None
        assert device.available is False
        device.set_forced_availability(forced_availability=SimpleNamespace(value="FORCE_TRUE"))
        assert device.available is True

    def test_force_false_overrides_available(self) -> None:
        store = _store_with(device=_device(address="VCU1", available=True), channels=[])
        device = store.get_device(address="VCU1")
        assert device is not None
        device.set_forced_availability(forced_availability="FORCE_FALSE")
        assert device.available is False

    def test_not_set_falls_back_to_summary(self) -> None:
        store = _store_with(device=_device(address="VCU1", available=True), channels=[])
        device = store.get_device(address="VCU1")
        assert device is not None
        device.set_forced_availability(forced_availability="NOT_SET")
        assert device.available is True


class TestDeviceClientProperty:
    """``device.client`` builds lazily and needs a bound transport."""

    def test_raises_without_transport(self) -> None:
        store = _store_with(device=_device(address="VCU1"), channels=[])
        device = store.get_device(address="VCU1")
        assert device is not None
        with pytest.raises(RuntimeError, match="no transport"):
            _ = device.client

    async def test_builds_and_caches_with_transport(self, http) -> None:
        t, _mock = http
        store = _store_with(device=_device(address="VCU1"), channels=[])
        store.set_transport(transport=t)
        device = store.get_device(address="VCU1")
        assert device is not None
        client = device.client
        assert isinstance(client, DeviceClient)
        assert device.client is client


class TestDeviceReloadExport:
    """Device/channel config reload + device-definition export route to the daemon."""

    async def test_reload_device_config(self, http) -> None:
        t, mock = http
        mock.post("/api/v1/devices/VCU1/reload", status=202)
        store = _store_with(device=_device(address="VCU1"), channels=[])
        store.set_transport(transport=t)
        device = store.get_device(address="VCU1")
        assert device is not None
        await device.reload_device_config()
        assert _find_call(mock, "POST").path == "/api/v1/devices/VCU1/reload"

    async def test_reload_channel_config(self, http) -> None:
        t, mock = http
        mock.post("/api/v1/devices/VCU1/channels/1/reload", status=202)
        store = _store_with(device=_device(address="VCU1"), channels=[_channel(address="VCU1", number=1)])
        store.set_transport(transport=t)
        device = store.get_device(address="VCU1")
        assert device is not None
        channel = device.channels.get("VCU1:1")
        assert channel is not None
        await channel.reload_channel_config()
        assert _find_call(mock, "POST").path == "/api/v1/devices/VCU1/channels/1/reload"

    async def test_export_device_definition_returns_bytes(self, http) -> None:
        t, mock = http
        mock.get("/api/v1/devices/VCU1/export-definition", body=b"PKzipbytes", content_type="application/zip")
        store = _store_with(device=_device(address="VCU1"), channels=[])
        store.set_transport(transport=t)
        device = store.get_device(address="VCU1")
        assert device is not None
        archive = await device.export_device_definition()
        assert archive == b"PKzipbytes"
        assert _find_get(mock).path == "/api/v1/devices/VCU1/export-definition"

    async def test_reload_raises_without_transport(self) -> None:
        store = _store_with(device=_device(address="VCU1"), channels=[])
        device = store.get_device(address="VCU1")
        assert device is not None
        with pytest.raises(RuntimeError, match="no transport"):
            await device.reload_device_config()
