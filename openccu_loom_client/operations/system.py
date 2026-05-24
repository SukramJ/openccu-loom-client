# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""System-level REST operations: snapshot, health, diagnostics, interfaces."""

from __future__ import annotations

from typing import Any

from openccu_loom_types.rest import (
    Health,
    Info,
    InterfaceState,
    Snapshot,
    StartupCaptureConfig,
    SystemCCUEntry,
)

from openccu_loom_client.operations._base import _OperationsBase


class SystemOperations(_OperationsBase):
    """Daemon-level endpoints — info, health, snapshot, interfaces, diagnostics."""

    # ---- info / health ----

    async def get_info(self) -> Info:
        """Build + runtime info (also runs at connect() in HttpTransport)."""
        payload = await self._transport.request("GET", "/info")
        return Info.model_validate(payload)

    async def get_health(self) -> Health:
        payload = await self._transport.request("GET", "/health")
        return Health.model_validate(payload)

    async def get_diagnostics(self) -> dict[str, Any]:
        """Structured snapshot for repair/diagnose flows.

        Wire: ``GET /diagnostics``. The schema is open — the daemon's
        component layout decides what's in there. HA repair flows
        get a typed map to display.
        """
        payload = await self._transport.request("GET", "/diagnostics")
        return dict(payload or {})

    # ---- snapshot ----

    async def get_snapshot(self) -> Snapshot:
        """One-shot dump of every device / program / sysvar / interface.

        Wire: ``GET /snapshot``. The HA bootstrap path calls this once
        at connect time to seed the local :class:`LoomStore`. For
        large CCUs the daemon's streaming snapshot ask (asks.md H1)
        will eventually replace this with cursor pagination — until
        then the client is expected to take the one-shot hit.
        """
        payload = await self._transport.request("GET", "/snapshot")
        return Snapshot.model_validate(payload)

    # ---- interfaces ----

    async def list_interfaces(self) -> list[InterfaceState]:
        payload = await self._transport.request("GET", "/interfaces")
        return [InterfaceState.model_validate(i) for i in (payload or [])]

    async def reconnect_interface(self, *, interface_id: str) -> None:
        """Wire: ``POST /interfaces/{id}/reconnect``."""
        await self._transport.request(
            "POST",
            f"/interfaces/{interface_id}/reconnect",
            allow_retry=False,
        )

    # ---- CCU repair surface ----

    async def list_system_ccus(self) -> list[SystemCCUEntry]:
        """Wire: ``GET /system/ccu``. Repair / config-flow read-out."""
        payload = await self._transport.request("GET", "/system/ccu")
        return [SystemCCUEntry.model_validate(c) for c in (payload or [])]

    # ---- lifecycle / status (admin) ----

    async def get_system_status(self) -> dict[str, Any]:
        """Recent system-status events. Wire: ``GET /system/status``."""
        payload = await self._transport.request("GET", "/system/status")
        return dict(payload or {})

    async def restart(self) -> dict[str, Any]:
        """Trigger a graceful daemon shutdown/restart (admin).

        Wire: ``POST /system/restart``. Not retried.
        """
        payload = await self._transport.request(
            "POST", "/system/restart", allow_retry=False
        )
        return dict(payload or {})

    async def get_startup_capture(self) -> StartupCaptureConfig:
        """Read the startup-capture toggle (admin).

        Wire: ``GET /system/startup-capture``.
        """
        payload = await self._transport.request("GET", "/system/startup-capture")
        return StartupCaptureConfig.model_validate(payload)

    async def set_startup_capture(
        self, *, config: StartupCaptureConfig
    ) -> StartupCaptureConfig:
        """Persist the startup-capture toggle (admin).

        Wire: ``PUT /system/startup-capture``.
        """
        payload = await self._transport.request(
            "PUT",
            "/system/startup-capture",
            json_body=config.model_dump(mode="json", exclude_none=True),
            allow_retry=True,
        )
        return StartupCaptureConfig.model_validate(payload)
