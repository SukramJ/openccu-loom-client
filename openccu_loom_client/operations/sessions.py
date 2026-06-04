# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Edit-lock session REST operations (``/sessions/edit``).

Cooperative 5-minute edit locks keyed on a resource string, so two
operators don't clobber each other's config/paramset edits. Bodies are
free-form in the daemon's OpenAPI; acquire/heartbeat return the
lock state dict.
"""

from __future__ import annotations

from typing import Any

from openccu_loom_client.operations._base import _OperationsBase


class SessionsOperations(_OperationsBase):
    """Acquire / release / refresh / take over cooperative edit locks."""

    async def acquire(self, *, key: str, subject: str | None = None) -> dict[str, Any]:
        """
        Acquire a 5-minute edit lock on a resource key.

        Wire: ``POST /sessions/edit``.
        """
        body: dict[str, Any] = {"key": key}
        if subject is not None:
            body["subject"] = subject
        payload = await self._transport.request(
            "POST", "/sessions/edit", json_body=body, allow_retry=False
        )
        return dict(payload or {})

    async def release(self) -> None:
        """Release the current edit lock. Wire: ``DELETE /sessions/edit``."""
        await self._transport.request("DELETE", "/sessions/edit")

    async def heartbeat(self) -> dict[str, Any]:
        """Refresh the edit lock's TTL. Wire: ``POST /sessions/edit/heartbeat``."""
        payload = await self._transport.request(
            "POST", "/sessions/edit/heartbeat", allow_retry=False
        )
        return dict(payload or {})

    async def take_over(self, *, key: str) -> None:
        """
        Force-close another user's edit lock.

        Wire: ``POST /sessions/edit/take-over``.
        """
        await self._transport.request(
            "POST",
            "/sessions/edit/take-over",
            json_body={"key": key},
            allow_retry=False,
        )
