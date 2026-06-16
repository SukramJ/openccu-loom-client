# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Importability + identity tests for the aiohomematic compat namespace.

The shim's main contract is that every ``from aiohomematic.* import …``
statement the ``homematicip_local`` HA integration uses today must
resolve against ``openccu_loom_client.compat.aiohomematic.*`` without
raising. Beyond that we assert a few identity invariants (CentralUnit
IS LoomClient, etc.) so a refactor that accidentally diverges shows
up here.
"""

from __future__ import annotations

import importlib

import pytest

# The full set of (module, symbol) pairs the homematicip_local component
# imports today. Each one MUST resolve from the compat namespace; if a
# name disappears from the daemon side, this test is the first to fail.
_HOMEMATICIP_LOCAL_IMPORTS: list[tuple[str, list[str]]] = [
    ("openccu_loom_client.compat.aiohomematic", ["__version__", "ccu_translations"]),
    (
        "openccu_loom_client.compat.aiohomematic.const",
        [
            "CCUType",
            "CHANNEL_ADDRESS_PATTERN",
            "CLICK_EVENTS",
            "CONF_PASSWORD",
            "CONF_USERNAME",
            "CallSource",
            "CATEGORIES",
            "CentralState",
            "ClientState",
            "DATA_POINT_EVENTS",
            "DEFAULT_ENABLE_PROGRAM_SCAN",
            "DEFAULT_ENABLE_SYSVAR_SCAN",
            "DEFAULT_INTERFACES_REQUIRING_PERIODIC_REFRESH",
            "DEFAULT_MULTIPLIER",
            "DEFAULT_OPTIONAL_SETTINGS",
            "DEFAULT_PROGRAM_MARKERS",
            "DEFAULT_SYSVAR_MARKERS",
            "DEFAULT_UN_IGNORES",
            "DEFAULT_USE_GROUP_CHANNEL_FOR_COVER_STATE",
            "DEVICE_ADDRESS_PATTERN",
            "DataPointCategory",
            "DataPointType",
            "DataPointUsage",
            "DescriptionMarker",
            "DeviceTriggerEventType",
            "FailureReason",
            "ForcedDeviceAvailability",
            "HubValueType",
            "IDENTIFIER_SEPARATOR",
            "IP_ANY_V4",
            "IntegrationIssueSeverity",
            "IntegrationIssueType",
            "Interface",
            "Manufacturer",
            "OptionalSettings",
            "PORT_ANY",
            "Parameter",
            "ParameterType",
            "ParamsetKey",
            "SYSVAR_STATE_PATH_ROOT",
            "ScheduleProfile",
            "ScheduleTimerConfig",
            "SystemInformation",
            "TimeoutConfig",
            "WeekdayStr",
            "get_interface_default_port",
        ],
    ),
    (
        "openccu_loom_client.compat.aiohomematic.exceptions",
        [
            "AuthFailure",
            "BaseHomematicException",
            "NoConnectionException",
            "ValidationException",
        ],
    ),
    (
        "openccu_loom_client.compat.aiohomematic.central",
        ["CentralConfig", "CentralUnit", "check_config", "list_ccus"],
    ),
    (
        "openccu_loom_client.compat.aiohomematic.central.events",
        [
            "CentralStateChangedEvent",
            "DataPointsCreatedEvent",
            "DataPointStateChangedEvent",
            "DeviceCreatedEvent",
            "DeviceLifecycleEvent",
            "DeviceLifecycleEventType",
            "DeviceRemovedEvent",
            "DeviceTriggerEvent",
            "OptimisticRollbackEvent",
            "SubscriptionGroup",
            "SystemStatusChangedEvent",
        ],
    ),
    ("openccu_loom_client.compat.aiohomematic.client", ["InterfaceConfig"]),
    (
        "openccu_loom_client.compat.aiohomematic.interfaces",
        [
            "ChannelEventGroupProtocol",
            "ClimateWeekProfileDataPointProtocol",
            "CombinedDataPointProtocol",
            "DeviceProtocol",
            "ScheduleChannelSwitchProtocol",
        ],
    ),
    (
        "openccu_loom_client.compat.aiohomematic.model.data_point",
        ["CallParameterCollector", "CallbackDataPoint"],
    ),
    ("openccu_loom_client.compat.aiohomematic.model.event", ["ClickEvent"]),
    (
        "openccu_loom_client.compat.aiohomematic.model.custom",
        [
            "BaseCustomDpLock",
            "BaseCustomDpSiren",
            "CustomDpBlind",
            "CustomDpCover",
            "CustomDpGarage",
            "CustomDpIpBlind",
            "CustomDpIpIrrigationValve",
            "CustomDpSoundPlayer",
            "CustomDpSwitch",
            "CustomDpTextDisplay",
            "LockState",
            "PlaySoundArgs",
            "SirenOnArgs",
        ],
    ),
    (
        "openccu_loom_client.compat.aiohomematic.model.custom.text_display",
        ["CustomDpTextDisplay"],
    ),
    (
        "openccu_loom_client.compat.aiohomematic.model.generic",
        [
            "BaseDpActionNumber",
            "BaseDpNumber",
            "DpAction",
            "DpActionSelect",
            "DpBinarySensor",
            "DpButton",
            "DpSelect",
            "DpSwitch",
            "DpText",
        ],
    ),
    (
        "openccu_loom_client.compat.aiohomematic.model.hub",
        [
            "HmUpdate",
            "ProgramDpButton",
            "ProgramDpSwitch",
            "SysvarDpBinarySensor",
            "SysvarDpNumber",
            "SysvarDpSelect",
            "SysvarDpSensor",
            "SysvarDpSwitch",
            "SysvarDpText",
        ],
    ),
    ("openccu_loom_client.compat.aiohomematic.model.schedule_models", ["ClimateWeekdaySchedule"]),
    ("openccu_loom_client.compat.aiohomematic.model.update", ["DpUpdate"]),
    (
        "openccu_loom_client.compat.aiohomematic.model.week_profile_data_point",
        ["WeekProfileDataPoint"],
    ),
    ("openccu_loom_client.compat.aiohomematic.store.persistent", ["cleanup_files"]),
    ("openccu_loom_client.compat.aiohomematic.support", ["find_free_port", "to_bool"]),
    ("openccu_loom_client.compat.aiohomematic.support.address", ["get_device_address"]),
    ("openccu_loom_client.compat.aiohomematic.type_aliases", ["UnsubscribeCallback"]),
    ("openccu_loom_client.compat.aiohomematic.ccu_translations", ["get_device_icon"]),
]


@pytest.mark.parametrize(
    ("module_name", "symbols"),
    _HOMEMATICIP_LOCAL_IMPORTS,
    ids=[m for m, _ in _HOMEMATICIP_LOCAL_IMPORTS],
)
def test_module_exports_expected_symbols(module_name: str, symbols: list[str]) -> None:
    """Each compat module must expose every symbol the HA component imports."""
    module = importlib.import_module(module_name)
    missing = [s for s in symbols if not hasattr(module, s)]
    assert missing == [], f"{module_name} missing: {missing}"


class TestIdentities:
    """Compat aliases should BE the underlying class, not a wrapper."""

    def test_central_unit_is_the_adapter(self) -> None:
        """
        CentralUnit wraps LoomClient with the aiohomematic coordinator surface.

        It exposes device_coordinator, hub_coordinator, … the component
        reaches into — a plain LoomClient alias is not enough.
        """
        from openccu_loom_client.compat.aiohomematic.central import CentralUnit
        from openccu_loom_client.compat.aiohomematic.central.adapter import LoomCentralAdapter

        assert CentralUnit is LoomCentralAdapter

    async def test_central_config_builds_adapter(self) -> None:
        """
        CentralConfig is an aiohomematic-shaped factory, not a bare LoomConfig alias.

        create_central() yields the adapter.
        """
        from openccu_loom_client.compat.aiohomematic.central import CentralConfig, CentralUnit

        central = await CentralConfig(
            name="home", host="loom.test", port=8080, tls=False, token="t0ken1"
        ).create_central()
        assert isinstance(central, CentralUnit)
        assert central.name == "home"

    def test_callback_data_point_is_data_point(self) -> None:
        from openccu_loom_client.compat.aiohomematic.model.data_point import CallbackDataPoint
        from openccu_loom_client.model import DataPoint

        assert CallbackDataPoint is DataPoint

    def test_data_point_state_changed_is_distinct_uniform_event(self) -> None:
        """
        ``DataPointStateChangedEvent`` is the uniform refresh event.

        It is NO LONGER an alias of the daemon's ``DataPointValueChangedEvent``:
        the refresh bridge fans value/custom/sysvar changes into this one
        event, keyed by unique_id, so generic, custom and hub entities all
        refresh through the same subscription contract.
        """
        from openccu_loom_client.compat.aiohomematic.central.events import DataPointStateChangedEvent
        from openccu_loom_client.events import DataPointValueChangedEvent

        assert DataPointStateChangedEvent is not DataPointValueChangedEvent
        assert DataPointStateChangedEvent.type_id == "client.data_point_state_changed"

    def test_subscription_group_is_shared(self) -> None:
        from openccu_loom_client.compat.aiohomematic.central.events import SubscriptionGroup
        from openccu_loom_client.events import SubscriptionGroup as RealGroup

        assert SubscriptionGroup is RealGroup

    def test_auth_failure_is_loom_auth_error(self) -> None:
        from openccu_loom_client import LoomAuthError
        from openccu_loom_client.compat.aiohomematic.exceptions import AuthFailure

        assert AuthFailure is LoomAuthError


class TestSupportHelpers:
    def test_find_free_port_returns_usable_port(self) -> None:
        from openccu_loom_client.compat.aiohomematic.support import find_free_port

        port = find_free_port()
        assert isinstance(port, int)
        assert port > 0

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (True, True),
            (False, False),
            (1, True),
            (0, False),
            ("true", True),
            ("YES", True),
            ("1", True),
            ("no", False),
            ("", False),
            (None, False),
        ],
    )
    def test_to_bool(self, value: object, expected: bool) -> None:
        from openccu_loom_client.compat.aiohomematic.support import to_bool

        assert to_bool(value=value) is expected

    def test_get_device_address_strips_channel(self) -> None:
        from openccu_loom_client.compat.aiohomematic.support.address import get_device_address

        assert get_device_address(address="VCU0001:3") == "VCU0001"
        assert get_device_address(address="VCU0001") == "VCU0001"


class TestCheckConfig:
    async def test_returns_failures_on_empty_input(self) -> None:
        from openccu_loom_client.compat.aiohomematic.central import check_config

        failures = await check_config(central_name="", host="")
        assert "central_name is required" in failures
        assert "host is required" in failures

    async def test_returns_empty_when_minimal_input_ok(self) -> None:
        from openccu_loom_client.compat.aiohomematic.central import check_config

        failures = await check_config(central_name="test", host="loom.local")
        assert failures == []


class TestListCcus:
    async def test_projects_entries_and_closes(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock, patch

        from openccu_loom_client.compat.aiohomematic.central import list_ccus

        client = MagicMock()
        client.connect = AsyncMock()
        client.close = AsyncMock()
        client.system.list_system_ccus = AsyncMock(
            return_value=[
                SimpleNamespace(name="Home", serial="ABC123", host="ccu.local", model="CCU3", available=True),
            ]
        )
        with (
            patch("openccu_loom_client.compat.aiohomematic.central.HttpTransport"),
            patch("openccu_loom_client.compat.aiohomematic.central.LoomClient", return_value=client),
        ):
            result = await list_ccus(host="daemon.local", token="tok", port=8080, tls=False)

        assert result == [
            {
                "name": "Home",
                "serial": "ABC123",
                "host": "ccu.local",
                "model": "CCU3",
                "available": True,
            }
        ]
        client.connect.assert_awaited_once()
        client.close.assert_awaited_once()

    async def test_closes_on_connect_error(self) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        from openccu_loom_client.compat.aiohomematic.central import list_ccus

        client = MagicMock()
        client.connect = AsyncMock(side_effect=RuntimeError("boom"))
        client.close = AsyncMock()
        with (
            patch("openccu_loom_client.compat.aiohomematic.central.HttpTransport"),
            patch("openccu_loom_client.compat.aiohomematic.central.LoomClient", return_value=client),
            pytest.raises(RuntimeError),
        ):
            await list_ccus(host="daemon.local", token="tok")
        client.close.assert_awaited_once()


class TestCallParameterCollector:
    async def test_collector_flushes_as_one_paramset_put(self) -> None:
        from openccu_loom_client.compat.aiohomematic.model.data_point import CallParameterCollector

        class _StubOps:
            def __init__(self) -> None:
                self.put_paramset_calls: list[dict] = []

            async def put_paramset(self, *, address, paramset_key, values):  # type: ignore[no-untyped-def]
                self.put_paramset_calls.append({"address": address, "paramset_key": paramset_key, "values": values})

        ops = _StubOps()
        async with CallParameterCollector(
            datapoints_ops=ops,  # type: ignore[arg-type]
            address="VCU0001",
            channel=1,
        ) as c:
            c.add_data(parameter="LEVEL", value=0.5)
            c.add_data(parameter="STATE", value=True)

        assert len(ops.put_paramset_calls) == 1
        call = ops.put_paramset_calls[0]
        assert call["address"] == "VCU0001"
        assert call["paramset_key"] == "VALUES"
        assert call["values"] == {"LEVEL": 0.5, "STATE": True}

    async def test_collector_no_op_on_empty(self) -> None:
        from openccu_loom_client.compat.aiohomematic.model.data_point import CallParameterCollector

        class _StubOps:
            def __init__(self) -> None:
                self.calls = 0

            async def put_paramset(self, **_kwargs):  # type: ignore[no-untyped-def]
                self.calls += 1

        ops = _StubOps()
        c = CallParameterCollector(
            datapoints_ops=ops,  # type: ignore[arg-type]
            address="VCU0001",
            channel=1,
        )
        await c.send_data()
        assert ops.calls == 0


class TestCustomDpMarkers:
    """The CustomDp* subclasses must extend CustomDataPoint so isinstance dispatches."""

    def test_custom_dp_switch_is_a_custom_data_point(self) -> None:
        from openccu_loom_client.compat.aiohomematic.model.custom import CustomDpSwitch
        from openccu_loom_client.model import CustomDataPoint

        assert issubclass(CustomDpSwitch, CustomDataPoint)

    def test_cover_blind_garage_share_lineage(self) -> None:
        from openccu_loom_client.compat.aiohomematic.model.custom import (
            CustomDpBlind,
            CustomDpCover,
            CustomDpGarage,
            CustomDpIpBlind,
        )

        assert issubclass(CustomDpBlind, CustomDpCover)
        assert issubclass(CustomDpIpBlind, CustomDpBlind)
        assert issubclass(CustomDpGarage, CustomDpCover)
