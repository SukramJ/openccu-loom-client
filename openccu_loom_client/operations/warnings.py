# SPDX-License-Identifier: MIT
# Copyright (C) 2026 SukramJ.

"""
Operator warnings and per-user silences (daemon ≥ 0.80.0, api 12.2.0).

The daemon aggregates what needs an operator's attention into one list:
unhealthy or degraded health components, error-grade incidents of the last
24 hours and per-central service-message backlogs. Each row carries a stable
id (``<source>:<key>``, e.g. ``health:mqtt``), a ``message_key`` naming the
catalogue entry that renders it and the ``args`` for that entry, so the
daemon stays the naming authority.

A silence belongs to the calling user and is invisible to every other one.
It ends after the chosen number of days, or earlier when the warning's
condition clears, so a re-occurrence alerts again.

A daemon older than api 12.2.0 has no such routes and answers 404, which
arrives as :class:`~openccu_loom_client.exceptions.LoomNotFoundError`.
"""

from __future__ import annotations

from urllib.parse import quote

from openccu_loom_client.operations._base import _OperationsBase
from openccu_loom_client.wire.rest import Warning as LoomWarning, WarningList


class WarningsOperations(_OperationsBase):
    """The operator warning list and the calling user's silences."""

    async def list_warnings(self) -> list[LoomWarning]:
        """
        Return the active warnings, each marked with the caller's silence.

        Wire: ``GET /warnings``. ``silenced`` and ``silenced_until`` describe
        the CALLING user only; another user's silence of the same warning
        does not show here.
        """
        payload = await self._transport.request(method="GET", path="/warnings")
        return list(WarningList.model_validate(payload or {"items": []}).items)

    async def silence_warning(self, *, warning_id: str, days: int) -> None:
        """
        Mute one active warning for the calling user.

        Wire: ``PUT /warnings/{id}/silence`` with ``{"days": days}``. The
        daemon accepts 1, 7 or 90 and refuses anything else with 400
        (:class:`~openccu_loom_client.exceptions.LoomValidationError`); the
        set is the daemon's, so it is not checked here. A warning that is
        not currently active answers 404
        (:class:`~openccu_loom_client.exceptions.LoomNotFoundError`) — the
        same answer a daemon older than api 12.2.0 gives, because it has
        no such route.
        """
        await self._transport.request(
            method="PUT",
            path=f"/warnings/{quote(warning_id, safe='')}/silence",
            json_body={"days": days},
            allow_retry=True,
        )

    async def unsilence_warning(self, *, warning_id: str) -> None:
        """
        Remove the calling user's silence for one warning.

        Wire: ``DELETE /warnings/{id}/silence``. Idempotent: removing a
        silence that does not exist also answers 204.
        """
        await self._transport.request(
            method="DELETE",
            path=f"/warnings/{quote(warning_id, safe='')}/silence",
            allow_retry=True,
        )
