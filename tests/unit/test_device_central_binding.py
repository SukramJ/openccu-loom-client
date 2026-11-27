# SPDX-License-Identifier: MIT
# Copyright (C) 2026 SukramJ.
"""
Device-addressed calls carry this client's central (``?central=``).

A daemon mediating several CCUs resolves an unbound ``/devices/{addr}`` on
the first central holding the address, and addresses repeat across CCUs.
openccu-loom binds every ``/devices/{addr}`` route to ``?central=`` (API
19.0.0, ``middleware.DeviceCentral``); the static siblings ``/devices``,
``/devices/refresh``, ``/devices/firmware/refresh`` and
``/devices/values:batch`` take no binding.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from openccu_loom_client import LoomClient
from openccu_loom_client.transport import HttpTransport
from openccu_loom_client.transport.http import is_device_addressed
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
    "capabilities": ["rest.v1"],
    "schema_digest": "sha256:test",
    "config_ui_url": "",
}


@pytest.fixture
async def http(mock_daemon: MockDaemon) -> AsyncIterator[tuple[HttpTransport, MockDaemon]]:
    t = HttpTransport(config=mock_daemon.config, backoff_sequence=(0.0,))
    mock_daemon.get("/api/v1/info", payload=_INFO)
    await t.connect()
    yield t, mock_daemon
    await t.close()


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/devices/0001D3C99C3C93", True),
        ("/devices/0001D3C99C3C93:1/paramsets/MASTER", True),
        ("/devices/0001D3C99C3C93/audit", True),
        ("/devices/INT0000001%3A1/schedule", True),
        ("/devices", False),
        ("/devices/", False),
        ("/devices/refresh", False),
        ("/devices/firmware/refresh", False),
        ("/devices/values:batch", False),
        ("/system/ccu", False),
        ("/audit", False),
    ],
)
def test_is_device_addressed(path: str, expected: bool) -> None:
    assert is_device_addressed(path=path) is expected


class TestTransportBinding:
    async def test_device_call_carries_the_bound_central(self, http) -> None:
        t, daemon = http
        t.bind_device_central(source=lambda: "ccu-a")
        daemon.post("/api/v1/devices/A1/reload", status=204)
        await t.request(method="POST", path="/devices/A1/reload")
        assert daemon.requests[-1].query == {"central": "ccu-a"}

    async def test_existing_params_are_kept_beside_the_central(self, http) -> None:
        t, daemon = http
        t.bind_device_central(source=lambda: "ccu-a")
        daemon.get("/api/v1/devices/A1/audit", payload=[])
        await t.request(method="GET", path="/devices/A1/audit", params={"op": "paramset"})
        assert daemon.requests[-1].query == {"op": "paramset", "central": "ccu-a"}

    async def test_a_central_the_caller_names_wins(self, http) -> None:
        t, daemon = http
        t.bind_device_central(source=lambda: "ccu-a")
        daemon.get("/api/v1/devices/A1", payload={})
        await t.request(method="GET", path="/devices/A1", params={"central": "ccu-b"})
        assert daemon.requests[-1].query == {"central": "ccu-b"}

    async def test_binary_download_is_bound_too(self, http) -> None:
        t, daemon = http
        t.bind_device_central(source=lambda: "ccu-a")
        daemon.get("/api/v1/devices/A1/icon", body=b"\x89PNG", content_type="image/png")
        await t.request_bytes(method="GET", path="/devices/A1/icon")
        assert daemon.requests[-1].query == {"central": "ccu-a"}

    @pytest.mark.parametrize("path", ["/devices/refresh", "/devices/values:batch", "/system/ccu"])
    async def test_static_and_other_routes_stay_unbound(self, http, path: str) -> None:
        t, daemon = http
        t.bind_device_central(source=lambda: "ccu-a")
        daemon.post(f"/api/v1{path}", status=204)
        await t.request(method="POST", path=path)
        assert daemon.requests[-1].query == {}

    async def test_unresolved_central_leaves_the_call_unbound(self, http) -> None:
        t, daemon = http
        t.bind_device_central(source=lambda: "")
        daemon.post("/api/v1/devices/A1/reload", status=204)
        await t.request(method="POST", path="/devices/A1/reload")
        assert daemon.requests[-1].query == {}

    async def test_unbound_transport_sends_no_central(self, http) -> None:
        t, daemon = http
        daemon.post("/api/v1/devices/A1/reload", status=204)
        await t.request(method="POST", path="/devices/A1/reload")
        assert daemon.requests[-1].query == {}


class TestClientWiresTheBinding:
    """The binding is pinned through LoomClient's constructor, not at the setter."""

    async def test_device_call_names_the_resolved_central(self, http) -> None:
        t, daemon = http
        client = LoomClient(config=daemon.config, http_transport=t)
        client.store.set_central_id(central_id="ccu-a")
        daemon.post("/api/v1/devices/A1/reload", status=204)
        await client.devices.reload_device_config(address="A1")
        assert daemon.requests[-1].query == {"central": "ccu-a"}

    async def test_follows_the_central_resolved_after_construction(self, http) -> None:
        t, daemon = http
        client = LoomClient(config=daemon.config, http_transport=t)
        daemon.post("/api/v1/devices/A1/reload", status=204)
        await client.devices.reload_device_config(address="A1")
        assert daemon.requests[-1].query == {}, "no central resolved yet: the call must go out unbound"
        client.store.set_central_id(central_id="ccu-b")
        daemon.post("/api/v1/devices/A1/reload", status=204)
        await client.devices.reload_device_config(address="A1")
        assert daemon.requests[-1].query == {"central": "ccu-b"}
