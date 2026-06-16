# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""User-administration REST operations (``/users``, admin-only)."""

from __future__ import annotations

from openccu_loom_types.rest import UserCreate, UserSummary, UserUpdate

from openccu_loom_client.operations._base import _OperationsBase


class UsersOperations(_OperationsBase):
    """CRUD over the daemon's user store."""

    async def list_users(self) -> list[UserSummary]:
        """Wire: ``GET /users``."""
        payload = await self._transport.request(method="GET", path="/users")
        return [UserSummary.model_validate(u) for u in (payload or [])]

    async def create_user(self, *, user: UserCreate) -> UserSummary:
        """Create a new user. Wire: ``POST /users``."""
        payload = await self._transport.request(
            method="POST",
            path="/users",
            json_body=user.model_dump(mode="json", exclude_none=True),
            allow_retry=False,
        )
        return UserSummary.model_validate(payload)

    async def update_user(self, *, subject: str, update: UserUpdate) -> None:
        """Update a user's password or role. Wire: ``PATCH /users/{subject}``."""
        await self._transport.request(
            method="PATCH",
            path=f"/users/{subject}",
            json_body=update.model_dump(mode="json", exclude_none=True),
            allow_retry=False,
        )

    async def delete_user(self, *, subject: str) -> None:
        """Delete a user. Wire: ``DELETE /users/{subject}``."""
        await self._transport.request(method="DELETE", path=f"/users/{subject}")
