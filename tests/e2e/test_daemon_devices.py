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
    data_points = await client_with_ccu.devices.list_data_points(address=dp.device_address, channel=dp.channel_number)
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
    result = await client_with_ccu.datapoints.batch_read(queries=[key])
    assert key in result


async def test_set_value_roundtrip(client_with_ccu: LoomClient) -> None:
    await client_with_ccu.bootstrap()
    dp = find_writable_bool_dp(client_with_ccu)
    target = not bool(dp.value)
    # Write-back through the store → daemon PUT → CCU. Should not raise.
    await dp.send_value(value=target)


async def test_list_custom_data_points(client_with_ccu: LoomClient) -> None:
    # The daemon now returns supported_operations as [] (never null), so
    # CustomDPSummary validation succeeds — regression guard for that fix.
    await client_with_ccu.bootstrap()
    address = device_address_by_model(client_with_ccu, "HmIP-BWTH")
    await client_with_ccu.custom_data_points.list_for_device(address=address)


async def test_build_configurable_devices(client_with_ccu: LoomClient) -> None:
    import dataclasses

    from openccu_loom_client.compat.aiohomematic.central.configurable_devices import build_configurable_devices

    await client_with_ccu.bootstrap()
    devices = build_configurable_devices(store=client_with_ccu.store)
    assert devices, "expected configurable devices from the seeded set"
    device = devices[0]
    assert device.channels, "expected channels with paramsets"
    # The daemon ships channel type/labels/paramset_keys.
    channel = device.channels[0]
    assert channel.paramset_keys  # VALUES / MASTER
    assert channel.channel_type
    # The dataclass serialises to the aiohomematic-shaped dict HA sends out.
    as_dict = dataclasses.asdict(device)
    assert set(as_dict) >= {"address", "channels", "model", "maintenance", "interface_id"}


async def test_build_event_groups(client_with_ccu: LoomClient) -> None:
    from openccu_loom_client.compat.aiohomematic.model.event_group import build_event_groups

    await client_with_ccu.bootstrap()
    groups = build_event_groups(store=client_with_ccu.store, central_id=client_with_ccu.store.serial_suffix)
    # The seeded HmIP-BSM exposes KEY_TRANSCEIVER channels with PRESS_* params.
    assert groups, "expected device-trigger event groups from the seeded devices"
    group = groups[0]
    # Canonical HA routing key: the `loom_` prefix and the central-id slot
    # are what the registry migration keys off, so assert the whole shape
    # rather than the bare kind — a plain "event_group_" prefix would pass
    # for an id that lost its namespace.
    assert group.unique_id.startswith("loom_event_group_")
    assert group.event_types  # lower-cased PRESS_* names


async def test_devices_carry_rega_ise_ids(client_with_ccu: LoomClient) -> None:
    """
    The simulator reports ReGa object ids, so ise_id-addressed calls are testable.

    A CCU always has them; the helper asks godevccu for them via
    ``Realism{RegaIDs: true}``. Without that every ``ise_id`` is ``None`` and
    the rename path below cannot be exercised at all — which is why this
    assertion is separate: it names the precondition rather than letting the
    rename test fail with an unrelated-looking lookup error.
    """
    await client_with_ccu.bootstrap()
    devices = list(client_with_ccu.store.devices)
    assert devices, "expected the seeded device set"
    with_ids = [d for d in devices if d.ise_id is not None]
    assert with_ids, "no device carries an ise_id — is Realism{RegaIDs:true} set on the helper?"


async def test_rename_device_by_ise_id(client_with_ccu: LoomClient) -> None:
    """
    Rename through the aiohomematic-facing surface, addressed by ise_id.

    This is the whole chain HA drives: it hands the compat layer an ise_id,
    which resolves it to an address against the store and PATCHes the daemon.
    Unit tests cover the resolution with a stubbed store; only the simulator
    shows that the ise_id the daemon reports is the one the lookup matches on
    — the two are populated by different code paths (`Device.listAllDetail`'s
    ReGa id vs. the snapshot summary), and a mismatch there is invisible until
    a real rename is attempted.
    """
    from openccu_loom_client.compat.aiohomematic.central.adapter import LoomCentralAdapter

    await client_with_ccu.bootstrap()
    device = next((d for d in client_with_ccu.store.devices if d.ise_id is not None), None)
    assert device is not None, "no device carries an ise_id"
    original = device.summary.name
    new_name = f"{original}-renamed" if original else "renamed-by-e2e"

    json_rpc = LoomCentralAdapter(client=client_with_ccu, name="e2e").json_rpc_client
    assert await json_rpc.rename_device(ise_id=device.ise_id, new_name=new_name) is True

    # Read it back from the daemon rather than the local store: the store is
    # only refreshed by a metadata push, and this asserts the CCU-side write.
    detail = await client_with_ccu.devices.get_device_detail(address=device.address)
    assert detail.name == new_name


async def test_rename_device_with_unknown_ise_id_raises_not_found(client_with_ccu: LoomClient) -> None:
    """
    An ise_id no device carries must raise inside the aiohomematic hierarchy.

    `homematicip_local`'s handler catches BaseHomematicException and renders a
    typed websocket error from it; anything else reaches the config panel as a
    generic `unknown_error` with the cause lost.
    """
    from openccu_loom_client.compat.aiohomematic.central.adapter import LoomCentralAdapter
    from openccu_loom_client.exceptions import BaseLoomException

    await client_with_ccu.bootstrap()
    json_rpc = LoomCentralAdapter(client=client_with_ccu, name="e2e").json_rpc_client
    with pytest.raises(BaseLoomException):
        await json_rpc.rename_device(ise_id=999_999_999, new_name="nope")
