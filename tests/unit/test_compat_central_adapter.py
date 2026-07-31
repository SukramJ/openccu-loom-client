# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
LoomCentralAdapter — the aiohomematic CentralUnit surface over LoomClient.

Covers the implemented surface (CentralConfig auth resolution, identity,
system-information pre-flight, the action coordinators) and asserts the
data-point-model-dependent surface raises a clear NotImplementedError
rather than returning a wrong shape.
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from aiohomematic.central.events import EventBus as AioEventBus
from openccu_loom_types import DAEMON_API_VERSION
from openccu_loom_types.enums import DataPointCategory
from openccu_loom_types.rest import AlarmMessage, DataPointSummary, Link, ServiceMessage, Snapshot
import pytest

from openccu_loom_client import BasicAuth, BearerAuth
from openccu_loom_client.compat.aiohomematic._upstream import ParamsetKey
from openccu_loom_client.compat.aiohomematic.central import CentralConfig, check_config
from openccu_loom_client.compat.aiohomematic.central.adapter import (
    _ClientCoordinator,
    _Configuration,
    _IncidentStore,
    _JsonRpcClient,
    _LinkCoordinator,
    _ui_schema_to_parameter_data,
)
from openccu_loom_client.exceptions import LoomConflictError, LoomNotFoundError
from tests.helpers import MockDaemon


def _dp_summary(
    *, parameter: str, type_: str, read: bool, write: bool, value: object = None, unique_id: str | None = None
) -> DataPointSummary:
    return DataPointSummary.model_validate(
        {
            "parameter": parameter,
            "type": type_,
            "value": value,
            "observed": True,
            "operations": {"read": read, "write": write, "event": True},
            "unique_id": unique_id or f"loom_test_{parameter.lower()}",
        }
    )


_BASE = "/api/v1"
_INFO = {
    "version": "1.2.3",
    "api_version": DAEMON_API_VERSION,
    "commit": "deadbeef",
    "build_date": "2026-05-24T10:00:00Z",
    "addon_build": False,
    "started_at": "2026-05-24T10:01:00Z",
    "uptime": "PT60S",
    "capabilities": ["rest.v1"],
    "schema_digest": "sha256:test",
}


def _make_config(*, mock_daemon: MockDaemon | None = None, **overrides) -> CentralConfig:
    base = {
        "name": "home",
        "host": "loom.test",
        "port": 8080,
        "tls": False,
        "token": "tok-123456",
    }
    if mock_daemon is not None:
        # Point a config that will actually open a session at the live
        # in-process server (ephemeral host/port).
        base["host"] = mock_daemon.host
        base["port"] = mock_daemon.port
    base.update(overrides)
    return CentralConfig(**base)


class TestCentralConfigAuthResolution:
    async def test_token_becomes_bearer(self) -> None:
        central = await _make_config().create_central()
        assert isinstance(central._client.config.auth, BearerAuth)

    async def test_username_password_becomes_basic(self) -> None:
        central = await _make_config(token=None, username="admin", password="secret").create_central()
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
        # event_bus is aiohomematic's own bus (HA entities subscribe on it and
        # match real aiohomematic event types); the loom wire bus is `events`.
        assert isinstance(central.event_bus, AioEventBus)
        assert central.event_bus is not central.events


@pytest.fixture
async def connected(mock_daemon: MockDaemon):
    """Build a connected adapter (HTTP session open, no WS / no bootstrap)."""
    mock_daemon.get(f"{_BASE}/info", payload=_INFO)
    central = await _make_config(mock_daemon=mock_daemon).create_central()
    await central._client.connect()
    yield central, mock_daemon
    await central._client.close()


class TestSystemInformation:
    async def test_validate_config_populates_system_information(self, connected) -> None:
        central, mock = connected
        mock.get(f"{_BASE}/system/ccu", payload=[])
        mock.get(
            f"{_BASE}/interfaces",
            payload=[{"id": "home:HmIP-RF", "name": "HmIP-RF", "connected": True, "interface": "HmIP-RF"}],
        )
        info = await central.validate_config_and_get_system_information()
        assert info.version == "1.2.3"
        assert info.available_interfaces == ("home:HmIP-RF",)
        assert central.version == "1.2.3"

    async def test_ccu_security_flags_come_from_the_daemon(self, connected) -> None:
        # api 3.5.0: the flags describe the *CCU's* posture, not this
        # client's auth — the dashboard would otherwise claim the CCU is
        # authenticated purely because the client connected with a token.
        central, mock = connected
        mock.get(
            f"{_BASE}/system/ccu",
            payload=[{**_CCU_ENTRY, "auth_enabled": False, "https_redirect_enabled": True}],
        )
        mock.get(f"{_BASE}/interfaces", payload=[])
        info = await central.validate_config_and_get_system_information()
        assert info.auth_enabled is False
        assert info.https_redirect_enabled is True

    async def test_ccu_security_flags_stay_unknown_before_the_first_connect(self, connected) -> None:
        # The CCU-sourced set is empty until the daemon has reached the CCU
        # once. "Unknown" must not collapse into a claim either way.
        central, mock = connected
        mock.get(f"{_BASE}/system/ccu", payload=[_CCU_ENTRY])
        mock.get(f"{_BASE}/interfaces", payload=[])
        info = await central.validate_config_and_get_system_information()
        assert info.auth_enabled is None
        assert info.https_redirect_enabled is None


_CCU_ENTRY = {
    "name": "home",
    "host": "ccu.local",
    "available": True,
    "is_ha_app": False,
    "configured_interfaces": [],
    "serial": "0000DAEMON1234",  # daemon-reported serial → suffix daemon1234
    # Required since types 0.1.55 / daemon api 2.19.0.
    "readiness": {"phase": "ready", "ready": True, "interfaces_loaded": 1, "interfaces_total": 1},
}


class TestSerialInjection:
    """An injected serial (HA entry.unique_id) fills the key central-id slot."""

    async def _refresh(self, *, mock_daemon: MockDaemon, serial: str | None) -> str:
        mock_daemon.get(f"{_BASE}/info", payload=_INFO)
        mock_daemon.get(f"{_BASE}/system/ccu", payload=[_CCU_ENTRY])
        mock_daemon.get(f"{_BASE}/interfaces", payload=[])
        central = await _make_config(mock_daemon=mock_daemon, serial=serial).create_central()
        await central._client.connect()
        await central._refresh_system_information()
        suffix = central._client.store.serial_suffix
        await central._client.close()
        return suffix

    async def test_injected_serial_wins_over_daemon(self, mock_daemon: MockDaemon) -> None:
        assert await self._refresh(mock_daemon=mock_daemon, serial="3014F711A0001234") == "11a0001234"

    async def test_daemon_serial_used_without_injection(self, mock_daemon: MockDaemon) -> None:
        assert await self._refresh(mock_daemon=mock_daemon, serial=None) == "daemon1234"


class TestActionCoordinators:
    async def test_device_coordinator_get_device(self, connected) -> None:
        central, _ = connected
        central._client.store.load_snapshot(
            snapshot=Snapshot.model_validate(
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
                            "interface_id": "home:HmIP-RF",
                            "updatable": False,
                            "update_available": False,
                            "master_pushes_config_pending": False,
                            "has_sub_devices": False,
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
            payload=[{"id": "home:HmIP-RF", "name": "HmIP-RF", "connected": True, "interface": "HmIP-RF"}],
        )
        await central.client_coordinator.refresh()
        assert central.client_coordinator.has_client(interface_id="home:HmIP-RF") is True
        assert central.client_coordinator.has_client(interface_id="ghost") is False
        assert central.client_coordinator.has_clients is True

    async def test_json_rpc_client_get_alarm_messages(self, connected) -> None:
        central, mock = connected
        mock.get(f"{_BASE}/alarm-messages", payload=[])
        # Records are aiohomematic dataclasses now (the handler asdict()s them),
        # so the empty case is an empty tuple rather than the raw wire list.
        assert await central.json_rpc_client.get_alarm_messages() == ()

    async def test_json_rpc_client_accept_inbox_device(self, connected) -> None:
        central, mock = connected
        mock.post(f"{_BASE}/devices/VCU9/accept", status=202)
        await central.json_rpc_client.accept_device_in_inbox(device_address="VCU9")


class TestGenericDataPointModel:
    """The store builds categorised Dp* instances; query_facade filters them."""

    async def _populate(self, central) -> None:
        store = central._client.store
        store.load_snapshot(
            snapshot=Snapshot.model_validate(
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
                            "interface_id": "home:HmIP-RF",
                            "updatable": False,
                            "update_available": False,
                            "master_pushes_config_pending": False,
                            "has_sub_devices": False,
                        }
                    ],
                }
            )
        )
        store.attach_channel_data_points(
            device_address="VCU1",
            channel_number=1,
            data_points=[
                _dp_summary(parameter="STATE", type_="BOOL", read=True, write=True, unique_id="loom_vcu1_1_state"),
                _dp_summary(parameter="TEMPERATURE", type_="FLOAT", read=True, write=False),
            ],
        )

    async def test_get_data_points_returns_categorised_instances(self) -> None:
        from openccu_loom_client.compat.aiohomematic.model.generic import DpSensor, DpSwitch

        central = await _make_config().create_central()
        await self._populate(central)

        switches = central.query_facade.get_data_points(category=DataPointCategory.Switch)
        assert len(switches) == 1
        assert isinstance(switches[0], DpSwitch)
        assert switches[0].unique_id == "loom_vcu1_1_state"

        sensors = central.query_facade.get_data_points(category=DataPointCategory.Sensor)
        assert len(sensors) == 1
        assert isinstance(sensors[0], DpSensor)

    async def test_registered_filter(self) -> None:
        central = await _make_config().create_central()
        await self._populate(central)
        unreg = central.query_facade.get_data_points(category=DataPointCategory.Switch, registered=False)
        assert len(unreg) == 1
        unreg[0].register()
        assert central.query_facade.get_data_points(category=DataPointCategory.Switch, registered=False) == ()


class TestHubDataPointModel:
    async def test_sysvar_and_program_categorised(self) -> None:
        from openccu_loom_client.compat.aiohomematic.model.hub import ProgramDpButton, SysvarDpBinarySensor

        central = await _make_config().create_central()
        central._client.store.load_snapshot(
            snapshot=Snapshot.model_validate(
                {
                    "generated_at": "2026-05-24T08:00:00Z",
                    "interfaces": [
                        {
                            "id": "home:HmIP-RF",
                            "name": "HmIP",
                            "connected": True,
                            "interface": "HmIP-RF",
                            "central_id": "home",
                        }
                    ],
                    "devices": [],
                    "sysvars": [
                        {
                            "name": "Alarm",
                            "value_type": "LOGIC",
                            "value": True,
                            "observed": True,
                            "unique_id": "loom_11a0001234_sysvar_alarm",
                        }
                    ],
                    "programs": [{"id": "p1", "name": "All off", "active": True, "unique_id": "loom_test_p1"}],
                }
            )
        )
        # The serial fills the central-id slot of hub keys; the adapter
        # sets it from /system/ccu at start(), but this test drives the
        # store directly, so set it explicitly.
        central._client.store.set_serial(serial="3014F711A0001234")  # → 11a0001234
        # aiohomematic default mapping: LOGIC/ALARM read as binary
        # sensors (writable variants need the extended sysvar marker).
        binaries = central.hub_coordinator.get_hub_data_points(category=SysvarDpBinarySensor.default_category())
        assert len(binaries) == 1
        assert isinstance(binaries[0], SysvarDpBinarySensor)
        # canonical sysvar key: loom_<serial>_sysvar_<hub_slug(name)>.
        assert binaries[0].unique_id == "loom_11a0001234_sysvar_alarm"

        buttons = central.hub_coordinator.get_hub_data_points(
            category=ProgramDpButton.default_category(), registered=False
        )
        assert len(buttons) == 1
        assert isinstance(buttons[0], ProgramDpButton)
        # registered bookkeeping persists across scans (cached instances)
        buttons[0].register()
        assert (
            central.hub_coordinator.get_hub_data_points(category=ProgramDpButton.default_category(), registered=False)
            == ()
        )


class TestInternalSysvarInclusion:
    async def test_internal_included_disabled_dollar_excluded(self) -> None:
        from openccu_loom_client.compat.aiohomematic.model.hub import SysvarDpSensor

        central = await _make_config().create_central()
        central._client.store.load_snapshot(
            snapshot=Snapshot.model_validate(
                {
                    "generated_at": "2026-05-24T08:00:00Z",
                    "devices": [],
                    "programs": [],
                    "sysvars": [
                        # CCU bookkeeping variable: included, disabled by
                        # default (DEFAULT_INCLUDE_INTERNAL_SYSVARS=True).
                        {
                            "name": "svEnergyCounter_14179",
                            "value_type": "FLOAT",
                            "value": 1.0,
                            "observed": True,
                            "is_internal": True,
                            "unique_id": "loom_test_sysvar_energycounter",
                        },
                        # ${...} variables back dedicated hub singletons —
                        # never a generic sysvar entity.
                        {
                            "name": "${sysVarAlarmMessages}",
                            "value_type": "FLOAT",
                            "value": 0.0,
                            "observed": True,
                            "is_internal": True,
                            "unique_id": "loom_test_sysvar_alarmmessages",
                        },
                        # Plain user variable: included, disabled (no markers).
                        {
                            "name": "Temperatur Garten",
                            "value_type": "FLOAT",
                            "value": 21.5,
                            "observed": True,
                            "unique_id": "loom_test_sysvar_temperatur_garten",
                        },
                    ],
                }
            )
        )
        central._client.store.set_serial(serial="3014F711A0001234")
        sensors = central.hub_coordinator.get_hub_data_points(category=SysvarDpSensor.default_category())
        names = sorted(dp.name for dp in sensors)
        assert names == ["Temperatur Garten", "svEnergyCounter_14179"]
        assert all(dp.enabled_default is False for dp in sensors)

    async def test_enabled_default_flows_from_daemon(self) -> None:
        # The daemon (api >= 1.9.0) resolves the marker-driven
        # enabled-by-default flag; the client reads it off the wire.
        from openccu_loom_client.compat.aiohomematic.model.hub import SysvarDpSensor

        central = await _make_config().create_central()
        central._client.store.load_snapshot(
            snapshot=Snapshot.model_validate(
                {
                    "generated_at": "2026-05-24T08:00:00Z",
                    "devices": [],
                    "programs": [],
                    "sysvars": [
                        {
                            "name": "Marked",
                            "value_type": "FLOAT",
                            "value": 1.0,
                            "observed": True,
                            "enabled_default": True,
                            "unique_id": "loom_test_sysvar_marked",
                        },
                        {
                            "name": "Unmarked",
                            "value_type": "FLOAT",
                            "value": 2.0,
                            "observed": True,
                            "enabled_default": False,
                            "unique_id": "loom_test_sysvar_unmarked",
                        },
                    ],
                }
            )
        )
        central._client.store.set_serial(serial="3014F711A0001234")
        sensors = {
            dp.name: dp
            for dp in central.hub_coordinator.get_hub_data_points(category=SysvarDpSensor.default_category())
        }
        assert sensors["Marked"].enabled_default is True
        assert sensors["Unmarked"].enabled_default is False

    async def test_oldval_and_fixed_ids_excluded(self) -> None:
        from openccu_loom_client.compat.aiohomematic.model.hub import SysvarDpSensor

        central = await _make_config().create_central()
        central._client.store.load_snapshot(
            snapshot=Snapshot.model_validate(
                {
                    "generated_at": "2026-05-24T08:00:00Z",
                    "devices": [],
                    "programs": [],
                    "sysvars": [
                        # OldVal scratch values never spawn (hub.py _EXCLUDED).
                        {
                            "name": "svEnergyCounterOldVal_14179",
                            "value_type": "FLOAT",
                            "value": 1.0,
                            "observed": True,
                            "is_internal": True,
                            "unique_id": "loom_test_sysvar_oldval",
                        },
                        {
                            "name": "pcCCUID",
                            "value_type": "STRING",
                            "value": "x",
                            "observed": True,
                            "is_internal": True,
                            "unique_id": "loom_test_sysvar_pcccuid",
                        },
                        # Fixed CCU IDs 40/41 back the alarm/service-message
                        # hub singletons (IGNORE_SYSVARS_BY_ID).
                        {
                            "name": "Alarmmeldungen",
                            "value_type": "INTEGER",
                            "value": 0,
                            "observed": True,
                            "is_internal": True,
                            "vid": 40,
                            "unique_id": "loom_test_sysvar_alarmmeldungen",
                        },
                        {
                            "name": "Servicemeldungen",
                            "value_type": "INTEGER",
                            "value": 0,
                            "observed": True,
                            "is_internal": True,
                            "vid": 41,
                            "unique_id": "loom_test_sysvar_servicemeldungen",
                        },
                        # Control: a plain internal counter stays included.
                        {
                            "name": "svEnergyCounter_14179",
                            "value_type": "FLOAT",
                            "value": 2.0,
                            "observed": True,
                            "is_internal": True,
                            "vid": 14179,
                            "unique_id": "loom_test_sysvar_energycounter_ctrl",
                        },
                    ],
                }
            )
        )
        central._client.store.set_serial(serial="3014F711A0001234")
        sensors = central.hub_coordinator.get_hub_data_points(category=SysvarDpSensor.default_category())
        assert [dp.name for dp in sensors] == ["svEnergyCounter_14179"]


class TestEventGroupsAndInstallMode:
    async def test_get_event_groups_returns_tuple(self) -> None:
        central = await _make_config().create_central()
        # No devices loaded → empty, but it no longer raises.
        groups = central.query_facade.get_event_groups()
        assert isinstance(groups, tuple)

    async def test_install_mode_dps_empty_mapping(self) -> None:
        central = await _make_config().create_central()
        assert central.hub_coordinator.install_mode_dps == {}


class TestRenameDeviceByIseId:
    @staticmethod
    def _fake_client(devices: list[SimpleNamespace], calls: list[tuple[str, str]]) -> SimpleNamespace:
        async def patch_device(*, address: str, name: str) -> None:
            calls.append((address, name))

        return SimpleNamespace(
            store=SimpleNamespace(devices=devices),
            devices=SimpleNamespace(patch_device=patch_device),
        )

    async def test_maps_ise_id_to_address(self) -> None:
        calls: list[tuple[str, str]] = []
        client = self._fake_client(
            [
                SimpleNamespace(ise_id=4711, address="VCU0000001"),
                SimpleNamespace(ise_id=4712, address="VCU0000002"),
            ],
            calls,
        )
        # The handler passes aiohomematic's `new_name` kwarg and tests the bool.
        assert await _JsonRpcClient(client=client).rename_device(ise_id=4712, new_name="Kitchen") is True
        assert calls == [("VCU0000002", "Kitchen")]

    async def test_unknown_ise_id_raises_a_handler_catchable_error(self) -> None:
        """A bare ValueError escapes `except BaseHomematicException` and leaks an unknown_error."""
        from aiohomematic.exceptions import BaseHomematicException

        client = self._fake_client([SimpleNamespace(ise_id=1, address="VCU1")], [])
        with pytest.raises(BaseHomematicException):
            await _JsonRpcClient(client=client).rename_device(ise_id=9999, new_name="x")


class TestCheckConfig:
    async def test_check_config_static_validation(self) -> None:
        assert await check_config(central_name="home", host="loom.test") == []
        failures = await check_config(central_name="", host="")
        assert len(failures) == 2


class TestUnIgnoreCandidates:
    """
    HA's options flow calls get_un_ignore_candidates *synchronously*.

    aiohomematic computes the list from local caches with an
    ``include_master`` switch; the loom facade must match that
    signature and serve a prefetched cache — an async coroutine (the
    old shape) made HA's advanced-settings options step (where
    ``sub_devices_enabled`` lives) crash for the loom backend.
    """

    async def test_sync_signature_with_include_master(self) -> None:
        central = await _make_config().create_central()
        # Before any prefetch the facade degrades to an empty list.
        assert central.query_facade.get_un_ignore_candidates(include_master=True) == []

    async def test_prefetch_fills_cache(self, connected) -> None:
        central, mock = connected
        mock.get(
            f"{_BASE}/visibility/unignore/candidates",
            payload={"candidates": ["RSSI_PEER", "FROST_PROTECTION"], "include_master": True},
        )
        await central.query_facade.prefetch_un_ignore_candidates()
        assert central.query_facade.get_un_ignore_candidates(include_master=True) == [
            "RSSI_PEER",
            "FROST_PROTECTION",
        ]
        # Sync call without kwargs matches aiohomematic's default shape too.
        assert central.query_facade.get_un_ignore_candidates() == [
            "RSSI_PEER",
            "FROST_PROTECTION",
        ]

    async def test_prefetch_failure_is_non_fatal(self, connected) -> None:
        central, mock = connected
        mock.get(f"{_BASE}/visibility/unignore/candidates", status=500)
        await central.query_facade.prefetch_un_ignore_candidates()
        assert central.query_facade.get_un_ignore_candidates() == []


class TestParamsetDescriptionTransform:
    """The daemon ui-schema is translated into aiohomematic's ``ParameterData`` map."""

    def test_ui_schema_to_parameter_data(self) -> None:
        ui_schema = {
            "parameters": [
                {
                    "name": "ON_TIME",
                    "type": "FLOAT",
                    "unit": "s",
                    "min": 0.0,
                    "max": 100.0,
                    "default": 0.0,
                    "operations": {"read": True, "write": True, "event": False},
                    "flags": {"visible": True, "internal": False, "service": False},
                },
                {
                    "name": "CH_MODE",
                    "type": "ENUM",
                    "operations": {"read": True, "write": True, "event": True},
                    "flags": {"visible": True, "internal": False, "service": True},
                    # value list intentionally out of order to prove index sorting.
                    "value_list": [
                        {"value": 2, "key": "AUTO"},
                        {"value": 0, "key": "NORMAL"},
                        {"value": 1, "key": "MANU"},
                    ],
                },
            ]
        }
        result = _ui_schema_to_parameter_data(ui_schema=ui_schema)
        assert result["ON_TIME"] == {
            "ID": "ON_TIME",
            "TYPE": "FLOAT",
            "OPERATIONS": 3,  # READ | WRITE
            "FLAGS": 1,  # VISIBLE
            "MIN": 0.0,
            "MAX": 100.0,
            "DEFAULT": 0.0,
            "UNIT": "s",
        }
        assert result["CH_MODE"]["OPERATIONS"] == 7  # READ | WRITE | EVENT
        assert result["CH_MODE"]["FLAGS"] == 9  # VISIBLE | SERVICE
        # VALUE_LIST is an index-ordered tuple of the enum keys.
        assert result["CH_MODE"]["VALUE_LIST"] == ("NORMAL", "MANU", "AUTO")

    def test_ui_schema_skips_nameless_parameters(self) -> None:
        assert _ui_schema_to_parameter_data(ui_schema={"parameters": [{"type": "FLOAT"}]}) == {}
        assert _ui_schema_to_parameter_data(ui_schema={}) == {}


class TestPutParamset:
    """put_paramset validates against the ui-schema descriptions before writing."""

    @staticmethod
    def _config() -> tuple[_Configuration, SimpleNamespace]:
        calls: list[dict[str, object]] = []
        ui_schema = {
            "parameters": [
                {
                    "name": "ON_TIME",
                    "type": "FLOAT",
                    "min": 0.0,
                    "max": 100.0,
                    "operations": {"read": True, "write": True, "event": False},
                    "flags": {"visible": True},
                }
            ]
        }

        async def _get_ui_schema(**_kwargs: object) -> dict[str, object]:
            return ui_schema

        async def _put_paramset(**kwargs: object) -> None:
            calls.append(kwargs)

        client = SimpleNamespace(
            devices=SimpleNamespace(get_ui_schema=_get_ui_schema),
            datapoints=SimpleNamespace(put_paramset=_put_paramset),
        )
        return _Configuration(client=client), SimpleNamespace(calls=calls)

    async def test_valid_write_succeeds(self) -> None:
        config, spy = self._config()
        result = await config.put_paramset(
            channel_address="ABC:1", paramset_key=ParamsetKey.MASTER, values={"ON_TIME": 50.0}
        )
        assert result.success is True
        assert result.validated is True
        assert dict(result.validation_errors) == {}
        assert spy.calls == [{"address": "ABC:1", "paramset_key": "MASTER", "values": {"ON_TIME": 50.0}}]

    async def test_invalid_value_is_not_written(self) -> None:
        config, spy = self._config()
        result = await config.put_paramset(
            channel_address="ABC:1", paramset_key=ParamsetKey.MASTER, values={"ON_TIME": 9999.0}
        )
        assert result.success is False
        assert result.validated is True
        assert "ON_TIME" in result.validation_errors
        assert spy.calls == []

    async def test_validate_false_bypasses_validation(self) -> None:
        config, spy = self._config()
        result = await config.put_paramset(
            channel_address="ABC:1",
            paramset_key=ParamsetKey.MASTER,
            values={"ON_TIME": 9999.0},
            validate=False,
        )
        assert result.success is True
        assert result.validated is False
        assert len(spy.calls) == 1


class TestLinkCoordinator:
    """Signatures mirror aiohomematic's LinkCoordinator — the HA handlers depend on it."""

    @staticmethod
    def _coordinator() -> tuple[_LinkCoordinator, dict[str, Any]]:
        calls: dict[str, Any] = {}

        async def list_links(*, address: str, locale: str = "en") -> list[Any]:
            calls["list"] = {"address": address, "locale": locale}
            return [
                Link.model_validate(
                    {
                        "sender_address": "AAA:1",
                        "receiver_address": "BBB:2",
                        "name": "n",
                        "description": "d",
                        "flags": 1,
                        "sender_device_name": "S",
                        "sender_device_model": "SM",
                        "sender_channel_type": "KEY",
                        "sender_channel_type_label": "Key",
                        "sender_channel_name": "SC",
                        "receiver_device_name": "R",
                        "receiver_device_model": "RM",
                        "receiver_channel_type": "SW",
                        "receiver_channel_type_label": "Switch",
                        "receiver_channel_name": "RC",
                        "peer_address": "BBB:2",
                        "peer_device_name": "R",
                        "peer_device_model": "RM",
                        "direction": "out",
                    }
                )
            ]

        async def add_link(**kwargs: Any) -> None:
            calls["add"] = kwargs

        async def remove_link(**kwargs: Any) -> None:
            calls["remove"] = kwargs

        async def linkable_channels(**kwargs: Any) -> list[dict[str, str]]:
            calls["linkable"] = kwargs
            return [
                {
                    "address": "CCC:3",
                    "channel_type": "SW",
                    "channel_type_label": "Switch",
                    "channel_name": "C",
                    "device_address": "CCC",
                    "device_name": "Dev",
                    "device_model": "M",
                }
            ]

        client = SimpleNamespace(
            links=SimpleNamespace(
                list_links=list_links,
                add_link=add_link,
                remove_link=remove_link,
                linkable_channels=linkable_channels,
            )
        )
        return _LinkCoordinator(client=client), calls

    async def test_add_link_returns_true_and_derives_path_address(self) -> None:
        link, calls = self._coordinator()
        assert await link.add_link(sender_channel_address="AAA:1", receiver_channel_address="BBB:2") is True
        # The daemon path is the device address; the name defaults like the reference.
        assert calls["add"]["address"] == "AAA"
        assert calls["add"]["sender_address"] == "AAA:1"
        assert calls["add"]["name"] == "AAA:1 -> BBB:2"

    async def test_remove_link_returns_true(self) -> None:
        link, calls = self._coordinator()
        assert await link.remove_link(sender_channel_address="AAA:1", receiver_channel_address="BBB:2") is True
        assert calls["remove"] == {"address": "AAA", "sender": "AAA:1", "receiver": "BBB:2"}

    async def test_daemon_refusal_becomes_false_not_an_exception(self) -> None:
        """The handler renders add_link_failed on a falsy result — it must not see an exception."""
        link, _ = self._coordinator()

        async def boom(**_kwargs: Any) -> None:
            raise LoomConflictError(status=409, problem=None, raw_body=None, method="POST", url="/x")

        link._client.links.add_link = boom
        assert await link.add_link(sender_channel_address="AAA:1", receiver_channel_address="BBB:2") is False

    async def test_get_device_links_returns_asdict_able_dataclasses(self) -> None:
        link, _ = self._coordinator()
        links = await link.get_device_links(device_address="AAA", locale="de")
        assert dataclasses.is_dataclass(links[0])
        # The handler calls dataclasses.asdict on each — a pydantic model would raise.
        payload = dataclasses.asdict(links[0])
        assert payload["sender_address"] == "AAA:1"
        assert payload["flags"] == 1
        assert len(payload) == 19

    async def test_get_linkable_channels_splits_the_source_address(self) -> None:
        link, calls = self._coordinator()
        channels = await link.get_linkable_channels(
            interface_id="home:HmIP-RF", source_channel_address="AAA:1", role="sender"
        )
        assert calls["linkable"]["address"] == "AAA"
        assert calls["linkable"]["channel"] == 1
        assert calls["linkable"]["interface"] == "home:HmIP-RF"
        assert dataclasses.asdict(channels[0])["address"] == "CCC:3"


class TestJsonRpcClientRecords:
    """
    The CCU dashboard's message/inbox commands.

    The handlers call `dataclasses.asdict()` on the lists and test the mutations
    for a truthy result (`if not success: send_error(..., "..._failed")`), so the
    surface must hand back aiohomematic record dataclasses and `bool`.
    """

    @staticmethod
    def _client() -> tuple[_JsonRpcClient, dict[str, Any]]:
        timestamp = datetime(2026, 7, 13, 10, 0, tzinfo=UTC)
        service = ServiceMessage.model_validate(
            {
                "central": "home",
                "id": "S1",
                "name": "LOW_BAT",
                "address": "VCU1:0",
                "device_name": "Lamp",
                "type": "LOW_BAT",
                "description": "battery",
                "priority": 1,
                "timestamp": timestamp,
                "counter": 2,
                "quittable": True,
                "display_name": "Low battery",
            }
        )
        alarm = AlarmMessage.model_validate(
            {
                "central": "home",
                "id": "A1",
                "name": "ERROR",
                "description": "d",
                "device_name": "Lamp",
                "address": "VCU1:0",
                "state_value": "1",
                "timestamp": timestamp,
                "counter": 1,
                "last_trigger": "2026-07-13T09:00:00Z",
                "display_name": "Error",
                "rooms": ["Kitchen"],
            }
        )
        acks: dict[str, Any] = {"calls": []}

        async def ack_service(*, message_id: str) -> None:
            acks["calls"].append(("service", message_id))

        async def ack_alarm(*, message_id: str) -> None:
            acks["calls"].append(("alarm", message_id))

        async def ret(value: Any) -> Any:
            return value

        hub = SimpleNamespace(
            list_service_messages=lambda: ret([service]),
            list_alarm_messages=lambda: ret([alarm]),
            list_inbox=lambda: ret([{"central": "home", "address": "NEW1", "model": "HmIP-PS", "serial": "SER1"}]),
            ack_service_message=ack_service,
            ack_alarm_message=ack_alarm,
        )
        devices = SimpleNamespace(
            accept_device=lambda **_kw: ret(None),
            patch_device=lambda **_kw: ret(None),
        )
        store = SimpleNamespace(devices=[SimpleNamespace(address="VCU1", ise_id=4711)])
        client = SimpleNamespace(hub=hub, devices=devices, store=store)
        return _JsonRpcClient(client=client), acks

    async def test_service_messages_are_asdict_able_records(self) -> None:
        json_rpc, _ = self._client()
        messages = await json_rpc.get_service_messages()
        payload = dataclasses.asdict(messages[0])
        assert payload["msg_id"] == "S1"
        assert payload["msg_type_name"] == "LOW_BAT"
        assert payload["quittable"] is True
        assert payload["timestamp"].startswith("2026-07-13T10:00")

    async def test_alarm_messages_are_asdict_able_records(self) -> None:
        json_rpc, _ = self._client()
        alarms = await json_rpc.get_alarm_messages()
        payload = dataclasses.asdict(alarms[0])
        assert payload["alarm_id"] == "A1"
        assert payload["rooms"] == ("Kitchen",)

    async def test_inbox_devices_are_asdict_able_records(self) -> None:
        json_rpc, _ = self._client()
        devices = await json_rpc.get_inbox_devices()
        assert dataclasses.asdict(devices[0]) == {
            "device_id": "SER1",
            "address": "NEW1",
            "name": "NEW1",
            "device_type": "HmIP-PS",
            "interface": "",
        }

    async def test_mutations_return_true(self) -> None:
        """A None return made every accept/ack report failure to the panel."""
        json_rpc, acks = self._client()
        assert await json_rpc.accept_device_in_inbox(device_address="NEW1") is True
        assert await json_rpc.acknowledge_message(message_id="S1") is True
        assert await json_rpc.rename_device(ise_id=4711, new_name="Neu") is True
        assert acks["calls"] == [("service", "S1")]

    async def test_acknowledge_falls_back_to_the_alarm_store(self) -> None:
        """Both HA ack handlers route through the one primitive; the daemon splits the endpoints."""
        json_rpc, acks = self._client()

        async def ack_404(*, message_id: str) -> None:
            raise LoomNotFoundError(status=404, problem=None, raw_body=None, method="POST", url="/x")

        json_rpc._client.hub.ack_service_message = ack_404
        assert await json_rpc.acknowledge_message(message_id="A1") is True
        assert acks["calls"] == [("alarm", "A1")]

    async def test_unknown_ise_id_raises_a_handler_catchable_error(self) -> None:
        from aiohomematic.exceptions import BaseHomematicException

        json_rpc, _ = self._client()
        with pytest.raises(BaseHomematicException):
            await json_rpc.rename_device(ise_id=9999, new_name="x")


class TestIntegrationDashboardSurface:
    """
    The integration dashboard fetches its four sections in one Promise.all.

    Any one of them raising takes the whole tab down, so each must hand back the
    shape the handler reads rather than an AttributeError.
    """

    def test_clients_are_records_with_a_throttle(self) -> None:
        """The throttle view reads client.interface_id + client.command_throttle.* per client."""
        coordinator = _ClientCoordinator(client=SimpleNamespace())
        coordinator._interface_ids = frozenset({"home:HmIP-RF", "home:BidCos-RF"})
        stats = {
            client.interface_id: {
                "interval": client.command_throttle.interval,
                "is_enabled": client.command_throttle.is_enabled,
                "queue_size": client.command_throttle.queue_size,
            }
            for client in coordinator.clients
        }
        assert set(stats) == {"home:HmIP-RF", "home:BidCos-RF"}
        # The daemon serialises commands itself — throttling is honestly reported as off.
        assert stats["home:HmIP-RF"] == {"interval": 0.0, "is_enabled": False, "queue_size": 0}

    async def test_health_is_the_shape_the_card_renders(self, connected) -> None:
        """
        The health card is typed against SystemHealthData: central_state + overall_health_score.

        ws_get_system_health does `central.health.to_dict()` (no await), and the
        daemon's own /health probe ({status, components}) carries none of the
        fields the card reads — so the real upstream CentralHealth is built.
        """
        central, mock = connected
        mock.get(
            f"{_BASE}/interfaces",
            payload=[
                {"id": "home:HmIP-RF", "name": "HmIP-RF", "connected": True, "interface": "HmIP-RF"},
                {"id": "home:BidCos-RF", "name": "BidCos-RF", "connected": False, "interface": "BidCos-RF"},
            ],
        )
        await central.client_coordinator.refresh()
        health = central.health.to_dict()
        assert "central_state" in health
        assert "overall_health_score" in health
        # One of the two interfaces is connected.
        assert health["healthy_clients"] == ["home:HmIP-RF"]
        assert health["failed_clients"] == ["home:BidCos-RF"]
        assert health["overall_health_score"] == 0.5

    async def test_incidents_by_interface_are_to_dict_able(self) -> None:
        incident = {"id": "1", "interface_id": "home:HmIP-RF", "severity": "warn", "summary": "s"}

        async def list_incidents() -> dict[str, Any]:
            return {"incidents": [incident, {"id": "2", "interface_id": "other"}]}

        store = _IncidentStore(
            client=SimpleNamespace(diagnostics=SimpleNamespace(list_incidents=list_incidents)),
            looper=SimpleNamespace(),
        )
        incidents = await store.get_incidents_by_interface(interface_id="home:HmIP-RF")
        # The handler does [i.to_dict() for i in incidents] — plain dicts raised AttributeError.
        assert [i.to_dict() for i in incidents] == [incident]

    async def test_clear_incidents_reaches_the_daemon(self) -> None:
        """A client-side no-op left the list unchanged and the panel button dead."""
        calls: list[str] = []

        async def clear() -> None:
            calls.append("DELETE /incidents")

        store = _IncidentStore(
            client=SimpleNamespace(diagnostics=SimpleNamespace(clear_incidents=clear)),
            looper=SimpleNamespace(
                create_task=lambda **kwargs: asyncio.get_running_loop().create_task(kwargs["target"])
            ),
        )
        store.clear_incidents()
        await asyncio.sleep(0)
        assert calls == ["DELETE /incidents"]
