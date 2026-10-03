# SPDX-License-Identifier: MIT
# Copyright (C) 2026 SukramJ.

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
import pytest

from openccu_loom_client import BasicAuth, BearerAuth, BoxIngressConfig, LoomConfig, NoAuth
from openccu_loom_client.compat.aiohomematic._upstream import BackupData, ParamsetKey
from openccu_loom_client.compat.aiohomematic.central import CentralConfig, check_config
import openccu_loom_client.compat.aiohomematic.central.adapter as adapter_module
from openccu_loom_client.compat.aiohomematic.central.adapter import (
    CentralState,
    _ClientCoordinator,
    _Configuration,
    _IncidentStore,
    _JsonRpcClient,
    _LinkCoordinator,
    _ui_schema_to_parameter_data,
)
from openccu_loom_client.compat.aiohomematic.central.events import SystemInformationChangedEvent
from openccu_loom_client.events import ConnectionStateChangedEvent as LoomConnectionStateChangedEvent
from openccu_loom_client.exceptions import LoomConflictError, LoomForbiddenError, LoomNotFoundError
from openccu_loom_client.wire import DAEMON_API_VERSION
from openccu_loom_client.wire.enums import DataPointCategory
from openccu_loom_client.wire.rest import AlarmMessage, DataPointSummary, Kind2 as Kind, Link, ServiceMessage, Snapshot
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
    # The daemon's always-on set, not a subset: capabilities() in
    # internal/north/rest/handlers/info.go emits these three unconditionally
    # and has since the initial release, so a fake advertising only "rest.v1"
    # models a daemon that has never existed — and it hid the fact that this
    # adapter now names its preconditions at connect().
    "capabilities": ["rest.v1", "ws.broadcasts.v1", "errors.problem_details.v1"],
    "schema_digest": "sha256:test",
    "config_ui_url": "",
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


def _conn_event(*, connected: bool) -> LoomConnectionStateChangedEvent:
    """Build the client-side connection transition the adapter subscribes to."""
    return LoomConnectionStateChangedEvent(
        seq=0,
        kind=Kind.change,
        ts=datetime.now(tz=UTC),
        topic=None,
        type=LoomConnectionStateChangedEvent.type_id,
        connected=connected,
    )


class TestDegradedGrace:
    """A short WS flap must not be announced as a state change."""

    async def test_brief_drop_is_not_reported(self, monkeypatch) -> None:
        """
        The transport retries 0.5 s after a drop; most are over before that.

        Reporting each one costs more than it tells — a consumer that renders
        the transition would show a pair of them for an outage nobody noticed.
        """
        monkeypatch.setattr(adapter_module, "_DEGRADED_GRACE_SECONDS", 0.2)
        central = await _make_config().create_central()
        central._state = CentralState.Running

        await central._on_connection_state_changed(_conn_event(connected=False))
        await asyncio.sleep(0.05)
        assert central._state is CentralState.Running, "reported before the grace window elapsed"

        await central._on_connection_state_changed(_conn_event(connected=True))
        await asyncio.sleep(0.3)
        assert central._state is CentralState.Running, "a reconnect must cancel the pending report"

    async def test_sustained_drop_is_reported(self, monkeypatch) -> None:
        monkeypatch.setattr(adapter_module, "_DEGRADED_GRACE_SECONDS", 0.05)
        central = await _make_config().create_central()
        central._state = CentralState.Running

        await central._on_connection_state_changed(_conn_event(connected=False))
        await asyncio.sleep(0.2)
        assert central._state is CentralState.Degraded

    async def test_recovery_is_immediate(self, monkeypatch) -> None:
        """Staleness ending is news at once; by then the connection has proven itself."""
        monkeypatch.setattr(adapter_module, "_DEGRADED_GRACE_SECONDS", 5.0)
        central = await _make_config().create_central()
        central._state = CentralState.Degraded

        await central._on_connection_state_changed(_conn_event(connected=True))
        assert central._state is CentralState.Running

    async def test_stop_cancels_a_pending_report(self, monkeypatch) -> None:
        monkeypatch.setattr(adapter_module, "_DEGRADED_GRACE_SECONDS", 0.05)
        central = await _make_config().create_central()
        central._state = CentralState.Running
        await central._on_connection_state_changed(_conn_event(connected=False))
        pending = central._degraded_task
        assert pending is not None
        central._degraded_task.cancel()
        await asyncio.sleep(0.1)
        assert central._state is CentralState.Running


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


class TestConfigUIURL:
    """
    The browser-reachable Config-UI address, distinct from ``url``.

    ``url`` is how THIS process reaches the daemon; a consumer that linked
    a person there would send them at a container address no browser can
    follow. These pin that the two stay separate and that an unconfigured
    daemon yields no guess.
    """

    async def test_comes_from_the_daemon_when_configured(self, mock_daemon: MockDaemon) -> None:
        mock_daemon.get(
            f"{_BASE}/info",
            payload={**_INFO, "config_ui_url": "https://loom.example.de/app/"},
        )
        central = await _make_config(mock_daemon=mock_daemon).create_central()
        await central._client.connect()
        try:
            assert central.config_ui_url == "https://loom.example.de/app/"
            # The connection address answers a different question and must
            # not have been displaced by the public one.
            assert central.url != central.config_ui_url
        finally:
            await central._client.close()

    async def test_is_empty_when_the_daemon_has_no_public_url(self, connected) -> None:
        central, _ = connected
        assert central.config_ui_url == ""

    async def test_is_empty_before_connect(self, mock_daemon: MockDaemon) -> None:
        # No handshake yet, so no payload to read. Callers get "" rather
        # than an exception: this is a link, not a precondition.
        central = await _make_config(mock_daemon=mock_daemon).create_central()
        assert central.config_ui_url == ""


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

    async def test_version_comes_from_the_ccu_not_the_daemon(self, connected) -> None:
        """
        SystemInformation.version is the CCU firmware version.

        ``GET /info`` reports the daemon's own build version — HA renders
        ``central.version`` as the CCU device's sw_version, and the backup
        filename embeds it too, so leaking the daemon build version here
        makes both surfaces lie about which firmware runs.
        """
        central, mock = connected
        mock.get(f"{_BASE}/system/ccu", payload=[{**_CCU_ENTRY, "version": "3.87.6.20260404"}])
        mock.get(f"{_BASE}/interfaces", payload=[])
        info = await central.validate_config_and_get_system_information()
        assert info.version == "3.87.6.20260404"
        assert central.version == "3.87.6.20260404"
        # The daemon's own build version must not leak through.
        assert info.version != "1.2.3"

    async def test_version_falls_back_to_daemon_before_the_first_ccu_connect(self, connected) -> None:
        # The /system/ccu entry carries no version yet (older daemon, or the
        # daemon has never reached the CCU) — fall back rather than going empty.
        central, mock = connected
        mock.get(f"{_BASE}/system/ccu", payload=[_CCU_ENTRY])
        mock.get(f"{_BASE}/interfaces", payload=[])
        info = await central.validate_config_and_get_system_information()
        assert info.version == "1.2.3"
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

# An openccu-lite central whose daemon credential lacks the backup scope.
_LITE_ENTRY = {
    **_CCU_ENTRY,
    "system_type": "openccu-lite",
    "model": "openccu-lite",
    "features": {
        "system.backup.create": {"available": False, "reason": "missing_scope", "scope": "backup"},
        "hub.system_update.install": {"available": False, "reason": "not_supported_by_system"},
        "system.reboot": {"available": True},
    },
}


class TestSystemTypeAndFeatures:
    """The central's system type and feature map decide ccu_type, backup and system update."""

    async def test_lite_central_without_backup_scope(self, connected) -> None:
        from aiohomematic.const import CCUType

        central, mock = connected
        mock.get(f"{_BASE}/system/ccu", payload=[_LITE_ENTRY])
        mock.get(f"{_BASE}/interfaces", payload=[])
        info = await central.validate_config_and_get_system_information()
        assert info.ccu_type is CCUType.OPENCCU_LITE
        assert info.has_backup is False
        assert info.has_system_update is False

    async def test_lite_central_with_backup_scope(self, connected) -> None:
        from aiohomematic.const import CCUType

        central, mock = connected
        entry = {
            **_LITE_ENTRY,
            "features": {
                "system.backup.create": {"available": True},
                "hub.system_update.install": {"available": True},
            },
        }
        mock.get(f"{_BASE}/system/ccu", payload=[entry])
        mock.get(f"{_BASE}/interfaces", payload=[])
        info = await central.validate_config_and_get_system_information()
        assert info.ccu_type is CCUType.OPENCCU_LITE
        assert info.has_backup is True
        assert info.has_system_update is True

    async def test_openccu_without_feature_map_falls_back_to_the_type(self, connected) -> None:
        """An empty map is an older daemon, not a central that offers nothing."""
        from aiohomematic.const import CCUType

        central, mock = connected
        mock.get(f"{_BASE}/system/ccu", payload=[{**_CCU_ENTRY, "system_type": "ccu", "model": "OpenCCU"}])
        mock.get(f"{_BASE}/interfaces", payload=[])
        info = await central.validate_config_and_get_system_information()
        assert info.ccu_type is CCUType.OPENCCU
        assert info.has_backup is True
        assert info.has_system_update is True

    async def test_unread_model_and_no_features_is_unknown(self, connected) -> None:
        from aiohomematic.const import CCUType

        central, mock = connected
        mock.get(f"{_BASE}/system/ccu", payload=[{**_CCU_ENTRY, "model": ""}])
        mock.get(f"{_BASE}/interfaces", payload=[])
        info = await central.validate_config_and_get_system_information()
        assert info.ccu_type is CCUType.UNKNOWN
        assert info.has_backup is False
        assert info.has_system_update is False

    async def test_reads_this_centrals_entry_not_the_first(self, connected) -> None:
        from aiohomematic.const import CCUType

        central, mock = connected
        other = {**_CCU_ENTRY, "name": "other", "system_type": "ccu", "model": "OpenCCU"}
        mock.get(f"{_BASE}/system/ccu", payload=[other, _LITE_ENTRY])
        mock.get(f"{_BASE}/interfaces", payload=[])
        info = await central.validate_config_and_get_system_information()
        assert info.ccu_type is CCUType.OPENCCU_LITE
        assert info.has_backup is False

    async def test_list_ccus_carries_system_type_and_features(self, mock_daemon: MockDaemon) -> None:
        from openccu_loom_client.compat.aiohomematic.central import list_ccus

        mock_daemon.get(f"{_BASE}/info", payload=_INFO)
        mock_daemon.get(
            f"{_BASE}/system/ccu",
            payload=[_LITE_ENTRY, {**_CCU_ENTRY, "name": "older"}],
        )
        result = await list_ccus(host=mock_daemon.host, port=mock_daemon.port, token="tok-123456")
        assert result[0]["system_type"] == "openccu-lite"
        assert result[0]["features"] == {
            "system.backup.create": {"available": False, "reason": "missing_scope", "scope": "backup"},
            "hub.system_update.install": {"available": False, "reason": "not_supported_by_system", "scope": None},
            "system.reboot": {"available": True, "reason": None, "scope": None},
        }
        # Plain values, not wire enums: the config flow stores them as-is.
        assert type(result[0]["system_type"]) is str
        assert type(result[0]["features"]["system.backup.create"]["reason"]) is str
        # An older daemon reports neither.
        assert result[1]["system_type"] is None
        assert result[1]["features"] == {}


def _features_event(*, central: str, backup: bool) -> Any:
    """Build the daemon's ``central.features_changed`` broadcast."""
    from openccu_loom_client.events import CentralFeaturesChangedEvent
    from openccu_loom_client.wire.rest import CentralFeaturesChangedPayload

    return CentralFeaturesChangedEvent(
        seq=0,
        kind=Kind.change,
        ts=datetime.now(tz=UTC),
        topic=None,
        type=CentralFeaturesChangedEvent.type_id,
        payload=CentralFeaturesChangedPayload.model_validate(
            {
                "central": central,
                "system_type": "openccu-lite",
                "features": {"system.backup.create": {"available": backup}},
            }
        ),
    )


async def _wait_for(predicate: Any) -> bool:
    """Poll ``predicate`` for up to two seconds; the refresh runs as a background task."""
    deadline = asyncio.get_running_loop().time() + 2.0
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return bool(predicate())


class TestFeaturesChangedSubscriber:
    """
    A ``central.features_changed`` broadcast re-reads this central's system information.

    Driven through ``start()`` so the subscription is the one production
    installs, with only the network-heavy bring-up steps stubbed out.
    """

    @staticmethod
    async def _started(
        *,
        mock_daemon: MockDaemon,
        monkeypatch: pytest.MonkeyPatch,
        later_ccu: dict[str, Any] | None = None,
        received: dict[str, list[Any]] | None = None,
    ) -> tuple[Any, list[int]]:
        mock_daemon.get(f"{_BASE}/info", payload=_INFO)
        # The first read (start) finds backup unavailable; every later one
        # (the broadcast-driven refresh) finds it granted unless the caller
        # names another entry.
        mock_daemon.get(f"{_BASE}/system/ccu", payload=[_LITE_ENTRY])
        granted = {**_LITE_ENTRY, "features": {"system.backup.create": {"available": True}}}
        mock_daemon.get(f"{_BASE}/system/ccu", payload=[later_ccu if later_ccu is not None else granted])
        mock_daemon.get(f"{_BASE}/interfaces", payload=[])
        central = await _make_config(mock_daemon=mock_daemon).create_central()
        if received is not None:
            # Subscribed before start(), so the initial population is observed too.
            for key, sink in received.items():
                central.event_bus.subscribe(
                    event_type=SystemInformationChangedEvent,
                    event_key=key,
                    handler=lambda *, event, sink=sink: sink.append(event),
                )

        async def _noop(*_args: Any, **_kwargs: Any) -> None:
            return None

        for target, attr in (
            (central.client_coordinator, "refresh"),
            (central._client, "wait_until_ready"),
            (central._client, "start_events"),
            (central, "_bootstrap_model"),
            (central.query_facade, "prefetch_un_ignore_candidates"),
            (central, "_emit_data_points_created"),
            (central, "_hub_reconcile_loop"),
        ):
            monkeypatch.setattr(target, attr, _noop)

        refreshes: list[int] = []
        real_refresh = central._refresh_system_information

        async def _counting_refresh() -> None:
            refreshes.append(1)
            await real_refresh()

        monkeypatch.setattr(central, "_refresh_system_information", _counting_refresh)
        await central.start()
        return central, refreshes

    async def test_event_for_this_central_refreshes(self, mock_daemon: MockDaemon, monkeypatch) -> None:
        central, refreshes = await self._started(mock_daemon=mock_daemon, monkeypatch=monkeypatch)
        try:
            assert central.system_information.has_backup is False
            assert len(refreshes) == 1
            await central._client.events.publish(event=_features_event(central="home", backup=True))
            assert await _wait_for(lambda: central.system_information.has_backup is True), (
                "a features_changed broadcast for this central must re-read its system information"
            )
            assert len(refreshes) == 2
        finally:
            await central.stop()

    async def test_event_for_another_central_is_ignored(self, mock_daemon: MockDaemon, monkeypatch) -> None:
        central, refreshes = await self._started(mock_daemon=mock_daemon, monkeypatch=monkeypatch)
        try:
            await central._client.events.publish(event=_features_event(central="other", backup=True))
            await asyncio.sleep(0.1)
            assert len(refreshes) == 1
            assert central.system_information.has_backup is False
        finally:
            await central.stop()

    async def test_no_refresh_after_stop(self, mock_daemon: MockDaemon, monkeypatch) -> None:
        central, refreshes = await self._started(mock_daemon=mock_daemon, monkeypatch=monkeypatch)
        events = central._client.events
        await central.stop()
        await events.publish(event=_features_event(central="home", backup=True))
        await asyncio.sleep(0.1)
        assert len(refreshes) == 1

    async def test_refresh_failure_is_logged_not_raised(self, mock_daemon: MockDaemon, monkeypatch, caplog) -> None:
        central, _ = await self._started(mock_daemon=mock_daemon, monkeypatch=monkeypatch)
        try:

            async def _boom() -> None:
                raise RuntimeError("daemon went away")

            monkeypatch.setattr(central, "_refresh_system_information", _boom)
            await central._client.events.publish(event=_features_event(central="home", backup=True))
            assert await _wait_for(lambda: "daemon went away" in caplog.text)
            # Logged by the adapter, not caught by the bus as a raising handler.
            assert "event handler raised" not in caplog.text
            assert central.system_information.has_backup is False
        finally:
            await central.stop()


class TestSystemInformationChangedEvent:
    """
    A re-read that changes this central's system information is announced on ``event_bus``.

    Driven through ``start()`` and the ``central.features_changed`` subscriber,
    the production path that re-reads it.
    """

    _started = staticmethod(TestFeaturesChangedSubscriber._started)

    async def test_changed_value_publishes_one_event(self, mock_daemon: MockDaemon, monkeypatch) -> None:
        received: dict[str, list[Any]] = {"home": []}
        central, refreshes = await self._started(mock_daemon=mock_daemon, monkeypatch=monkeypatch, received=received)
        try:
            await central._client.events.publish(event=_features_event(central="home", backup=True))
            assert await _wait_for(lambda: len(received["home"]) == 1), (
                "a re-read that grants backup must announce the change"
            )
            await asyncio.sleep(0.1)
            assert len(refreshes) == 2
            assert len(received["home"]) == 1
            event = received["home"][0]
            assert event.key == "home"
            assert event.central_name == "home"
            assert event.previous.has_backup is False
            assert event.current.has_backup is True
            assert event.current is central.system_information
        finally:
            await central.stop()

    async def test_identical_value_publishes_nothing(self, mock_daemon: MockDaemon, monkeypatch) -> None:
        received: dict[str, list[Any]] = {"home": []}
        central, refreshes = await self._started(
            mock_daemon=mock_daemon, monkeypatch=monkeypatch, later_ccu=_LITE_ENTRY, received=received
        )
        try:
            await central._client.events.publish(event=_features_event(central="home", backup=False))
            assert await _wait_for(lambda: len(refreshes) == 2)
            await asyncio.sleep(0.1)
            assert received["home"] == []
        finally:
            await central.stop()

    async def test_initial_population_publishes_nothing(self, mock_daemon: MockDaemon, monkeypatch) -> None:
        received: dict[str, list[Any]] = {"home": []}
        central, refreshes = await self._started(mock_daemon=mock_daemon, monkeypatch=monkeypatch, received=received)
        try:
            await asyncio.sleep(0.1)
            assert len(refreshes) == 1
            # The start read replaced the UNKNOWN placeholder with the lite
            # entry, a different value — and still announced nothing.
            assert received["home"] == []
        finally:
            await central.stop()

    async def test_failed_refresh_publishes_nothing_and_keeps_value(
        self, mock_daemon: MockDaemon, monkeypatch, caplog
    ) -> None:
        received: dict[str, list[Any]] = {"home": []}
        central, refreshes = await self._started(mock_daemon=mock_daemon, monkeypatch=monkeypatch, received=received)
        try:
            before = central.system_information

            async def _boom(*_args: Any, **_kwargs: Any) -> None:
                raise RuntimeError("daemon went away")

            monkeypatch.setattr(central._client.system, "get_info", _boom)
            await central._client.events.publish(event=_features_event(central="home", backup=True))
            assert await _wait_for(lambda: "daemon went away" in caplog.text)
            await asyncio.sleep(0.1)
            assert len(refreshes) == 2
            assert received["home"] == []
            assert central.system_information is before
        finally:
            await central.stop()

    async def test_subscriber_for_another_central_is_not_called(self, mock_daemon: MockDaemon, monkeypatch) -> None:
        received: dict[str, list[Any]] = {"home": [], "other": []}
        central, _ = await self._started(mock_daemon=mock_daemon, monkeypatch=monkeypatch, received=received)
        try:
            await central._client.events.publish(event=_features_event(central="home", backup=True))
            assert await _wait_for(lambda: len(received["home"]) == 1)
            await asyncio.sleep(0.1)
            assert received["other"] == []
        finally:
            await central.stop()

    def test_is_an_aiohomematic_event(self) -> None:
        from aiohomematic.central.events.types import Event as AioEvent

        from openccu_loom_client.compat.aiohomematic.const import make_system_information

        info = make_system_information()
        event = SystemInformationChangedEvent(
            timestamp=datetime.now(tz=UTC), central_name="home", previous=info, current=info
        )
        assert isinstance(event, AioEvent)
        assert event.key == "home"


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
                            "firmware": {
                                "Current": "1.0.0",
                                "Available": "",
                                "Updatable": False,
                                "UpdateState": "UP_TO_DATE",
                            },
                            "availability": {
                                "IsReachable": True,
                                "LastUpdated": None,
                                "BatteryLevel": None,
                                "LowBattery": None,
                                "SignalStrength": None,
                            },
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
                            "firmware": {
                                "Current": "1.0.0",
                                "Available": "",
                                "Updatable": False,
                                "UpdateState": "UP_TO_DATE",
                            },
                            "availability": {
                                "IsReachable": True,
                                "LastUpdated": None,
                                "BatteryLevel": None,
                                "LowBattery": None,
                                "SignalStrength": None,
                            },
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


_BOX_TOKEN = "olt_0123456789abcdef0123456789abcdef"


class TestCheckConfig:
    async def test_check_config_static_validation(self) -> None:
        assert await check_config(central_name="home", host="loom.test") == []
        failures = await check_config(central_name="", host="")
        assert len(failures) == 2

    @pytest.mark.parametrize("extra", [{"box_port": 8443}, {"box_path_prefix": "/addons/other"}])
    async def test_box_details_without_a_box_token_fail(self, extra: dict[str, Any]) -> None:
        failures = await check_config(central_name="home", host="box.test", **extra)
        assert failures == ["box_token is required when box_port or box_path_prefix is given"]

    async def test_complete_box_config_passes(self) -> None:
        failures = await check_config(
            central_name="home",
            host="box.test",
            box_token=_BOX_TOKEN,
            box_port=8443,
            box_path_prefix="/addons/other",
        )
        assert failures == []

    @pytest.mark.parametrize("kwarg", ["box_tokne", "box_username", "box_password"])
    async def test_unknown_box_kwarg_raises(self, kwarg: str) -> None:
        # box_username / box_password are gone with the box-account mode; a
        # caller still passing them must fail loudly, not connect directly.
        with pytest.raises(TypeError, match=kwarg):
            await check_config(central_name="home", host="box.test", **{kwarg: "x"})

    async def test_non_box_unknown_kwarg_is_ignored(self) -> None:
        assert await check_config(central_name="home", host="loom.test", interface_configs=frozenset()) == []


class TestCentralConfigBoxIngress:
    """The box_* keywords reach LoomConfig.box_ingress; an unknown box_* key is never swallowed."""

    async def test_box_params_build_box_ingress_with_no_auth(self) -> None:
        central = await _make_config(
            token=None, box_token=_BOX_TOKEN, box_port=8443, box_path_prefix="/addons/other"
        ).create_central()
        box = central._client.config.box_ingress
        assert box == BoxIngressConfig(token=_BOX_TOKEN, port=8443, path_prefix="/addons/other")
        assert isinstance(central._client.config.auth, NoAuth)

    async def test_box_path_prefix_defaults(self) -> None:
        central = await _make_config(token=None, box_token=_BOX_TOKEN).create_central()
        box = central._client.config.box_ingress
        assert box is not None
        assert box.port is None
        assert box.path_prefix == BoxIngressConfig(token="x").path_prefix

    async def test_without_box_params_connects_directly(self) -> None:
        central = await _make_config().create_central()
        assert central._client.config.box_ingress is None

    @pytest.mark.parametrize(
        "credential",
        [
            {"token": "tok-123456"},
            {"token": None, "username": "admin", "password": "secret"},
            {"token": None, "auth": BearerAuth(token="tok-123456")},
        ],
    )
    def test_a_daemon_credential_beside_the_box_token_is_refused(self, credential: dict[str, Any]) -> None:
        # The box token occupies the Authorization header the gate reads; a
        # daemon credential could not ride beside it, so it must not be
        # silently dropped either.
        with pytest.raises(ValueError, match="only credential"):
            _make_config(box_token=_BOX_TOKEN, **credential)

    async def test_empty_daemon_token_beside_the_box_token_is_no_credential(self) -> None:
        central = await _make_config(token="", box_token=_BOX_TOKEN).create_central()
        assert isinstance(central._client.config.auth, NoAuth)

    def test_no_credentials_outside_box_mode_still_raises(self) -> None:
        with pytest.raises(ValueError, match="CentralConfig needs an auth method, a token, or username\\+password"):
            CentralConfig(host="loom.test", token=None, box_port=8443)

    @pytest.mark.parametrize("kwarg", ["box_tokne", "box_username", "box_password"])
    def test_unknown_box_kwarg_raises(self, kwarg: str) -> None:
        with pytest.raises(TypeError, match=kwarg):
            _make_config(token=None, box_token=_BOX_TOKEN, **{kwarg: "x"})

    async def test_non_box_unknown_kwarg_is_still_ignored(self) -> None:
        central = await _make_config(callback_host="1.2.3.4").create_central()
        assert central.name == "home"


class TestListCcusBoxIngress:
    """list_ccus builds the same box config, with the box token as the only credential."""

    @staticmethod
    async def _built_config(**kwargs: Any) -> LoomConfig:
        from unittest.mock import AsyncMock, MagicMock, patch

        from openccu_loom_client.compat.aiohomematic.central import list_ccus

        client = MagicMock()
        client.connect = AsyncMock()
        client.close = AsyncMock()
        client.system.list_system_ccus = AsyncMock(return_value=[])
        with (
            patch("openccu_loom_client.compat.aiohomematic.central.HttpTransport"),
            patch("openccu_loom_client.compat.aiohomematic.central.LoomClient", return_value=client) as client_cls,
        ):
            assert await list_ccus(host="box.test", tls=True, **kwargs) == []
        config = client_cls.call_args.kwargs["config"]
        assert isinstance(config, LoomConfig)
        return config

    async def test_box_params_build_box_ingress_with_no_auth(self) -> None:
        config = await self._built_config(box_token=_BOX_TOKEN, box_port=8443, box_path_prefix="/addons/other")
        assert config.box_ingress == BoxIngressConfig(token=_BOX_TOKEN, port=8443, path_prefix="/addons/other")
        assert isinstance(config.auth, NoAuth)

    async def test_a_daemon_token_beside_the_box_token_is_refused(self) -> None:
        with pytest.raises(ValueError, match="only credential"):
            await self._built_config(token="tok-123456", box_token=_BOX_TOKEN)

    async def test_without_box_params_blank_token_stays_bearer(self) -> None:
        config = await self._built_config()
        assert config.box_ingress is None
        assert isinstance(config.auth, BearerAuth)


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
                "timestamp": timestamp,
                "last_timestamp": datetime(2026, 7, 13, 11, 0, tzinfo=UTC),
                "counter": 2,
                "rooms": ["Kitchen", "Hall"],
                "functions": ["Light"],
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
                "timestamp": timestamp,
                "last_timestamp": datetime(2026, 7, 13, 11, 30, tzinfo=UTC),
                "counter": 1,
                "display_name": "Error",
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
            list_inbox=lambda: ret(
                [
                    {"central": "home", "address": "NEW1", "model": "HmIP-PS", "serial": "SER1"},
                    # Accepted, not released: same listing, different action.
                    {
                        "central": "home",
                        "address": "MID1",
                        "model": "HmIP-BSM",
                        "serial": "SER2",
                        "awaiting_release": True,
                    },
                ]
            ),
            ack_service_message=ack_service,
            ack_alarm_message=ack_alarm,
        )
        devices = SimpleNamespace(
            accept_device=lambda **_kw: ret(None),
            release_device=lambda **_kw: ret(None),
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
        # rooms/functions have been on the record all along; the daemon only
        # started populating them with api 5.0.0, and they arrive as arrays.
        assert payload["rooms"] == ("Kitchen", "Hall")
        assert payload["functions"] == ("Light",)
        assert payload["last_timestamp"].startswith("2026-07-13T11:00")

    async def test_alarm_messages_are_asdict_able_records(self) -> None:
        json_rpc, _ = self._client()
        alarms = await json_rpc.get_alarm_messages()
        payload = dataclasses.asdict(alarms[0])
        assert payload["alarm_id"] == "A1"
        assert payload["last_timestamp"].startswith("2026-07-13T11:30")
        # An alarm entry is backed by a system variable a program raises, not
        # by a device: the CCU reports the trigger data point as the "unknown"
        # sentinel, so these three never carried data and left the wire with
        # api 4.0.0. Filling them here would mean inventing a device.
        assert payload["device_name"] == ""
        assert payload["last_trigger"] == ""
        assert payload["rooms"] == ()

    async def test_inbox_devices_are_asdict_able_records(self) -> None:
        json_rpc, _ = self._client()
        devices = await json_rpc.get_inbox_devices()
        assert dataclasses.asdict(devices[0]) == {
            "device_id": "SER1",
            "address": "NEW1",
            "name": "NEW1",
            "device_type": "HmIP-PS",
            "interface": "",
            "awaiting_release": False,
        }

    async def test_awaiting_release_survives_the_conversion(self) -> None:
        """
        The two inbox states share one listing and need different actions.

        An entry flagged here is already accepted and needs a release; the rest
        are waiting for an accept. The flag used to be dropped in the
        conversion, leaving a consumer one action for both — wrong for half of
        them.
        """
        json_rpc, _ = self._client()
        by_address = {d.address: d for d in await json_rpc.get_inbox_devices()}
        assert by_address["MID1"].awaiting_release is True
        assert by_address["NEW1"].awaiting_release is False

    async def test_release_is_offered_as_its_own_mutation(self) -> None:
        json_rpc, _ = self._client()
        assert await json_rpc.release_device_in_inbox(device_address="MID1") is True

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


class TestCreateBackupAndDownload:
    """
    #579: the compat backup surface returns a ``BackupData``.

    HA's "create backup" button (and the backup agent, the update entity and
    the WS API) read ``.filename`` / ``.content`` off the result, exactly as
    they do for aiohomematic's ``CentralUnit.create_backup_and_download``.
    Returning the daemon's raw trigger dict raised ``AttributeError`` on every
    press; this pins the aiohomematic-shaped return.
    """

    async def test_returns_backup_data_with_filename_and_content(self, connected) -> None:
        central, mock_daemon = connected
        # POST /backups is async: it returns a job id; the archive then appears
        # in GET /backups, and is fetched by id.
        mock_daemon.post(f"{_BASE}/backups", payload={"id": "bk-1"}, status=202)
        mock_daemon.get(
            f"{_BASE}/backups",
            payload=[{"id": "bk-1", "central": "home", "bytes": 11, "created_at": "2026-08-17T00:00:00Z"}],
        )
        mock_daemon.get(
            f"{_BASE}/backups/bk-1/download",
            body=b"SBK-ARCHIVE",
            content_type="application/octet-stream",
        )

        result = await central.create_backup_and_download()

        assert isinstance(result, BackupData)
        assert result.content == b"SBK-ARCHIVE"
        assert result.filename.endswith(".sbk")

    async def test_backup_filename_carries_the_ccu_firmware_version(self, connected) -> None:
        """
        The filename mirrors aiohomematic's ``hostname-version-timestamp.sbk``.

        ``version`` there is the CCU's own firmware version — a daemon build
        version (e.g. "0.61.4") produces a filename HA users cannot match
        back to the firmware their backup actually came from.
        """
        central, mock_daemon = connected
        mock_daemon.get(
            f"{_BASE}/system/ccu",
            payload=[{**_CCU_ENTRY, "hostname": "Otto", "version": "3.87.6.20260404"}],
        )
        mock_daemon.get(f"{_BASE}/interfaces", payload=[])
        await central.validate_config_and_get_system_information()

        mock_daemon.post(f"{_BASE}/backups", payload={"id": "bk-2"}, status=202)
        mock_daemon.get(
            f"{_BASE}/backups",
            payload=[{"id": "bk-2", "central": "home", "bytes": 11, "created_at": "2026-08-17T00:00:00Z"}],
        )
        mock_daemon.get(
            f"{_BASE}/backups/bk-2/download",
            body=b"SBK-ARCHIVE",
            content_type="application/octet-stream",
        )

        result = await central.create_backup_and_download()

        assert result is not None
        assert result.filename.startswith("Otto-3.87.6.20260404-")
        # The daemon's own build version (from the `connected` fixture's
        # /info mock) must not leak into the backup filename.
        assert "1.2.3" not in result.filename

    async def test_filename_comes_from_the_backup_entry_when_the_daemon_recorded_one(self, connected) -> None:
        """
        The daemon's own recorded filename wins over the locally rebuilt one.

        Since daemon api 7.1.0 a listed ``BackupEntry`` can carry
        ``filename``, named from the CCU's hostname/firmware *at backup
        time*. Rebuilding it here would read the *current* system
        information instead, so an archive downloaded after a firmware
        update would falsely claim the new version.
        """
        central, mock_daemon = connected
        mock_daemon.post(f"{_BASE}/backups", payload={"id": "bk-3"}, status=202)
        mock_daemon.get(
            f"{_BASE}/backups",
            payload=[
                {
                    "id": "bk-3",
                    "central": "home",
                    "bytes": 11,
                    "created_at": "2026-08-17T00:00:00Z",
                    "filename": "CCU3-3.71.7.20240304-20260817000000.sbk",
                }
            ],
        )
        mock_daemon.get(
            f"{_BASE}/backups/bk-3/download",
            body=b"SBK-ARCHIVE",
            content_type="application/octet-stream",
        )

        result = await central.create_backup_and_download()

        assert result is not None
        # Exactly the daemon-recorded name — not the locally rebuilt one,
        # which would read "home-unknown-…" off this fixture's unconfigured
        # system information.
        assert result.filename == "CCU3-3.71.7.20240304-20260817000000.sbk"

    async def test_filename_falls_back_to_the_local_construction_when_the_entry_carries_none(self, connected) -> None:
        """An older daemon lists no ``filename`` on the entry — rebuild it locally."""
        central, mock_daemon = connected
        mock_daemon.post(f"{_BASE}/backups", payload={"id": "bk-4"}, status=202)
        mock_daemon.get(
            f"{_BASE}/backups",
            payload=[{"id": "bk-4", "central": "home", "bytes": 11, "created_at": "2026-08-17T00:00:00Z"}],
        )
        mock_daemon.get(
            f"{_BASE}/backups/bk-4/download",
            body=b"SBK-ARCHIVE",
            content_type="application/octet-stream",
        )

        result = await central.create_backup_and_download()

        assert result is not None
        # The locally-rebuilt "<hostname>-<version>-<timestamp>.sbk" shape —
        # not asserted via a second `_backup_filename()` call, which would
        # race the minute-granularity timestamp against this one.
        assert result.filename.startswith("home-unknown-")
        assert result.filename.endswith(".sbk")

    async def test_returns_none_when_the_trigger_yields_no_id(self, connected) -> None:
        central, mock_daemon = connected
        mock_daemon.post(f"{_BASE}/backups", payload={}, status=202)
        assert await central.create_backup_and_download() is None


class TestDeleteDeviceCallShape:
    """The adapter takes aiohomematic's (interface_id, device_address) shape."""

    async def test_delete_device_routes_by_address(self, connected) -> None:
        central, mock_daemon = connected
        mock_daemon.delete(f"{_BASE}/devices/ABC0000001", status=204)

        await central.device_coordinator.delete_device(interface_id="HmIP-RF", device_address="ABC0000001")

        deletes = [r for r in mock_daemon.requests if r.method == "DELETE"]
        assert [r.path for r in deletes] == [f"{_BASE}/devices/ABC0000001"]

    async def test_delete_device_propagates_a_403(self, connected) -> None:
        central, mock_daemon = connected
        mock_daemon.delete(
            f"{_BASE}/devices/ABC0000001",
            status=403,
            payload={
                "type": "https://openccu-loom.dev/errors/forbidden",
                "title": "Forbidden",
                "status": 403,
                "code": "forbidden",
            },
            content_type="application/problem+json",
        )
        with pytest.raises(LoomForbiddenError):
            await central.device_coordinator.delete_device(interface_id="HmIP-RF", device_address="ABC0000001")


class TestBackupForbiddenIsNotSwallowed:
    """A 403 on the backup trigger re-raises instead of degrading to None."""

    async def test_trigger_403_raises(self, connected) -> None:
        central, mock_daemon = connected
        mock_daemon.post(
            f"{_BASE}/backups",
            status=403,
            payload={
                "type": "https://openccu-loom.dev/errors/forbidden",
                "title": "Forbidden",
                "status": 403,
                "code": "forbidden",
            },
            content_type="application/problem+json",
        )
        with pytest.raises(LoomForbiddenError):
            await central.create_backup_and_download()

    async def test_other_trigger_failures_still_yield_none(self, connected) -> None:
        central, mock_daemon = connected
        mock_daemon.post(f"{_BASE}/backups", status=500, payload={"status": 500})
        assert await central.create_backup_and_download() is None
