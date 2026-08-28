# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Tier A e2e — read-only system / admin surface against a CCU-less daemon.

Exercises the operation façades that don't need simulated hardware, so
they confirm the REST contract (and auth) for the broad admin surface
the HA integration and ops tooling rely on. Only endpoints verified to
return 200 for the admin user are asserted here; ``/config/effective``
and ``/centrals`` are 404 in this build and deliberately not touched.
"""

from __future__ import annotations

import pytest

from openccu_loom_client import LoomClient

pytestmark = pytest.mark.e2e


async def test_get_diagnostics(client_no_ccu: LoomClient) -> None:
    diag = await client_no_ccu.system.get_diagnostics()
    assert isinstance(diag, dict)


async def test_get_log_level(client_no_ccu: LoomClient) -> None:
    level = await client_no_ccu.diagnostics.get_log_level()
    assert isinstance(level, dict)


async def test_list_interfaces(client_no_ccu: LoomClient) -> None:
    # No centrals configured → an empty but well-formed list.
    interfaces = await client_no_ccu.system.list_interfaces()
    assert isinstance(interfaces, list)


async def test_list_system_ccus(client_no_ccu: LoomClient) -> None:
    # Empty with no centrals, but must unwrap the {"entries": [...]} envelope
    # without raising (regression guard for the envelope-unwrap fix).
    ccus = await client_no_ccu.system.list_system_ccus()
    assert isinstance(ccus, list)


async def test_system_status(client_no_ccu: LoomClient) -> None:
    status = await client_no_ccu.system.get_system_status()
    assert isinstance(status, dict)


async def test_visibility_unignore(client_no_ccu: LoomClient) -> None:
    unignore = await client_no_ccu.visibility.get_unignore()
    assert unignore is not None


async def test_audit_log(client_no_ccu: LoomClient) -> None:
    audit = await client_no_ccu.diagnostics.list_audit()
    assert isinstance(audit, list)
