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
        payload = await self._transport.request(method="POST", path="/backups", allow_retry=False)
        return dict(payload or {})

    async def list_backups(self) -> dict[str, Any]:
        """List locally stored backups. Wire: ``GET /backups``."""
        payload = await self._transport.request(method="GET", path="/backups")
        return dict(payload or {})

    async def download_backup(self, *, backup_id: str) -> bytes:
        """Stream a backup ``.sbk`` file. Wire: ``GET /backups/{id}/download``."""
        return await self._transport.request_bytes(method="GET", path=f"/backups/{backup_id}/download")

    async def upload_backup(
        self,
        *,
        content: bytes,
        filename: str = "backup.sbk",
        total_timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """
        Import an externally produced ``.sbk`` archive (admin).

        Wire: ``POST /backups/upload`` (multipart, 201). The stored
        archive then restores through :meth:`restore_backup` like any
        locally-taken backup.

        The daemon inspects the archive before storing it, so picking the
        wrong file fails here rather than at restore time when the CCU is
        already being wiped. The check is structural — a readable tar
        carrying ``usr_local.tar.gz`` and its signature; the signature
        itself cannot be verified without the CCU's key material and is
        not claimed to be. Returns the :class:`BackupEntry` fields plus
        ``firmware_version`` / ``product`` read from the archive, so the
        caller can compare them against the target CCU.

        Raises the usual typed errors: 413 when the archive exceeds the
        accepted size, 422 when it is not a CCU system backup (unreadable
        as a tar, or missing the configuration archive or its signature),
        503 when the daemon has no upload storage wired up. Requires
        daemon api ≥ 3.10.0.

        ``total_timeout_seconds`` caps the whole transfer; by default a
        large archive on a slow link is bounded only by the per-chunk
        stall timeout.
        """
        payload = await self._transport.request_upload(
            method="POST",
            path="/backups/upload",
            field_name="file",
            filename=filename,
            content=content,
            total_timeout_seconds=total_timeout_seconds,
        )
        return dict(payload or {})

    async def restore_backup(self, *, backup_id: str) -> None:
        """
        Restore a CCU backup from a stored snapshot.

        Wire: ``POST /backups/{id}/restore``. Not retried — a restore is
        a destructive, non-idempotent CCU operation.
        """
        await self._transport.request(method="POST", path=f"/backups/{backup_id}/restore", allow_retry=False)
