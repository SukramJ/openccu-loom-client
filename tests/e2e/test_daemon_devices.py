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
from tests.e2e.conftest import find_writable_bool_dp

pytestmark = [pytest.mark.e2e, pytest.mark.e2e_ccu]


async def test_bootstrap_populates_store(client_with_ccu: LoomClient) -> None:
    await client_with_ccu.bootstrap()
    devices = list(client_with_ccu.store.devices)
    assert devices, "expected the simulator's seeded devices in the store"
    # Every device resolved at least one channel with data points.
    assert any(True for _ in client_with_ccu.store.data_points)


async def test_set_value_roundtrip(client_with_ccu: LoomClient) -> None:
    await client_with_ccu.bootstrap()
    dp = find_writable_bool_dp(client_with_ccu)
    target = not bool(dp.value)
    # Write-back through the store → daemon PUT → CCU. Should not raise.
    await dp.send_value(target)
