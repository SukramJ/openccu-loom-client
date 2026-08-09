# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Coverage for the heating-group operations module.

Exercises list / create / update / delete plus the type and suitable-member
reads against an in-process mock daemon, pinning the REST paths, the optional
``central`` query parameter, the required ``type_id`` filter, and the
request/response shapes to the daemon contract.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from openccu_loom_types import DAEMON_API_VERSION
from openccu_loom_types.rest import CreateGroupRequest, UpdateGroupRequest
import pytest

from openccu_loom_client.operations import GroupsOperations
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


@pytest.fixture
async def http(mock_daemon: MockDaemon) -> AsyncIterator[tuple[HttpTransport, MockDaemon]]:
    t = HttpTransport(config=mock_daemon.config, backoff_sequence=(0.0,))
    mock_daemon.get("/api/v1/info", payload=_INFO)
    await t.connect()
    yield t, mock_daemon
    await t.close()


class TestGroupsOperations:
    async def test_list_groups_unwraps_entries_and_sends_central(self, http) -> None:
        t, mock = http
        mock.get(
            "/api/v1/groups",
            payload={
                "entries": [
                    {
                        "central": "home",
                        "groups": [
                            {
                                "id": 2,
                                "name": "Bad",
                                "forbid_single_operation": False,
                                "type_id": "hmip.heating.group",
                                "members": [{"address": "ABC:1"}],
                            }
                        ],
                    }
                ]
            },
        )
        rows = await GroupsOperations(transport=t).list_groups(central="home")
        assert rows[0].central == "home"
        assert rows[0].groups[0].id == 2
        assert rows[0].groups[0].members[0].address == "ABC:1"
        assert mock.requests[-1].query["central"] == "home"

    async def test_list_groups_omits_central_when_none(self, http) -> None:
        t, mock = http
        mock.get("/api/v1/groups", payload={"entries": []})
        rows = await GroupsOperations(transport=t).list_groups()
        assert rows == []
        assert mock.requests[-1].query.get("central") is None

    async def test_list_types(self, http) -> None:
        t, mock = http
        mock.get(
            "/api/v1/groups/types",
            payload={"types": [{"id": "hmip.heating.group", "label_key": "grp.heat"}]},
        )
        types = await GroupsOperations(transport=t).list_types()
        assert types[0].id == "hmip.heating.group"

    async def test_suitable_members_sends_type_id_and_central(self, http) -> None:
        t, mock = http
        mock.get(
            "/api/v1/groups/suitable-members",
            payload={
                "assignable": [{"address": "ABC:1", "device_name": "Thermostat", "config_pending": True}],
                "leftover": [{"address": "DEF:2"}],
            },
        )
        resp = await GroupsOperations(transport=t).suitable_members(type_id="hmip.heating.group", central="home")
        assert resp.assignable[0].address == "ABC:1"
        assert resp.assignable[0].config_pending is True
        assert resp.leftover[0].address == "DEF:2"
        assert mock.requests[-1].query["type_id"] == "hmip.heating.group"
        assert mock.requests[-1].query["central"] == "home"

    async def test_create_group_posts_request_body(self, http) -> None:
        t, mock = http
        mock.post(
            "/api/v1/groups",
            status=201,
            payload={
                "id": 3,
                "name": "Duschbad",
                "forbid_single_operation": False,
                "type_id": "hmip.heating.group",
                "members": [{"address": "ABC:1"}],
            },
        )
        entry = await GroupsOperations(transport=t).create_group(
            request=CreateGroupRequest(type_id="hmip.heating.group", name="Duschbad", members=["ABC:1"]),
            central="home",
        )
        assert entry.id == 3
        assert entry.name == "Duschbad"
        body = mock.requests[-1].json()
        assert body["type_id"] == "hmip.heating.group"
        assert body["name"] == "Duschbad"
        assert body["members"] == ["ABC:1"]
        # exclude_none drops the unset operate-only flag rather than sending null.
        assert "forbid_single_operation" not in body
        assert mock.requests[-1].query["central"] == "home"

    async def test_update_group_puts_body(self, http) -> None:
        t, mock = http
        mock.put("/api/v1/groups/3", status=204)
        await GroupsOperations(transport=t).update_group(
            group_id=3,
            request=UpdateGroupRequest(name="Duschbad", members=["ABC:1", "DEF:2"]),
        )
        req = mock.requests[-1]
        assert req.method == "PUT"
        assert req.path == "/api/v1/groups/3"
        assert req.json()["members"] == ["ABC:1", "DEF:2"]

    async def test_delete_group(self, http) -> None:
        t, mock = http
        mock.delete("/api/v1/groups/3", status=204)
        await GroupsOperations(transport=t).delete_group(group_id=3)
        req = mock.requests[-1]
        assert req.method == "DELETE"
        assert req.path == "/api/v1/groups/3"
