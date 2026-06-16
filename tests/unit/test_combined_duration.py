# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Combined duration data point (DURATION_VALUE + DURATION_UNIT)."""

from __future__ import annotations

from typing import Any

from openccu_loom_types.rest import DataPointSummary, DeviceSummary

from openccu_loom_client.compat.aiohomematic.model.combined import CombinedDurationDp, channel_has_duration_pair
from openccu_loom_client.model import Device
from openccu_loom_client.store import LoomStore

_ADDRESS = "VCU0000001"
_CHANNEL = 3


class _FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []

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


def _dp_summary(*, parameter: str, value: Any, type_: str = "FLOAT") -> DataPointSummary:
    return DataPointSummary.model_validate(
        {
            "parameter": parameter,
            "type": type_,
            "value": value,
            "observed": True,
            "min": 0,
            "max": 60,
            "operations": {"read": True, "write": True, "event": True},
        }
    )


def _store_with_duration_pair(*, unit_value: Any = 1) -> tuple[LoomStore, Device, _FakeTransport]:
    transport = _FakeTransport()
    store = LoomStore(transport=transport)  # type: ignore[arg-type]
    store.set_serial(serial="ABC1234567")
    device = store._upsert_device_summary(
        summary=DeviceSummary.model_validate(
            {
                "address": _ADDRESS,
                "interface": "HmIP-RF",
                "model": "HmIP-MOD-HO",
                "name": "Garage",
                "available": True,
                "channels_count": 3,
            }
        )
    )
    store.attach_channel_data_points(
        device_address=_ADDRESS,
        channel_number=_CHANNEL,
        data_points=[
            _dp_summary(parameter="DURATION_VALUE", value=1.0),
            _dp_summary(parameter="DURATION_UNIT", value=unit_value, type_="ENUM"),
        ],
    )
    return store, device, transport


class TestCombinedDurationDp:
    def test_unique_id(self) -> None:
        store, device, _transport = _store_with_duration_pair()
        dp = CombinedDurationDp(store=store, device=device, channel_no=_CHANNEL)
        assert dp.unique_id == "loom_combined_vcu0000001_3_duration"

    def test_value_converts_minutes_to_seconds(self) -> None:
        store, device, _transport = _store_with_duration_pair(unit_value=1)
        dp = CombinedDurationDp(store=store, device=device, channel_no=_CHANNEL)
        # 1 minute → 60 seconds.
        assert dp.value == 60.0
        assert dp.is_valid is True
        assert dp.unit == "s"
        assert dp.min == 0
        assert dp.max == 60

    def test_value_unit_factors(self) -> None:
        for unit_value, expected in ((0, 1.0), (1, 60.0), (2, 3600.0)):
            store, device, _transport = _store_with_duration_pair(unit_value=unit_value)
            dp = CombinedDurationDp(store=store, device=device, channel_no=_CHANNEL)
            assert dp.value == expected

    async def test_send_value_writes_unit_then_value(self) -> None:
        store, device, transport = _store_with_duration_pair()
        dp = CombinedDurationDp(store=store, device=device, channel_no=_CHANNEL)
        await dp.send_value(value=90.0)
        base = f"/devices/{_ADDRESS}/channels/{_CHANNEL}/data-points"
        assert transport.calls == [
            ("PUT", f"{base}/DURATION_UNIT/value", {"value": 0}),
            ("PUT", f"{base}/DURATION_VALUE/value", {"value": 90}),
        ]

    def test_channel_has_duration_pair(self) -> None:
        store, _device, _transport = _store_with_duration_pair()
        assert channel_has_duration_pair(store=store, address=_ADDRESS, channel_no=_CHANNEL) is True
        assert channel_has_duration_pair(store=store, address=_ADDRESS, channel_no=1) is False
