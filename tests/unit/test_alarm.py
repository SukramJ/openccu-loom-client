# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Alarm-panel surface: store section, domain wrapper, REST façade, events.

Covers the daemon-≥ 0.42.0 alarm system client-side: the store's
panel catalogue + live-update apply methods, the AlarmPanel wrapper
(incl. the master fan-out semantics mirroring the daemon's MQTT
``MasterArm``), the ``/alarm`` REST façade against the mock daemon,
the WS event dispatch, and the bootstrap feature-detection (404 =
alarm subsystem disabled).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from openccu_loom_client import LoomClient
from openccu_loom_client.events.types import (
    AlarmJournalAppendedEvent,
    AlarmPanelChangedEvent,
    AlarmStateChangedEvent,
    event_from_envelope,
)
from openccu_loom_client.operations import AlarmOperations
from openccu_loom_client.store import LoomStore
from openccu_loom_client.transport import HttpTransport
from openccu_loom_client.wire import DAEMON_API_VERSION
from openccu_loom_client.wire.rest import (
    AlarmOutput,
    AlarmPanelEntity,
    AlarmTriggeredMotionSensor,
    AlarmZone,
    AlarmZoneCreate,
    AlarmZoneStatus,
    Kind2 as Kind,
)
from openccu_loom_client.wire.ws import (
    AlarmCountdownPayload,
    AlarmHealthChangedPayload,
    AlarmPanelChangedPayload,
    AlarmReadinessChangedPayload,
    AlarmStateChangedPayload,
    AlarmTriggeredPayload,
    WsEnvelope,
)
from tests.helpers import MockDaemon

# ---- fixtures ----


def _panel_entity(
    *,
    zone_id: str = "erdgeschoss",
    name: str = "Erdgeschoss",
    state: str = "disarmed",
    available: bool = True,
    master: bool = False,
    supported_modes: list[str] | None = None,
    code_arm_required: bool = False,
    code_disarm_required: bool = False,
) -> AlarmPanelEntity:
    return AlarmPanelEntity.model_validate(
        {
            "unique_id": f"openccu-loom_alarm_{zone_id}",
            "zone_id": zone_id,
            "name": name,
            "category": "alarm_control_panel",
            "state": state,
            "available": available,
            "master": master,
            "supported_modes": supported_modes if supported_modes is not None else ["perimeter", "full"],
            "code_arm_required": code_arm_required,
            "code_disarm_required": code_disarm_required,
        }
    )


def _zone_status(*, zone_id: str = "erdgeschoss", **overrides: Any) -> AlarmZoneStatus:
    payload: dict[str, Any] = {
        "id": zone_id,
        "name": "Erdgeschoss",
        "state": "armed",
        "mode": "full",
        "bypassed": ["s1"],
        "countdown": {"kind": "exit_delay", "remaining_s": 20, "total_s": 30},
        "readiness": {"full": {"ready": False, "blockers": ["sensor.window"]}},
        "walktest_active": False,
    }
    payload.update(overrides)
    return AlarmZoneStatus.model_validate(payload)


class _FakeTransport:
    """
    Records every call so assertions can match against URL + body.

    Answers `/arm` with an `AlarmArmAccepted` record, because that is what the
    daemon answers: `POST /alarm/zones/{id}/arm` returns 200 with a body
    (`internal/north/rest/handlers/alarm.go:169`, documented in the spec).
    Returning `None` here modelled a response the daemon never sends, which
    is why the store could discard the record without anything noticing.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Any = None,
        json_body: Any = None,
        headers: Any = None,
        allow_retry: Any = None,
    ) -> Any:
        self.calls.append((method, path, json_body))
        if path.endswith("/arm"):
            return {"state": "armed", "bypassed": [], "exit_delay_s": 0}
        return None


def _store_with_panels(*panels: AlarmPanelEntity, transport: Any = None) -> LoomStore:
    store = LoomStore(transport=transport)
    store.attach_alarm_panels(panels=list(panels))
    return store


# ---- store: catalogue ----


class TestAttachAlarmPanels:
    def test_panels_land_keyed_by_unique_id_and_zone(self) -> None:
        store = _store_with_panels(_panel_entity(), _panel_entity(zone_id="og", name="OG"))
        assert {p.unique_id for p in store.alarm_panels} == {
            "openccu-loom_alarm_erdgeschoss",
            "openccu-loom_alarm_og",
        }
        panel = store.get_alarm_panel_by_zone(zone_id="og")
        assert panel is not None
        assert panel.name == "OG"

    def test_reattach_updates_in_place_and_drops_stale(self) -> None:
        store = _store_with_panels(_panel_entity(), _panel_entity(zone_id="og"))
        live = store.get_alarm_panel(unique_id="openccu-loom_alarm_erdgeschoss")
        store.attach_alarm_panels(panels=[_panel_entity(name="Renamed", state="armed_away")])
        assert store.get_alarm_panel(unique_id="openccu-loom_alarm_og") is None
        assert store.get_alarm_panel(unique_id="openccu-loom_alarm_erdgeschoss") is live
        assert live is not None
        assert live.name == "Renamed"
        assert live.state == "armed_away"

    def test_factory_hook_builds_subclass(self) -> None:
        from openccu_loom_client.model import AlarmPanel

        class CategorisedPanel(AlarmPanel):
            pass

        store = LoomStore()
        store.set_alarm_panel_factory(factory=CategorisedPanel)
        store.attach_alarm_panels(panels=[_panel_entity()])
        assert isinstance(store.get_alarm_panel_by_zone(zone_id="erdgeschoss"), CategorisedPanel)

    def test_zone_statuses_seed_live_detail(self) -> None:
        store = _store_with_panels(_panel_entity())
        store.attach_alarm_zone_statuses(statuses=[_zone_status()])
        panel = store.get_alarm_panel_by_zone(zone_id="erdgeschoss")
        assert panel is not None
        assert panel.mode == "full"
        assert panel.bypassed == ("s1",)
        assert panel.countdown_kind == "exit_delay"
        assert panel.countdown_remaining_s == 20
        assert panel.readiness["full"].ready is False
        assert panel.walktest_active is False

    def test_unknown_zone_status_is_ignored(self) -> None:
        store = _store_with_panels(_panel_entity())
        store.attach_alarm_zone_statuses(statuses=[_zone_status(zone_id="unknown")])
        panel = store.get_alarm_panel_by_zone(zone_id="erdgeschoss")
        assert panel is not None
        assert panel.mode is None


# ---- store: live updates ----


class TestAlarmApply:
    def test_panel_changed_updates_live_instance(self) -> None:
        store = _store_with_panels(_panel_entity())
        panel = store.get_alarm_panel_by_zone(zone_id="erdgeschoss")
        store.apply_alarm_panel_changed(
            payload=AlarmPanelChangedPayload.model_validate(
                {
                    "unique_id": "openccu-loom_alarm_erdgeschoss",
                    "zone_id": "erdgeschoss",
                    "name": "Erdgeschoss",
                    "state": "armed_home",
                    "available": False,
                    "code_arm_required": True,
                    "code_disarm_required": True,
                }
            )
        )
        assert panel is not None
        assert panel.state == "armed_home"
        assert panel.available is False

    def test_panel_changed_propagates_code_policy(self) -> None:
        # Live policy edits (code created/deleted, policy switch) ride the
        # push — the panel must reflect them without a catalogue reconcile.
        store = _store_with_panels(_panel_entity())
        panel = store.get_alarm_panel_by_zone(zone_id="erdgeschoss")
        assert panel is not None
        assert panel.code_arm_required is False
        store.apply_alarm_panel_changed(
            payload=AlarmPanelChangedPayload.model_validate(
                {
                    "unique_id": "openccu-loom_alarm_erdgeschoss",
                    "zone_id": "erdgeschoss",
                    "name": "Erdgeschoss",
                    "state": "disarmed",
                    "available": True,
                    "code_arm_required": True,
                    "code_disarm_required": True,
                }
            )
        )
        assert panel.code_arm_required is True
        assert panel.code_disarm_required is True

    def test_panel_changed_removed_drops_panel(self) -> None:
        store = _store_with_panels(_panel_entity())
        store.apply_alarm_panel_changed(
            payload=AlarmPanelChangedPayload.model_validate(
                {
                    "unique_id": "openccu-loom_alarm_erdgeschoss",
                    "zone_id": "erdgeschoss",
                    "name": "Erdgeschoss",
                    "state": "disarmed",
                    "available": True,
                    "code_arm_required": False,
                    "code_disarm_required": False,
                    "removed": True,
                }
            )
        )
        assert store.get_alarm_panel(unique_id="openccu-loom_alarm_erdgeschoss") is None
        assert store.get_alarm_panel_by_zone(zone_id="erdgeschoss") is None

    def test_panel_changed_unknown_seeds_stub(self) -> None:
        store = LoomStore()
        store.apply_alarm_panel_changed(
            payload=AlarmPanelChangedPayload.model_validate(
                {
                    "unique_id": "openccu-loom_alarm_master",
                    "zone_id": "master",
                    "name": "Alarmanlage",
                    "state": "disarmed",
                    "available": True,
                    "code_arm_required": True,
                    "code_disarm_required": False,
                }
            )
        )
        stub = store.get_alarm_panel(unique_id="openccu-loom_alarm_master")
        assert stub is not None
        assert stub.is_master is True
        assert stub.supported_modes == ()

    def test_state_changed_updates_mode_and_ends_countdown(self) -> None:
        store = _store_with_panels(_panel_entity())
        store.attach_alarm_zone_statuses(statuses=[_zone_status(state="arming")])
        panel = store.get_alarm_panel_by_zone(zone_id="erdgeschoss")
        assert panel is not None
        assert panel.countdown_remaining_s == 20
        store.apply_alarm_state_changed(
            payload=AlarmStateChangedPayload.model_validate(
                {
                    "zone_id": "erdgeschoss",
                    "zone_name": "Erdgeschoss",
                    "old_state": "arming",
                    "new_state": "armed",
                    "mode": "full",
                }
            )
        )
        assert panel.mode == "full"
        assert panel.countdown_kind is None
        assert panel.countdown_remaining_s is None

    def test_countdown_tick_sets_fields(self) -> None:
        store = _store_with_panels(_panel_entity())
        store.apply_alarm_countdown(
            payload=AlarmCountdownPayload.model_validate(
                {
                    "zone_id": "erdgeschoss",
                    "kind": "entry_delay",
                    "remaining_s": 12,
                    "total_s": 30,
                    "remaining_ms": 12000,
                    "total_ms": 30000,
                }
            )
        )
        panel = store.get_alarm_panel_by_zone(zone_id="erdgeschoss")
        assert panel is not None
        assert panel.countdown_kind == "entry_delay"
        assert panel.countdown_remaining_s == 12
        assert panel.countdown_total_s == 30

    def test_readiness_changed_replaces_map(self) -> None:
        store = _store_with_panels(_panel_entity())
        store.apply_alarm_readiness_changed(
            payload=AlarmReadinessChangedPayload.model_validate(
                {
                    "zone_id": "erdgeschoss",
                    "readiness": {"perimeter": {"ready": True}},
                }
            )
        )
        panel = store.get_alarm_panel_by_zone(zone_id="erdgeschoss")
        assert panel is not None
        assert panel.readiness["perimeter"].ready is True

    def test_triggered_records_incident(self) -> None:
        store = _store_with_panels(_panel_entity())
        store.apply_alarm_triggered(
            payload=AlarmTriggeredPayload.model_validate(
                {
                    "zone_id": "erdgeschoss",
                    "zone_name": "Erdgeschoss",
                    "incident_id": 7,
                    "sensor_id": "s1",
                    "sensor_name": "Fenster Küche",
                    "cause": "sensor_open",
                    "mode": "full",
                }
            )
        )
        panel = store.get_alarm_panel_by_zone(zone_id="erdgeschoss")
        assert panel is not None
        assert panel.last_incident_id == 7
        assert panel.last_incident_cause == "sensor_open"
        assert panel.last_incident_sensor == "Fenster Küche"

    def test_health_changed_latches_flag(self) -> None:
        store = LoomStore()
        assert store.alarm_healthy is True
        store.apply_alarm_health_changed(
            payload=AlarmHealthChangedPayload.model_validate({"healthy": False, "note": "output unreachable"})
        )
        assert store.alarm_healthy is False

    def test_events_for_unknown_zone_are_ignored(self) -> None:
        store = LoomStore()
        store.apply_alarm_countdown(
            payload=AlarmCountdownPayload.model_validate(
                {
                    "zone_id": "ghost",
                    "kind": "exit_delay",
                    "remaining_s": 1,
                    "total_s": 2,
                    "remaining_ms": 1000,
                    "total_ms": 2000,
                }
            )
        )
        # No panel, no crash.
        assert list(store.alarm_panels) == []


# ---- store: write-back + master fan-out ----


class TestAlarmWriteBack:
    async def test_arm_posts_body(self) -> None:
        transport = _FakeTransport()
        store = _store_with_panels(_panel_entity(), transport=transport)
        panel = store.get_alarm_panel_by_zone(zone_id="erdgeschoss")
        assert panel is not None
        await panel.arm(mode="full", code="1234", skip_delay=True)
        assert transport.calls == [
            (
                "POST",
                "/alarm/zones/erdgeschoss/arm",
                {"mode": "full", "skip_delay": True, "code": "1234"},
            )
        ]

    async def test_disarm_without_code_posts_empty_body(self) -> None:
        transport = _FakeTransport()
        store = _store_with_panels(_panel_entity(), transport=transport)
        panel = store.get_alarm_panel_by_zone(zone_id="erdgeschoss")
        assert panel is not None
        await panel.disarm()
        assert transport.calls == [("POST", "/alarm/zones/erdgeschoss/disarm", {})]

    async def test_zone_id_is_percent_encoded(self) -> None:
        transport = _FakeTransport()
        store = LoomStore(transport=transport)
        await store.silence_alarm_zone(zone_id="a|b:c")
        assert transport.calls == [("POST", "/alarm/zones/a%7Cb%3Ac/silence", {})]

    async def test_master_arm_fans_out_to_mode_capable_zones(self) -> None:
        transport = _FakeTransport()
        store = _store_with_panels(
            _panel_entity(zone_id="eg", supported_modes=["perimeter", "full"]),
            _panel_entity(zone_id="og", supported_modes=["full"]),
            _panel_entity(zone_id="keller", supported_modes=["perimeter"]),
            _panel_entity(zone_id="master", master=True, supported_modes=[]),
            transport=transport,
        )
        master = store.get_alarm_panel_by_zone(zone_id="master")
        assert master is not None
        await master.arm(mode="full")
        assert [c[1] for c in transport.calls] == [
            "/alarm/zones/eg/arm",
            "/alarm/zones/og/arm",
        ]

    async def test_master_disarm_fans_out_to_all_zones(self) -> None:
        transport = _FakeTransport()
        store = _store_with_panels(
            _panel_entity(zone_id="eg"),
            _panel_entity(zone_id="og"),
            _panel_entity(zone_id="master", master=True, supported_modes=[]),
            transport=transport,
        )
        master = store.get_alarm_panel_by_zone(zone_id="master")
        assert master is not None
        await master.disarm(code="1234")
        assert [c[1] for c in transport.calls] == [
            "/alarm/zones/eg/disarm",
            "/alarm/zones/og/disarm",
        ]

    async def test_master_silence_uses_silence_all(self) -> None:
        transport = _FakeTransport()
        store = _store_with_panels(
            _panel_entity(zone_id="eg"),
            _panel_entity(zone_id="master", master=True, supported_modes=[]),
            transport=transport,
        )
        master = store.get_alarm_panel_by_zone(zone_id="master")
        assert master is not None
        await master.silence()
        assert transport.calls == [("POST", "/alarm/silence-all", None)]


class _ResetTransport:
    """Records every call and answers it with the daemon's counter body."""

    def __init__(self, *, reset: int = 0, failed: int = 0) -> None:
        self.calls: list[dict[str, Any]] = []
        self._result: dict[str, Any] = {"reset": reset, "failed": failed, "sensors": []}

    async def request(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self._result


class TestMotionResetVerbs:
    """``AlarmPanel.reset_motion`` → store → transport (daemon ≥ 0.58.1)."""

    async def test_zone_panel_resets_its_own_zone(self) -> None:
        transport = _ResetTransport(reset=2, failed=1)
        store = _store_with_panels(_panel_entity(zone_id="eg"), transport=transport)
        panel = store.get_alarm_panel_by_zone(zone_id="eg")
        assert panel is not None
        result = await panel.reset_motion()
        assert transport.calls[0]["path"] == "/alarm/zones/eg/reset-motion"
        assert transport.calls[0]["allow_retry"] is False
        # There is no ``alarm.*`` broadcast for a reset pass, so these
        # counters are the only report the caller ever gets.
        assert (result.reset, result.failed) == (2, 1)

    async def test_master_uses_the_aggregate_route(self) -> None:
        """
        One daemon-side pass, not a loop over the zones.

        A detector enrolled in two zones would otherwise be written
        twice, and the operator would get two partial counter sets to
        reconcile instead of one.
        """
        transport = _ResetTransport()
        store = _store_with_panels(
            _panel_entity(zone_id="eg"),
            _panel_entity(zone_id="og"),
            _panel_entity(zone_id="master", master=True, supported_modes=[]),
            transport=transport,
        )
        master = store.get_alarm_panel_by_zone(zone_id="master")
        assert master is not None
        await master.reset_motion()
        assert [(c["method"], c["path"]) for c in transport.calls] == [("POST", "/alarm/reset-motion")]

    async def test_zone_id_is_percent_encoded(self) -> None:
        transport = _ResetTransport()
        store = _store_with_panels(_panel_entity(zone_id="a|b:c"), transport=transport)
        panel = store.get_alarm_panel_by_zone(zone_id="a|b:c")
        assert panel is not None
        await panel.reset_motion()
        assert transport.calls[0]["path"] == "/alarm/zones/a%7Cb%3Ac/reset-motion"


class TestTriggeredMotionCounts:
    """``LoomStore.apply_triggered_motion`` — the counts behind the HA sensor."""

    @staticmethod
    def _sensor(*, zone_id: str, sensor_id: str) -> AlarmTriggeredMotionSensor:
        return AlarmTriggeredMotionSensor.model_validate(
            {
                "sensor_id": sensor_id,
                "zone_id": zone_id,
                "channel_address": f"{sensor_id}:1",
                "parameter": "MOTION",
            }
        )

    def test_counts_land_per_zone_and_total_on_the_master(self) -> None:
        store = _store_with_panels(
            _panel_entity(zone_id="eg"),
            _panel_entity(zone_id="og"),
            _panel_entity(zone_id="master", master=True, supported_modes=[]),
        )
        store.apply_triggered_motion(
            sensors=[
                self._sensor(zone_id="eg", sensor_id="s1"),
                self._sensor(zone_id="eg", sensor_id="s2"),
                self._sensor(zone_id="og", sensor_id="s3"),
            ]
        )
        counts = {p.zone_id: p.triggered_motion_count for p in store.alarm_panels}
        # The master's total is the scope its aggregate reset covers.
        assert counts == {"eg": 2, "og": 1, "master": 3}

    def test_a_zone_dropping_to_zero_is_cleared(self) -> None:
        """
        Every panel is written, not just the ones named in the answer.

        Skipping the absent ones would leave a cleared zone showing its
        last non-zero count forever — the endpoint reports what *is*
        latched, never what stopped being.
        """
        store = _store_with_panels(_panel_entity(zone_id="eg"), _panel_entity(zone_id="og"))
        store.apply_triggered_motion(sensors=[self._sensor(zone_id="eg", sensor_id="s1")])
        store.apply_triggered_motion(sensors=[self._sensor(zone_id="og", sensor_id="s2")])
        counts = {p.zone_id: p.triggered_motion_count for p in store.alarm_panels}
        assert counts == {"eg": 0, "og": 1}

    def test_count_starts_at_zero(self) -> None:
        store = _store_with_panels(_panel_entity(zone_id="eg"))
        panel = store.get_alarm_panel_by_zone(zone_id="eg")
        assert panel is not None
        assert panel.triggered_motion_count == 0


# ---- events: dispatch ----


def _envelope(*, type_: str, payload: dict) -> WsEnvelope:
    return WsEnvelope.model_validate(
        {
            "topic": "alarm.panel",
            "type": type_,
            "ts": "2026-07-16T08:42:13Z",
            "seq": 1,
            "kind": "change",
            "payload": payload,
        }
    )


class TestAlarmEventDispatch:
    def test_state_changed_keyed_by_zone(self) -> None:
        event = event_from_envelope(
            envelope=_envelope(
                type_="alarm.state_changed",
                payload={
                    "zone_id": "eg",
                    "zone_name": "EG",
                    "old_state": "disarmed",
                    "new_state": "arming",
                    "mode": "full",
                },
            )
        )
        assert isinstance(event, AlarmStateChangedEvent)
        assert event.event_key == "eg"
        assert event.kind is Kind.change

    def test_panel_changed_keyed_by_unique_id(self) -> None:
        event = event_from_envelope(
            envelope=_envelope(
                type_="alarm.panel_changed",
                payload={
                    "unique_id": "openccu-loom_alarm_eg",
                    "zone_id": "eg",
                    "name": "EG",
                    "state": "armed_away",
                    "available": True,
                    "code_arm_required": False,
                    "code_disarm_required": False,
                },
            )
        )
        assert isinstance(event, AlarmPanelChangedEvent)
        assert event.event_key == "openccu-loom_alarm_eg"

    def test_notification_keyed_by_zone(self) -> None:
        from openccu_loom_client.events.types import AlarmNotificationEvent

        event = event_from_envelope(
            envelope=_envelope(
                type_="alarm.notification",
                payload={
                    "zone_id": "eg",
                    "zone_name": "EG",
                    "output_id": "out|1",
                    "output_name": "Push",
                    "incident_id": 9,
                    "mode": "full",
                },
            )
        )
        assert isinstance(event, AlarmNotificationEvent)
        assert event.event_key == "eg"
        assert event.payload.incident_id == 9

    def test_journal_appended_parses_class_alias(self) -> None:
        event = event_from_envelope(
            envelope=_envelope(
                type_="alarm.journal_appended",
                payload={"entry_id": 5, "class": "arm", "event": "armed full"},
            )
        )
        assert isinstance(event, AlarmJournalAppendedEvent)
        assert event.payload.class_.value == "arm"
        # Engine-global entry (no zone) → unkeyed.
        assert event.event_key is None


# ---- REST façade against the mock daemon ----

_INFO = {
    "version": "1.2.3",
    "api_version": DAEMON_API_VERSION,
    "commit": "deadbeef",
    "build_date": "2026-05-24T10:00:00Z",
    "addon_build": False,
    "started_at": "2026-05-24T10:01:00Z",
    "uptime": "PT60S",
    "capabilities": ["rest.v1", "ws.broadcasts.v1", "alarm.v1"],
    "schema_digest": "sha256:test",
    "config_ui_url": "",
}

# A daemon whose alarm subsystem is off: no ``alarm.v1`` token.
_INFO_NO_ALARM = {**_INFO, "capabilities": ["rest.v1", "ws.broadcasts.v1"]}


@pytest.fixture
async def http(mock_daemon: MockDaemon) -> AsyncIterator[HttpTransport]:
    t = HttpTransport(config=mock_daemon.config, backoff_sequence=(0.0,))
    mock_daemon.get("/api/v1/info", payload=_INFO)
    await t.connect()
    yield t
    await t.close()


class TestAlarmOperations:
    async def test_list_panels(self, mock_daemon: MockDaemon, http: HttpTransport) -> None:
        mock_daemon.get("/api/v1/alarm/panels", payload=[_panel_entity().model_dump(mode="json")])
        panels = await AlarmOperations(transport=http).list_panels()
        assert len(panels) == 1
        assert panels[0].unique_id == "openccu-loom_alarm_erdgeschoss"

    async def test_get_zone_statuses_unwraps_envelope(self, mock_daemon: MockDaemon, http: HttpTransport) -> None:
        mock_daemon.get("/api/v1/alarm/state", payload={"zones": [_zone_status().model_dump(mode="json")]})
        statuses = await AlarmOperations(transport=http).get_zone_statuses()
        assert len(statuses) == 1
        assert statuses[0].id == "erdgeschoss"

    async def test_arm_zone_posts_and_parses_accepted(self, mock_daemon: MockDaemon, http: HttpTransport) -> None:
        mock_daemon.post(
            "/api/v1/alarm/zones/eg/arm",
            payload={"state": "arming", "bypassed": ["s1"], "exit_delay_s": 30},
        )
        accepted = await AlarmOperations(transport=http).arm_zone(zone_id="eg", mode="full", force=True)
        assert accepted.exit_delay_s == 30
        sent = mock_daemon.requests[-1].json()
        assert sent == {"mode": "full", "force": True}

    async def test_journal_query_params(self, mock_daemon: MockDaemon, http: HttpTransport) -> None:
        mock_daemon.get("/api/v1/alarm/journal", payload=[])
        await AlarmOperations(transport=http).list_journal(zone_id="eg", journal_class="trigger", limit=10)
        query = mock_daemon.requests[-1].query
        assert query == {"zone": "eg", "class": "trigger", "limit": "10"}

    async def test_output_test_id_is_percent_encoded(self, mock_daemon: MockDaemon, http: HttpTransport) -> None:
        mock_daemon.post("/api/v1/alarm/outputs/home|OUT:3/test", status=204)
        await AlarmOperations(transport=http).test_output(output_id="home|OUT:3", optical_only=True)
        assert mock_daemon.requests[-1].json() == {"optical_only": True}

    async def test_zone_outputs_are_sent_under_their_wire_names(
        self, mock_daemon: MockDaemon, http: HttpTransport
    ) -> None:
        """
        The output class must reach the daemon as ``class``, not ``class_``.

        The generated model renames the field because ``class`` is a Python
        keyword, and the schema marks the property required — so a body
        dumped by field name fails validation and no output is ever
        enrolled. The symptom is a zone that arms and then stays silent.
        """
        mock_daemon.put("/api/v1/alarm/zones/eg/outputs", status=204)
        output = AlarmOutput.model_validate(
            {
                "id": "home|SIREN:3",
                "class": "acoustic_siren",
                "central": "home",
                "channel_address": "SIREN:3",
            }
        )
        await AlarmOperations(transport=http).replace_zone_outputs(zone_id="eg", outputs=[output])
        sent = mock_daemon.requests[-1].json()
        assert sent == [
            {
                "id": "home|SIREN:3",
                "class": "acoustic_siren",
                "central": "home",
                "channel_address": "SIREN:3",
            }
        ]

    async def test_list_sensor_candidates(self, mock_daemon: MockDaemon, http: HttpTransport) -> None:
        """Sensor enrolment was the one alarm surface without a candidate list."""
        mock_daemon.get(
            "/api/v1/alarm/sensor-candidates",
            payload=[
                {
                    "central": "home",
                    "interface_id": "home:HmIP-RF",
                    "device_address": "ABC123",
                    "channel_address": "ABC123:1",
                    "channel_no": 1,
                    "parameter": "SMOKE_DETECTOR_ALARM_STATUS",
                    "sensor_type": "hazard",
                    "security_class": "smoke",
                    "value_list": ["IDLE_OFF", "PRIMARY_ALARM", "INTRUSION_ALARM"],
                    "active_values": ["PRIMARY_ALARM"],
                }
            ],
        )
        candidates = await AlarmOperations(transport=http).list_sensor_candidates()
        assert candidates[0].sensor_type is not None
        assert candidates[0].sensor_type.value == "hazard"
        # The clearest reason active_values exists: the value list contains
        # INTRUSION_ALARM, which the alarm system drives — the default
        # "anything but index 0 is active" rule would feed the alarm its own echo.
        assert candidates[0].active_values == ["PRIMARY_ALARM"]
        assert mock_daemon.requests[-1].query == {}

    async def test_sensor_candidates_unenrolled_filter(self, mock_daemon: MockDaemon, http: HttpTransport) -> None:
        mock_daemon.get("/api/v1/alarm/sensor-candidates", payload=[])
        await AlarmOperations(transport=http).list_sensor_candidates(unenrolled_only=True)
        assert mock_daemon.requests[-1].query == {"enrolled": "false"}

    async def test_list_incidents_requires_a_zone(self, mock_daemon: MockDaemon, http: HttpTransport) -> None:
        mock_daemon.get("/api/v1/alarm/incidents", payload=[])
        await AlarmOperations(transport=http).list_incidents(zone_id="eg", limit=10)
        assert mock_daemon.requests[-1].query == {"zone_id": "eg", "limit": "10"}

    async def test_get_incident_carries_the_source_ledger(self, mock_daemon: MockDaemon, http: HttpTransport) -> None:
        mock_daemon.get(
            "/api/v1/alarm/incidents/42",
            payload={
                "id": 42,
                "zone_id": "eg",
                "mode": "full",
                "cause": "sensor",
                "sources": [
                    {"ref": "r1", "name": "Fenster Küche", "at": "2026-08-05T10:00:00Z"},
                    {"ref": "r2", "name": "Bewegung Flur", "at": "2026-08-05T10:00:04Z"},
                ],
                "started_at": "2026-08-05T10:00:00Z",
                "silenced": False,
                "retrigger_cycles": 0,
                "acoustic_seconds": 180,
                "open": True,
            },
        )
        incident = await AlarmOperations(transport=http).get_incident(incident_id=42)
        assert incident.open is True
        assert incident.sources is not None
        # Oldest first: "what else went off while the alarm ran" is the
        # question the ledger answers after the fact.
        assert [s.ref for s in incident.sources] == ["r1", "r2"]

    async def test_readiness_map(self, mock_daemon: MockDaemon, http: HttpTransport) -> None:
        mock_daemon.get(
            "/api/v1/alarm/zones/eg/readiness",
            payload={"full": {"ready": False, "blockers": ["sensor.window"]}},
        )
        readiness = await AlarmOperations(transport=http).get_zone_readiness(zone_id="eg")
        assert readiness["full"].ready is False
        assert readiness["full"].blockers == ["sensor.window"]

    async def test_create_zone_with_alarm_zone_create_omits_id(
        self, mock_daemon: MockDaemon, http: HttpTransport
    ) -> None:
        """
        The daemon mints the zone id itself, since api 7.1.0.

        The POST body is ``AlarmZoneCreate``: the server mints the zone id
        itself and ignores one sent in the body. Building an ``AlarmZone``
        just to create one means inventing an id that is discarded;
        ``AlarmZoneCreate`` has no ``id`` field at all, so the serialised
        body carries no ``id`` key.
        """
        mock_daemon.post("/api/v1/alarm/zones", payload={"id": "eg", "name": "Erdgeschoss"})
        zone = await AlarmOperations(transport=http).create_zone(
            zone=AlarmZoneCreate.model_validate({"name": "Erdgeschoss"})
        )
        assert zone.id == "eg"
        sent = mock_daemon.requests[-1].json()
        assert "id" not in sent
        assert sent == {"name": "Erdgeschoss"}

    async def test_create_zone_still_accepts_a_full_alarm_zone(
        self, mock_daemon: MockDaemon, http: HttpTransport
    ) -> None:
        """Existing callers that build a full ``AlarmZone`` (with an id) keep working."""
        mock_daemon.post("/api/v1/alarm/zones", payload={"id": "eg", "name": "Erdgeschoss"})
        zone = await AlarmOperations(transport=http).create_zone(
            zone=AlarmZone.model_validate({"id": "eg", "name": "Erdgeschoss"})
        )
        assert zone.id == "eg"
        sent = mock_daemon.requests[-1].json()
        assert sent == {"id": "eg", "name": "Erdgeschoss"}


# ---- motion reset ----

_TRIGGERED_MOTION = {
    "sensor_id": "home|00091BE9965DEB:1|MOTION",
    "zone_id": "eg",
    "name": "Bewegung Flur",
    "channel_address": "00091BE9965DEB:1",
    "parameter": "MOTION",
}


class TestMotionReset:
    """
    ``GET /alarm/triggered-motion`` + the two reset verbs (daemon ≥ 0.58.1).

    A detector holds its ``MOTION`` flag until its own blocking time
    expires, and reads as open until then — which blocks an arm with no
    recourse but waiting. These three routes are what a reset control
    needs.
    """

    async def test_list_triggered_motion_unfiltered(self, mock_daemon: MockDaemon, http: HttpTransport) -> None:
        mock_daemon.get("/api/v1/alarm/triggered-motion", payload=[_TRIGGERED_MOTION])
        sensors = await AlarmOperations(transport=http).list_triggered_motion()
        assert len(sensors) == 1
        assert sensors[0].channel_address == "00091BE9965DEB:1"
        # ``parameter`` is the sensor's own state parameter, never the
        # reset one — a caller that writes it back would re-arm the latch.
        assert sensors[0].parameter == "MOTION"
        assert mock_daemon.requests[-1].query == {}

    async def test_list_triggered_motion_filters_by_zone(self, mock_daemon: MockDaemon, http: HttpTransport) -> None:
        mock_daemon.get("/api/v1/alarm/triggered-motion", payload=[])
        await AlarmOperations(transport=http).list_triggered_motion(zone_id="eg")
        assert mock_daemon.requests[-1].query == {"zone_id": "eg"}

    async def test_list_triggered_motion_covers_presence_detectors(
        self, mock_daemon: MockDaemon, http: HttpTransport
    ) -> None:
        """An HmIP-SPI latches ``PRESENCE_DETECTION_STATE``, not ``MOTION``."""
        mock_daemon.get(
            "/api/v1/alarm/triggered-motion",
            payload=[{**_TRIGGERED_MOTION, "parameter": "PRESENCE_DETECTION_STATE", "name": None}],
        )
        sensors = await AlarmOperations(transport=http).list_triggered_motion()
        assert sensors[0].parameter == "PRESENCE_DETECTION_STATE"
        assert sensors[0].name is None

    async def test_reset_zone_motion_parses_counters(self, mock_daemon: MockDaemon, http: HttpTransport) -> None:
        mock_daemon.post(
            "/api/v1/alarm/zones/eg/reset-motion",
            payload={"reset": 2, "failed": 1, "sensors": [_TRIGGERED_MOTION]},
        )
        result = await AlarmOperations(transport=http).reset_zone_motion(zone_id="eg")
        # ``failed`` arrives in the body, not as an HTTP error: the verb
        # ran and a partial result is actionable.
        assert (result.reset, result.failed) == (2, 1)
        assert result.sensors[0].zone_id == "eg"
        assert mock_daemon.requests[-1].json() is None

    async def test_nothing_latched_differs_from_a_failed_write(
        self, mock_daemon: MockDaemon, http: HttpTransport
    ) -> None:
        """
        ``reset == 0 and failed == 0`` is a different outcome from ``failed > 0``.

        Collapsing the two would tell an operator "nothing to do" when a
        detector in fact did not answer — the case where the latch stays
        and the zone still refuses to arm.
        """
        ops = AlarmOperations(transport=http)
        mock_daemon.post("/api/v1/alarm/reset-motion", payload={"reset": 0, "failed": 0, "sensors": []})
        mock_daemon.post("/api/v1/alarm/reset-motion", payload={"reset": 0, "failed": 3, "sensors": []})
        quiet = await ops.reset_all_motion()
        unreachable = await ops.reset_all_motion()
        assert (quiet.reset, quiet.failed) == (0, 0)
        assert (unreachable.reset, unreachable.failed) == (0, 3)

    async def test_zone_id_is_percent_encoded(self) -> None:
        calls: list[dict[str, Any]] = []

        class _Transport:
            async def request(self, **kwargs: Any) -> Any:
                calls.append(kwargs)
                return {"reset": 0, "failed": 0, "sensors": []}

        await AlarmOperations(transport=_Transport()).reset_zone_motion(zone_id="a|b:c")  # type: ignore[arg-type]
        assert calls[0]["path"] == "/alarm/zones/a%7Cb%3Ac/reset-motion"

    async def test_resets_are_never_retried_but_the_read_is(self) -> None:
        """
        The verbs write to devices, so a blind replay is real radio traffic.

        The listing is a plain read and keeps the transport's idempotent
        default (``allow_retry=None`` → retried because it is a ``GET``).
        """
        calls: list[dict[str, Any]] = []

        class _Transport:
            async def request(self, **kwargs: Any) -> Any:
                calls.append(kwargs)
                return [] if kwargs["method"] == "GET" else {"reset": 0, "failed": 0, "sensors": []}

        ops = AlarmOperations(transport=_Transport())  # type: ignore[arg-type]
        await ops.reset_zone_motion(zone_id="eg")
        await ops.reset_all_motion()
        await ops.list_triggered_motion()
        assert [c["allow_retry"] for c in calls] == [False, False, None]


# ---- bootstrap feature detection ----

_EMPTY_SNAPSHOT = {
    "generated_at": "2026-07-16T08:00:00Z",
    "devices": [],
    "interfaces": [
        {
            "id": "home:HmIP-RF",
            "name": "HmIP-RF",
            "connected": True,
            "interface": "HmIP-RF",
            "central_id": "home",
        }
    ],
}


class TestBootstrapAlarm:
    async def test_bootstrap_populates_panels(self, mock_daemon: MockDaemon) -> None:
        mock_daemon.get("/api/v1/info", payload=_INFO)
        mock_daemon.get("/api/v1/snapshot", payload=_EMPTY_SNAPSHOT)
        mock_daemon.get("/api/v1/alarm/panels", payload=[_panel_entity().model_dump(mode="json")])
        mock_daemon.get("/api/v1/alarm/state", payload={"zones": [_zone_status().model_dump(mode="json")]})
        client = LoomClient(config=mock_daemon.config)
        try:
            await client.connect()
            await client.bootstrap()
            panel = client.store.get_alarm_panel_by_zone(zone_id="erdgeschoss")
            assert panel is not None
            assert panel.mode == "full"
        finally:
            await client.close()

    async def test_bootstrap_skips_alarm_on_404(self, mock_daemon: MockDaemon) -> None:
        # Token advertised but no /alarm stubs registered — the mock answers
        # 404 problem+json; the fallback path must degrade to "no alarm".
        mock_daemon.get("/api/v1/info", payload=_INFO)
        mock_daemon.get("/api/v1/snapshot", payload=_EMPTY_SNAPSHOT)
        client = LoomClient(config=mock_daemon.config)
        try:
            await client.connect()
            await client.bootstrap()
            assert list(client.store.alarm_panels) == []
        finally:
            await client.close()

    async def test_bootstrap_skips_alarm_without_capability(self, mock_daemon: MockDaemon) -> None:
        # No ``alarm.v1`` token → the section is skipped without a single
        # /alarm round-trip (the capability gate, daemon ≥ 0.43.1).
        mock_daemon.get("/api/v1/info", payload=_INFO_NO_ALARM)
        mock_daemon.get("/api/v1/snapshot", payload=_EMPTY_SNAPSHOT)
        client = LoomClient(config=mock_daemon.config)
        try:
            await client.connect()
            await client.bootstrap()
            assert list(client.store.alarm_panels) == []
            assert not any(r.path.startswith("/api/v1/alarm") for r in mock_daemon.requests)
        finally:
            await client.close()

    async def test_bootstrap_seeds_the_triggered_motion_counts(self, mock_daemon: MockDaemon) -> None:
        """
        Without the cold-start read the count sits at 0 until an alarm event lands.

        Nothing pushes a latch, so "no event yet" would otherwise be
        indistinguishable from "nothing latched" for as long as the alarm
        stays quiet — which is exactly when someone looks at the count to
        find out why a zone will not arm.
        """
        mock_daemon.get("/api/v1/info", payload=_INFO)
        mock_daemon.get("/api/v1/snapshot", payload=_EMPTY_SNAPSHOT)
        mock_daemon.get("/api/v1/alarm/panels", payload=[_panel_entity(zone_id="eg").model_dump(mode="json")])
        mock_daemon.get("/api/v1/alarm/state", payload={"zones": []})
        mock_daemon.get("/api/v1/alarm/triggered-motion", payload=[_TRIGGERED_MOTION])
        client = LoomClient(config=mock_daemon.config)
        try:
            await client.connect()
            await client.bootstrap()
            panel = client.store.get_alarm_panel_by_zone(zone_id="eg")
            assert panel is not None
            assert panel.triggered_motion_count == 1
        finally:
            await client.close()

    async def test_bootstrap_survives_a_daemon_without_the_route(self, mock_daemon: MockDaemon) -> None:
        """
        The route only exists from daemon 0.58.0 — a 404 must not fail setup.

        The same guard protects the event-driven refresh: a failing read
        there would propagate out of a background task.
        """
        mock_daemon.get("/api/v1/info", payload=_INFO)
        mock_daemon.get("/api/v1/snapshot", payload=_EMPTY_SNAPSHOT)
        mock_daemon.get("/api/v1/alarm/panels", payload=[_panel_entity(zone_id="eg").model_dump(mode="json")])
        mock_daemon.get("/api/v1/alarm/state", payload={"zones": []})
        # No stub for /alarm/triggered-motion — the mock answers 404.
        client = LoomClient(config=mock_daemon.config)
        try:
            await client.connect()
            await client.bootstrap()
            panel = client.store.get_alarm_panel_by_zone(zone_id="eg")
            assert panel is not None
            assert panel.triggered_motion_count == 0
        finally:
            await client.close()
