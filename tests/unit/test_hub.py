# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Program + Sysvar domain models + store integration + bridge."""

from __future__ import annotations

from typing import Any

from openccu_loom_types.rest import ProgramSummary, Snapshot, SysvarSummary
from openccu_loom_types.ws import ProgramExecutedPayload, SysvarChangedPayload
import pytest

from openccu_loom_client.store import LoomStore


def _program_summary(*, program_id: str = "p1") -> ProgramSummary:
    return ProgramSummary.model_validate(
        {
            "id": program_id,
            "name": "All off",
            "description": "",
            "active": True,
            "unique_id": f"loom_test_{program_id}",
        }
    )


def _sysvar_summary(*, name: str = "temp", value: Any = 21.5) -> SysvarSummary:
    return SysvarSummary.model_validate(
        {
            "name": name,
            "description": "",
            "unit": "°C",
            "value_type": "FLOAT",
            "value": value,
            "observed": True,
            "unique_id": f"loom_test_{name.lower()}",
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
