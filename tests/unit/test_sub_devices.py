# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Channel-group / sub-device surface of the domain model.

The HA integration's ``sub_devices_enabled`` option splits multi-group
devices into HA sub-devices. It consumes exactly this surface:
``device.has_sub_devices``, ``channel.is_in_multi_group`` and
``channel.group_master`` (``.name`` / ``.room`` / ``.group_no`` with
aiohomematic ``ChannelNameData`` semantics). These tests pin that the
loom model reproduces aiohomematic's behaviour bit for bit.
"""

from __future__ import annotations

from typing import Any

from openccu_loom_types.rest import ChannelSummary, DeviceDetail, DeviceSummary, Snapshot

from openccu_loom_client.store import LoomStore

# ---- fixtures ----


def _device(*, address: str, name: str, model: str = "HmIP-DRSI4") -> DeviceSummary:
    return DeviceSummary.model_validate(
        {
            "address": address,
            "interface": "home:HmIP-RF",
            "interface_id": "home:HmIP-RF",
            "model": model,
            "name": name,
            "available": True,
            "channels_count": 0,
        }
    )


def _channel(
    *,
    address: str,
    number: int,
    name: str | None = None,
    group_no: int | None = None,
    is_group_master: bool = False,
    is_in_multi_group: bool = False,
    room: str | None = None,
) -> ChannelSummary:
    body: dict[str, Any] = {
        "address": f"{address}:{number}",
        "number": number,
        "paramset_key": "VALUES",
        "data_points_count": 1,
    }
    if name is not None:
        body["name"] = name
    if group_no is not None:
        body["group_no"] = group_no
        body["is_group_master"] = is_group_master
        body["is_in_multi_group"] = is_in_multi_group
    if room is not None:
        body["room"] = room
    return ChannelSummary.model_validate(body)


def _store_with(device: DeviceSummary, channels: list[ChannelSummary]) -> LoomStore:
    store = LoomStore()
    store.load_snapshot(
        Snapshot.model_validate(
            {"generated_at": "2026-06-12T08:00:00Z", "devices": [device.model_dump()]}
        )
    )
    detail = DeviceDetail.model_validate(
        {**device.model_dump(), "channels": [c.model_dump() for c in channels]}
    )
    store.attach_device_detail(detail)
    return store


def _drsi4_store() -> LoomStore:
    """Two multi-member switch groups (6 + 10) plus ungrouped channels."""
    addr = "0009DRSI4001"
    dev = _device(address=addr, name="Verteiler")
    channels = [
        _channel(address=addr, number=0),
        _channel(
            address=addr,
            number=5,
            name="Verteiler:5",
            group_no=6,
            is_in_multi_group=True,
            room="Keller",
        ),
        _channel(
            address=addr,
            number=6,
            name="Verteiler Licht",
            group_no=6,
            is_group_master=True,
            is_in_multi_group=True,
            room="Keller",
        ),
        _channel(
            address=addr,
            number=7,
            name="Verteiler:7",
            group_no=6,
            is_in_multi_group=True,
            room="Keller",
        ),
        _channel(address=addr, number=9, name="Verteiler:9", group_no=10, is_in_multi_group=True),
        _channel(
            address=addr,
            number=10,
            name="Verteiler:10",
            group_no=10,
            is_group_master=True,
            is_in_multi_group=True,
        ),
        _channel(address=addr, number=11, name="Verteiler:11", group_no=10, is_in_multi_group=True),
    ]
    return _store_with(dev, channels)


# ---- device.has_sub_devices ----


class TestHasSubDevices:
    """aiohomematic semantics: at least two multi-member groups."""

    def test_two_multi_member_groups_split(self) -> None:
        store = _drsi4_store()
        device = store.get_device(address="0009DRSI4001")
        assert device is not None
        assert device.has_sub_devices is True

    def test_single_group_does_not_split(self) -> None:
        addr = "000858A994D482"
        dev = _device(address=addr, name="Galerie", model="HmIP-BSM")
        channels = [
            _channel(address=addr, number=0),
            _channel(
                address=addr,
                number=3,
                name="Galerie Schaltzustand",
                group_no=4,
                is_in_multi_group=True,
            ),
            _channel(
                address=addr,
                number=4,
                name="Galerie",
                group_no=4,
                is_group_master=True,
                is_in_multi_group=True,
            ),
            _channel(address=addr, number=5, name="Galerie:5", group_no=4, is_in_multi_group=True),
        ]
        device = _store_with(dev, channels).get_device(address=addr)
        assert device is not None
        assert device.has_sub_devices is False

    def test_multiple_singleton_groups_do_not_split(self) -> None:
        addr = "0015226998783B"
        dev = _device(address=addr, name="Türgong", model="HmIP-MP3P")
        channels = [
            _channel(address=addr, number=2, name="Türgong:2", group_no=2, is_group_master=True),
            _channel(address=addr, number=6, name="Türgong:6", group_no=6, is_group_master=True),
        ]
        device = _store_with(dev, channels).get_device(address=addr)
        assert device is not None
        assert device.has_sub_devices is False

    def test_no_groups(self) -> None:
        addr = "NOGROUP01"
        dev = _device(address=addr, name="Sensor", model="HmIP-SWDO")
        device = _store_with(dev, [_channel(address=addr, number=1)]).get_device(address=addr)
        assert device is not None
        assert device.has_sub_devices is False


# ---- channel group surface ----


class TestChannelGroupSurface:
    """group_no / is_in_multi_group / group_master views."""

    def test_group_fields(self) -> None:
        store = _drsi4_store()
        vch = store.get_channel(address="0009DRSI4001", number=5)
        master = store.get_channel(address="0009DRSI4001", number=6)
        ungrouped = store.get_channel(address="0009DRSI4001", number=0)
        assert vch is not None and master is not None and ungrouped is not None
        assert vch.group_no == 6
        assert vch.is_in_multi_group is True
        assert vch.is_group_master is False
        assert master.is_group_master is True
        assert ungrouped.group_no is None
        assert ungrouped.is_in_multi_group is False
        assert ungrouped.group_master is None

    def test_group_master_resolves_master_channel(self) -> None:
        store = _drsi4_store()
        vch = store.get_channel(address="0009DRSI4001", number=7)
        assert vch is not None
        gm = vch.group_master
        assert gm is not None
        assert gm.group_no == 6
        assert gm.room == "Keller"

    def test_group_master_name_strips_device_prefix(self) -> None:
        """User-named master 'Verteiler Licht' → 'Licht' (aiohomematic strip)."""
        store = _drsi4_store()
        vch = store.get_channel(address="0009DRSI4001", number=5)
        assert vch is not None
        gm = vch.group_master
        assert gm is not None
        assert gm.name == "Licht"

    def test_group_master_default_name_reduces_to_number(self) -> None:
        """CCU-default master name 'Verteiler:10' → '10' (numeric form)."""
        store = _drsi4_store()
        vch = store.get_channel(address="0009DRSI4001", number=9)
        assert vch is not None
        gm = vch.group_master
        assert gm is not None
        assert gm.name == "10"
        assert gm.name.isnumeric()

    def test_room_absent_degrades_to_none(self) -> None:
        """Daemons older than api 1.6.0 omit the room field."""
        store = _drsi4_store()
        vch = store.get_channel(address="0009DRSI4001", number=9)
        assert vch is not None
        assert vch.room is None
