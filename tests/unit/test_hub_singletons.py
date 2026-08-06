# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Hub singleton data points — unique ids, values, attributes, actions."""

from __future__ import annotations

from typing import Any

from openccu_loom_types.rest import AddonUpdateStatus, AlarmMessage, ServiceMessage, SystemUpdateEntry

from openccu_loom_client.compat.aiohomematic.model.hub.singletons import (
    AddonUpdateDp,
    AlarmMessagesSensor,
    ConnectionLatencySensor,
    InboxSensor,
    InstallModeDpButton,
    InstallModeDpSensor,
    InterfaceConnectivityDp,
    LastEventAgeSensor,
    ServiceMessagesSensor,
    SystemHealthSensor,
    SystemUpdateDp,
)
from openccu_loom_client.store import LoomStore


def _store() -> LoomStore:
    store = LoomStore()
    store.set_serial(serial="ABC1234567")
    store.set_central_name(central_name="home")
    return store


def _alarm(
    *, name: str, display_name: str | None = None, timestamp: str | None = "2026-06-12T08:00:00Z"
) -> AlarmMessage:
    """
    Build one wire alarm message.

    An alarm entry names no device: it is backed by an alarm system
    variable a program raises, and the CCU reports the trigger data point
    as the "unknown" sentinel — so there is no device_name to pass. The
    timestamp is optional because a CCU can report an occurrence of 0,
    which the daemon omits rather than turning into the 1970 epoch.
    """
    payload: dict[str, object] = {"id": f"al-{name}", "name": name, "counter": 1}
    if display_name is not None:
        payload["display_name"] = display_name
    if timestamp is not None:
        payload["timestamp"] = timestamp
    return AlarmMessage.model_validate(payload)


def _service(*, name: str, device_name: str | None = None) -> ServiceMessage:
    return ServiceMessage.model_validate(
        {
            "id": f"sm-{name}",
            "name": name,
            "device_name": device_name,
            "timestamp": "2026-06-12T08:00:00Z",
            "counter": 1,
            "quittable": False,
        }
    )


class _FakeSystemOps:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.addon_install_calls = 0

    async def install_system_update(self, *, central: str | None = None) -> None:
        self.calls.append({"central": central})

    async def install_addon_update(self) -> None:
        self.addon_install_calls += 1


def _addon_status(**overrides: Any) -> AddonUpdateStatus:
    payload: dict[str, Any] = {
        "supported": True,
        "current_version": "0.50.0",
        "latest_version": "0.50.1",
        "update_available": True,
        "release_url": "https://github.com/SukramJ/openccu-loom/releases/tag/v0.50.1",
        "state": "idle",
    }
    payload.update(overrides)
    return AddonUpdateStatus.model_validate(payload)


class _FakeHubOps:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def set_install_mode_interface(self, *, interface: str, active: bool, seconds: int = 60) -> None:
        self.calls.append({"interface": interface, "active": active, "seconds": seconds})


class TestUniqueIds:
    def test_hub_singleton_unique_ids(self) -> None:
        store = _store()
        assert AlarmMessagesSensor(store=store).unique_id == "loom_abc1234567_hub_alarm-messages"
        assert ServiceMessagesSensor(store=store).unique_id == "loom_abc1234567_hub_service-messages"
        assert InboxSensor(store=store).unique_id == "loom_abc1234567_hub_inbox"
        assert SystemHealthSensor(store=store).unique_id == "loom_abc1234567_hub_system-health"
        assert ConnectionLatencySensor(store=store).unique_id == "loom_abc1234567_hub_connection-latency"
        assert LastEventAgeSensor(store=store).unique_id == "loom_abc1234567_hub_last-event-age"
        assert (
            SystemUpdateDp(store=store, system_ops=_FakeSystemOps()).unique_id  # type: ignore[arg-type]
            == "loom_abc1234567_hub_system-update"
        )

    def test_connectivity_unique_id_slugifies_interface_id(self) -> None:
        store = _store()
        dp = InterfaceConnectivityDp(store=store, interface_id="HmIP-RF")
        assert dp.unique_id == "loom_abc1234567_hub_connectivity-hmip-rf"
        assert dp.name == "Connectivity HmIP-RF"
        assert dp.translation_key == "interface_connectivity"

    def test_install_mode_unique_ids_use_install_mode_address(self) -> None:
        store = _store()
        hmip_sensor = InstallModeDpSensor(store=store, interface="HmIP-RF")
        bidcos_sensor = InstallModeDpSensor(store=store, interface="BidCos-RF")
        hmip_button = InstallModeDpButton(
            store=store,
            hub_ops=_FakeHubOps(),
            interface="HmIP-RF",
            sensor=hmip_sensor,  # type: ignore[arg-type]
        )
        bidcos_button = InstallModeDpButton(
            store=store,
            hub_ops=_FakeHubOps(),
            interface="BidCos-RF",
            sensor=bidcos_sensor,  # type: ignore[arg-type]
        )
        assert hmip_sensor.unique_id == "loom_abc1234567_install_mode_hmip"
        assert bidcos_sensor.unique_id == "loom_abc1234567_install_mode_bidcos"
        assert hmip_button.unique_id == "loom_abc1234567_install_mode_hmip-button"
        assert bidcos_button.unique_id == "loom_abc1234567_install_mode_bidcos-button"
        assert hmip_sensor.name == "install_mode_hmip"
        assert hmip_button.name == "install_mode_hmip_button"

    def test_singleton_channel_is_none(self) -> None:
        # Singletons carry no wire summary, so the sysvar/program device
        # link can never apply — they always stay on the hub device.
        store = _store()
        assert InboxSensor(store=store).channel is None
        assert SystemHealthSensor(store=store).channel is None


class TestMessageSensors:
    def test_alarm_messages_count_and_attributes(self) -> None:
        dp = AlarmMessagesSensor(store=_store())
        assert dp.value is None
        assert dp.is_valid is False
        changed = dp.update_messages(
            messages=[
                _alarm(name="SABOTAGE", display_name="Sabotage"),
                _alarm(name="ERROR_OVERHEAT"),
            ]
        )
        assert changed is True
        assert dp.value == 2
        assert dp.is_valid is True
        # The translated label wins over the raw code, and the raised-at
        # stamp stands in for the device the entry does not name.
        assert dp.attributes == {
            "alarm_1": "Sabotage (2026-06-12T08:00:00+00:00)",
            "alarm_2": "ERROR_OVERHEAT (2026-06-12T08:00:00+00:00)",
        }
        assert dp.additional_information == dp.attributes
        # Unchanged payload only refreshes.
        assert (
            dp.update_messages(
                messages=[
                    _alarm(name="SABOTAGE", display_name="Sabotage"),
                    _alarm(name="ERROR_OVERHEAT"),
                ]
            )
            is False
        )

    def test_alarm_message_without_a_timestamp_renders_the_label_alone(self) -> None:
        """A CCU report carrying no occurrence must not produce a 1970 stamp."""
        dp = AlarmMessagesSensor(store=_store())
        dp.update_messages(messages=[_alarm(name="SABOTAGE", timestamp=None)])
        assert dp.attributes == {"alarm_1": "SABOTAGE"}

    def test_service_messages_count_and_attributes(self) -> None:
        dp = ServiceMessagesSensor(store=_store())
        assert dp.update_messages(messages=[_service(name="LOWBAT", device_name="Valve")])
        assert dp.value == 1
        assert dp.attributes == {"message_1": "Valve: LOWBAT"}

    def test_singletons_are_enabled_by_default(self) -> None:
        store = _store()
        assert AlarmMessagesSensor(store=store).enabled_default is True
        assert InboxSensor(store=store).enabled_default is True
        assert InstallModeDpSensor(store=store, interface="HmIP-RF").enabled_default is True


class TestMetricsSensors:
    def test_metrics_are_none_until_observed(self) -> None:
        store = _store()
        health = SystemHealthSensor(store=store)
        latency = ConnectionLatencySensor(store=store)
        age = LastEventAgeSensor(store=store)
        for dp in (health, latency, age):
            assert dp.value is None
            assert dp.is_valid is False
        # The daemon ships None until observed — applying it changes nothing.
        assert health.update_value(value=None) is False
        assert health.value is None
        assert health.update_value(value=98.5) is True
        assert health.value == 98.5
        assert health.unit == "%"
        assert latency.unit == "ms"
        assert age.unit == "s"


class TestSystemUpdate:
    def test_update_data_tracks_availability_and_progress(self) -> None:
        dp = SystemUpdateDp(store=_store(), system_ops=_FakeSystemOps())  # type: ignore[arg-type]
        assert dp.update_available is False
        assert dp.in_progress is False
        entry = SystemUpdateEntry.model_validate(
            {
                "central": "home",
                "current_firmware": "3.75.6",
                "available_firmware": "3.77.7",
                "update_available": True,
                "in_progress": False,
                "observed": True,
            }
        )
        assert dp.update_data(entry=entry) is True
        assert dp.current_firmware == "3.75.6"
        assert dp.firmware == "3.75.6"
        assert dp.available_firmware == "3.77.7"
        assert dp.latest_firmware == "3.77.7"
        assert dp.update_available is True
        assert dp.in_progress is False
        # Same payload again: refresh only.
        assert dp.update_data(entry=entry) is False
        in_progress = entry.model_copy(update={"in_progress": True})
        assert dp.update_data(entry=in_progress) is True
        assert dp.in_progress is True

    def test_latest_firmware_falls_back_to_installed(self) -> None:
        dp = SystemUpdateDp(store=_store(), system_ops=_FakeSystemOps())  # type: ignore[arg-type]
        entry = SystemUpdateEntry.model_validate(
            {
                "central": None,
                "current_firmware": "3.75.6",
                "available_firmware": None,
                "update_available": False,
                "in_progress": False,
                "observed": True,
            }
        )
        dp.update_data(entry=entry)
        assert dp.latest_firmware == "3.75.6"

    async def test_install_targets_this_central(self) -> None:
        ops = _FakeSystemOps()
        dp = SystemUpdateDp(store=_store(), system_ops=ops)  # type: ignore[arg-type]
        assert await dp.install() is True
        assert ops.calls == [{"central": "home"}]
        assert dp.in_progress is True


class TestAddonUpdate:
    def test_unique_id_and_category_match_the_hub_update_path(self) -> None:
        dp = AddonUpdateDp(store=_store(), system_ops=_FakeSystemOps())  # type: ignore[arg-type]
        assert dp.unique_id == "loom_abc1234567_hub_addon-update"
        # Same category as SystemUpdateDp — the HA update platform spawns
        # both through the identical hub-update path.
        assert dp.category == SystemUpdateDp(store=_store(), system_ops=_FakeSystemOps()).category  # type: ignore[arg-type]

    def test_update_status_tracks_versions_and_progress(self) -> None:
        dp = AddonUpdateDp(store=_store(), system_ops=_FakeSystemOps())  # type: ignore[arg-type]
        assert dp.update_available is False
        assert dp.in_progress is False
        status = _addon_status()
        assert dp.update_status(status=status) is True
        assert dp.current_firmware == "0.50.0"
        assert dp.firmware == "0.50.0"
        assert dp.available_firmware == "0.50.1"
        assert dp.latest_firmware == "0.50.1"
        assert dp.update_available is True
        assert dp.release_url == "https://github.com/SukramJ/openccu-loom/releases/tag/v0.50.1"
        assert dp.in_progress is False
        # Same payload again: refresh only.
        assert dp.update_status(status=status) is False
        # downloading and installing both render as install-in-progress.
        assert dp.update_status(status=_addon_status(state="downloading")) is True
        assert dp.in_progress is True
        assert dp.update_status(status=_addon_status(state="installing")) is True
        assert dp.in_progress is True
        # checking is not an install.
        assert dp.update_status(status=_addon_status(state="checking")) is True
        assert dp.in_progress is False

    def test_failed_state_carries_error(self) -> None:
        dp = AddonUpdateDp(store=_store(), system_ops=_FakeSystemOps())  # type: ignore[arg-type]
        dp.update_status(status=_addon_status(state="failed", error="checksum mismatch"))
        assert dp.state == "failed"
        assert dp.error == "checksum mismatch"
        assert dp.in_progress is False

    def test_latest_firmware_falls_back_to_installed(self) -> None:
        dp = AddonUpdateDp(store=_store(), system_ops=_FakeSystemOps())  # type: ignore[arg-type]
        dp.update_status(status=_addon_status(latest_version=None, update_available=False))
        assert dp.latest_firmware == "0.50.0"

    async def test_install_triggers_daemon_and_flips_in_progress(self) -> None:
        ops = _FakeSystemOps()
        dp = AddonUpdateDp(store=_store(), system_ops=ops)  # type: ignore[arg-type]
        assert await dp.install() is True
        assert ops.addon_install_calls == 1
        assert dp.state == "installing"
        assert dp.in_progress is True


class TestInstallMode:
    def test_sensor_value_and_is_active(self) -> None:
        dp = InstallModeDpSensor(store=_store(), interface="HmIP-RF")
        assert dp.is_active is False
        assert dp.update_value(value=42) is True
        assert dp.value == 42
        assert dp.is_active is True
        assert dp.update_value(value=0) is True
        assert dp.is_active is False

    async def test_button_press_activates_and_starts_countdown(self) -> None:
        store = _store()
        ops = _FakeHubOps()
        sensor = InstallModeDpSensor(store=store, interface="HmIP-RF")
        button = InstallModeDpButton(
            store=store,
            hub_ops=ops,
            interface="HmIP-RF",
            sensor=sensor,  # type: ignore[arg-type]
        )
        await button.press()
        assert ops.calls == [{"interface": "HmIP-RF", "active": True, "seconds": 60}]
        assert sensor.value == 60
        assert button.sensor is sensor

    async def test_button_activate_and_deactivate(self) -> None:
        store = _store()
        ops = _FakeHubOps()
        sensor = InstallModeDpSensor(store=store, interface="BidCos-RF")
        button = InstallModeDpButton(
            store=store,
            hub_ops=ops,
            interface="BidCos-RF",
            sensor=sensor,  # type: ignore[arg-type]
        )
        assert await button.activate(time=120) is True
        assert sensor.value == 120
        assert await button.deactivate() is True
        assert sensor.value == 0
        assert ops.calls == [
            {"interface": "BidCos-RF", "active": True, "seconds": 120},
            {"interface": "BidCos-RF", "active": False, "seconds": 0},
        ]
