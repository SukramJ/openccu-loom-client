# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Security & Safety surface: REST façade, WS event dispatch, HA entities.

The domain reports hazards and faults with or without an alarm engine,
so nothing here gates on the ``alarm.v1`` capability. Live state arrives
as ``security.*`` broadcasts (daemon ≥ 0.54.0 / api 5.1.0); the REST
reads are the bootstrap and the reconcile path.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from openccu_loom_client.events.types import (
    SecurityClassChangedEvent,
    SecurityFaultChangedEvent,
    SecurityNotificationEvent,
    SecurityStateChangedEvent,
    SecurityZoneChangedEvent,
    event_from_envelope,
)
from openccu_loom_client.operations import SecurityOperations
from openccu_loom_client.transport import HttpTransport
from openccu_loom_client.wire import DAEMON_API_VERSION
from openccu_loom_client.wire.rest import Kind2 as Kind, SecuritySourceOverride
from openccu_loom_client.wire.ws import WsEnvelope
from tests.helpers import MockDaemon

_INFO = {
    "version": "0.54.0",
    "api_version": DAEMON_API_VERSION,
    "commit": "deadbeef",
    "build_date": "2026-08-05T10:00:00Z",
    "addon_build": False,
    "started_at": "2026-08-05T10:01:00Z",
    "uptime": "PT60S",
    "capabilities": ["rest.v1", "ws.broadcasts.v1"],
    "schema_digest": "sha256:test",
    "config_ui_url": "",
}

_SOURCE = {
    "ref": "home|home:HmIP-RF|ABC123:1|SMOKE_DETECTOR_ALARM_STATUS",
    "central": "home",
    "interface_id": "home:HmIP-RF",
    "channel_address": "ABC123:1",
    "device_address": "ABC123",
    "parameter": "SMOKE_DETECTOR_ALARM_STATUS",
    "name": "Rauchmelder Flur",
    "class": "smoke",
    "active": True,
    "relevant": True,
}


@pytest.fixture
async def http(mock_daemon: MockDaemon) -> AsyncIterator[HttpTransport]:
    t = HttpTransport(config=mock_daemon.config, backoff_sequence=(0.0,))
    mock_daemon.get("/api/v1/info", payload=_INFO)
    await t.connect()
    yield t
    await t.close()


class TestSecurityOperations:
    async def test_snapshot(self, mock_daemon: MockDaemon, http: HttpTransport) -> None:
        mock_daemon.get(
            "/api/v1/security",
            payload={
                "severity": "alarm",
                "engine_healthy": True,
                "classes": [
                    {
                        "class": "smoke",
                        "active": True,
                        "severity": "alarm",
                        "known": 3,
                        "sources": [{"ref": "r1", "name": "Flur", "at": "2026-08-05T10:00:00Z"}],
                    }
                ],
            },
        )
        snap = await SecurityOperations(transport=http).get_snapshot()
        assert snap.severity == "alarm"
        assert snap.engine_healthy is True
        assert snap.classes[0].class_ == "smoke"
        assert snap.classes[0].known == 3

    async def test_get_class_percent_encodes(self, mock_daemon: MockDaemon, http: HttpTransport) -> None:
        mock_daemon.get(
            "/api/v1/security/classes/smoke",
            payload={"class": "smoke", "active": False, "severity": "ok", "known": 1},
        )
        state = await SecurityOperations(transport=http).get_class(security_class="smoke")
        assert state.active is False

    async def test_list_sources_without_filters_sends_no_query(
        self, mock_daemon: MockDaemon, http: HttpTransport
    ) -> None:
        """The unfiltered list is the only way to find a misclassified source."""
        mock_daemon.get("/api/v1/security/sources", payload=[_SOURCE])
        sources = await SecurityOperations(transport=http).list_sources()
        assert sources[0].class_ == "smoke"
        assert mock_daemon.requests[-1].query == {}

    async def test_list_sources_filters(self, mock_daemon: MockDaemon, http: HttpTransport) -> None:
        mock_daemon.get("/api/v1/security/sources", payload=[])
        await SecurityOperations(transport=http).list_sources(
            security_class="water",
            central="home",
            zone_id="eg",
            relevant_only=True,
            active_only=True,
        )
        assert mock_daemon.requests[-1].query == {
            "class": "water",
            "central": "home",
            "zone_id": "eg",
            "relevant": "true",
            "active": "true",
        }

    async def test_unset_flags_are_omitted_not_sent_as_false(
        self, mock_daemon: MockDaemon, http: HttpTransport
    ) -> None:
        """The daemon models both flags as a one-value enum: only "true" is valid."""
        mock_daemon.get("/api/v1/security/sources", payload=[])
        await SecurityOperations(transport=http).list_sources(relevant_only=False, active_only=False)
        assert mock_daemon.requests[-1].query == {}

    async def test_source_override_percent_encodes_the_routing_key(
        self, mock_daemon: MockDaemon, http: HttpTransport
    ) -> None:
        """The ref carries pipes and a colon; an unencoded path would 404."""
        ref = "home|home:HmIP-RF|ABC123:1|SMOKE_DETECTOR_ALARM_STATUS"
        mock_daemon.put(f"/api/v1/security/sources/{ref}", status=204)
        await SecurityOperations(transport=http).set_source_override(
            ref=ref,
            override=SecuritySourceOverride.model_validate({"class": "technical", "note": "Prüfmelder"}),
        )
        assert mock_daemon.requests[-1].json() == {"class": "technical", "note": "Prüfmelder"}

    async def test_override_that_only_names_a_class_does_not_exclude(
        self, mock_daemon: MockDaemon, http: HttpTransport
    ) -> None:
        """
        Omitting ``included`` leaves inclusion unchanged.

        Sending ``included: false`` by accident would remove the source
        from every aggregate — the failure mode is a hazard that stops
        being reported while the UI still shows it as classified.
        """
        mock_daemon.put("/api/v1/security/sources/r1", status=204)
        await SecurityOperations(transport=http).set_source_override(
            ref="r1", override=SecuritySourceOverride.model_validate({"class": "gas"})
        )
        assert "included" not in mock_daemon.requests[-1].json()

    async def test_list_faults(self, mock_daemon: MockDaemon, http: HttpTransport) -> None:
        mock_daemon.get(
            "/api/v1/security/faults",
            payload=[
                {
                    "id": "f1",
                    "class": "battery",
                    "reason": "low_battery",
                    "severity": "warning",
                    "source": {"ref": "r1", "at": "2026-08-05T10:00:00Z"},
                    "since": "2026-08-05T09:00:00Z",
                }
            ],
        )
        faults = await SecurityOperations(transport=http).list_faults()
        assert faults[0].reason.value == "low_battery"

    async def test_acknowledge_fault_is_not_retried(self, mock_daemon: MockDaemon, http: HttpTransport) -> None:
        mock_daemon.post("/api/v1/security/faults/f1/acknowledge", status=204)
        await SecurityOperations(transport=http).acknowledge_fault(fault_id="f1")
        assert mock_daemon.requests[-1].path.endswith("/security/faults/f1/acknowledge")

    async def test_empty_collection_survives_a_null_body(self, mock_daemon: MockDaemon, http: HttpTransport) -> None:
        """A daemon answering `null` for an empty ledger must not crash the read."""
        mock_daemon.get("/api/v1/security/faults", payload=None)
        assert await SecurityOperations(transport=http).list_faults() == []


class TestSecurityEventDispatch:
    """
    Each broadcast becomes its typed event with a usable routing key.

    The key decides which subscriptions a frame reaches: a wrong one
    means a subscriber that filters (``event_key="smoke"``) silently
    never fires while the bus reports the event delivered.
    """

    def _envelope(self, *, type_: str, payload: dict[str, Any], topic: str) -> WsEnvelope:
        return WsEnvelope.model_validate(
            {
                "topic": topic,
                "type": type_,
                "ts": "2026-08-05T08:42:13Z",
                "seq": 7,
                "kind": "change",
                "payload": payload,
            }
        )

    def test_state_changed(self) -> None:
        event = event_from_envelope(
            envelope=self._envelope(
                type_="security.state_changed",
                topic="security.state",
                payload={
                    "severity": "alarm",
                    "previous_severity": "ok",
                    "active_classes": ["smoke"],
                    "open_faults": 2,
                },
            )
        )
        assert isinstance(event, SecurityStateChangedEvent)
        assert event.payload.severity == "alarm"
        assert event.payload.previous_severity == "ok"
        assert event.payload.open_faults == 2

    def test_class_changed_keys_on_the_class(self) -> None:
        event = event_from_envelope(
            envelope=self._envelope(
                type_="security.class_changed",
                topic="security.state",
                payload={"class": "smoke", "active": True, "sources": [{"ref": "r1", "at": "2026-08-05T10:00:00Z"}]},
            )
        )
        assert isinstance(event, SecurityClassChangedEvent)
        assert event.event_key == "smoke"
        assert event.payload.sources is not None
        assert event.payload.sources[0].ref == "r1"

    def test_zone_changed_keys_on_the_zone(self) -> None:
        event = event_from_envelope(
            envelope=self._envelope(
                type_="security.zone_changed",
                topic="security.state",
                payload={"zone_id": "z1", "zone_slug": "erdgeschoss", "state": "triggered"},
            )
        )
        assert isinstance(event, SecurityZoneChangedEvent)
        assert event.event_key == "z1"
        assert event.payload.zone_slug == "erdgeschoss"

    def test_fault_changed_keys_on_the_fault(self) -> None:
        event = event_from_envelope(
            envelope=self._envelope(
                type_="security.fault_changed",
                topic="security.faults",
                payload={
                    "fault_id": "f1",
                    "class": "battery",
                    "reason": "low_battery",
                    "severity": "warning",
                    "source": {"ref": "r1", "at": "2026-08-05T10:00:00Z"},
                    "open": True,
                    "acknowledged": False,
                    "open_count": 3,
                },
            )
        )
        assert isinstance(event, SecurityFaultChangedEvent)
        assert event.event_key == "f1"
        assert event.payload.open is True
        assert event.payload.open_count == 3

    def test_notification_carries_prose_and_the_i18n_key(self) -> None:
        event = event_from_envelope(
            envelope=self._envelope(
                type_="security.notification",
                topic="security.notifications",
                payload={
                    "class": "smoke",
                    "severity": "alarm",
                    "verb": "triggered",
                    "subject": "Rauchalarm",
                    "message": "Rauchmelder Flur meldet Rauch.",
                    "i18n_key": "security.smoke.triggered",
                    "args": {"name": "Rauchmelder Flur"},
                    "at": "2026-08-05T10:00:00Z",
                    "fault": False,
                },
            )
        )
        assert isinstance(event, SecurityNotificationEvent)
        assert event.event_key == "smoke"
        assert event.payload.i18n_key == "security.smoke.triggered"
        assert event.payload.fault is False
        assert event.kind == Kind.change
