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

from openccu_loom_types.rest import (
    CalculatedDPSummary,
    ChannelSummary,
    CustomDPSummary,
    DataPointSummary,
    DeviceDetail,
    DeviceSummary,
    Operations,
    Snapshot,
)
from openccu_loom_types.ws import (
    CustomDataPointStateChangedPayload,
    DataPointValueChangedPayload,
    DeviceCreatedPayload,
    DeviceRemovedPayload,
)
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
            "updatable": False,
            "update_available": False,
            "master_pushes_config_pending": False,
            "has_sub_devices": False,
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
            "unique_id": f"loom_test_{parameter.lower()}",
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
                "firmware": {},
                "availability": {},
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
                "firmware": {},
                "availability": {},
                "channels": [_channel_summary(address="VCU0001", number=n).model_dump() for n in (1, 2, 3)],
            }
        )
        store.attach_device_detail(detail=first)
        # Second detail: channel 2 vanished.
        second = DeviceDetail.model_validate(
            {
                **_device_summary().model_dump(),
                "firmware": {},
                "availability": {},
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
                    "firmware": {},
                    "availability": {},
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
                    "firmware": {},
                    "availability": {},
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
                    "firmware": {},
                    "availability": {},
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
                "unique_id": "loom_test_vcu0001_1_level",
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
                "unique_id": "loom_test_vcu9999_1_never_heard_of",
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

    def test_device_created_is_capped_against_unbounded_growth(self) -> None:
        # F2: a hostile daemon streaming unique addresses must not grow the
        # store without bound. New addresses past the cap are refused.
        store = LoomStore(max_devices=3)
        for i in range(10):
            store.apply_device_created(
                payload=DeviceCreatedPayload.model_validate(
                    {
                        "central": "home",
                        "interface_id": "home:HmIP-RF",
                        "device_address": f"VCU_FLOOD_{i}",
                        "model": "HmIP-PSM",
                    }
                )
            )
        assert len(list(store.devices)) == 3
        # Below-cap addresses were admitted; over-cap ones were dropped.
        assert store.get_device(address="VCU_FLOOD_0") is not None
        assert store.get_device(address="VCU_FLOOD_9") is None

    def test_snapshot_load_is_capped(self) -> None:
        # The same cap guards the bootstrap path, not just live pushes.
        store = LoomStore(max_devices=2)
        store.load_snapshot(snapshot=_snapshot(devices=[_device_summary(address=f"VCU{i:04d}") for i in range(5)]))
        assert len(list(store.devices)) == 2

    def test_known_device_still_updates_at_cap(self) -> None:
        # An update to an already-known address must never be refused, even
        # when the store sits exactly at the cap.
        store = LoomStore(max_devices=1)
        store.load_snapshot(snapshot=_snapshot(devices=[_device_summary(name="original")]))
        store.apply_device_created(
            payload=DeviceCreatedPayload.model_validate(
                {
                    "central": "home",
                    "interface_id": "home:HmIP-RF",
                    "device_address": "VCU0001",
                    "model": "HmIP-PSM",
                }
            )
        )
        assert store.get_device(address="VCU0001") is not None

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


class _PayloadTransport:
    """Returns a settable payload from every request (for refresh tests)."""

    def __init__(self, *, payload: Any) -> None:
        self.payload = payload

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
        return self.payload


def _dp_payload(*, parameter: str, value: Any, modified_at: str) -> dict[str, Any]:
    return {
        "parameter": parameter,
        "value": value,
        "observed": True,
        "operations": Operations(read=True, write=True, event=True).model_dump(),
        "modified_at": modified_at,
        "unique_id": f"loom_test_{parameter.lower()}",
    }


class TestRefreshStaleGuard:
    """B8: a refresh_* GET must not clobber a newer value from a live push."""

    def _store_with_value(self, *, transport: _PayloadTransport, value: Any, modified_at: str) -> LoomStore:
        store = LoomStore(transport=transport)  # type: ignore[arg-type]
        store.load_snapshot(snapshot=_snapshot(devices=[_device_summary()]))
        store.attach_device_detail(
            detail=DeviceDetail.model_validate(
                {
                    **_device_summary().model_dump(),
                    "firmware": {},
                    "availability": {},
                    "channels": [_channel_summary(address="VCU0001", number=1).model_dump()],
                }
            )
        )
        store.attach_channel_data_points(
            device_address="VCU0001",
            channel_number=1,
            data_points=[_dp_summary(parameter="STATE", value=value)],
        )
        # Seed the in-store modified_at via a live push.
        store.apply_value_changed(
            payload=DataPointValueChangedPayload.model_validate(
                {
                    "central": "home",
                    "device_address": "VCU0001",
                    "channel": 1,
                    "parameter": "STATE",
                    "paramset_key": "VALUES",
                    "value": value,
                    "modified_at": modified_at,
                    "unique_id": "loom_test_vcu0001_1_state",
                }
            )
        )
        return store

    async def test_older_refresh_is_ignored(self) -> None:
        # In-store value is from 10:00; the REST refresh carries an older 09:00 value.
        transport = _PayloadTransport(
            payload=_dp_payload(parameter="STATE", value=False, modified_at="2026-05-24T09:00:00Z")
        )
        store = self._store_with_value(transport=transport, value=True, modified_at="2026-05-24T10:00:00Z")

        await store.refresh_data_point(address="VCU0001", channel=1, parameter="STATE")

        dp = store.get_data_point(address="VCU0001", channel=1, parameter="STATE")
        assert dp is not None
        assert dp.value is True  # stale refresh did NOT overwrite the newer push

    async def test_newer_refresh_is_applied(self) -> None:
        # In-store value is from 10:00; the REST refresh carries a newer 11:00 value.
        transport = _PayloadTransport(
            payload=_dp_payload(parameter="STATE", value=False, modified_at="2026-05-24T11:00:00Z")
        )
        store = self._store_with_value(transport=transport, value=True, modified_at="2026-05-24T10:00:00Z")

        await store.refresh_data_point(address="VCU0001", channel=1, parameter="STATE")

        dp = store.get_data_point(address="VCU0001", channel=1, parameter="STATE")
        assert dp is not None
        assert dp.value is False  # newer refresh wins


class TestInPlaceUpsertHardening:
    """
    Re-attaching a catalogue must update live wrappers in place, never rebuild.

    A rebuild orphans the reference a consumer already holds — after a
    replay-lost re-bootstrap that silently freezes every entity.
    """

    def _detail(self) -> DeviceDetail:
        return DeviceDetail.model_validate(
            {
                **_device_summary().model_dump(),
                "firmware": {},
                "availability": {},
                "channels": [_channel_summary(address="VCU0001", number=1).model_dump()],
            }
        )

    def _populated(self) -> LoomStore:
        store = LoomStore()
        store.load_snapshot(snapshot=_snapshot(devices=[_device_summary()]))
        store.attach_device_detail(detail=self._detail())
        store.attach_channel_data_points(
            device_address="VCU0001",
            channel_number=1,
            data_points=[_dp_summary(parameter="LEVEL", value=0.5)],
        )
        return store

    def test_surviving_dp_keeps_identity_and_updates_value(self) -> None:
        store = self._populated()
        original = store.get_data_point(address="VCU0001", channel=1, parameter="LEVEL")
        assert original is not None

        store.attach_channel_data_points(
            device_address="VCU0001",
            channel_number=1,
            data_points=[_dp_summary(parameter="LEVEL", value=0.9)],
        )

        after = store.get_data_point(address="VCU0001", channel=1, parameter="LEVEL")
        assert after is original  # same live instance — not rebuilt
        assert after.value == 0.9  # summary updated in place

    def test_surviving_channel_keeps_identity(self) -> None:
        store = self._populated()
        original = store.get_channel(address="VCU0001", number=1)
        assert original is not None

        store.attach_device_detail(detail=self._detail())

        assert store.get_channel(address="VCU0001", number=1) is original

    def test_reattach_does_not_drop_calculated_dps(self) -> None:
        store = self._populated()

        def _calc_factory(*, summary: Any, device_address: str, channel_number: int, store: LoomStore) -> Any:
            return object()

        store.set_calculated_data_point_factory(factory=_calc_factory)
        store.attach_channel_calculated_data_points(
            device_address="VCU0001",
            channel_number=1,
            calculated=[
                CalculatedDPSummary.model_validate(
                    {
                        "name": "OPERATING_VOLTAGE",
                        "value": 3.0,
                        "observed": True,
                        "available": True,
                        "unique_id": "loom_calc_v",
                    }
                )
            ],
        )
        calc = store.get_data_point(address="VCU0001", channel=1, parameter="OPERATING_VOLTAGE")
        assert calc is not None

        # A replay-lost re-bootstrap re-runs the generic per-channel attach.
        store.attach_channel_data_points(
            device_address="VCU0001",
            channel_number=1,
            data_points=[_dp_summary(parameter="LEVEL", value=0.5)],
        )

        # The calculated DP (attached by a different path) must survive.
        assert store.get_data_point(address="VCU0001", channel=1, parameter="OPERATING_VOLTAGE") is calc

    def test_device_removal_purges_week_profile_dp(self) -> None:
        store = self._populated()
        store.set_week_profile_data_point(address="VCU0001", data_point=object())
        assert store.get_week_profile_data_point(address="VCU0001") is not None

        store.apply_device_removed(
            payload=DeviceRemovedPayload.model_validate(
                {"central": "home", "interface_id": "home:HmIP-RF", "device_address": "VCU0001"}
            )
        )

        assert store.get_week_profile_data_point(address="VCU0001") is None


class _RacingCdpTransport:
    """On the CDP GET, fires a live push (bumping the generation) before returning a now-stale payload."""

    def __init__(self, *, store: LoomStore, stale_state: dict[str, Any], pushed_state: dict[str, Any]) -> None:
        self._store = store
        self._stale_state = stale_state
        self._pushed_state = pushed_state

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
        # Simulate a state_changed push landing while the GET is in flight.
        self._store.apply_custom_data_point_state_changed(
            payload=CustomDataPointStateChangedPayload.model_validate(
                {
                    "central": "home",
                    "device_address": "VCU0001",
                    "channel": 1,
                    "name": "SWITCH",
                    "unique_id": "loom_test_switch_1",
                    "state": self._pushed_state,
                }
            )
        )
        return {"state": self._stale_state}


class TestCdpRefreshStaleGuard:
    """A CDP refresh GET must not clobber a newer state applied by a push mid-flight."""

    async def test_push_during_refresh_wins(self) -> None:
        store = LoomStore()
        store.load_snapshot(snapshot=_snapshot(devices=[_device_summary()]))
        store.attach_custom_data_points(
            device_address="VCU0001",
            cdps=[_cdp_summary(name="SWITCH", channel_no=1)],
        )
        transport = _RacingCdpTransport(
            store=store,
            stale_state={"value": False},
            pushed_state={"value": True},
        )
        store.set_transport(transport=transport)  # type: ignore[arg-type]

        await store.refresh_custom_data_point(address="VCU0001", name="SWITCH")

        cdp = store.get_custom_data_point(address="VCU0001", name="SWITCH")
        assert cdp is not None
        assert cdp.state == {"value": True}  # the pushed state survived; the stale GET was dropped


class _CalcRefreshTransport:
    """Serves one calc-dps record so the refresh path can be driven directly."""

    def __init__(self, *, record: dict[str, Any]) -> None:
        self.record = record

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
        return self.record


class TestCalculatedRefreshCarriesAvailability:
    """A calc-dps re-read is where the client learns the daemon's verdict."""

    async def test_refresh_applies_availability(self) -> None:
        from openccu_loom_client.compat.aiohomematic.model.calculated import make_calculated_data_point

        store = LoomStore()
        store.load_snapshot(snapshot=_snapshot(devices=[_device_summary()]))
        store.set_calculated_data_point_factory(factory=make_calculated_data_point)
        store.attach_channel_calculated_data_points(
            device_address="VCU0001",
            channel_number=1,
            calculated=[
                CalculatedDPSummary.model_validate(
                    {
                        "name": "DEW_POINT",
                        "category": "sensor",
                        "value": 9.3,
                        "observed": True,
                        "available": True,
                        "unique_id": "loom_calc_dew",
                    }
                )
            ],
        )
        dp = store.get_data_point(address="VCU0001", channel=1, parameter="DEW_POINT")
        assert dp is not None
        assert dp.is_valid is True

        # A source went bad: the daemon keeps computing the value, only the
        # verdict flips — and the value's timestamp does not advance, because
        # a status fault is not a value change.
        store.set_transport(  # type: ignore[arg-type]
            transport=_CalcRefreshTransport(
                record={
                    "name": "DEW_POINT",
                    "category": "sensor",
                    "value": 9.3,
                    "observed": True,
                    "available": False,
                    "unique_id": "loom_calc_dew",
                }
            )
        )
        await store.refresh_calculated_data_point(address="VCU0001", channel=1, name="DEW_POINT")

        assert dp.value == 9.3
        assert dp.is_valid is False


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
                    "firmware": {},
                    "availability": {},
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
                    "firmware": {},
                    "availability": {},
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
                    "firmware": {},
                    "availability": {},
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
                    "firmware": {},
                    "availability": {},
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


class TestGetChannelByAddress:
    """Canonical ``"ADDR:idx"`` lookup — feeds the sysvar/program device link."""

    def _store_with_channel(self) -> LoomStore:
        store = LoomStore()
        store.load_snapshot(snapshot=_snapshot(devices=[_device_summary()]))
        store.attach_device_detail(
            detail=DeviceDetail.model_validate(
                {
                    **_device_summary().model_dump(),
                    "firmware": {},
                    "availability": {},
                    "channels": [_channel_summary(address="VCU0001", number=1).model_dump()],
                }
            )
        )
        return store

    def test_resolves_canonical_address(self) -> None:
        store = self._store_with_channel()
        channel = store.get_channel_by_address(channel_address="VCU0001:1")
        assert channel is store.get_channel(address="VCU0001", number=1)

    def test_unknown_channel_yields_none(self) -> None:
        store = self._store_with_channel()
        assert store.get_channel_by_address(channel_address="VCU0001:9") is None
        assert store.get_channel_by_address(channel_address="GHOST:1") is None

    def test_malformed_address_yields_none(self) -> None:
        store = self._store_with_channel()
        # Bare device address (no channel separator) and a non-numeric
        # index must degrade to None, never raise.
        assert store.get_channel_by_address(channel_address="VCU0001") is None
        assert store.get_channel_by_address(channel_address="VCU0001:x") is None
        assert store.get_channel_by_address(channel_address="") is None


def _cdp_summary(*, name: str, channel_no: int) -> CustomDPSummary:
    return CustomDPSummary.model_validate(
        {
            "name": name,
            "category": "switch",
            "channel_no": channel_no,
            "supported_operations": ["turn_on", "turn_off"],
            "unique_id": f"loom_test_{name.lower()}_{channel_no}",
        }
    )


class TestCustomDataPointChannelIndex:
    """N3: get_custom_data_point_by_channel is an O(1) index kept in lock-step."""

    def _store(self) -> LoomStore:
        store = LoomStore()
        store.attach_custom_data_points(
            device_address="VCU0001",
            cdps=[_cdp_summary(name="Switch1", channel_no=1), _cdp_summary(name="Switch3", channel_no=3)],
        )
        return store

    def test_lookup_hit_and_miss(self) -> None:
        store = self._store()
        cdp1 = store.get_custom_data_point_by_channel(address="VCU0001", channel_no=1)
        cdp3 = store.get_custom_data_point_by_channel(address="VCU0001", channel_no=3)
        assert cdp1 is not None and cdp1.summary.channel_no == 1
        assert cdp3 is not None and cdp3.summary.channel_no == 3
        # Miss: unknown channel, unknown device.
        assert store.get_custom_data_point_by_channel(address="VCU0001", channel_no=2) is None
        assert store.get_custom_data_point_by_channel(address="VCU9999", channel_no=1) is None

    def test_reattach_drops_stale_index_entries(self) -> None:
        store = self._store()
        # Re-attach a smaller catalogue (channel 3 gone) — its index entry must clear.
        store.attach_custom_data_points(device_address="VCU0001", cdps=[_cdp_summary(name="Switch1", channel_no=1)])
        assert store.get_custom_data_point_by_channel(address="VCU0001", channel_no=1) is not None
        assert store.get_custom_data_point_by_channel(address="VCU0001", channel_no=3) is None

    def test_device_removal_clears_index(self) -> None:
        store = self._store()
        store.apply_device_removed(
            payload=DeviceRemovedPayload.model_validate(
                {"central": "home", "interface_id": "home:HmIP-RF", "device_address": "VCU0001"}
            )
        )
        assert store.get_custom_data_point_by_channel(address="VCU0001", channel_no=1) is None
        assert store.get_custom_data_point_by_channel(address="VCU0001", channel_no=3) is None


class TestAiohomematicModelCompat:
    """
    Device/Channel members `homematicip_local`'s config + link handlers read.

    The handlers address channels by full ``"<device>:<channel>"`` address and
    read ``channel.type_name`` / ``device.sub_model`` (aiohomematic's spelling)
    when building the config form and the link views.
    """

    @staticmethod
    def _store_with_channel() -> LoomStore:
        store = LoomStore()
        store.set_serial(serial="ABC1234567")
        store.set_central_name(central_name="home")
        store.attach_device_detail(
            detail=DeviceDetail.model_validate(
                {
                    "address": "VCU1",
                    "interface": "HmIP-RF",
                    "interface_id": "home-HmIP-RF",
                    "model": "HmIP-PS",
                    "sub_model": "VARIANT-B",
                    "name": "Lamp",
                    "available": True,
                    "channels_count": 1,
                    "updatable": False,
                    "update_available": False,
                    "master_pushes_config_pending": False,
                    "has_sub_devices": False,
                    "firmware": {},
                    "availability": {},
                    "channels": [
                        {
                            "address": "VCU1:3",
                            "number": 3,
                            "name": "Ch3",
                            "type": "SWITCH_VIRTUAL_RECEIVER",
                            "type_label": "Switch",
                            "paramset_key": "VALUES",
                            "paramset_keys": ["VALUES", "MASTER"],
                            "data_points_count": 0,
                            "is_group_master": False,
                            "is_in_multi_group": False,
                            "is_custom_dp_primary": False,
                            "data_points": [],
                        }
                    ],
                }
            )
        )
        return store

    def test_get_channel_by_channel_address(self) -> None:
        device = self._store_with_channel().get_device(address="VCU1")
        assert device is not None
        channel = device.get_channel(channel_address="VCU1:3")
        assert channel is not None
        assert channel.address == "VCU1:3"

    def test_get_channel_by_number_still_works(self) -> None:
        """The loom-internal callers pass `number` — it must keep resolving."""
        device = self._store_with_channel().get_device(address="VCU1")
        assert device is not None
        channel = device.get_channel(number=3)
        assert channel is not None
        assert channel.address == "VCU1:3"

    def test_foreign_or_malformed_channel_address_is_none(self) -> None:
        device = self._store_with_channel().get_device(address="VCU1")
        assert device is not None
        assert device.get_channel(channel_address="OTHER:3") is None
        assert device.get_channel(channel_address="VCU1:xx") is None
        assert device.get_channel() is None

    def test_type_name_and_sub_model(self) -> None:
        device = self._store_with_channel().get_device(address="VCU1")
        assert device is not None
        assert device.sub_model == "VARIANT-B"
        channel = device.get_channel(channel_address="VCU1:3")
        assert channel is not None
        assert channel.type_name == "SWITCH_VIRTUAL_RECEIVER"


class TestCcuDashboardDeviceSurface:
    """Device members the CCU dashboard's firmware / signal-quality / statistics views read."""

    @staticmethod
    def _store_with_rssi(*, updatable: bool = True) -> LoomStore:
        store = LoomStore()
        store.set_serial(serial="ABC1234567")
        store.set_central_name(central_name="home")
        store.attach_device_detail(
            detail=DeviceDetail.model_validate(
                {
                    "address": "VCU1",
                    "interface": "HmIP-RF",
                    "interface_id": "home-HmIP-RF",
                    "model": "HmIP-PS",
                    "name": "Lamp",
                    "available": True,
                    "channels_count": 1,
                    "updatable": True,
                    "update_available": True,
                    "master_pushes_config_pending": False,
                    "has_sub_devices": False,
                    "firmware": {
                        "Current": "1.0",
                        "Available": "1.2",
                        "Updatable": updatable,
                        "UpdateState": "AVAILABLE",
                    },
                    "availability": {},
                    "channels": [
                        {
                            "address": "VCU1:0",
                            "number": 0,
                            "name": "MAINT",
                            "type": "MAINTENANCE",
                            "type_label": "M",
                            "paramset_key": "VALUES",
                            "paramset_keys": ["VALUES"],
                            "data_points_count": 1,
                            "is_group_master": False,
                            "is_in_multi_group": False,
                            "is_custom_dp_primary": False,
                            "data_points": [],
                        }
                    ],
                }
            )
        )
        store.attach_channel_data_points(
            device_address="VCU1",
            channel_number=0,
            data_points=[
                DataPointSummary.model_validate(
                    {
                        "parameter": "RSSI_DEVICE",
                        "type": "INTEGER",
                        "value": -63,
                        "observed": True,
                        "operations": {"read": True, "write": False, "event": True},
                        "unique_id": "loom_x_rssi",
                    }
                )
            ],
        )
        return store

    def test_firmware_updatable(self) -> None:
        device = self._store_with_rssi().get_device(address="VCU1")
        assert device is not None
        assert device.firmware_updatable is True
        not_updatable = self._store_with_rssi(updatable=False).get_device(address="VCU1")
        assert not_updatable is not None
        assert not_updatable.firmware_updatable is False

    def test_get_generic_data_point_finds_rssi_across_channels(self) -> None:
        """The signal-quality view looks the RSSI up by bare parameter."""
        from aiohomematic.const import Parameter

        device = self._store_with_rssi().get_device(address="VCU1")
        assert device is not None
        # The handler passes aiohomematic's Parameter StrEnum.
        data_point = device.get_generic_data_point(parameter=Parameter.RSSI_DEVICE)
        assert data_point is not None
        assert data_point.value == -63

    def test_get_generic_data_point_scoping_and_misses(self) -> None:
        device = self._store_with_rssi().get_device(address="VCU1")
        assert device is not None
        assert device.get_generic_data_point(channel_address="VCU1:0", parameter="RSSI_DEVICE") is not None
        assert device.get_generic_data_point(channel_address="VCU1:9", parameter="RSSI_DEVICE") is None
        assert device.get_generic_data_point(parameter="NOPE") is None
        assert device.get_generic_data_point() is None

    async def test_update_firmware_posts_and_returns_true(self) -> None:
        store = self._store_with_rssi()
        calls: list[dict[str, object]] = []

        class _Transport:
            async def request(self, **kwargs: object) -> None:
                calls.append(kwargs)

        store._transport = _Transport()  # type: ignore[assignment]
        device = store.get_device(address="VCU1")
        assert device is not None
        assert await device.update_firmware(refresh_after_update_intervals=(10, 60)) is True
        assert calls[0]["method"] == "POST"
        assert calls[0]["path"] == "/devices/VCU1/firmware/update"
        # Starting an OTA is never retried.
        assert calls[0]["allow_retry"] is False
