# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Coverage for the admin / ops operation modules.

One representative round-trip per module (auth, users, centrals,
config, diagnostics, backup, sessions, matter, visibility) plus the
system + devices admin extensions, exercised against an in-process
mock daemon so the paths and request shapes are pinned to the daemon
contract.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from openccu_loom_types.rest import TokenCreate, UserCreate
import pytest

from openccu_loom_client.operations import (
    AuthOperations,
    BackupOperations,
    CentralsOperations,
    ConfigOperations,
    DiagnosticsOperations,
    MatterOperations,
    SessionsOperations,
    SystemOperations,
    UsersOperations,
    VisibilityOperations,
)
from openccu_loom_client.operations.devices import DevicesOperations
from openccu_loom_client.transport import HttpTransport
from tests.helpers import MockDaemon

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


@pytest.fixture
async def http(mock_daemon: MockDaemon) -> AsyncIterator[tuple[HttpTransport, MockDaemon]]:
    t = HttpTransport(config=mock_daemon.config, backoff_sequence=(0.0,))
    mock_daemon.get("/api/v1/info", payload=_INFO)
    await t.connect()
    yield t, mock_daemon
    await t.close()


class TestAuthOperations:
    async def test_me(self, http) -> None:
        t, mock = http
        mock.get("/api/v1/auth/me", payload={"subject": "admin", "role": "admin"})
        ident = await AuthOperations(transport=t).me()
        assert ident.subject == "admin"
        assert ident.role == "admin"

    async def test_create_token_v2(self, http) -> None:
        t, mock = http
        mock.post(
            "/api/v1/auth/tokens/v2",
            payload={"token": "secret-xyz", "fingerprint": "ab12"},
        )
        created = await AuthOperations(transport=t).create_token_v2(token=TokenCreate(subject="ha", role="operator"))
        assert created.token == "secret-xyz"
        assert created.fingerprint == "ab12"


class TestUsersOperations:
    async def test_create_user(self, http) -> None:
        t, mock = http
        mock.post(
            "/api/v1/users",
            payload={
                "subject": "alice",
                "role": "operator",
                "created_at": "2026-05-24T10:00:00Z",
            },
        )
        summary = await UsersOperations(transport=t).create_user(
            user=UserCreate(username="alice", password="pw", role="operator")
        )
        assert summary.subject == "alice"

    async def test_delete_user(self, http) -> None:
        t, mock = http
        mock.delete("/api/v1/users/alice", status=204)
        await UsersOperations(transport=t).delete_user(subject="alice")


class TestCentralsOperations:
    async def test_list_centrals(self, http) -> None:
        t, mock = http
        mock.get(
            "/api/v1/centrals",
            payload=[
                {
                    "name": "home",
                    "host": "10.0.0.2",
                    "interfaces": [{"name": "HmIP-RF"}],
                    "enabled": True,
                }
            ],
        )
        rows = await CentralsOperations(transport=t).list_centrals()
        assert rows[0].name == "home"


class TestConfigOperations:
    async def test_get_config(self, http) -> None:
        t, mock = http
        mock.get("/api/v1/config", payload={})
        snap = await ConfigOperations(transport=t).get_config()
        assert snap is not None

    async def test_put_section_returns_ack(self, http) -> None:
        t, mock = http
        mock.put(
            "/api/v1/config/sections/north",
            payload={"section": "north", "version": 3, "restart_required": False},
        )
        ack = await ConfigOperations(transport=t).put_section(section="north", values={"port": 9090})
        assert ack["version"] == 3

    async def test_get_schema_parses_daemon_shape(self, http) -> None:
        """
        The daemon's ``{sections, fields}`` schema validates against SchemaResponse.

        Mirrors the real ``GET /config/schema`` payload, including the
        per-field ``default`` the daemon emits but ``openapi.yaml`` does
        not yet document — Pydantic ignores the extra key, so the parse
        succeeds. (Confirms the formerly-xfail e2e case is now sound.)
        """
        t, mock = http
        mock.get(
            "/api/v1/config/schema",
            payload={
                "sections": ["north", "ccu"],
                "fields": [
                    {
                        "path": "north.port",
                        "class": "basic",
                        "go_type": "int",
                        "restart_required": True,
                        "default": 8080,
                    }
                ],
            },
        )
        schema = await ConfigOperations(transport=t).get_schema()
        assert schema.sections == ["north", "ccu"]
        assert schema.fields[0].path == "north.port"
        assert schema.fields[0].restart_required is True


class TestDiagnosticsOperations:
    async def test_values_cache_stats(self, http) -> None:
        t, mock = http
        mock.get(
            "/api/v1/admin/values-cache/stats",
            payload={
                "rows": 10,
                "value_json_bytes": 2048,
                "restored_rows": 10,
                "cast_failures": 0,
                "gc_rows_deleted": 0,
                "flush_batches": 1,
                "flushed_entries": 10,
            },
        )
        stats = await DiagnosticsOperations(transport=t).get_values_cache_stats()
        assert stats.rows == 10

    async def test_set_log_level(self, http) -> None:
        t, mock = http
        mock.put("/api/v1/diagnostics/log-level", payload={"level": "debug"})
        result = await DiagnosticsOperations(transport=t).set_log_level(level="debug")
        assert result["level"] == "debug"

    async def test_metrics_returns_bytes(self, http) -> None:
        t, mock = http
        mock.get(
            "/api/v1/metrics",
            body=b"# HELP up\nup 1\n",
            content_type="text/plain",
        )
        raw = await DiagnosticsOperations(transport=t).get_metrics()
        assert raw == b"# HELP up\nup 1\n"


class TestBackupOperations:
    async def test_download_backup_returns_bytes(self, http) -> None:
        t, mock = http
        mock.get(
            "/api/v1/backups/b1/download",
            body=b"SBK-ARCHIVE",
            content_type="application/octet-stream",
        )
        raw = await BackupOperations(transport=t).download_backup(backup_id="b1")
        assert raw == b"SBK-ARCHIVE"

    async def test_restore_backup(self, http) -> None:
        t, mock = http
        mock.post("/api/v1/backups/b1/restore", status=202)
        await BackupOperations(transport=t).restore_backup(backup_id="b1")


class TestSessionsOperations:
    async def test_acquire_sends_key(self, http) -> None:
        t, mock = http
        mock.post("/api/v1/sessions/edit", payload={"key": "cfg:north", "held": True})
        result = await SessionsOperations(transport=t).acquire(key="cfg:north")
        assert result["held"] is True


class TestMatterOperations:
    async def test_get_status(self, http) -> None:
        t, mock = http
        mock.get(
            "/api/v1/matter/status",
            payload={
                "enabled": True,
                "listening": True,
                "endpoint_count": 5,
                "fabric_count": 1,
                "enabled_count": 5,
                "advertising": False,
                "commissioning_window_open": False,
            },
        )
        status = await MatterOperations(transport=t).get_status()
        assert status.endpoint_count == 5


class TestVisibilityOperations:
    async def test_get_unignore(self, http) -> None:
        t, mock = http
        mock.get("/api/v1/visibility/unignore", payload={"centrals": []})
        result = await VisibilityOperations(transport=t).get_unignore()
        assert result.centrals == []


class TestSystemAdminExtensions:
    async def test_restart(self, http) -> None:
        t, mock = http
        mock.post("/api/v1/system/restart", payload={"status": "restarting"})
        result = await SystemOperations(transport=t).restart()
        assert result["status"] == "restarting"

    async def test_list_system_ccus_unwraps_entries_envelope(self, http) -> None:
        # The daemon returns {"entries": [...]}, not a bare list.
        t, mock = http
        entry = {
            "name": "ccu-e2e",
            "host": "127.0.0.1",
            "available": True,
            "model": "CCU3",
            "version": "3.0",
            "hostname": "ccu",
            "serial": "ABC1234567",
            "url": "http://127.0.0.1",
            "is_ha_app": False,
            "configured_interfaces": [],
        }
        mock.get("/api/v1/system/ccu", payload={"entries": [entry]})
        ccus = await SystemOperations(transport=t).list_system_ccus()
        assert [c.name for c in ccus] == ["ccu-e2e"]

    async def test_list_system_ccus_tolerates_bare_list(self, http) -> None:
        # Forward-compatibility: a bare list must still parse.
        t, mock = http
        mock.get("/api/v1/system/ccu", payload=[])
        assert await SystemOperations(transport=t).list_system_ccus() == []


class TestDevicesAdminExtensions:
    async def test_accept_device(self, http) -> None:
        t, mock = http
        mock.post("/api/v1/devices/VCU9/accept", status=202)
        await DevicesOperations(transport=t).accept_device(address="VCU9")

    async def test_get_ui_schema(self, http) -> None:
        t, mock = http
        mock.get(
            "/api/v1/devices/VCU9/channels/1/ui-schema",
            payload={"fields": []},
        )
        schema = await DevicesOperations(transport=t).get_ui_schema(address="VCU9", channel=1)
        assert schema == {"fields": []}
