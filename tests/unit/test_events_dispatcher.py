# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
WsEnvelope → typed-LoomEvent dispatcher coverage.

The registry is the single point that translates wire-side events
into the application's domain model. These tests assert:

1. Every broadcast `name` advertised in the daemon's `wsapi.json`
   has a Python binding here — otherwise the client would silently
   drop matching frames as UnknownLoomEvent and HA would lose events.
2. Each binding deserializes a realistic payload cleanly.
3. Unknown `type` strings degrade gracefully to UnknownLoomEvent
   without raising — required for forward-compat against newer
   daemon versions that introduce broadcasts before the client
   knows about them.
4. Malformed payloads on a known `type` log a warning and also
   degrade to UnknownLoomEvent — required so a single bad frame
   never tears down the event loop.
"""

from __future__ import annotations

import json
import os
import pathlib

from openccu_loom_types.ws import WsEnvelope
import pytest

from openccu_loom_client.events import (
    AddonUpdateStateChangedEvent,
    CentralStateChangedEvent,
    CustomDataPointStateChangedEvent,
    DaemonStatusChangedEvent,
    DataPointValueChangedEvent,
    DeviceCreatedEvent,
    DeviceMetadataChangedEvent,
    DeviceRemovedEvent,
    HubAlarmMessageCountChangedEvent,
    HubConnectivityChangedEvent,
    HubInboxChangedEvent,
    HubMetricsChangedEvent,
    HubServiceMessageCountChangedEvent,
    ProgramExecutedEvent,
    ScheduleChangedEvent,
    SystemStatusChangedEvent,
    SysvarChangedEvent,
    UnknownLoomEvent,
    event_from_envelope,
)
from openccu_loom_client.events.types import DataPointOptimisticRolledBackEvent, DeviceTriggerEvent, known_event_types


def _find_wsapi() -> pathlib.Path | None:
    """
    Locate the daemon's ``wsapi.json``.

    The daemon repo is normally checked out beside this one
    (``…/GitHub/openccu-loom``). Resolve it relative to this file so the
    drift check runs on any machine; an explicit ``OPENCCU_LOOM_REPO``
    env var overrides the location (CI, non-sibling layouts).
    """
    candidates = []
    if env := os.environ.get("OPENCCU_LOOM_REPO"):
        candidates.append(pathlib.Path(env))
    # …/openccu-loom-client/tests/unit/this_file → parents[3] == GitHub
    candidates.append(pathlib.Path(__file__).resolve().parents[3] / "openccu-loom")
    for repo in candidates:
        wsapi = repo / "assets/wsapi.json"
        if wsapi.is_file():
            return wsapi
    return None


_WSAPI = _find_wsapi()


def _broadcasts_in_wsapi() -> set[str]:
    if _WSAPI is None:
        pytest.skip("openccu-loom repo not found beside this one (set OPENCCU_LOOM_REPO)")
    doc = json.loads(_WSAPI.read_text())
    return {c["name"] for c in doc["commands"] if c.get("kind") == "broadcast"}


class TestRegistryCoverage:
    def test_every_daemon_broadcast_has_a_python_binding(self) -> None:
        """
        Live drift check against the daemon's broadcast catalogue.

        Skipped when the daemon repo isn't checked out beside this one.
        """
        wire_broadcasts = _broadcasts_in_wsapi()
        client_bindings = known_event_types()
        # We bind a couple of payloads ahead of the broadcast being
        # advertised (DeviceCreated/Removed land in 0.1.2 but aren't
        # in wsapi.json yet); the test only checks the reverse.
        missing = wire_broadcasts - client_bindings
        assert missing == set(), (
            f"daemon advertises {len(wire_broadcasts)} broadcasts but "
            f"our event registry is missing bindings for: {sorted(missing)}"
        )


class TestDispatch:
    def _envelope(self, *, type_: str, payload: dict, seq: int = 1) -> WsEnvelope:
        return WsEnvelope.model_validate(
            {
                "topic": "does.not.matter",
                "type": type_,
                "ts": "2026-05-24T08:42:13Z",
                "seq": seq,
                "kind": "change",
                "payload": payload,
            }
        )

    def test_datapoint_value_changed(self) -> None:
        env = self._envelope(
            type_="datapoint.value_changed",
            payload={
                "central": "home",
                "device_address": "0001",
                "channel": 1,
                "parameter": "LEVEL",
                "paramset_key": "VALUES",
                "value": 0.5,
                "available": True,
                "modified_at": "2026-05-24T08:42:13Z",
                "unique_id": "loom_test_level",
            },
        )
        ev = event_from_envelope(envelope=env)
        assert isinstance(ev, DataPointValueChangedEvent)
        assert ev.device_address == "0001"
        assert ev.parameter == "LEVEL"
        assert ev.payload.value == 0.5
        assert ev.seq == 1

    def test_device_created_payload_lands_typed(self) -> None:
        env = self._envelope(
            type_="device.created",
            payload={
                "central": "home",
                "interface_id": "home:HmIP-RF",
                "device_address": "00010001",
                "model": "HmIP-eTRV-2",
            },
        )
        ev = event_from_envelope(envelope=env)
        assert isinstance(ev, DeviceCreatedEvent)
        assert ev.payload.device_address == "00010001"
        assert ev.payload.model == "HmIP-eTRV-2"

    def test_device_removed_payload_lands_typed(self) -> None:
        env = self._envelope(
            type_="device.removed",
            payload={
                "central": "home",
                "interface_id": "home:HmIP-RF",
                "device_address": "00010001",
            },
        )
        ev = event_from_envelope(envelope=env)
        assert isinstance(ev, DeviceRemovedEvent)

    def test_device_metadata_changed_payload_lands_typed(self) -> None:
        env = self._envelope(
            type_="device.metadata_changed",
            payload={
                "central": "home",
                "interface_id": "home:HmIP-RF",
                "device_address": "00010001",
            },
        )
        ev = event_from_envelope(envelope=env)
        assert isinstance(ev, DeviceMetadataChangedEvent)
        # Shares the lifecycle topic with device.created: routed by `type`.
        assert ev.payload.device_address == "00010001"
        assert ev.event_key == "home"

    def test_schedule_changed_payload_lands_typed(self) -> None:
        env = self._envelope(
            type_="schedules.changed",
            payload={
                "central": "home",
                "interface_id": "home:HmIP-RF",
                "device_address": "00010001",
                "channel": 1,
            },
        )
        ev = event_from_envelope(envelope=env)
        assert isinstance(ev, ScheduleChangedEvent)
        assert ev.payload.channel == 1
        assert ev.event_key == "home"

    def test_daemon_status_changed_payload_lands_typed(self) -> None:
        env = self._envelope(
            type_="daemon_status.changed",
            payload={"status": "offline", "reason": "shutdown", "event_at": "2026-05-24T08:42:13Z"},
        )
        ev = event_from_envelope(envelope=env)
        assert isinstance(ev, DaemonStatusChangedEvent)
        assert ev.payload.status.value == "offline"
        assert ev.payload.reason == "shutdown"
        # Daemon-level, not central-scoped: no routing key to filter on.
        assert ev.event_key is None

    def test_hub_count_pushes_land_typed_and_central_keyed(self) -> None:
        # alarm / service / inbox share HubCountChangedPayload but distinct types.
        for type_, cls in (
            ("hub.alarm_message", HubAlarmMessageCountChangedEvent),
            ("hub.service_message", HubServiceMessageCountChangedEvent),
            ("hub.inbox_changed", HubInboxChangedEvent),
        ):
            ev = event_from_envelope(envelope=self._envelope(type_=type_, payload={"central": "home", "count": 3}))
            assert isinstance(ev, cls)
            assert ev.payload.count == 3
            assert ev.event_key == "home"  # central-keyed for per-CCU routing

    def test_hub_metrics_push_lands_typed(self) -> None:
        ev = event_from_envelope(
            envelope=self._envelope(
                type_="hub.metrics_changed",
                payload={"central": "home", "metric": "system_health", "value": 95, "unit": "%"},
            )
        )
        assert isinstance(ev, HubMetricsChangedEvent)
        assert ev.payload.metric == "system_health"
        assert ev.payload.value == 95
        assert ev.event_key == "home"

    def test_connectivity_push_lands_typed(self) -> None:
        ev = event_from_envelope(
            envelope=self._envelope(
                type_="connectivity.changed",
                payload={"central": "home", "interface_id": "HmIP-RF", "reachable": True, "latency_ms": 12},
            )
        )
        assert isinstance(ev, HubConnectivityChangedEvent)
        assert ev.payload.interface_id == "HmIP-RF"
        assert ev.payload.reachable is True
        assert ev.event_key == "home"

    def test_addon_update_state_changed_lands_typed(self) -> None:
        ev = event_from_envelope(
            envelope=self._envelope(
                type_="addon_update.state_changed",
                payload={
                    "supported": True,
                    "current_version": "0.50.0",
                    "latest_version": "0.50.1",
                    "update_available": True,
                    "release_url": "https://github.com/SukramJ/openccu-loom/releases/tag/v0.50.1",
                    "state": "idle",
                },
            )
        )
        assert isinstance(ev, AddonUpdateStateChangedEvent)
        assert ev.payload.update_available is True
        assert ev.payload.state.value == "idle"
        # Daemon-global broadcast: no central tag, so no routing key.
        assert ev.event_key is None

    def test_sysvar_changed_lands_typed(self) -> None:
        env = self._envelope(
            type_="hub.sysvar_changed",
            payload={
                "central": "home",
                "name": "my_var",
                "value_type": "FLOAT",
                "value": 42.0,
                "previous": 41.0,
                "unique_id": "loom_test_my_var",
            },
        )
        ev = event_from_envelope(envelope=env)
        assert isinstance(ev, SysvarChangedEvent)
        # Unlinked push: the device-link fields default to "no link".
        assert ev.payload.channel is None
        assert ev.payload.device_address is None

    def test_sysvar_changed_carries_device_link(self) -> None:
        # A sysvar linked to a device channel (CCU channel assignment or
        # name match) pushes the link so live updates route to the device.
        env = self._envelope(
            type_="hub.sysvar_changed",
            payload={
                "central": "home",
                "name": "my_var",
                "value_type": "FLOAT",
                "value": 42.0,
                "unique_id": "loom_test_my_var",
                "channel": "VCU0001:1",
                "device_address": "VCU0001",
            },
        )
        ev = event_from_envelope(envelope=env)
        assert isinstance(ev, SysvarChangedEvent)
        assert ev.payload.channel == "VCU0001:1"
        assert ev.payload.device_address == "VCU0001"

    def test_program_executed_carries_device_link(self) -> None:
        env = self._envelope(
            type_="hub.program_executed",
            payload={
                "central": "home",
                "program_id": "p1",
                "trigger": "manual",
                "success": True,
                "channel": "VCU0001:1",
                "device_address": "VCU0001",
            },
        )
        ev = event_from_envelope(envelope=env)
        assert isinstance(ev, ProgramExecutedEvent)
        assert ev.payload.channel == "VCU0001:1"
        assert ev.payload.device_address == "VCU0001"

    @pytest.mark.parametrize(
        ("type_id", "payload", "expected_cls"),
        [
            (
                "central.state_changed",
                {"central": "home", "old_state": "INIT", "new_state": "RUNNING"},
                CentralStateChangedEvent,
            ),
            (
                "system.status_changed",
                {
                    "central": "home",
                    "component": "central",
                    "healthy": True,
                    "event_at": "2026-05-24T08:42:13Z",
                },
                SystemStatusChangedEvent,
            ),
            (
                "hub.program_executed",
                {
                    "central": "home",
                    "program_id": "prog42",
                    "trigger": "manual",
                    "success": True,
                },
                ProgramExecutedEvent,
            ),
            (
                "custom_data_point.state_changed",
                {
                    "central": "home",
                    "device_address": "0001",
                    "channel": 1,
                    "name": "main",
                    "state": {"on": True},
                    "unique_id": "loom_test_main",
                },
                CustomDataPointStateChangedEvent,
            ),
            (
                "device.trigger",
                {
                    "central": "home",
                    "interface_id": "home:HmIP-RF",
                    "device_address": "0001",
                    "channel": 1,
                    "event_type": "homematic.keypress",
                    "parameter": "PRESS_SHORT",
                    "unique_id": "loom_event_0001_1_press_short",
                },
                DeviceTriggerEvent,
            ),
            (
                "datapoint.optimistic_rolled_back",
                {
                    "central": "home",
                    "device_address": "0001",
                    "channel": 1,
                    "parameter": "LEVEL",
                    "paramset_key": "VALUES",
                    "reason": "timeout",
                    "sent": 0.8,
                    "present": 0.5,
                    "unique_id": "loom_test_level",
                },
                DataPointOptimisticRolledBackEvent,
            ),
        ],
    )
    def test_other_known_types(self, type_id: str, payload: dict, expected_cls: type) -> None:
        env = self._envelope(type_=type_id, payload=payload)
        ev = event_from_envelope(envelope=env)
        assert isinstance(ev, expected_cls)

    def test_device_trigger_keyed_per_data_point(self) -> None:
        env = self._envelope(
            type_="device.trigger",
            payload={
                "central": "home",
                "interface_id": "home:HmIP-RF",
                "device_address": "VCU1",
                "channel": 1,
                "event_type": "homematic.keypress",
                "parameter": "PRESS_SHORT",
                "unique_id": "loom_event_vcu1_1_press_short",
            },
        )
        ev = event_from_envelope(envelope=env)
        assert isinstance(ev, DeviceTriggerEvent)
        # The event keys off the daemon-supplied canonical unique_id.
        assert ev.event_key == "loom_event_vcu1_1_press_short"


class TestForwardCompatibility:
    def test_unknown_type_becomes_unknown_event(self) -> None:
        env = WsEnvelope.model_validate(
            {
                "topic": "future.thing",
                "type": "loom.0.3.new_event_type",
                "ts": "2026-05-24T08:42:13Z",
                "seq": 99,
                "kind": "change",
                "payload": {"any": "shape"},
            }
        )
        ev = event_from_envelope(envelope=env)
        assert isinstance(ev, UnknownLoomEvent)
        assert ev.raw_payload == {"any": "shape"}
        # Metadata is still typed.
        assert ev.seq == 99
        assert ev.topic == "future.thing"

    def test_malformed_known_payload_degrades_to_unknown(self) -> None:
        env = WsEnvelope.model_validate(
            {
                "topic": "device.0001.channels.1.data_points.LEVEL",
                "type": "datapoint.value_changed",
                "ts": "2026-05-24T08:42:13Z",
                "seq": 100,
                "kind": "change",
                # Missing every required field of DataPointValueChangedPayload.
                "payload": {"some": "garbage"},
            }
        )
        ev = event_from_envelope(envelope=env)
        assert isinstance(ev, UnknownLoomEvent)
        assert ev.raw_payload == {"some": "garbage"}
