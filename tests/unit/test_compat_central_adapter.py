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

from types import SimpleNamespace

from aiohomematic.central.events import EventBus as AioEventBus
from openccu_loom_types.enums import DataPointCategory
from openccu_loom_types.rest import DataPointSummary, Snapshot
import pytest

from openccu_loom_client import BasicAuth, BearerAuth
from openccu_loom_client.compat.aiohomematic.central import CentralConfig, check_config
from openccu_loom_client.compat.aiohomematic.central.adapter import _JsonRpcClient
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
    "api_version": "1.0.0",
    "commit": "deadbeef",
    "build_date": "2026-05-24T10:00:00Z",
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


_CCU_ENTRY = {
    "name": "home",
    "host": "ccu.local",
    "available": True,
    "is_ha_app": False,
    "configured_interfaces": [],
    "serial": "0000DAEMON1234",  # daemon-reported serial → suffix daemon1234
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
        await _JsonRpcClient(client=client).rename_device(ise_id=4712, name="Kitchen")
        assert calls == [("VCU0000002", "Kitchen")]

    async def test_unknown_ise_id_raises(self) -> None:
        client = self._fake_client([SimpleNamespace(ise_id=1, address="VCU1")], [])
        with pytest.raises(ValueError, match="ise_id 9999"):
            await _JsonRpcClient(client=client).rename_device(ise_id=9999, name="x")


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
