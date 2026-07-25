# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Heating-group administration REST operations.

Covers ``/groups`` (list / create / update / delete), ``/groups/types`` and
``/groups/suitable-members``. Group membership is applied by the CCU itself
through the daemon's HMServer proxy; this module only wraps the daemon's REST
surface. The optional ``central`` argument selects the target CCU — omit it
when only one CCU is configured.
"""

from __future__ import annotations

from openccu_loom_types.rest import (
    CreateGroupRequest,
    GroupCentralEntry,
    GroupEntry,
    GroupTypeEntry,
    SuitableMembersResponse,
    UpdateGroupRequest,
)

from openccu_loom_client.operations._base import _OperationsBase


class GroupsOperations(_OperationsBase):
    """Read and administer heating groups (admin-gated on the daemon)."""

    async def list_groups(self, *, central: str | None = None) -> list[GroupCentralEntry]:
        """List heating groups, one entry per central. Wire: ``GET /groups``."""
        payload = await self._transport.request(method="GET", path="/groups", params=_central_params(central=central))
        entries = payload.get("entries") if isinstance(payload, dict) else None
        return [GroupCentralEntry.model_validate(entry) for entry in entries or []]

    async def list_types(self, *, central: str | None = None) -> list[GroupTypeEntry]:
        """List the group types a new group can be created as. Wire: ``GET /groups/types``."""
        payload = await self._transport.request(
            method="GET", path="/groups/types", params=_central_params(central=central)
        )
        types = payload.get("types") if isinstance(payload, dict) else None
        return [GroupTypeEntry.model_validate(group_type) for group_type in types or []]

    async def suitable_members(self, *, type_id: str, central: str | None = None) -> SuitableMembersResponse:
        """
        List the devices assignable to a group of the given type.

        Wire: ``GET /groups/suitable-members``. The daemon enriches each
        candidate with device/channel name, model, room, function and the
        ``config_pending`` flag; ``assignable`` can be added now, ``leftover``
        cannot (wrong type / already grouped).
        """
        params: dict[str, str] = {"type_id": type_id}
        if central is not None:
            params["central"] = central
        payload = await self._transport.request(method="GET", path="/groups/suitable-members", params=params)
        return SuitableMembersResponse.model_validate(payload)

    async def create_group(self, *, request: CreateGroupRequest, central: str | None = None) -> GroupEntry:
        """Create a heating group. Wire: ``POST /groups`` (201). Not retried — it has side effects."""
        payload = await self._transport.request(
            method="POST",
            path="/groups",
            params=_central_params(central=central),
            json_body=self._to_json_body(request),
            allow_retry=False,
        )
        return GroupEntry.model_validate(payload)

    async def update_group(self, *, group_id: int, request: UpdateGroupRequest, central: str | None = None) -> None:
        """
        Edit a heating group's name, members and operate-only flag.

        Wire: ``PUT /groups/{id}`` (204). Idempotent (the body is the desired
        end state), so it is retried on transient failures.
        """
        await self._transport.request(
            method="PUT",
            path=f"/groups/{group_id}",
            params=_central_params(central=central),
            json_body=self._to_json_body(request),
            allow_retry=True,
        )

    async def delete_group(self, *, group_id: int, central: str | None = None) -> None:
        """Delete a heating group. Wire: ``DELETE /groups/{id}`` (204)."""
        await self._transport.request(
            method="DELETE", path=f"/groups/{group_id}", params=_central_params(central=central)
        )


def _central_params(*, central: str | None) -> dict[str, str] | None:
    """Build the optional ``?central=`` query params, omitted when the single CCU is implied."""
    return {"central": central} if central is not None else None
