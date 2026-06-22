# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""CustomDataPoint domain model + CDP operations + store integration."""

from __future__ import annotations

from typing import Any

from openccu_loom_types.rest import CustomDPSummary
from openccu_loom_types.ws import CustomDataPointStateChangedPayload, DeviceRemovedPayload
import pytest

from openccu_loom_client.operations import CustomDataPointsOperations
from openccu_loom_client.store import LoomStore


def _cdp_summary(*, name: str = "main", kind: str = "switch") -> CustomDPSummary:
    return CustomDPSummary.model_validate(
        {
            "name": name,
            "category": "switch",
            "channel_no": 1,
            "supported_operations": ["turn_on", "turn_off"],
            "kind": kind,
            "channels": [1],
            "capabilities": {"on_off": True},
            "unique_id": f"loom_test_{name.lower()}",
        }
    )


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


class TestAttachAndQuery:
    def test_attach_replaces_per_device_cdps(self) -> None:
        store = LoomStore()
        store.attach_custom_data_points(
            device_address="VCU0001",
            cdps=[_cdp_summary(name="a"), _cdp_summary(name="b")],
        )
        assert {c.name for c in store.custom_data_points_of(address="VCU0001")} == {
            "a",
            "b",
        }
        # Re-attach with a different set — old entries vanish.
        store.attach_custom_data_points(device_address="VCU0001", cdps=[_cdp_summary(name="c")])
        names = {c.name for c in store.custom_data_points_of(address="VCU0001")}
        assert names == {"c"}

    def test_get_returns_none_for_unknown(self) -> None:
        store = LoomStore()
        assert store.get_custom_data_point(address="X", name="y") is None


class TestStateUpdates:
    def test_apply_state_changed_overwrites_state(self) -> None:
        store = LoomStore()
        store.attach_custom_data_points(device_address="VCU0001", cdps=[_cdp_summary(name="main")])
        cdp = store.get_custom_data_point(address="VCU0001", name="main")
        assert cdp is not None
        assert cdp.state == {}

        payload = CustomDataPointStateChangedPayload.model_validate(
            {
                "central": "home",
                "device_address": "VCU0001",
                "channel": 1,
                "name": "main",
                "kind": "switch",
                "state": {"on": True, "level": 1.0},
                "unique_id": "loom_test_vcu0001_1_main",
            }
        )
        store.apply_custom_data_point_state_changed(payload=payload)
        assert cdp.state == {"on": True, "level": 1.0}

        # Second push replaces the dict wholesale.
        payload2 = CustomDataPointStateChangedPayload.model_validate(
            {
                "central": "home",
                "device_address": "VCU0001",
                "channel": 1,
                "name": "main",
                "kind": "switch",
                "state": {"on": False},
                "unique_id": "loom_test_vcu0001_1_main",
            }
        )
        store.apply_custom_data_point_state_changed(payload=payload2)
        assert cdp.state == {"on": False}

    def test_state_property_is_defensive_copy(self) -> None:
        """Callers can't mutate the store's record through the returned dict."""
        store = LoomStore()
        store.attach_custom_data_points(device_address="VCU0001", cdps=[_cdp_summary()])
        store.apply_custom_data_point_state_changed(
            payload=CustomDataPointStateChangedPayload.model_validate(
                {
                    "central": "home",
                    "device_address": "VCU0001",
                    "channel": 1,
                    "name": "main",
                    "state": {"on": True},
                    "unique_id": "loom_test_vcu0001_1_main",
                }
            )
        )
        cdp = store.get_custom_data_point(address="VCU0001", name="main")
        assert cdp is not None
        cdp.state["mutated"] = "hacked"
        # The stored state is untouched.
        assert cdp.state == {"on": True}

    def test_apply_for_unknown_cdp_is_noop(self) -> None:
        store = LoomStore()
        store.apply_custom_data_point_state_changed(
            payload=CustomDataPointStateChangedPayload.model_validate(
                {
                    "central": "home",
                    "device_address": "UNKNOWN",
                    "channel": 1,
                    "name": "ghost",
                    "state": {},
                    "unique_id": "loom_test_unknown_1_ghost",
                }
            )
        )


class TestInvoke:
    async def test_invoke_round_trips_to_transport(self) -> None:
        transport = _FakeTransport()
        store = LoomStore(transport=transport)  # type: ignore[arg-type]
        store.attach_custom_data_points(device_address="VCU0001", cdps=[_cdp_summary(name="main")])
        cdp = store.get_custom_data_point(address="VCU0001", name="main")
        assert cdp is not None
        await cdp.invoke(operation="turn_on", params={"level": 0.5}, priority="high")

        assert len(transport.calls) == 1
        method, path, body = transport.calls[0]
        assert method == "POST"
        assert path == "/devices/VCU0001/cdps/main/turn_on"
        assert body == {"params": {"level": 0.5}, "priority": "high"}

    async def test_invoke_without_params(self) -> None:
        transport = _FakeTransport()
        store = LoomStore(transport=transport)  # type: ignore[arg-type]
        store.attach_custom_data_points(device_address="VCU0001", cdps=[_cdp_summary()])
        cdp = store.get_custom_data_point(address="VCU0001", name="main")
        assert cdp is not None
        await cdp.invoke(operation="turn_off")
        body = transport.calls[0][2]
        # No params + no priority still POSTs {} — the daemon parses the
        # body strictly and rejects an absent payload with 400.
        assert body == {}

    async def test_invoke_without_transport_raises(self) -> None:
        store = LoomStore()
        with pytest.raises(RuntimeError):
            await store.invoke_custom_data_point(address="X", name="y", operation="z")


class TestDeviceRemovalCleansUpCdps:
    def test_remove_device_drops_its_cdps(self) -> None:
        store = LoomStore()
        store.attach_custom_data_points(device_address="VCU0001", cdps=[_cdp_summary(name="main")])
        store.apply_device_removed(
            payload=DeviceRemovedPayload.model_validate(
                {
                    "central": "home",
                    "interface_id": "home:HmIP-RF",
                    "device_address": "VCU0001",
                }
            )
        )
        assert store.custom_data_points_of(address="VCU0001") == []


class TestCustomDataPointsOperations:
    async def test_list_for_device(self) -> None:
        transport = _FakeTransport()

        async def fake_request(
            method: str,
            path: str,
            *,
            params: Any = None,
            json_body: Any = None,
            headers: Any = None,
            allow_retry: Any = None,
        ) -> Any:
            transport.calls.append((method, path, json_body))
            return [_cdp_summary(name="a").model_dump()]

        transport.request = fake_request  # type: ignore[assignment]
        ops = CustomDataPointsOperations(transport=transport)  # type: ignore[arg-type]
        result = await ops.list_for_device(address="VCU0001")
        assert len(result) == 1
        assert result[0].name == "a"
        assert transport.calls[0] == ("GET", "/devices/VCU0001/cdps", None)
