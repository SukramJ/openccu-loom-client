# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""LoomCentralAdapter — the aiohomematic CentralUnit surface over LoomClient.

Covers the implemented surface (CentralConfig auth resolution, identity,
system-information pre-flight, the action coordinators) and asserts the
data-point-model-dependent surface raises a clear NotImplementedError
rather than returning a wrong shape.
"""

from __future__ import annotations

import pytest
from aioresponses import aioresponses
from openccu_loom_types.enums import DataPointCategory
from openccu_loom_types.rest import DataPointSummary, Snapshot

from openccu_loom_client import BasicAuth, BearerAuth, LoomConfig
from openccu_loom_client.compat.aiohomematic.central import CentralConfig, check_config


def _dp_summary(
    *, parameter: str, type_: str, read: bool, write: bool, value: object = None
) -> DataPointSummary:
    return DataPointSummary.model_validate(
        {
            "parameter": parameter,
            "type": type_,
            "value": value,
            "observed": True,
            "operations": {"read": read, "write": write, "event": True},
        }
    )

_BASE = "http://loom.test:8080/api/v1"
_INFO = {
    "version": "1.2.3",
    "api_version": "1.0.0",
    "commit": "deadbeef",
    "build_date": "2026-05-24T10:00:00Z",
    "started_at": "2026-05-24T10:01:00Z",
    "uptime": "PT60S",
    "capabilities": ["rest.v1"],
}


def _make_config(**overrides) -> CentralConfig:
    base = {
        "name": "home",
        "host": "loom.test",
        "port": 8080,
        "tls": False,
        "token": "tok-123456",
    }
    base.update(overrides)
    return CentralConfig(**base)


class TestCentralConfigAuthResolution:
    async def test_token_becomes_bearer(self) -> None:
        central = await _make_config().create_central()
        assert isinstance(central._client.config.auth, BearerAuth)

    async def test_username_password_becomes_basic(self) -> None:
        central = await _make_config(
            token=None, username="admin", password="secret"
        ).create_central()
        assert isinstance(central._client.config.auth, BasicAuth)

    def test_no_credentials_raises(self) -> None:
        with pytest.raises(ValueError, match="auth method"):
            CentralConfig(host="loom.test", token=None)

    async def test_ignores_aiohomematic_only_kwargs(self) -> None:
        # The component passes the full CCU keyword set; none of these
        # daemon-obsolete args should break construction.
        central = await _make_config(
            callback_host="1.2.3.4",
            callback_port_xml_rpc=2010,
            interface_configs=frozenset(),
            storage_directory="/tmp/x",
            json_port=2010,
            optional_settings=frozenset(),
        ).create_central()
        assert central.name == "home"


class TestIdentity:
    async def test_identity_before_start(self) -> None:
        central = await _make_config().create_central()
        assert central.name == "home"
        assert central.model == "openccu-loom"
        assert central.url == "http://loom.test:8080/api/v1"
        assert central.state.value == "stopped"
        assert central.available is False
        assert central.event_bus is central.events


@pytest.fixture
async def connected(config: LoomConfig):
    """A connected adapter (HTTP session open, no WS / no bootstrap)."""
    with aioresponses() as mock:
        mock.get(f"{_BASE}/info", payload=_INFO, repeat=True)
        central = await _make_config().create_central()
        await central._client.connect()
        yield central, mock
    await central._client.close()


class TestSystemInformation:
    async def test_validate_config_populates_system_information(self, connected) -> None:
        central, mock = connected
        mock.get(f"{_BASE}/system/ccu", payload=[], repeat=True)
        mock.get(
            f"{_BASE}/interfaces",
            payload=[
                {"id": "home:HmIP-RF", "name": "HmIP-RF", "connected": True, "interface": "HmIP-RF"}
            ],
            repeat=True,
        )
        info = await central.validate_config_and_get_system_information()
        assert info.version == "1.2.3"
        assert info.available_interfaces == ("home:HmIP-RF",)
        assert central.version == "1.2.3"


class TestActionCoordinators:
    async def test_device_coordinator_get_device(self, connected) -> None:
        central, _ = connected
        central._client.store.load_snapshot(
            Snapshot.model_validate(
                {
                    "generated_at": "2026-05-24T08:00:00Z",
                    "devices": [
                        {
                            "address": "VCU1",
                            "interface": "home:HmIP-RF",
                            "model": "HmIP-PSM",
                            "name": "Lamp",
                            "available": True,
                            "channels_count": 0,
                        }
                    ],
                }
            )
        )
        device = central.device_coordinator.get_device(address="VCU1")
        assert device is not None
        assert device.name == "Lamp"

    async def test_hub_coordinator_set_system_variable(self, connected) -> None:
        central, mock = connected
        mock.put(f"{_BASE}/sysvars/temp", status=202)
        await central.hub_coordinator.set_system_variable(legacy_name="temp", value=21.5)

    async def test_client_coordinator_has_client(self, connected) -> None:
        central, mock = connected
        mock.get(
            f"{_BASE}/interfaces",
            payload=[
                {"id": "home:HmIP-RF", "name": "HmIP-RF", "connected": True, "interface": "HmIP-RF"}
            ],
        )
        await central.client_coordinator.refresh()
        assert central.client_coordinator.has_client(interface_id="home:HmIP-RF") is True
        assert central.client_coordinator.has_client(interface_id="ghost") is False
        assert central.client_coordinator.has_clients is True

    async def test_json_rpc_client_get_alarm_messages(self, connected) -> None:
        central, mock = connected
        mock.get(f"{_BASE}/alarm-messages", payload=[])
        assert await central.json_rpc_client.get_alarm_messages() == []

    async def test_json_rpc_client_accept_inbox_device(self, connected) -> None:
        central, mock = connected
        mock.post(f"{_BASE}/devices/VCU9/accept", status=202)
        await central.json_rpc_client.accept_device_in_inbox(device_address="VCU9")


class TestGenericDataPointModel:
    """The store builds categorised Dp* instances; query_facade filters them."""

    async def _populate(self, central) -> None:
        store = central._client.store
        store.load_snapshot(
            Snapshot.model_validate(
                {
                    "generated_at": "2026-05-24T08:00:00Z",
                    "devices": [
                        {
                            "address": "VCU1",
                            "interface": "home:HmIP-RF",
                            "model": "HmIP-PSM",
                            "name": "Lamp",
                            "available": True,
                            "channels_count": 1,
                        }
                    ],
                }
            )
        )
        store.attach_channel_data_points(
            device_address="VCU1",
            channel_number=1,
            data_points=[
                _dp_summary(parameter="STATE", type_="BOOL", read=True, write=True),
                _dp_summary(parameter="TEMPERATURE", type_="FLOAT", read=True, write=False),
            ],
        )

    async def test_get_data_points_returns_categorised_instances(self) -> None:
        from openccu_loom_client.compat.aiohomematic.model.generic import (
            DpSensor,
            DpSwitch,
        )

        central = await _make_config().create_central()
        await self._populate(central)

        switches = central.query_facade.get_data_points(category=DataPointCategory.Switch)
        assert len(switches) == 1
        assert isinstance(switches[0], DpSwitch)
        assert switches[0].unique_id == "vcu1_1_state"

        sensors = central.query_facade.get_data_points(category=DataPointCategory.Sensor)
        assert len(sensors) == 1
        assert isinstance(sensors[0], DpSensor)

    async def test_registered_filter(self) -> None:
        central = await _make_config().create_central()
        await self._populate(central)
        unreg = central.query_facade.get_data_points(
            category=DataPointCategory.Switch, registered=False
        )
        assert len(unreg) == 1
        unreg[0].register()
        assert (
            central.query_facade.get_data_points(
                category=DataPointCategory.Switch, registered=False
            )
            == ()
        )


class TestHubDataPointModel:
    async def test_sysvar_and_program_categorised(self) -> None:
        from openccu_loom_client.compat.aiohomematic.model.hub import (
            ProgramDpButton,
            SysvarDpSwitch,
        )

        central = await _make_config().create_central()
        central._client.store.load_snapshot(
            Snapshot.model_validate(
                {
                    "generated_at": "2026-05-24T08:00:00Z",
                    "devices": [],
                    "sysvars": [
                        {
                            "name": "Alarm",
                            "value_type": "BOOL",
                            "value": True,
                            "observed": True,
                        }
                    ],
                    "programs": [{"id": "p1", "name": "All off", "active": True}],
                }
            )
        )
        switches = central.hub_coordinator.get_hub_data_points(
            category=SysvarDpSwitch.default_category()
        )
        assert len(switches) == 1
        assert isinstance(switches[0], SysvarDpSwitch)
        assert switches[0].unique_id == "sysvar_alarm"

        buttons = central.hub_coordinator.get_hub_data_points(
            category=ProgramDpButton.default_category(), registered=False
        )
        assert len(buttons) == 1
        assert isinstance(buttons[0], ProgramDpButton)
        # registered bookkeeping persists across scans (cached instances)
        buttons[0].register()
        assert (
            central.hub_coordinator.get_hub_data_points(
                category=ProgramDpButton.default_category(), registered=False
            )
            == ()
        )


class TestStillStubbedModelSurface:
    async def test_get_event_groups_raises_with_daemon_reason(self) -> None:
        central = await _make_config().create_central()
        with pytest.raises(NotImplementedError, match="not yet broadcast"):
            central.query_facade.get_event_groups(event_type="keypress")


class TestCheckConfig:
    async def test_check_config_static_validation(self) -> None:
        assert await check_config(central_name="home", host="loom.test") == []
        failures = await check_config(central_name="", host="")
        assert len(failures) == 2
