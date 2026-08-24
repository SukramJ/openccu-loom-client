# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Tests for the capability tokens and :meth:`LoomClient.has_capability`.

The tokens are only ever compared, never parsed, so a typo cannot fail
loudly: a misspelled string in ``required_capabilities`` reads as "the
daemon is missing a capability" on every daemon that will ever exist.
These pin the shape that turns that into an error at the call site.
"""

from __future__ import annotations

from openccu_loom_types import DAEMON_API_VERSION
import pytest

from openccu_loom_client import ALWAYS_ON, Capability, LoomClient
from tests.helpers.mock_daemon import MockDaemon

_BASE_INFO = {
    "version": "1.2.3",
    "api_version": DAEMON_API_VERSION,
    "commit": "deadbeef",
    "build_date": "2026-05-24T10:00:00Z",
    "addon_build": False,
    "started_at": "2026-05-24T10:01:00Z",
    "uptime": "PT60S",
    "schema_digest": "sha256:test",
    "config_ui_url": "",
}


def _info(*capabilities: str) -> dict[str, object]:
    return {**_BASE_INFO, "capabilities": list(capabilities)}


class TestCapabilityTokens:
    def test_tokens_are_their_wire_strings(self) -> None:
        """
        The enum members compare equal to the wire strings.

        ``StrEnum`` is the point: a caller may hand either a member or a
        raw token to ``has_capability``, and code that already carries
        strings keeps working.
        """
        assert Capability.ADMIN_PERSISTENCE == "admin.persistence.v1"
        assert Capability.MQTT_RAW == "mqtt.raw.v1"
        assert Capability.ADDON_SELF_UPDATE == "addon_self_update"

    def test_tokens_are_unique(self) -> None:
        """No two names may wrap the same token — that would make one of them dead."""
        values = [c.value for c in Capability]
        assert len(values) == len(set(values))

    def test_always_on_is_a_subset_of_the_catalogue(self) -> None:
        assert set(Capability) >= ALWAYS_ON


class TestHasCapability:
    async def test_false_before_connect(self, mock_daemon: MockDaemon) -> None:
        """
        No handshake means no evidence, and no evidence must read as absent.

        Returning True here would make every feature-detect succeed against
        a client that has not talked to a daemon at all.
        """
        client = LoomClient(config=mock_daemon.config)
        assert client.has_capability(Capability.ALARM) is False

    async def test_reports_an_advertised_token(self, mock_daemon: MockDaemon) -> None:
        mock_daemon.get("/api/v1/info", payload=_info("rest.v1", "admin.persistence.v1"))
        async with LoomClient(config=mock_daemon.config) as client:
            assert client.has_capability(Capability.ADMIN_PERSISTENCE) is True

    async def test_reports_an_absent_token(self, mock_daemon: MockDaemon) -> None:
        """The negative half: a daemon without the token must answer False."""
        mock_daemon.get("/api/v1/info", payload=_info("rest.v1"))
        async with LoomClient(config=mock_daemon.config) as client:
            assert client.has_capability(Capability.ADMIN_PERSISTENCE) is False

    async def test_accepts_a_raw_string(self, mock_daemon: MockDaemon) -> None:
        """A token this package does not name must still be checkable."""
        mock_daemon.get("/api/v1/info", payload=_info("rest.v1", "some.future.v1"))
        async with LoomClient(config=mock_daemon.config) as client:
            assert client.has_capability("some.future.v1") is True
            assert client.has_capability("never.emitted.v1") is False

    async def test_empty_capability_list_reads_as_absent(self, mock_daemon: MockDaemon) -> None:
        mock_daemon.get("/api/v1/info", payload=_info())
        async with LoomClient(config=mock_daemon.config) as client:
            assert client.has_capability(Capability.REST) is False


class TestRequiredCapabilitiesAcceptTokens:
    async def test_connect_accepts_enum_members(self, mock_daemon: MockDaemon) -> None:
        """
        The handshake takes the members directly.

        This is the reason the enum exists: the call site that used to
        carry a hand-typed string now carries a name, so a typo is an
        AttributeError here instead of a permanent handshake failure that
        reads like the daemon's fault.
        """
        mock_daemon.get("/api/v1/info", payload=_info("rest.v1", "alarm.v1"))
        async with LoomClient(config=mock_daemon.config) as client:
            await client.connect(required_capabilities=(Capability.ALARM,))
            assert client.has_capability(Capability.ALARM) is True

    async def test_connect_still_rejects_a_missing_one(self, mock_daemon: MockDaemon) -> None:
        from openccu_loom_client import LoomTransportError

        mock_daemon.get("/api/v1/info", payload=_info("rest.v1"))
        client = LoomClient(config=mock_daemon.config)
        with pytest.raises(LoomTransportError, match="missing required capabilities"):
            await client.connect(required_capabilities=(Capability.ALARM,))
        await client.close()
