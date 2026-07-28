# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Hub-side REST operations: programs, sysvars, messages, rooms/areas."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from openccu_loom_types.rest import (
    AlarmMessage,
    Area,
    AreaRoomRef,
    Function,
    InstallModeInterfaceEntry,
    InstallModeInterfaceRequest,
    ProgramSummary,
    Room,
    ServiceMessage,
    SysvarSummary,
)

from openccu_loom_client.operations._base import _OperationsBase


class HubOperations(_OperationsBase):
    """Programs, system variables, alarm/service messages, install mode."""

    # ---- programs ----

    async def list_programs(self) -> list[ProgramSummary]:
        """List all programs on the CCU. Wire: ``GET /programs``."""
        return await self._request_list(method="GET", path="/programs", model=ProgramSummary)

    async def execute_program(self, *, program_id: str) -> None:
        """Wire: ``POST /programs/{id}/execute``."""
        await self._transport.request(
            method="POST",
            path=f"/programs/{program_id}/execute",
            allow_retry=False,
        )

    async def set_program_enabled(self, *, program_id: str, active: bool) -> None:
        """
        Toggle a program's active flag.

        Wire: ``PATCH /programs/{id}`` with ``{active}``. Idempotent —
        setting the same flag twice is a no-op on the CCU.
        """
        await self._transport.request(
            method="PATCH",
            path=f"/programs/{program_id}",
            json_body={"active": active},
            allow_retry=True,
        )

    # ---- sysvars ----

    async def list_sysvars(self) -> list[SysvarSummary]:
        """List all system variables on the CCU. Wire: ``GET /sysvars``."""
        return await self._request_list(method="GET", path="/sysvars", model=SysvarSummary)

    async def create_sysvar(
        self,
        *,
        name: str,
        value: Any = None,
        value_type: str | None = None,
        description: str | None = None,
        unit: str | None = None,
        value_list: list[str] | None = None,
        min: Any = None,  # noqa: A002 — CCU/Rega field name  # pylint: disable=redefined-builtin
        max: Any = None,  # noqa: A002 — CCU/Rega field name  # pylint: disable=redefined-builtin
    ) -> None:
        """
        Create a system variable on the CCU.

        Wire: ``POST /sysvars`` (Rega ``create_system_variable``). Only
        the fields the caller supplies are sent; the daemon applies CCU
        defaults for the rest. Not retried — creation is not idempotent
        (a retry would surface as a duplicate-name error).
        """
        body: dict[str, Any] = {"name": name}
        if value is not None:
            body["value"] = value
        if value_type is not None:
            body["value_type"] = value_type
        if description is not None:
            body["description"] = description
        if unit is not None:
            body["unit"] = unit
        if value_list is not None:
            body["value_list"] = value_list
        if min is not None:
            body["min"] = min
        if max is not None:
            body["max"] = max
        await self._transport.request(
            method="POST",
            path="/sysvars",
            json_body=body,
            allow_retry=False,
        )

    async def update_sysvar_metadata(
        self,
        *,
        name: str,
        description: str | None = None,
        unit: str | None = None,
        min: str | None = None,  # noqa: A002 — CCU bounds are strings  # pylint: disable=redefined-builtin
        max: str | None = None,  # noqa: A002 — CCU bounds are strings  # pylint: disable=redefined-builtin
        value_list: list[str] | None = None,
    ) -> None:
        """
        Update a sysvar's metadata (description, unit, bounds, enum labels).

        Wire: ``PATCH /sysvars/{name}``. All fields optional — omitted
        fields leave the CCU metadata unchanged. Type changes are not
        supported (delete + recreate instead). Idempotent.
        """
        body: dict[str, Any] = {}
        if description is not None:
            body["description"] = description
        if unit is not None:
            body["unit"] = unit
        if min is not None:
            body["min"] = min
        if max is not None:
            body["max"] = max
        if value_list is not None:
            body["value_list"] = value_list
        await self._transport.request(
            method="PATCH",
            path=f"/sysvars/{quote(name, safe='')}",
            json_body=body,
            allow_retry=True,
        )

    async def get_sysvar(self, *, name: str) -> SysvarSummary:
        """Return one system variable by name. Wire: ``GET /sysvars/{name}``."""
        payload = await self._transport.request(method="GET", path=f"/sysvars/{quote(name, safe='')}")
        return SysvarSummary.model_validate(payload)

    async def set_sysvar(self, *, name: str, value: Any) -> None:
        """
        Write a new runtime value to a sysvar.

        Wire: ``PUT /sysvars/{name}`` with a :class:`SysvarSetRequest`.
        Type compatibility is the daemon's responsibility — wrong
        types come back as 422 ``LoomValidationError``.
        """
        await self._transport.request(
            method="PUT",
            path=f"/sysvars/{quote(name, safe='')}",
            json_body={"value": value},
            allow_retry=True,
        )

    async def delete_sysvar(self, *, name: str) -> None:
        """Delete a system variable by name. Wire: ``DELETE /sysvars/{name}``."""
        await self._transport.request(method="DELETE", path=f"/sysvars/{quote(name, safe='')}")

    async def fetch_system_variables(self, *, central_name: str | None = None) -> None:
        """
        Force a CCU re-pull of the system-variable catalogue into the hub model.

        Wire: ``POST /sysvars/fetch``. Pass ``central_name`` to scope the
        refresh to one CCU (``?central=``); omitted, every registered
        central is refreshed. Async on the daemon side — the call returns
        202. Not retried: a duplicate re-pull is wasted CCU radio work.
        """
        params = {"central": central_name} if central_name is not None else None
        await self._transport.request(
            method="POST",
            path="/sysvars/fetch",
            params=params,
            allow_retry=False,
        )

    # ---- messages ----

    async def list_alarm_messages(self) -> list[AlarmMessage]:
        """List pending alarm messages. Wire: ``GET /alarm-messages``."""
        return await self._request_list(method="GET", path="/alarm-messages", model=AlarmMessage)

    async def ack_alarm_message(self, *, message_id: str) -> None:
        """
        Acknowledge (clear) an alarm message.

        Wire: ``POST /alarm-messages/{id}/ack``. Idempotent — acking an
        already-cleared message is a no-op.
        """
        await self._transport.request(
            method="POST",
            path=f"/alarm-messages/{message_id}/ack",
            allow_retry=True,
        )

    async def list_service_messages(self) -> list[ServiceMessage]:
        """List pending service messages. Wire: ``GET /service-messages``."""
        return await self._request_list(method="GET", path="/service-messages", model=ServiceMessage)

    async def ack_service_message(self, *, message_id: str) -> None:
        """
        Acknowledge a service message.

        Wire: ``POST /service-messages/{id}/ack``. Idempotent.
        """
        await self._transport.request(
            method="POST",
            path=f"/service-messages/{message_id}/ack",
            allow_retry=True,
        )

    # ---- rooms / functions ----

    async def list_rooms(self) -> list[Room]:
        """
        Return the aggregated room index with device counts.

        Wire: ``GET /rooms``.
        """
        return await self._request_list(method="GET", path="/rooms", model=Room)

    async def list_functions(self) -> list[Function]:
        """
        Return the aggregated function-group index with device counts.

        Wire: ``GET /functions``.
        """
        return await self._request_list(method="GET", path="/functions", model=Function)

    # ---- areas (room groupings; daemon ≥ 0.49.3, api 3.2.0) ----

    async def list_areas(self) -> list[Area]:
        """
        Return the operator-defined areas with their assigned rooms.

        An area groups CCU rooms one level up — a floor, a shed, a
        terrace roof. It lives in the daemon's database only (the CCU
        knows nothing of areas) and is unrelated to an *alarm zone*.
        Rooms are ``(central, room)`` pairs, one area per room.

        Wire: ``GET /areas`` — ordered by ``position``, ties by name.
        """
        return await self._request_list(method="GET", path="/areas", model=Area)

    async def create_area(self, *, area: Area) -> Area:
        """
        Create an area (operator scope).

        Wire: ``POST /areas``. The daemon generates the id — whatever
        ``area.id`` carries is ignored — and returns the created area.
        Not retried: creation is not idempotent.
        """
        payload = await self._transport.request(
            method="POST",
            path="/areas",
            json_body=self._to_json_body(area),
            allow_retry=False,
        )
        return Area.model_validate(payload)

    async def update_area(self, *, area_id: str, area: Area) -> None:
        """
        Rename or reorder an area (operator scope).

        Wire: ``PUT /areas/{id}``. Idempotent — the body carries the
        full desired state. Room assignments are not touched here; use
        :meth:`replace_area_rooms`.
        """
        await self._transport.request(
            method="PUT",
            path=f"/areas/{quote(area_id, safe='')}",
            json_body=self._to_json_body(area),
            allow_retry=True,
        )

    async def delete_area(self, *, area_id: str) -> None:
        """
        Delete an area and clear its room assignments (operator scope).

        Wire: ``DELETE /areas/{id}``. The rooms themselves survive — they
        simply end up unassigned.
        """
        await self._transport.request(method="DELETE", path=f"/areas/{quote(area_id, safe='')}")

    async def replace_area_rooms(self, *, area_id: str, rooms: list[AreaRoomRef]) -> None:
        """
        Replace one area's room set wholesale (operator scope).

        Wire: ``PUT /areas/{id}/rooms``. Idempotent full-set replace:
        rooms omitted from the body are unassigned, and a room currently
        held by another area moves here (one area per room).
        """
        await self._transport.request(
            method="PUT",
            path=f"/areas/{quote(area_id, safe='')}/rooms",
            json_body=[self._to_json_body(room) for room in rooms],
            allow_retry=True,
        )

    async def list_inbox(self) -> list[dict[str, Any]]:
        """
        List pending pairing candidates not yet accepted.

        Wire: ``GET /inbox`` — one entry per inbox device, across all
        centrals (each entry carries its ``central``). Promote a
        candidate with :meth:`DevicesOperations.accept_device`.
        """
        payload = await self._transport.request(method="GET", path="/inbox")
        return [dict(e) for e in (payload or [])]

    # ---- install mode ----

    async def list_install_mode_interfaces(self) -> list[InstallModeInterfaceEntry]:
        """
        Return the per-interface install-mode state.

        Wire: ``GET /install-mode/interfaces`` — one entry per CCU
        interface, across all centrals.
        """
        return await self._request_list(method="GET", path="/install-mode/interfaces", model=InstallModeInterfaceEntry)

    async def set_install_mode_interface(self, *, interface: str, active: bool, seconds: int = 60) -> None:
        """
        Toggle install mode for one interface.

        Wire: ``POST /install-mode/interfaces`` with an
        :class:`InstallModeInterfaceRequest`. Not retried — a retry
        could re-arm pairing after the user cancelled it.
        """
        body = InstallModeInterfaceRequest(interface=interface, active=active, seconds=seconds)
        await self._transport.request(
            method="POST",
            path="/install-mode/interfaces",
            json_body=self._to_json_body(body),
            allow_retry=False,
        )
