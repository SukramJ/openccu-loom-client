# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Tier B e2e — live WebSocket events against the godevccu-backed daemon.

The simulator is stimulated (via the helper's control API) to produce
CCU-side events; the daemon broadcasts them, and we assert the client
surfaces the matching typed :class:`LoomEvent`. Covers the freshly
bound broadcasts (``value_changed``, ``device.trigger``,
``optimistic_rolled_back``).
"""

from __future__ import annotations

import asyncio

import pytest

from openccu_loom_client import LoomClient
from openccu_loom_client.events import DataPointValueChangedEvent
from openccu_loom_client.events.types import DataPointOptimisticRolledBackEvent, DeviceTriggerEvent
from tests.e2e.conftest import find_writable_bool_dp
from tests.helpers.godevccu_driver import GodevccuDriver

pytestmark = [pytest.mark.e2e, pytest.mark.e2e_ccu]

_EVENT_TIMEOUT_S = 15.0
# start_events() sends the WS subscribe asynchronously; let it settle
# before stimulating, or the broadcast can fire before the subscription
# is active and be missed (broadcasts aren't replayed without `since`).
_WS_SETTLE_S = 1.0


async def test_value_changed_pushed_over_ws(client_with_ccu: LoomClient) -> None:
    # Write through the client → daemon PUT → CCU; the simulator echoes a
    # paramset event back via the daemon callback, which the daemon
    # rebroadcasts as datapoint.value_changed over the WebSocket. This
    # mirrors the daemon's own custom_dp_roundtrip e2e test.
    await client_with_ccu.bootstrap()
    dp = find_writable_bool_dp(client_with_ccu)
    target = not bool(dp.value)

    seen = asyncio.Event()
    captured: dict[str, object] = {}

    async def on_change(event: DataPointValueChangedEvent) -> None:
        if event.payload.device_address == dp.device_address:
            captured["value"] = event.payload.value
            seen.set()

    client_with_ccu.events.subscribe(event_type=DataPointValueChangedEvent, handler=on_change)
    await client_with_ccu.start_events()
    await asyncio.sleep(_WS_SETTLE_S)

    await dp.send_value(target)

    await asyncio.wait_for(seen.wait(), timeout=_EVENT_TIMEOUT_S)
    assert captured["value"] == target


async def test_store_reflects_value_change(client_with_ccu: LoomClient) -> None:
    # The bridge applies the broadcast to the store, so the live DataPoint
    # wrapper's .value updates in place — what HA entities read. Poll the
    # store (rather than race the event fan-out) until it reflects the write.
    await client_with_ccu.bootstrap()
    dp = find_writable_bool_dp(client_with_ccu)
    target = not bool(dp.value)

    await client_with_ccu.start_events()
    await asyncio.sleep(_WS_SETTLE_S)
    await dp.send_value(target)

    loop = asyncio.get_running_loop()
    deadline = loop.time() + _EVENT_TIMEOUT_S
    while loop.time() < deadline:
        refreshed = client_with_ccu.store.get_data_point(
            address=dp.device_address, channel=dp.channel_number, parameter="STATE"
        )
        if (
            refreshed is not None
            and refreshed.value is not None
            and bool(refreshed.value) == target
        ):
            return
        await asyncio.sleep(0.1)
    pytest.fail(f"store did not reflect STATE={target} within {_EVENT_TIMEOUT_S}s")


@pytest.mark.xfail(
    reason="needs the daemon's interface_id for FireEvent; wire it up once "
    "the test can resolve it from the snapshot",
    strict=False,
)
async def test_device_trigger_pushed_over_ws(
    client_with_ccu: LoomClient, godevccu: GodevccuDriver
) -> None:
    await client_with_ccu.bootstrap()

    seen = asyncio.Event()

    async def on_trigger(_event: DeviceTriggerEvent) -> None:
        seen.set()

    client_with_ccu.events.subscribe(event_type=DeviceTriggerEvent, handler=on_trigger)
    await client_with_ccu.start_events()
    await asyncio.sleep(_WS_SETTLE_S)

    # A keypress is a non-state event: fire it directly on the simulator.
    # interface_id resolution is the open piece — see xfail reason.
    godevccu.fire_event(
        interface_id="HmIP-RF", address="<keypress-channel>", value_key="PRESS_SHORT", value=True
    )

    await asyncio.wait_for(seen.wait(), timeout=_EVENT_TIMEOUT_S)


@pytest.mark.xfail(
    reason="rollback requires a write the simulator rejects/never confirms; "
    "model a non-confirming DP in the harness to drive this deterministically",
    strict=False,
)
async def test_optimistic_rollback_pushed_over_ws(
    client_with_ccu: LoomClient, godevccu: GodevccuDriver
) -> None:
    await client_with_ccu.bootstrap()
    dp = find_writable_bool_dp(client_with_ccu)

    seen = asyncio.Event()

    async def on_rollback(_event: DataPointOptimisticRolledBackEvent) -> None:
        seen.set()

    client_with_ccu.events.subscribe(
        event_type=DataPointOptimisticRolledBackEvent, handler=on_rollback
    )
    await client_with_ccu.start_events()
    await asyncio.sleep(_WS_SETTLE_S)

    await dp.send_value(not bool(dp.value))
    await asyncio.wait_for(seen.wait(), timeout=_EVENT_TIMEOUT_S)
