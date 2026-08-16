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

from openccu_loom_types import DAEMON_API_VERSION
from openccu_loom_types.rest import StartupCaptureConfigWrite, TokenCreate, UserCreate
import pytest

from openccu_loom_client.exceptions import LoomValidationError
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
    "api_version": DAEMON_API_VERSION,
    "commit": "deadbeef",
    "build_date": "2026-05-24T10:00:00Z",
    "addon_build": False,
    "started_at": "2026-05-24T10:01:00Z",
    "uptime": "PT60S",
    "capabilities": ["rest.v1"],
    "schema_digest": "sha256:test",
    "config_ui_url": "",
}

# The minimal ``SystemCCUEntry`` the daemon always fills in — everything a
# CCU only reports after a successful connect is added per test.
_CCU_ENTRY = {
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
    # Required since types 0.1.55 / daemon api 2.19.0.
    "readiness": {
        "phase": "ready",
        "ready": True,
        "interfaces_loaded": 1,
        "interfaces_total": 1,
    },
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

    async def test_upload_backup_sends_multipart_and_returns_archive_facts(self, http) -> None:
        # api 3.10.0: an externally produced .sbk is imported through a
        # multipart file part, and the daemon answers with the stored
        # BackupEntry plus what it read out of the archive.
        t, mock = http
        mock.post(
            "/api/v1/backups/upload",
            status=201,
            payload={
                "id": "imported-1",
                "central": "home",
                "bytes": 11,
                "created_at": "2026-07-30T10:00:00Z",
                "firmware_version": "3.89.8",
                "product": "OpenCCU",
            },
        )
        entry = await BackupOperations(transport=t).upload_backup(content=b"SBK-ARCHIVE", filename="ccu.sbk")
        assert entry["id"] == "imported-1"
        assert entry["firmware_version"] == "3.89.8"
        sent = mock.requests[-1]
        assert sent.headers["Content-Type"].startswith("multipart/form-data")
        # The part carries the caller's filename under the daemon's "file" field.
        assert b'name="file"' in sent.body
        assert b'filename="ccu.sbk"' in sent.body
        assert b"SBK-ARCHIVE" in sent.body

    async def test_upload_backup_maps_a_rejected_archive_to_a_typed_error(self, http) -> None:
        # 422 = not a CCU system backup. The daemon problem+json path must
        # work on the upload route too — otherwise "wrong file picked"
        # surfaces as an untyped transport failure.
        t, mock = http
        mock.post(
            "/api/v1/backups/upload",
            status=422,
            payload={
                "type": "https://openccu-loom.dev/errors/validation",
                "title": "Not a CCU system backup",
                "status": 422,
            },
        )
        with pytest.raises(LoomValidationError) as err:
            await BackupOperations(transport=t).upload_backup(content=b"nonsense")
        assert err.value.status == 422


class TestSessionsOperations:
    async def test_acquire_sends_key(self, http) -> None:
        t, mock = http
        mock.post("/api/v1/sessions/edit", payload={"key": "cfg:north", "held": True})
        result = await SessionsOperations(transport=t).acquire(key="cfg:north")
        assert result["held"] is True

    async def test_release_names_the_lock_and_proves_ownership(self, http) -> None:
        # DELETE /sessions/edit has always demanded key + token; api 6.0.0
        # finally declares it — pin that the client sends both.
        t, mock = http
        mock.delete("/api/v1/sessions/edit", status=204)
        await SessionsOperations(transport=t).release(key="cfg:north", token="tok-abc")
        assert mock.requests[-1].json() == {"key": "cfg:north", "token": "tok-abc"}

    async def test_heartbeat_carries_the_same_body(self, http) -> None:
        t, mock = http
        mock.post("/api/v1/sessions/edit/heartbeat", payload={"key": "cfg:north", "token": "tok-abc"})
        result = await SessionsOperations(transport=t).heartbeat(key="cfg:north", token="tok-abc")
        assert result["key"] == "cfg:north"
        assert mock.requests[-1].json() == {"key": "cfg:north", "token": "tok-abc"}


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

    async def test_list_sessions_carries_occupancy(self, http) -> None:
        t, mock = http
        mock.get(
            "/api/v1/matter/sessions",
            payload={
                "sessions": [
                    {
                        "session_id": 42,
                        "fabric_index": 1,
                        "peer_node_id": "0x000000000001B669",
                        "local_node_id": "0x0000000000000001",
                        "is_pase": False,
                        "subscriptions": 2,
                        "last_activity": "2026-08-16T10:00:00Z",
                        "last_peer_activity": "2026-08-16T09:59:00Z",
                        "idle_seconds": 5,
                        "peer_idle_seconds": 65,
                    }
                ],
                "occupancy": {"live": 1, "reserved": 0, "capacity": 65534, "free": 65533},
            },
        )
        result = await MatterOperations(transport=t).list_sessions()
        assert result.sessions[0].peer_idle_seconds == 65
        assert result.occupancy.free == 65533

    async def test_list_endpoints(self, http) -> None:
        t, mock = http
        mock.get(
            "/api/v1/matter/endpoints",
            payload={
                "endpoints": [
                    {
                        "endpoint_id": 2,
                        "parent_endpoint_id": 1,
                        "device_type": 256,
                        "device_type_name": "On/Off Light",
                        "reachable": True,
                        "friendly_name": "Flur",
                        "device_address": "ABC0000001",
                        "channel_address": "ABC0000001:3",
                        "clusters": [{"id": 6, "name": "OnOff", "revision": 6}],
                    }
                ]
            },
        )
        result = await MatterOperations(transport=t).list_endpoints()
        assert result.endpoints[0].clusters[0].name == "OnOff"

    async def test_get_mdns_diagnostics(self, http) -> None:
        t, mock = http
        mock.get(
            "/api/v1/matter/mdns",
            payload={
                "advertising": True,
                "services": [
                    {
                        "service_type": "_matterc._udp",
                        "instance_name": "A1B2C3D4E5F60708",
                        "host_name": "loom.local",
                        "port": 5540,
                        "addresses": ["192.0.2.10"],
                        "subtypes": ["_L840", "_CM"],
                        "txt": {"D": "840"},
                    }
                ],
                "findings": [{"severity": "warning", "code": "no-ipv6", "message": "announcement without IPv6"}],
            },
        )
        result = await MatterOperations(transport=t).get_mdns_diagnostics()
        assert result.advertising is True
        assert result.findings[0].code == "no-ipv6"

    async def test_get_compatibility(self, http) -> None:
        t, mock = http
        mock.get(
            "/api/v1/matter/compatibility",
            payload={
                "ecosystems": [{"ecosystem": "apple", "vendor_id": 4937, "fabric_index": 1, "label": "Apple Home"}],
                "endpoint_count": 12,
                "findings": [
                    {
                        "ecosystem": "google",
                        "code": "device-type-hidden",
                        "message": "valve will not appear in Google Home",
                        "device_type": 66,
                    }
                ],
            },
        )
        result = await MatterOperations(transport=t).get_compatibility()
        assert result.ecosystems[0].ecosystem.value == "apple"
        assert result.findings[0].device_type == 66

    async def test_list_diagnostic_events(self, http) -> None:
        t, mock = http
        mock.get(
            "/api/v1/matter/events",
            payload={
                "events": [
                    {
                        "at": "2026-08-16T10:00:00Z",
                        "kind": "pairing",
                        "severity": "warning",
                        "message": "commissioner refused: another was already mid-handshake",
                        "detail": {"peer": "192.0.2.20"},
                    }
                ]
            },
        )
        result = await MatterOperations(transport=t).list_diagnostic_events()
        assert result.events[0].kind.value == "pairing"

    async def test_force_sync(self, http) -> None:
        t, mock = http
        mock.post("/api/v1/matter/force-sync", status=204)
        await MatterOperations(transport=t).force_sync()
        assert mock.requests[-1].path == "/api/v1/matter/force-sync"

    async def test_factory_reset_names_the_action(self, http) -> None:
        # The daemon refuses a reset that does not carry the literal
        # confirm token — pin that the client sends it verbatim.
        t, mock = http
        mock.post("/api/v1/matter/factory-reset", status=204)
        await MatterOperations(transport=t).factory_reset(confirm="remove-all-fabrics")
        assert mock.requests[-1].json() == {"confirm": "remove-all-fabrics"}


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

    async def test_set_startup_capture_uses_the_write_shape(self, http) -> None:
        # api 6.0.0 splits read and write: an omitted ``anonymise`` on the
        # write means *true* (the privacy-preserving default), while the
        # response always carries the effective value.
        t, mock = http
        mock.put(
            "/api/v1/system/startup-capture",
            payload={"enabled": True, "duration_seconds": 300, "anonymise": True},
        )
        result = await SystemOperations(transport=t).set_startup_capture(
            config=StartupCaptureConfigWrite(enabled=True, duration_seconds=300)
        )
        # The client serialises the write shape's default explicitly
        # (``anonymise: true`` — the same meaning as omitting it), and the
        # read shape reports the effective value back.
        assert mock.requests[-1].json() == {"enabled": True, "duration_seconds": 300, "anonymise": True}
        assert result.anonymise is True

    async def test_list_system_ccus_unwraps_entries_envelope(self, http) -> None:
        # The daemon returns {"entries": [...]}, not a bare list.
        t, mock = http
        mock.get("/api/v1/system/ccu", payload={"entries": [_CCU_ENTRY]})
        ccus = await SystemOperations(transport=t).list_system_ccus()
        assert [c.name for c in ccus] == ["ccu-e2e"]

    async def test_list_system_ccus_tolerates_bare_list(self, http) -> None:
        # Forward-compatibility: a bare list must still parse.
        t, mock = http
        mock.get("/api/v1/system/ccu", payload=[])
        assert await SystemOperations(transport=t).list_system_ccus() == []

    async def test_list_system_ccus_carries_the_ccu_reported_facts(self, http) -> None:
        # api 3.5.0 / 3.8.0: security posture, astro position, time zone,
        # recovery availability and the CCU's own interface list ride along
        # on the same entry — the repair flow reads them from there.
        t, mock = http
        mock.get(
            "/api/v1/system/ccu",
            payload={
                "entries": [
                    {
                        **_CCU_ENTRY,
                        "auth_enabled": True,
                        "https_redirect_enabled": False,
                        "longitude": 8.6821,
                        "latitude": 50.1109,
                        "timezone": "Europe/Berlin",
                        "recovery_mode_supported": True,
                        "ccu_interfaces": [
                            {
                                "type": "HmIP-RF",
                                "address": "hmip",
                                "port": 2010,
                                "url": "http://127.0.0.1:2010",
                            }
                        ],
                    }
                ]
            },
        )
        (ccu,) = await SystemOperations(transport=t).list_system_ccus()
        assert ccu.auth_enabled is True
        assert ccu.https_redirect_enabled is False
        assert (ccu.longitude, ccu.latitude) == (8.6821, 50.1109)
        assert ccu.timezone == "Europe/Berlin"
        assert ccu.recovery_mode_supported is True
        assert ccu.ccu_interfaces is not None
        assert ccu.ccu_interfaces[0].port == 2010

    async def test_install_system_update_omits_the_body_by_default(self, http) -> None:
        # A pre-3.11.0 daemon must keep seeing the exact request shape it
        # validated before — the backup option is opt-in, never implied.
        t, mock = http
        mock.post("/api/v1/system/update/install", status=202)
        await SystemOperations(transport=t).install_system_update(central="home")
        sent = mock.requests[-1]
        assert sent.query == {"central": "home"}
        assert sent.body == b""

    async def test_install_system_update_can_request_a_backup_first(self, http) -> None:
        t, mock = http
        mock.post("/api/v1/system/update/install", status=202)
        await SystemOperations(transport=t).install_system_update(central="home", backup_first=True)
        assert mock.requests[-1].json() == {"backup_first": True}


class TestSystemCCUMaintenance:
    """The CCU-hardware verbs (api 3.8.0 / 3.9.0) — not the daemon's own restart."""

    @pytest.mark.parametrize(
        ("verb", "path"),
        [
            ("reboot_ccu", "reboot"),
            ("poweroff_ccu", "poweroff"),
            ("restart_ccu_safe_mode", "safe-mode"),
            ("restart_ccu_recovery_mode", "recovery-mode"),
        ],
    )
    async def test_maintenance_verb_hits_its_route(self, http, verb: str, path: str) -> None:
        t, mock = http
        mock.post(f"/api/v1/system/ccu/home/{path}", status=202)
        await getattr(SystemOperations(transport=t), verb)(central="home")
        sent = mock.requests[-1]
        assert (sent.method, sent.path) == ("POST", f"/api/v1/system/ccu/home/{path}")

    async def test_central_name_is_path_encoded(self, http) -> None:
        # A central name is operator-chosen free text. Unencoded, the ``?``
        # would cut the path short and turn the rest into a query string —
        # the request would hit a different route entirely. The server
        # decodes it back, so the stub is registered on the decoded path.
        t, mock = http
        mock.post("/api/v1/system/ccu/haus?nord/reboot", status=202)
        await SystemOperations(transport=t).reboot_ccu(central="haus?nord")
        sent = mock.requests[-1]
        assert sent.path == "/api/v1/system/ccu/haus?nord/reboot"
        assert sent.query == {}

    async def test_set_ccu_position_sends_both_coordinates(self, http) -> None:
        t, mock = http
        mock.put("/api/v1/system/ccu/home/position", status=204)
        await SystemOperations(transport=t).set_ccu_position(central="home", longitude=8.6821, latitude=50.1109)
        sent = mock.requests[-1]
        assert sent.method == "PUT"
        assert sent.json() == {"longitude": 8.6821, "latitude": 50.1109}

    async def test_recovery_mode_on_an_unsupported_backend_raises(self, http) -> None:
        # 422 is the daemon saying the central's backend cannot host the
        # action (stock CCU3, CUxD, Homegear) — a caller must be able to
        # tell that apart from a transport failure.
        t, mock = http
        mock.post(
            "/api/v1/system/ccu/home/recovery-mode",
            status=422,
            payload={
                "type": "https://openccu-loom.dev/errors/validation",
                "title": "The central's backend cannot host this action",
                "status": 422,
            },
        )
        with pytest.raises(LoomValidationError):
            await SystemOperations(transport=t).restart_ccu_recovery_mode(central="home")


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
