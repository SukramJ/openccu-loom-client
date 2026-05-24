# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Hub-side REST operations: programs, sysvars, messages."""

from __future__ import annotations

from typing import Any

from openccu_loom_types.rest import (
    AlarmMessage,
    Function,
    InstallModeState,
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
        payload = await self._transport.request("GET", "/programs")
        return [ProgramSummary.model_validate(p) for p in (payload or [])]

    async def execute_program(self, *, program_id: str) -> None:
        """Wire: ``POST /programs/{id}/execute``."""
        await self._transport.request(
            "POST",
            f"/programs/{program_id}/execute",
            allow_retry=False,
        )

    async def set_program_enabled(self, *, program_id: str, active: bool) -> None:
        """Toggle a program's active flag.

        Wire: ``PATCH /programs/{id}`` with ``{active}``. Idempotent —
        setting the same flag twice is a no-op on the CCU.
        """
        await self._transport.request(
            "PATCH",
            f"/programs/{program_id}",
            json_body={"active": active},
            allow_retry=True,
        )

    # ---- sysvars ----

    async def list_sysvars(self) -> list[SysvarSummary]:
        payload = await self._transport.request("GET", "/sysvars")
        return [SysvarSummary.model_validate(s) for s in (payload or [])]

    async def create_sysvar(
        self,
        *,
        name: str,
        value: Any = None,
        value_type: str | None = None,
        description: str | None = None,
        unit: str | None = None,
        value_list: list[str] | None = None,
        min: Any = None,  # CCU/Rega field name
        max: Any = None,  # CCU/Rega field name
    ) -> None:
        """Create a system variable on the CCU.

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
            "POST",
            "/sysvars",
            json_body=body,
            allow_retry=False,
        )

    async def update_sysvar_metadata(
        self,
        *,
        name: str,
        description: str | None = None,
        unit: str | None = None,
        min: str | None = None,  # CCU convention: bounds are strings
        max: str | None = None,  # CCU convention: bounds are strings
        value_list: list[str] | None = None,
    ) -> None:
        """Update a sysvar's metadata (description, unit, bounds, enum labels).

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
            "PATCH",
            f"/sysvars/{name}",
            json_body=body,
            allow_retry=True,
        )

    async def get_sysvar(self, *, name: str) -> SysvarSummary:
        payload = await self._transport.request("GET", f"/sysvars/{name}")
        return SysvarSummary.model_validate(payload)

    async def set_sysvar(self, *, name: str, value: Any) -> None:
        """Write a new runtime value to a sysvar.

        Wire: ``PUT /sysvars/{name}`` with a :class:`SysvarSetRequest`.
        Type compatibility is the daemon's responsibility — wrong
        types come back as 422 ``LoomValidationError``.
        """
        await self._transport.request(
            "PUT",
            f"/sysvars/{name}",
            json_body={"value": value},
            allow_retry=True,
        )

    async def delete_sysvar(self, *, name: str) -> None:
        await self._transport.request("DELETE", f"/sysvars/{name}")

    # ---- messages ----

    async def list_alarm_messages(self) -> list[AlarmMessage]:
        payload = await self._transport.request("GET", "/alarm-messages")
        return [AlarmMessage.model_validate(m) for m in (payload or [])]

    async def ack_alarm_message(self, *, message_id: str) -> None:
        """Acknowledge (clear) an alarm message.

        Wire: ``POST /alarm-messages/{id}/ack``. Idempotent — acking an
        already-cleared message is a no-op.
        """
        await self._transport.request(
            "POST",
            f"/alarm-messages/{message_id}/ack",
            allow_retry=True,
        )

    async def list_service_messages(self) -> list[ServiceMessage]:
        payload = await self._transport.request("GET", "/service-messages")
        return [ServiceMessage.model_validate(m) for m in (payload or [])]

    async def ack_service_message(self, *, message_id: str) -> None:
        """Acknowledge a service message.

        Wire: ``POST /service-messages/{id}/ack``. Idempotent.
        """
        await self._transport.request(
            "POST",
            f"/service-messages/{message_id}/ack",
            allow_retry=True,
        )

    # ---- rooms / functions ----

    async def list_rooms(self) -> list[Room]:
        """Aggregated room index with device counts.

        Wire: ``GET /rooms``.
        """
        payload = await self._transport.request("GET", "/rooms")
        return [Room.model_validate(r) for r in (payload or [])]

    async def list_functions(self) -> list[Function]:
        """Aggregated function-group index with device counts.

        Wire: ``GET /functions``.
        """
        payload = await self._transport.request("GET", "/functions")
        return [Function.model_validate(f) for f in (payload or [])]

    async def list_inbox(self) -> dict[str, Any]:
        """Pending pairing candidates not yet accepted.

        Wire: ``GET /inbox``. Promote a candidate with
        :meth:`DevicesOperations.accept_device`.
        """
        payload = await self._transport.request("GET", "/inbox")
        return dict(payload or {})

    # ---- install mode ----

    async def get_install_mode(self) -> InstallModeState:
        payload = await self._transport.request("GET", "/install-mode")
        return InstallModeState.model_validate(payload)

    async def set_install_mode(self, *, active: bool, seconds: int = 60) -> None:
        await self._transport.request(
            "POST",
            "/install-mode",
            json_body={"active": active, "seconds": seconds},
            allow_retry=False,
        )
