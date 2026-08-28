# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Device-icon wiring for HA's config-panel icon proxy.

HA's ``ws_list_devices`` calls ``get_configurable_devices()`` and then,
per device, ``ccu_translations.get_device_icon(model=d.model)`` — the
filename it returns is proxied from the CCU's ``/config/img/devices/250``
path. The daemon resolves the model→icon mapping server-side and ships
the bare PNG filename on ``DeviceSummary.model_icon``; these tests pin
that the client surfaces it (``Device.icon``) and folds it into the
process-wide lookup whenever the configurable-device list is built.
"""

from __future__ import annotations

from collections.abc import Iterator

from openccu_loom_types.rest import ChannelSummary, DeviceDetail, DeviceSummary, Snapshot
import pytest

from openccu_loom_client.compat.aiohomematic import ccu_translations
from openccu_loom_client.compat.aiohomematic.central.configurable_devices import build_configurable_devices
from openccu_loom_client.store import LoomStore


@pytest.fixture(autouse=True)
def _isolate_icon_registry() -> Iterator[None]:
    """Reset the process-wide icon lookup around each test."""
    ccu_translations.clear_device_icons()
    yield
    ccu_translations.clear_device_icons()


def _device(
    *, address: str = "VCU0001", model: str = "HmIP-eTRV-2", model_icon: str | None = "hmip-etrv.png"
) -> DeviceSummary:
    body: dict[str, object] = {
        "address": address,
        "interface": "home:HmIP-RF",
        "interface_id": "home:HmIP-RF",
        "model": model,
        "name": "Thermostat",
        "available": True,
        "channels_count": 1,
        "updatable": False,
        "update_available": False,
        "master_pushes_config_pending": False,
        "has_sub_devices": False,
        "firmware": {"Current": "1.0.0", "Available": "", "Updatable": False, "UpdateState": "UP_TO_DATE"},
        "availability": {
            "IsReachable": True,
            "LastUpdated": None,
            "BatteryLevel": None,
            "LowBattery": None,
            "SignalStrength": None,
        },
    }
    if model_icon is not None:
        body["model_icon"] = model_icon
    return DeviceSummary.model_validate(body)


def _store_with(*summaries: DeviceSummary) -> LoomStore:
    store = LoomStore()
    store.load_snapshot(
        snapshot=Snapshot.model_validate(
            {"generated_at": "2026-06-12T08:00:00Z", "devices": [s.model_dump() for s in summaries]}
        )
    )
    for summary in summaries:
        channel = ChannelSummary.model_validate(
            {
                "address": f"{summary.address}:1",
                "number": 1,
                "paramset_key": "VALUES",
                "paramset_keys": ["VALUES"],
                "data_points_count": 1,
            }
        )
        detail = DeviceDetail.model_validate(
            {**summary.model_dump(), "firmware": {}, "availability": {}, "channels": [channel.model_dump()]}
        )
        store.attach_device_detail(detail=detail)
    return store


class TestDeviceIconProperty:
    def test_reads_model_icon(self) -> None:
        store = _store_with(_device(model_icon="hmip-etrv.png"))
        device = store.get_device(address="VCU0001")
        assert device is not None
        assert device.icon == "hmip-etrv.png"

    def test_empty_icon_is_none(self) -> None:
        # The daemon sends an empty string when no artwork is known.
        store = _store_with(_device(model_icon=""))
        device = store.get_device(address="VCU0001")
        assert device is not None
        assert device.icon is None

    def test_missing_icon_is_none(self) -> None:
        store = _store_with(_device(model_icon=None))
        device = store.get_device(address="VCU0001")
        assert device is not None
        assert device.icon is None


class TestGetDeviceIcon:
    def test_unknown_model_is_none(self) -> None:
        assert ccu_translations.get_device_icon(model="HmIP-eTRV-2") is None

    def test_register_then_lookup(self) -> None:
        store = _store_with(_device(model="HmIP-eTRV-2", model_icon="hmip-etrv.png"))
        ccu_translations.register_device_icons(devices=store.devices)
        assert ccu_translations.get_device_icon(model="HmIP-eTRV-2") == "hmip-etrv.png"

    def test_lookup_is_case_insensitive(self) -> None:
        store = _store_with(_device(model="HmIP-eTRV-2", model_icon="hmip-etrv.png"))
        ccu_translations.register_device_icons(devices=store.devices)
        # HA may pass the model in any casing; aiohomematic lower-cases the key.
        assert ccu_translations.get_device_icon(model="HMIP-ETRV-2") == "hmip-etrv.png"
        assert ccu_translations.get_device_icon(model="hmip-etrv-2") == "hmip-etrv.png"

    def test_models_without_artwork_are_skipped(self) -> None:
        store = _store_with(_device(model="NO-ART", model_icon=""))
        ccu_translations.register_device_icons(devices=store.devices)
        assert ccu_translations.get_device_icon(model="NO-ART") is None


class TestBuildConfigurableDevicesWiring:
    def test_listing_devices_refreshes_icon_lookup(self) -> None:
        # The exact flow HA drives: build the configurable list, then ask
        # for each model's icon.
        store = _store_with(
            _device(address="VCU0001", model="HmIP-eTRV-2", model_icon="hmip-etrv.png"),
            _device(address="VCU0002", model="HmIP-PSM", model_icon="hmip-psm.png"),
        )
        devices = build_configurable_devices(store=store)
        models = {d.model for d in devices}
        assert {"HmIP-eTRV-2", "HmIP-PSM"} <= models
        assert ccu_translations.get_device_icon(model="HmIP-eTRV-2") == "hmip-etrv.png"
        assert ccu_translations.get_device_icon(model="HmIP-PSM") == "hmip-psm.png"
