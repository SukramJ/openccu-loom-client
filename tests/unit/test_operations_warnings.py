# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Operator warnings and per-user silences (daemon api 12.2.0).

Pinned against the in-process mock daemon: the paths, the silence body, and
how the two refusals the daemon documents reach a caller.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from openccu_loom_client import LoomClient
from openccu_loom_client.exceptions import LoomNotFoundError, LoomValidationError
from openccu_loom_client.operations import WarningsOperations
from openccu_loom_client.transport import HttpTransport
from openccu_loom_client.wire import DAEMON_API_VERSION
from tests.helpers import MockDaemon

_INFO = {
    "version": "0.80.0",
    "api_version": DAEMON_API_VERSION,
    "commit": "deadbeef",
    "build_date": "2026-09-28T10:00:00Z",
    "addon_build": False,
    "started_at": "2026-09-28T10:01:00Z",
    "uptime": "PT60S",
    "capabilities": ["rest.v1"],
    "schema_digest": "sha256:test",
    "config_ui_url": "",
}


@pytest.fixture
async def http(mock_daemon: MockDaemon) -> AsyncIterator[tuple[HttpTransport, MockDaemon]]:
    t = HttpTransport(config=mock_daemon.config, backoff_sequence=(0.0,))
    mock_daemon.get("/api/v1/info", payload=_INFO)
    await t.connect()
    yield t, mock_daemon
    await t.close()


def _problem(*, code: str, status: int, title: str) -> dict[str, object]:
    return {"type": f"https://openccu-loom.dev/errors/{code}", "title": title, "status": status, "code": code}


class TestListWarnings:
    async def test_rows_carry_the_callers_silence(self, http) -> None:
        t, mock = http
        mock.get(
            "/api/v1/warnings",
            payload={
                "items": [
                    {
                        "id": "health:mqtt",
                        "severity": "error",
                        "message_key": "warnings.health",
                        "args": {"component": "mqtt"},
                        "silenced": True,
                        "silenced_until": "2026-10-05T10:00:00Z",
                    },
                    {
                        "id": "servicemsg:ccu1",
                        "severity": "warning",
                        "central": "ccu1",
                        "message_key": "warnings.service_messages",
                        "silenced": False,
                    },
                ]
            },
        )
        rows = await WarningsOperations(transport=t).list_warnings()
        assert [r.id for r in rows] == ["health:mqtt", "servicemsg:ccu1"]
        assert rows[0].silenced is True
        assert rows[0].silenced_until is not None
        assert rows[0].args == {"component": "mqtt"}
        assert rows[1].central == "ccu1"
        assert rows[1].silenced_until is None

    async def test_no_warnings_is_an_empty_list(self, http) -> None:
        t, mock = http
        mock.get("/api/v1/warnings", payload={"items": []})
        assert await WarningsOperations(transport=t).list_warnings() == []


class TestSilences:
    async def test_silence_puts_the_days_for_the_warning(self, http) -> None:
        t, mock = http
        mock.put("/api/v1/warnings/health:mqtt/silence", status=204)
        await WarningsOperations(transport=t).silence_warning(warning_id="health:mqtt", days=7)
        (req,) = [r for r in mock.requests if r.method == "PUT"]
        assert req.path == "/api/v1/warnings/health:mqtt/silence"
        assert req.json() == {"days": 7}

    async def test_silencing_an_inactive_warning_is_not_found(self, http) -> None:
        t, mock = http
        mock.put(
            "/api/v1/warnings/health:gone/silence",
            status=404,
            payload=_problem(code="not_found", status=404, title="No such active warning"),
        )
        with pytest.raises(LoomNotFoundError):
            await WarningsOperations(transport=t).silence_warning(warning_id="health:gone", days=7)

    async def test_a_period_the_daemon_does_not_offer_is_a_validation_error(self, http) -> None:
        t, mock = http
        mock.put(
            "/api/v1/warnings/health:mqtt/silence",
            status=400,
            payload=_problem(code="validation", status=400, title="Invalid silence period"),
        )
        with pytest.raises(LoomValidationError):
            await WarningsOperations(transport=t).silence_warning(warning_id="health:mqtt", days=3)

    async def test_unsilence_deletes_the_callers_silence(self, http) -> None:
        t, mock = http
        mock.delete("/api/v1/warnings/servicemsg:ccu1/silence", status=204)
        await WarningsOperations(transport=t).unsilence_warning(warning_id="servicemsg:ccu1")
        (req,) = [r for r in mock.requests if r.method == "DELETE"]
        assert req.path == "/api/v1/warnings/servicemsg:ccu1/silence"


def test_the_client_exposes_the_warnings_operations(mock_daemon: MockDaemon) -> None:
    client = LoomClient(config=mock_daemon.config)
    assert isinstance(client.warnings, WarningsOperations)
