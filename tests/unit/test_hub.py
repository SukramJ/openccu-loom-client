# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Program + Sysvar domain models + store integration + bridge."""

from __future__ import annotations

from typing import Any

import pytest

from openccu_loom_client.store import LoomStore
from openccu_loom_client.wire.rest import DeviceDetail, DeviceSummary, ProgramSummary, Snapshot, SysvarSummary
from openccu_loom_client.wire.ws import ProgramExecutedPayload, SysvarChangedPayload


def _program_summary(*, program_id: str = "p1", **extra: Any) -> ProgramSummary:
    return ProgramSummary.model_validate(
        {
            "id": program_id,
            "name": "All off",
            "description": "",
            "active": True,
            "unique_id": f"loom_test_{program_id}",
            **extra,
        }
    )


def _sysvar_summary(*, name: str = "temp", value: Any = 21.5, **extra: Any) -> SysvarSummary:
    return SysvarSummary.model_validate(
        {
            "name": name,
            "description": "",
            "unit": "°C",
            "value_type": "FLOAT",
            "value": value,
            "observed": True,
            "unique_id": f"loom_test_{name.lower()}",
            **extra,
        }
    )


def _snapshot(
    *,
    programs: list[ProgramSummary] | None = None,
    sysvars: list[SysvarSummary] | None = None,
) -> Snapshot:
    return Snapshot.model_validate(
        {
            "generated_at": "2026-05-24T08:00:00Z",
            "devices": [],
            "programs": [p.model_dump() for p in (programs or [])],
            "sysvars": [s.model_dump() for s in (sysvars or [])],
        }
    )


def _attach_device_with_channel(*, store: LoomStore, address: str = "VCU0001", number: int = 1) -> None:
    """Load one device with one channel into the store's channel graph."""
    summary = DeviceSummary.model_validate(
        {
            "address": address,
            "interface": "home:HmIP-RF",
            "interface_id": "home:HmIP-RF",
            "model": "HmIP-PSM",
            "name": "Lamp",
            "available": True,
            "channels_count": 1,
            "updatable": False,
            "update_available": False,
            "master_pushes_config_pending": False,
            "has_sub_devices": False,
            "firmware": {"Current": "1.0.0", "Available": "", "Updatable": False, "UpdateState": "UP_TO_DATE"},
            "availability": {
                "IsReachable": True,
                "LastUpdated": None,
                "BatteryLevel": None,
                "LowBattery": None,
                "SignalStrength": None,
            },
        }
    )
    store.load_snapshot(
        snapshot=Snapshot.model_validate({"generated_at": "2026-05-24T08:00:00Z", "devices": [summary.model_dump()]})
    )
    store.attach_device_detail(
        detail=DeviceDetail.model_validate(
            {
                **summary.model_dump(),
                "firmware": {},
                "availability": {},
                "channels": [
                    {
                        "address": f"{address}:{number}",
                        "number": number,
                        "paramset_key": "VALUES",
                        "data_points_count": 0,
                    }
                ],
            }
        )
    )


class _FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []

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
        return None


class TestSnapshotLoad:
    def test_programs_and_sysvars_land_from_snapshot(self) -> None:
        store = LoomStore()
        store.load_snapshot(
            snapshot=_snapshot(
                programs=[_program_summary(program_id="p1"), _program_summary(program_id="p2")],
                sysvars=[_sysvar_summary(name="a"), _sysvar_summary(name="b")],
            )
        )
        assert {p.id for p in store.programs} == {"p1", "p2"}
        assert {s.name for s in store.sysvars} == {"a", "b"}

    def test_reload_updates_in_place(self) -> None:
        store = LoomStore()
        store.load_snapshot(snapshot=_snapshot(programs=[_program_summary()]))
        original = store.get_program(program_id="p1")
        assert original is not None
        store.load_snapshot(
            snapshot=_snapshot(
                programs=[
                    ProgramSummary.model_validate(
                        {"id": "p1", "name": "Renamed", "active": False, "unique_id": "loom_test_p1"}
                    )
                ]
            )
        )
        same = store.get_program(program_id="p1")
        assert same is original
        assert same.name == "Renamed"
        assert same.active is False


class TestSysvarValueUpdate:
    def test_apply_sysvar_changed_updates_value(self) -> None:
        store = LoomStore()
        store.load_snapshot(snapshot=_snapshot(sysvars=[_sysvar_summary(name="temp", value=21.5)]))
        sysvar = store.get_sysvar(name="temp")
        assert sysvar is not None
        assert sysvar.value == 21.5

        store.apply_sysvar_changed(
            payload=SysvarChangedPayload.model_validate(
                {
                    "central": "home",
                    "name": "temp",
                    "value_type": "FLOAT",
                    "value": 22.0,
                    "previous": 21.5,
                    "unique_id": "loom_test_temp",
                }
            )
        )
        assert sysvar.value == 22.0

    def test_apply_sysvar_changed_for_unknown_sysvar_is_noop(self) -> None:
        store = LoomStore()
        store.apply_sysvar_changed(
            payload=SysvarChangedPayload.model_validate(
                {
                    "central": "home",
                    "name": "GHOST",
                    "value_type": "FLOAT",
                    "value": 1.0,
                    "unique_id": "loom_test_ghost",
                }
            )
        )
        assert store.get_sysvar(name="GHOST") is None


class TestSysvarSetValue:
    async def test_set_value_round_trips(self) -> None:
        transport = _FakeTransport()
        store = LoomStore(transport=transport)  # type: ignore[arg-type]
        store.load_snapshot(snapshot=_snapshot(sysvars=[_sysvar_summary(name="temp")]))
        sysvar = store.get_sysvar(name="temp")
        assert sysvar is not None
        await sysvar.set_value(value=22.0)
        assert transport.calls[0] == ("PUT", "/sysvars/temp", {"value": 22.0})

    async def test_set_value_without_transport_raises(self) -> None:
        store = LoomStore()
        store.load_snapshot(snapshot=_snapshot(sysvars=[_sysvar_summary()]))
        sysvar = store.get_sysvar(name="temp")
        assert sysvar is not None
        store._transport = None  # type: ignore[attr-defined]
        with pytest.raises(RuntimeError):
            await sysvar.set_value(value=22.0)

    async def test_set_value_percent_encodes_sysvar_name(self) -> None:
        # A free-form sysvar name with reserved characters must be encoded
        # into the path segment, not injected as path/query structure.
        transport = _FakeTransport()
        store = LoomStore(transport=transport)  # type: ignore[arg-type]
        store.load_snapshot(snapshot=_snapshot(sysvars=[_sysvar_summary(name="Küche/Licht?on")]))
        sysvar = store.get_sysvar(name="Küche/Licht?on")
        assert sysvar is not None
        await sysvar.set_value(value=1)
        method, path, _body = transport.calls[0]
        assert method == "PUT"
        assert path == "/sysvars/K%C3%BCche%2FLicht%3Fon"


class TestProgramExecute:
    async def test_execute_round_trips(self) -> None:
        transport = _FakeTransport()
        store = LoomStore(transport=transport)  # type: ignore[arg-type]
        store.load_snapshot(snapshot=_snapshot(programs=[_program_summary(program_id="p1")]))
        program = store.get_program(program_id="p1")
        assert program is not None
        await program.execute()
        assert transport.calls[0] == ("POST", "/programs/p1/execute", None)


class TestProgramExecutedEvent:
    def test_apply_program_executed_is_diagnostic_only(self) -> None:
        """
        The catalogue doesn't change on execution.

        The event is informational; subscribers may use it independently.
        """
        store = LoomStore()
        store.apply_program_executed(
            payload=ProgramExecutedPayload.model_validate(
                {
                    "central": "home",
                    "program_id": "p1",
                    "trigger": "manual",
                    "success": True,
                }
            )
        )
        # No assertion on state — just that it doesn't crash.


class TestSysvarDeviceLink:
    """Sysvar→device attachment: channel_address / device_address / channel."""

    def test_linked_sysvar_surfaces_channel_and_device_address(self) -> None:
        store = LoomStore()
        store.load_snapshot(
            snapshot=_snapshot(sysvars=[_sysvar_summary(channel="VCU0001:1", device_address="VCU0001")])
        )
        sysvar = store.get_sysvar(name="temp")
        assert sysvar is not None
        assert sysvar.channel_address == "VCU0001:1"
        assert sysvar.device_address == "VCU0001"

    def test_unlinked_sysvar_normalizes_to_none(self) -> None:
        store = LoomStore()
        store.load_snapshot(snapshot=_snapshot(sysvars=[_sysvar_summary()]))
        sysvar = store.get_sysvar(name="temp")
        assert sysvar is not None
        assert sysvar.channel_address is None
        assert sysvar.device_address is None
        assert sysvar.channel is None

    def test_empty_link_normalizes_to_none(self) -> None:
        # A daemon serialising the "no link" case as empty strings must
        # read identically to the absent-field case.
        store = LoomStore()
        store.load_snapshot(snapshot=_snapshot(sysvars=[_sysvar_summary(channel="", device_address="")]))
        sysvar = store.get_sysvar(name="temp")
        assert sysvar is not None
        assert sysvar.channel_address is None
        assert sysvar.device_address is None
        assert sysvar.channel is None

    def test_channel_resolves_linked_channel_from_graph(self) -> None:
        store = LoomStore()
        store.load_snapshot(
            snapshot=_snapshot(sysvars=[_sysvar_summary(channel="VCU0001:1", device_address="VCU0001")])
        )
        _attach_device_with_channel(store=store, address="VCU0001", number=1)
        sysvar = store.get_sysvar(name="temp")
        assert sysvar is not None
        channel = sysvar.channel
        assert channel is not None
        assert channel.address == "VCU0001:1"
        # The device hop HA consumers walk for device_info routing.
        assert channel.device is not None
        assert channel.device.address == "VCU0001"

    def test_channel_none_when_linked_channel_not_loaded(self) -> None:
        store = LoomStore()
        store.load_snapshot(snapshot=_snapshot(sysvars=[_sysvar_summary(channel="GHOST:7", device_address="GHOST")]))
        sysvar = store.get_sysvar(name="temp")
        assert sysvar is not None
        assert sysvar.channel_address == "GHOST:7"
        assert sysvar.channel is None

    def test_apply_sysvar_changed_updates_link(self) -> None:
        store = LoomStore()
        store.load_snapshot(snapshot=_snapshot(sysvars=[_sysvar_summary()]))
        store.apply_sysvar_changed(
            payload=SysvarChangedPayload.model_validate(
                {
                    "central": "home",
                    "name": "temp",
                    "value_type": "FLOAT",
                    "value": 22.0,
                    "unique_id": "loom_test_temp",
                    "channel": "VCU0001:1",
                    "device_address": "VCU0001",
                }
            )
        )
        sysvar = store.get_sysvar(name="temp")
        assert sysvar is not None
        assert sysvar.channel_address == "VCU0001:1"
        assert sysvar.device_address == "VCU0001"

    def test_apply_sysvar_changed_without_link_clears_it(self) -> None:
        # The push always carries the daemon's current link (absent =
        # unlinked), so a removed CCU channel assignment propagates live.
        store = LoomStore()
        store.load_snapshot(
            snapshot=_snapshot(sysvars=[_sysvar_summary(channel="VCU0001:1", device_address="VCU0001")])
        )
        store.apply_sysvar_changed(
            payload=SysvarChangedPayload.model_validate(
                {
                    "central": "home",
                    "name": "temp",
                    "value_type": "FLOAT",
                    "value": 22.0,
                    "unique_id": "loom_test_temp",
                }
            )
        )
        sysvar = store.get_sysvar(name="temp")
        assert sysvar is not None
        assert sysvar.channel_address is None
        assert sysvar.device_address is None


class TestProgramDeviceLink:
    """Program→device attachment: channel_address / device_address / channel."""

    def test_linked_program_surfaces_channel_and_device_address(self) -> None:
        store = LoomStore()
        store.load_snapshot(
            snapshot=_snapshot(programs=[_program_summary(channel="VCU0001:1", device_address="VCU0001")])
        )
        _attach_device_with_channel(store=store, address="VCU0001", number=1)
        program = store.get_program(program_id="p1")
        assert program is not None
        assert program.channel_address == "VCU0001:1"
        assert program.device_address == "VCU0001"
        channel = program.channel
        assert channel is not None
        assert channel.address == "VCU0001:1"

    def test_unlinked_program_normalizes_to_none(self) -> None:
        store = LoomStore()
        store.load_snapshot(snapshot=_snapshot(programs=[_program_summary()]))
        program = store.get_program(program_id="p1")
        assert program is not None
        assert program.channel_address is None
        assert program.device_address is None
        assert program.channel is None


class TestProgramExecuteAvailable:
    """api 3.12.0: the daemon answers whether a run would do anything."""

    def _program(self, *, store: LoomStore, **extra: Any):
        store.load_snapshot(snapshot=_snapshot(programs=[_program_summary(**extra)]))
        program = store.get_program(program_id="p1")
        assert program is not None
        return program

    def test_deactivated_program_reports_execute_unavailable(self) -> None:
        # The CCU refuses a manual run while the activity flag is off.
        program = self._program(store=LoomStore(), active=False, execute_available=False)
        assert program.execute_available is False

    def test_active_program_reports_execute_available(self) -> None:
        program = self._program(store=LoomStore(), execute_available=True)
        assert program.execute_available is True

    def test_missing_field_fails_open(self) -> None:
        # An older daemon omits the field, and the CCU may not have
        # reported the flag yet. Neither may grey out the control — a
        # falsy-vs-None mix-up here would silently disable every button.
        program = self._program(store=LoomStore())
        assert program.summary.execute_available is None
        assert program.execute_available is True

    def test_refresh_carries_the_flag_through(self) -> None:
        # The flag rides the periodic program refresh, not a push: it
        # changes whenever the activity flag does, and the wrapper is
        # mutated in place rather than rebuilt.
        store = LoomStore()
        program = self._program(store=store, execute_available=True)
        store._upsert_program(summary=_program_summary(active=False, execute_available=False))
        assert store.get_program(program_id="p1") is program
        assert program.execute_available is False

    def test_apply_program_executed_folds_present_link(self) -> None:
        store = LoomStore()
        store.load_snapshot(snapshot=_snapshot(programs=[_program_summary()]))
        store.apply_program_executed(
            payload=ProgramExecutedPayload.model_validate(
                {
                    "central": "home",
                    "program_id": "p1",
                    "trigger": "manual",
                    "success": True,
                    "channel": "VCU0001:1",
                    "device_address": "VCU0001",
                }
            )
        )
        program = store.get_program(program_id="p1")
        assert program is not None
        assert program.channel_address == "VCU0001:1"
        assert program.device_address == "VCU0001"

    def test_apply_program_executed_absent_link_keeps_existing(self) -> None:
        # Absence is ambiguous on this push (unlinked vs. hub model not
        # yet loaded daemon-side) — it must never clear a known link.
        store = LoomStore()
        store.load_snapshot(
            snapshot=_snapshot(programs=[_program_summary(channel="VCU0001:1", device_address="VCU0001")])
        )
        store.apply_program_executed(
            payload=ProgramExecutedPayload.model_validate(
                {
                    "central": "home",
                    "program_id": "p1",
                    "trigger": "manual",
                    "success": True,
                }
            )
        )
        program = store.get_program(program_id="p1")
        assert program is not None
        assert program.channel_address == "VCU0001:1"
        assert program.device_address == "VCU0001"
