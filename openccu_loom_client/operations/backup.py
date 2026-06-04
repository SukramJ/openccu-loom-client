# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
CCU backup REST operations (``/backups``).

The daemon drives the CCU's Rega ``create_backup_start`` and stores the
resulting ``.sbk`` archive locally. Bodies are free-form in the
daemon's OpenAPI, so list/trigger return dicts; the archive download
returns raw bytes.
"""

from __future__ import annotations

from typing import Any

from openccu_loom_client.operations._base import _OperationsBase


class BackupOperations(_OperationsBase):
    """Trigger / list / download / restore CCU backups (admin)."""

    async def trigger_backup(self) -> dict[str, Any]:
        """Trigger a CCU backup. Wire: ``POST /backups``."""
        payload = await self._transport.request("POST", "/backups", allow_retry=False)
        return dict(payload or {})

    async def list_backups(self) -> dict[str, Any]:
        """List locally stored backups. Wire: ``GET /backups``."""
        payload = await self._transport.request("GET", "/backups")
        return dict(payload or {})

    async def download_backup(self, *, backup_id: str) -> bytes:
        """Stream a backup ``.sbk`` file. Wire: ``GET /backups/{id}/download``."""
        return await self._transport.request_bytes("GET", f"/backups/{backup_id}/download")

    async def restore_backup(self, *, backup_id: str) -> None:
        """
        Restore a CCU backup from a stored snapshot.

        Wire: ``POST /backups/{id}/restore``. Not retried — a restore is
        a destructive, non-idempotent CCU operation.
        """
        await self._transport.request("POST", f"/backups/{backup_id}/restore", allow_retry=False)
