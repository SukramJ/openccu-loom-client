# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Edit-lock session REST operations (``/sessions/edit``).

Cooperative 5-minute edit locks keyed on a resource string, so two
operators don't clobber each other's config/paramset edits.
``acquire`` returns the lock state (including the ``token`` that
``release`` / ``heartbeat`` must present — declared as required since
daemon api 6.0.0, though the handlers always demanded it).
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
        payload = await self._transport.request(method="POST", path="/sessions/edit", json_body=body, allow_retry=False)
        return dict(payload or {})

    async def release(self, *, key: str, token: str) -> None:
        """
        Release an edit lock.

        Wire: ``DELETE /sessions/edit``. The body names the lock (``key``)
        and proves ownership (``token``, from :meth:`acquire`) — required
        by the handler all along, declared in the contract since api 6.0.0.
        """
        await self._transport.request(
            method="DELETE",
            path="/sessions/edit",
            json_body={"key": key, "token": token},
        )

    async def heartbeat(self, *, key: str, token: str) -> dict[str, Any]:
        """
        Refresh an edit lock's TTL.

        Wire: ``POST /sessions/edit/heartbeat``. Same ``key`` + ``token``
        body as :meth:`release`; returns the refreshed lock state.
        """
        payload = await self._transport.request(
            method="POST",
            path="/sessions/edit/heartbeat",
            json_body={"key": key, "token": token},
            allow_retry=False,
        )
        return dict(payload or {})

    async def take_over(self, *, key: str) -> None:
        """
        Force-close another user's edit lock.

        Wire: ``POST /sessions/edit/take-over``.
        """
        await self._transport.request(
            method="POST",
            path="/sessions/edit/take-over",
            json_body={"key": key},
            allow_retry=False,
        )
