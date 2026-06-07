# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Tier B e2e — hub surface (programs + system variables) via godevccu.

The simulator seeds a fixed set of programs and sysvars; these tests
exercise the REST list/get/set/execute paths and store population
against the real daemon. The matching WS broadcasts
(``hub.sysvar_changed`` / ``hub.program_executed``) are not emitted by
the daemon for a self-initiated change against the simulator, so the
event assertions are xfail until that push path is driveable.
"""

from __future__ import annotations

import asyncio

import pytest

from openccu_loom_client import LoomClient
from openccu_loom_client.events import ProgramExecutedEvent, SysvarChangedEvent

pytestmark = [pytest.mark.e2e, pytest.mark.e2e_ccu]

_EVENT_TIMEOUT_S = 10.0
_WS_SETTLE_S = 1.0


# ---- programs ----


async def test_list_programs(client_with_ccu: LoomClient) -> None:
    await client_with_ccu.bootstrap()
    programs = await client_with_ccu.hub.list_programs()
    assert len(programs) >= 4
    assert all(p.id and p.name for p in programs)
    # The store mirrors the same programs after bootstrap.
    assert {p.id for p in client_with_ccu.store.programs} >= {p.id for p in programs}


async def test_execute_program_does_not_raise(client_with_ccu: LoomClient) -> None:
    await client_with_ccu.bootstrap()
    programs = await client_with_ccu.hub.list_programs()
    assert programs, "expected seeded programs"
    await client_with_ccu.hub.execute_program(program_id=programs[0].id)


# ---- sysvars ----


async def test_list_sysvars(client_with_ccu: LoomClient) -> None:
    await client_with_ccu.bootstrap()
    sysvars = await client_with_ccu.hub.list_sysvars()
    names = {s.name for s in sysvars}
    assert {"TargetTemperature", "Presence", "AlarmLevel"} <= names
    # The store mirrors them as Sysvar wrappers.
    assert {s.name for s in client_with_ccu.store.sysvars} >= names


async def test_set_sysvar_roundtrip(client_with_ccu: LoomClient) -> None:
    await client_with_ccu.bootstrap()
    before = await client_with_ccu.hub.get_sysvar(name="TargetTemperature")
    target = round(float(before.value or 0) + 1.0, 1)
    await client_with_ccu.hub.set_sysvar(name="TargetTemperature", value=target)
    after = await client_with_ccu.hub.get_sysvar(name="TargetTemperature")
    assert abs(float(after.value) - target) < 0.01


async def test_set_sysvar_via_store_wrapper(client_with_ccu: LoomClient) -> None:
    await client_with_ccu.bootstrap()
    sysvar = client_with_ccu.store.get_sysvar(name="TargetTemperature")
    assert sysvar is not None
    target = round(float(sysvar.value or 0) + 2.0, 1)
    await sysvar.set_value(target)
    after = await client_with_ccu.hub.get_sysvar(name="TargetTemperature")
    assert abs(float(after.value) - target) < 0.01


# ---- events (xfail: daemon doesn't broadcast on self-initiated change) ----


@pytest.mark.xfail(
    reason="daemon does not broadcast hub.sysvar_changed for a client-initiated "
    "set against the simulator; needs a CCU-side change to drive it",
    strict=False,
)
async def test_sysvar_changed_event(client_with_ccu: LoomClient) -> None:
    await client_with_ccu.bootstrap()
    seen = asyncio.Event()

    async def on_sysvar(_event: SysvarChangedEvent) -> None:
        seen.set()

    client_with_ccu.events.subscribe(event_type=SysvarChangedEvent, handler=on_sysvar)
    await client_with_ccu.start_events()
    await asyncio.sleep(_WS_SETTLE_S)

    await client_with_ccu.hub.set_sysvar(name="TargetTemperature", value=19.0)
    await asyncio.wait_for(seen.wait(), timeout=_EVENT_TIMEOUT_S)


@pytest.mark.xfail(
    reason="daemon does not broadcast hub.program_executed for a client-initiated "
    "execute against the simulator; needs a CCU-side trigger to drive it",
    strict=False,
)
async def test_program_executed_event(client_with_ccu: LoomClient) -> None:
    await client_with_ccu.bootstrap()
    programs = await client_with_ccu.hub.list_programs()
    seen = asyncio.Event()

    async def on_program(_event: ProgramExecutedEvent) -> None:
        seen.set()

    client_with_ccu.events.subscribe(event_type=ProgramExecutedEvent, handler=on_program)
    await client_with_ccu.start_events()
    await asyncio.sleep(_WS_SETTLE_S)

    await client_with_ccu.hub.execute_program(program_id=programs[0].id)
    await asyncio.wait_for(seen.wait(), timeout=_EVENT_TIMEOUT_S)
