# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""WsEnvelope → typed-LoomEvent dispatcher coverage.

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
import pathlib

import pytest
from openccu_loom_types.ws import WsEnvelope

from openccu_loom_client.events import (
    CentralStateChangedEvent,
    CustomDataPointStateChangedEvent,
    DataPointValueChangedEvent,
    DeviceCreatedEvent,
    DeviceRemovedEvent,
    ProgramExecutedEvent,
    SystemStatusChangedEvent,
    SysvarChangedEvent,
    UnknownLoomEvent,
    event_from_envelope,
)
from openccu_loom_client.events.types import known_event_types

_LOOM_REPO = pathlib.Path("/Users/markus/Documents/GitHub/openccu-loom")
_WSAPI = _LOOM_REPO / "assets/wsapi.json"


def _broadcasts_in_wsapi() -> set[str]:
    if not _WSAPI.is_file():
        pytest.skip(f"wsapi.json not at {_WSAPI}")
    doc = json.loads(_WSAPI.read_text())
    return {c["name"] for c in doc["commands"] if c.get("kind") == "broadcast"}


class TestRegistryCoverage:
    def test_every_daemon_broadcast_has_a_python_binding(self) -> None:
        """Live drift check against the daemon's broadcast catalogue.

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
                "topic": "doesnt.matter",
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
                "modified_at": "2026-05-24T08:42:13Z",
            },
        )
        ev = event_from_envelope(env)
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
        ev = event_from_envelope(env)
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
        ev = event_from_envelope(env)
        assert isinstance(ev, DeviceRemovedEvent)

    def test_sysvar_changed_lands_typed(self) -> None:
        env = self._envelope(
            type_="hub.sysvar_changed",
            payload={
                "central": "home",
                "name": "my_var",
                "value_type": "FLOAT",
                "value": 42.0,
                "previous": 41.0,
            },
        )
        ev = event_from_envelope(env)
        assert isinstance(ev, SysvarChangedEvent)

    @pytest.mark.parametrize(
        "type_id, payload, expected_cls",
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
                },
                CustomDataPointStateChangedEvent,
            ),
        ],
    )
    def test_other_known_types(
        self, type_id: str, payload: dict, expected_cls: type
    ) -> None:
        env = self._envelope(type_=type_id, payload=payload)
        ev = event_from_envelope(env)
        assert isinstance(ev, expected_cls)


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
        ev = event_from_envelope(env)
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
        ev = event_from_envelope(env)
        assert isinstance(ev, UnknownLoomEvent)
        assert ev.raw_payload == {"some": "garbage"}
