# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
LoomStore + domain-model behaviour.

The store is the in-memory mirror of one daemon's CCU model. Tests
cover the three populating paths (snapshot → detail → data-points),
the live-update apply methods, and the write-back through a
transport stand-in.
"""

from __future__ import annotations

from typing import Any

from openccu_loom_types.rest import ChannelSummary, DataPointSummary, DeviceDetail, DeviceSummary, Operations, Snapshot
from openccu_loom_types.ws import DataPointValueChangedPayload, DeviceCreatedPayload, DeviceRemovedPayload
import pytest

from openccu_loom_client.store import LoomStore

# ---- fixtures ----


def _device_summary(*, address: str = "VCU0001", name: str = "Lamp") -> DeviceSummary:
    return DeviceSummary.model_validate(
        {
            "address": address,
            "interface": "home:HmIP-RF",
            "interface_id": "home:HmIP-RF",
            "model": "HmIP-PSM",
            "name": name,
            "available": True,
            "channels_count": 3,
        }
    )


def _channel_summary(*, address: str, number: int) -> ChannelSummary:
    return ChannelSummary.model_validate(
        {
            "address": f"{address}:{number}",
            "number": number,
            "paramset_key": "VALUES",
            "data_points_count": 2,
        }
    )


def _dp_summary(*, parameter: str, value: Any = None) -> DataPointSummary:
    return DataPointSummary.model_validate(
        {
            "parameter": parameter,
            "value": value,
            "observed": value is not None,
            "operations": Operations(read=True, write=True, event=True).model_dump(),
        }
    )


def _snapshot(*, devices: list[DeviceSummary]) -> Snapshot:
    return Snapshot.model_validate(
        {"generated_at": "2026-05-24T08:00:00Z", "devices": [d.model_dump() for d in devices]}
    )


class _FakeTransport:
    """Records every call so assertions can match against URL + body."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Any = None,
        json_body: Any = None,
        headers: Any = None,
        allow_retry: Any = None,
    ) -> Any:
        self.calls.append((method, path, json_body))
        return None


# ---- snapshot load ----


class TestLoadSnapshot:
    def test_empty_snapshot_leaves_store_empty(self) -> None:
        store = LoomStore()
        store.load_snapshot(snapshot=_snapshot(devices=[]))
        assert list(store.devices) == []

    def test_devices_land_with_summary_only(self) -> None:
        store = LoomStore()
        store.load_snapshot(
            snapshot=_snapshot(
                devices=[
                    _device_summary(address="VCU0001", name="A"),
                    _device_summary(address="VCU0002", name="B"),
                ]
            )
        )
        assert {d.address for d in store.devices} == {"VCU0001", "VCU0002"}
        # Channels aren't part of the snapshot.
        assert store.channels_of(address="VCU0001") == []

    def test_reloading_snapshot_updates_summary_in_place(self) -> None:
        store = LoomStore()
        store.load_snapshot(snapshot=_snapshot(devices=[_device_summary(name="A")]))
        original = store.get_device(address="VCU0001")
        assert original is not None

        # Same address, different name → in-place update of the existing
        # Device wrapper, so HA-side entity refs stay valid.
        store.load_snapshot(snapshot=_snapshot(devices=[_device_summary(name="A-renamed")]))
        same = store.get_device(address="VCU0001")
        assert same is original
        assert same.name == "A-renamed"


# ---- attach detail + DPs ----


class TestAttachDetailAndDataPoints:
    def test_detail_adds_channels_and_metadata(self) -> None:
        store = LoomStore()
        store.load_snapshot(snapshot=_snapshot(devices=[_device_summary()]))
        detail = DeviceDetail.model_validate(
            {
                **_device_summary().model_dump(),
                "channels": [
                    _channel_summary(address="VCU0001", number=1).model_dump(),
                    _channel_summary(address="VCU0001", number=2).model_dump(),
                ],
            }
        )
        store.attach_device_detail(detail=detail)
        device = store.get_device(address="VCU0001")
        assert device is not None
        assert {c.number for c in device.channels} == {1, 2}

    def test_detail_garbage_collects_vanished_channels(self) -> None:
        store = LoomStore()
        store.load_snapshot(snapshot=_snapshot(devices=[_device_summary()]))
        # First detail: channels 1, 2, 3.
        first = DeviceDetail.model_validate(
            {
                **_device_summary().model_dump(),
                "channels": [_channel_summary(address="VCU0001", number=n).model_dump() for n in (1, 2, 3)],
            }
        )
        store.attach_device_detail(detail=first)
        # Second detail: channel 2 vanished.
        second = DeviceDetail.model_validate(
            {
                **_device_summary().model_dump(),
                "channels": [_channel_summary(address="VCU0001", number=n).model_dump() for n in (1, 3)],
            }
        )
        store.attach_device_detail(detail=second)
        device = store.get_device(address="VCU0001")
        assert device is not None
        assert {c.number for c in device.channels} == {1, 3}

    def test_attach_data_points_populates_lookup(self) -> None:
        store = LoomStore()
        store.load_snapshot(snapshot=_snapshot(devices=[_device_summary()]))
        store.attach_device_detail(
            detail=DeviceDetail.model_validate(
                {
                    **_device_summary().model_dump(),
                    "channels": [_channel_summary(address="VCU0001", number=1).model_dump()],
                }
            )
        )
        store.attach_channel_data_points(
            device_address="VCU0001",
            channel_number=1,
            data_points=[
                _dp_summary(parameter="LEVEL", value=0.5),
                _dp_summary(parameter="STATE", value=False),
            ],
        )
        dp = store.get_data_point(address="VCU0001", channel=1, parameter="LEVEL")
        assert dp is not None
        assert dp.value == 0.5
        assert dp.channel_address == "VCU0001:1"

    def test_re_attach_replaces_data_points_wholesale(self) -> None:
        store = LoomStore()
        store.load_snapshot(snapshot=_snapshot(devices=[_device_summary()]))
        store.attach_device_detail(
            detail=DeviceDetail.model_validate(
                {
                    **_device_summary().model_dump(),
                    "channels": [_channel_summary(address="VCU0001", number=1).model_dump()],
                }
            )
        )
        store.attach_channel_data_points(
            device_address="VCU0001",
            channel_number=1,
            data_points=[_dp_summary(parameter="LEVEL")],
        )
        # Re-attach with a different set; LEVEL should be gone.
        store.attach_channel_data_points(
            device_address="VCU0001",
            channel_number=1,
            data_points=[_dp_summary(parameter="STATE")],
        )
        assert store.get_data_point(address="VCU0001", channel=1, parameter="LEVEL") is None
        assert store.get_data_point(address="VCU0001", channel=1, parameter="STATE") is not None


# ---- live updates ----


class TestLiveUpdates:
    @pytest.fixture
    def populated(self) -> LoomStore:
        store = LoomStore()
        store.load_snapshot(snapshot=_snapshot(devices=[_device_summary()]))
        store.attach_device_detail(
            detail=DeviceDetail.model_validate(
                {
                    **_device_summary().model_dump(),
                    "channels": [_channel_summary(address="VCU0001", number=1).model_dump()],
                }
            )
        )
        store.attach_channel_data_points(
            device_address="VCU0001",
            channel_number=1,
            data_points=[_dp_summary(parameter="LEVEL", value=0.0)],
        )
        return store

    def test_apply_value_changed_updates_dp(self, populated: LoomStore) -> None:
        payload = DataPointValueChangedPayload.model_validate(
            {
                "central": "home",
                "device_address": "VCU0001",
                "channel": 1,
                "parameter": "LEVEL",
                "paramset_key": "VALUES",
                "value": 0.8,
                "modified_at": "2026-05-24T08:42:13Z",
            }
        )
        populated.apply_value_changed(payload=payload)
        dp = populated.get_data_point(address="VCU0001", channel=1, parameter="LEVEL")
        assert dp is not None
        assert dp.value == 0.8
        assert dp.is_observed is True

    def test_apply_value_changed_for_unknown_dp_is_noop(self, populated: LoomStore, caplog) -> None:
        payload = DataPointValueChangedPayload.model_validate(
            {
                "central": "home",
                "device_address": "VCU9999",
                "channel": 1,
                "parameter": "NEVER_HEARD_OF",
                "paramset_key": "VALUES",
                "value": 1.0,
                "modified_at": "2026-05-24T08:42:13Z",
            }
        )
        populated.apply_value_changed(payload=payload)
        # No crash, no addition.
        assert populated.get_device(address="VCU9999") is None

    def test_apply_device_created_seeds_stub(self) -> None:
        store = LoomStore()
        payload = DeviceCreatedPayload.model_validate(
            {
                "central": "home",
                "interface_id": "home:HmIP-RF",
                "device_address": "VCU_NEW",
                "model": "HmIP-eTRV-2",
            }
        )
        store.apply_device_created(payload=payload)
        new_dev = store.get_device(address="VCU_NEW")
        assert new_dev is not None
        assert new_dev.model == "HmIP-eTRV-2"
        # Channels still unknown until detail arrives.
        assert list(new_dev.channels) == []

    def test_apply_device_created_is_idempotent(self) -> None:
        store = LoomStore()
        store.load_snapshot(snapshot=_snapshot(devices=[_device_summary(name="original")]))
        payload = DeviceCreatedPayload.model_validate(
            {
                "central": "home",
                "interface_id": "home:HmIP-RF",
                "device_address": "VCU0001",
                "model": "HmIP-PSM",
            }
        )
        # Existing device → no overwrite (snapshot has the canonical info).
        store.apply_device_created(payload=payload)
        assert store.get_device(address="VCU0001").name == "original"

    def test_apply_device_removed_clears_device_and_children(self, populated: LoomStore) -> None:
        payload = DeviceRemovedPayload.model_validate(
            {
                "central": "home",
                "interface_id": "home:HmIP-RF",
                "device_address": "VCU0001",
            }
        )
        populated.apply_device_removed(payload=payload)
        assert populated.get_device(address="VCU0001") is None
        assert populated.get_channel(address="VCU0001", number=1) is None
        assert populated.get_data_point(address="VCU0001", channel=1, parameter="LEVEL") is None


# ---- write-back ----


class TestSetValue:
    async def test_send_value_round_trips_to_transport(self) -> None:
        transport = _FakeTransport()
        store = LoomStore(transport=transport)  # type: ignore[arg-type]
        store.load_snapshot(snapshot=_snapshot(devices=[_device_summary()]))
        store.attach_device_detail(
            detail=DeviceDetail.model_validate(
                {
                    **_device_summary().model_dump(),
                    "channels": [_channel_summary(address="VCU0001", number=1).model_dump()],
                }
            )
        )
        store.attach_channel_data_points(
            device_address="VCU0001",
            channel_number=1,
            data_points=[_dp_summary(parameter="STATE", value=False)],
        )
        dp = store.get_data_point(address="VCU0001", channel=1, parameter="STATE")
        assert dp is not None
        await dp.send_value(value=True)

        assert len(transport.calls) == 1
        method, path, body = transport.calls[0]
        assert method == "PUT"
        assert path == "/devices/VCU0001/channels/1/data-points/STATE/value"
        assert body == {"value": True}

    async def test_send_value_passes_priority(self) -> None:
        transport = _FakeTransport()
        store = LoomStore(transport=transport)  # type: ignore[arg-type]
        store.load_snapshot(snapshot=_snapshot(devices=[_device_summary()]))
        store.attach_device_detail(
            detail=DeviceDetail.model_validate(
                {
                    **_device_summary().model_dump(),
                    "channels": [_channel_summary(address="VCU0001", number=1).model_dump()],
                }
            )
        )
        store.attach_channel_data_points(
            device_address="VCU0001",
            channel_number=1,
            data_points=[_dp_summary(parameter="LEVEL")],
        )
        dp = store.get_data_point(address="VCU0001", channel=1, parameter="LEVEL")
        assert dp is not None
        await dp.send_value(value=0.5, priority="high")
        body = transport.calls[0][2]
        assert body == {"value": 0.5, "priority": "high"}

    async def test_set_value_without_transport_raises(self) -> None:
        store = LoomStore()
        with pytest.raises(RuntimeError, match="no transport bound"):
            await store.set_value(address="X", channel=1, parameter="P", value=1)


# ---- domain navigation ----


class TestModelNavigation:
    def test_channel_resolves_back_to_device(self) -> None:
        store = LoomStore()
        store.load_snapshot(snapshot=_snapshot(devices=[_device_summary()]))
        store.attach_device_detail(
            detail=DeviceDetail.model_validate(
                {
                    **_device_summary().model_dump(),
                    "channels": [_channel_summary(address="VCU0001", number=1).model_dump()],
                }
            )
        )
        channel = store.get_channel(address="VCU0001", number=1)
        assert channel is not None
        assert channel.device is store.get_device(address="VCU0001")

    def test_data_point_resolves_back_to_channel_and_device(self) -> None:
        store = LoomStore()
        store.load_snapshot(snapshot=_snapshot(devices=[_device_summary()]))
        store.attach_device_detail(
            detail=DeviceDetail.model_validate(
                {
                    **_device_summary().model_dump(),
                    "channels": [_channel_summary(address="VCU0001", number=1).model_dump()],
                }
            )
        )
        store.attach_channel_data_points(
            device_address="VCU0001",
            channel_number=1,
            data_points=[_dp_summary(parameter="LEVEL")],
        )
        dp = store.get_data_point(address="VCU0001", channel=1, parameter="LEVEL")
        assert dp is not None
        assert dp.channel is store.get_channel(address="VCU0001", number=1)
        assert dp.device is store.get_device(address="VCU0001")
