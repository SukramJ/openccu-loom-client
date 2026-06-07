# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Tier A e2e — connect/handshake/bootstrap against a CCU-less daemon.

These run the real daemon binary with ``centrals: []`` and need no
simulated hardware, so they verify the REST + WebSocket contract the
client depends on without godevccu.
"""

from __future__ import annotations

import pytest

from openccu_loom_client import LoomClient

pytestmark = pytest.mark.e2e


async def test_connect_runs_capability_handshake(client_no_ccu: LoomClient) -> None:
    # connect() already ran the GET /info handshake; re-read it explicitly.
    info = await client_no_ccu.system.get_info()
    assert info.api_version
    assert "rest.v1" in info.capabilities
    assert "ws.broadcasts.v1" in info.capabilities


async def test_health_probe_ok(client_no_ccu: LoomClient) -> None:
    health = await client_no_ccu.system.get_health()
    assert health.status


async def test_empty_snapshot_bootstrap(client_no_ccu: LoomClient) -> None:
    # With no centrals configured, bootstrap completes and the store
    # stays empty — exercising the full snapshot path against the daemon.
    await client_no_ccu.bootstrap()
    assert list(client_no_ccu.store.devices) == []


async def test_websocket_upgrade(client_no_ccu: LoomClient) -> None:
    # Proves the client's WS path (/api/v1/events) matches what the
    # daemon serves; start_events() opens the socket and the dispatch
    # loop without raising.
    await client_no_ccu.bootstrap()
    await client_no_ccu.start_events()
