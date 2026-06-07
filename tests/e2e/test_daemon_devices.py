# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Tier B e2e — device model against the godevccu-backed daemon.

These need both ``LOOM_DAEMON_BIN`` and ``GODEVCCU_E2E_BIN``; the daemon
is pointed at a CCU simulator seeded with the default HmIP device set.
"""

from __future__ import annotations

import pytest

from openccu_loom_client import LoomClient
from tests.e2e.conftest import device_address_by_model, find_writable_bool_dp

pytestmark = [pytest.mark.e2e, pytest.mark.e2e_ccu]

# Models the simulator always seeds (see cmd/godevccu-e2e defaultDevices).
_SEEDED_MODELS = {"HmIP-BWTH", "HmIP-BSM", "HmIP-SWSD", "HmIP-BROLL"}


async def test_bootstrap_populates_store(client_with_ccu: LoomClient) -> None:
    await client_with_ccu.bootstrap()
    devices = list(client_with_ccu.store.devices)
    assert devices, "expected the simulator's seeded devices in the store"
    # Every device resolved at least one channel with data points.
    assert any(True for _ in client_with_ccu.store.data_points)


async def test_list_devices_returns_seeded_models(client_with_ccu: LoomClient) -> None:
    listing = await client_with_ccu.devices.list_devices()
    models = {d.model for d in listing.items}
    assert models >= _SEEDED_MODELS


async def test_device_detail_and_channels_agree(client_with_ccu: LoomClient) -> None:
    await client_with_ccu.bootstrap()
    address = device_address_by_model(client_with_ccu, "HmIP-BSM")
    detail = await client_with_ccu.devices.get_device_detail(address=address)
    assert detail.model == "HmIP-BSM"
    channels = await client_with_ccu.devices.list_channels(address=address)
    assert len(channels) == detail.channels_count


async def test_list_data_points_has_state(client_with_ccu: LoomClient) -> None:
    await client_with_ccu.bootstrap()
    dp = find_writable_bool_dp(client_with_ccu)
    data_points = await client_with_ccu.devices.list_data_points(
        address=dp.device_address, channel=dp.channel_number
    )
    assert "STATE" in {d.parameter for d in data_points}


async def test_get_single_data_point(client_with_ccu: LoomClient) -> None:
    await client_with_ccu.bootstrap()
    dp = find_writable_bool_dp(client_with_ccu)
    summary = await client_with_ccu.devices.get_data_point(
        address=dp.device_address, channel=dp.channel_number, parameter="STATE"
    )
    assert summary.parameter == "STATE"


async def test_batch_read_returns_requested_keys(client_with_ccu: LoomClient) -> None:
    await client_with_ccu.bootstrap()
    dp = find_writable_bool_dp(client_with_ccu)
    key = (dp.device_address, dp.channel_number, "STATE")
    result = await client_with_ccu.datapoints.batch_read([key])
    assert key in result


async def test_set_value_roundtrip(client_with_ccu: LoomClient) -> None:
    await client_with_ccu.bootstrap()
    dp = find_writable_bool_dp(client_with_ccu)
    target = not bool(dp.value)
    # Write-back through the store → daemon PUT → CCU. Should not raise.
    await dp.send_value(target)


@pytest.mark.xfail(
    reason="daemon returns supported_operations: null for simulated CDPs, which "
    "fails CustomDPSummary validation — wire-contract gap in openccu-loom-types",
    strict=False,
)
async def test_list_custom_data_points(client_with_ccu: LoomClient) -> None:
    await client_with_ccu.bootstrap()
    # The thermostat exposes CDPs whose supported_operations come back null.
    address = device_address_by_model(client_with_ccu, "HmIP-BWTH")
    await client_with_ccu.custom_data_points.list_for_device(address=address)
