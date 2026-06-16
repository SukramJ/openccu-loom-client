# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""System-level REST operations: snapshot, health, diagnostics, interfaces."""

from __future__ import annotations

from typing import Any

from openccu_loom_types.rest import (
    Health,
    HubMetricsEntry,
    Info,
    InterfaceState,
    Snapshot,
    StartupCaptureConfig,
    SystemCCUEntry,
    SystemUpdateEntry,
)

from openccu_loom_client.operations._base import _OperationsBase


class SystemOperations(_OperationsBase):
    """Daemon-level endpoints — info, health, snapshot, interfaces, diagnostics."""

    # ---- info / health ----

    async def get_info(self) -> Info:
        """Build + runtime info (also runs at connect() in HttpTransport)."""
        payload = await self._transport.request(method="GET", path="/info")
        return Info.model_validate(payload)

    async def get_health(self) -> Health:
        """Return the daemon health probe. Wire: ``GET /health``."""
        payload = await self._transport.request(method="GET", path="/health")
        return Health.model_validate(payload)

    async def get_diagnostics(self) -> dict[str, Any]:
        """
        Structured snapshot for repair/diagnose flows.

        Wire: ``GET /diagnostics``. The schema is open — the daemon's
        component layout decides what's in there. HA repair flows
        get a typed map to display.
        """
        payload = await self._transport.request(method="GET", path="/diagnostics")
        return dict(payload or {})

    # ---- snapshot ----

    async def get_snapshot(self) -> Snapshot:
        """
        One-shot dump of every device / program / sysvar / interface.

        Wire: ``GET /snapshot``. The HA bootstrap path calls this once
        at connect time to seed the local :class:`LoomStore`. For
        large CCUs the daemon's streaming snapshot ask (asks.md H1)
        will eventually replace this with cursor pagination — until
        then the client is expected to take the one-shot hit.
        """
        payload = await self._transport.request(method="GET", path="/snapshot")
        return Snapshot.model_validate(payload)

    # ---- interfaces ----

    async def list_interfaces(self) -> list[InterfaceState]:
        """List all CCU interfaces and their state. Wire: ``GET /interfaces``."""
        payload = await self._transport.request(method="GET", path="/interfaces")
        return [InterfaceState.model_validate(i) for i in (payload or [])]

    async def reconnect_interface(self, *, interface_id: str) -> None:
        """Wire: ``POST /interfaces/{id}/reconnect``."""
        await self._transport.request(
            method="POST",
            path=f"/interfaces/{interface_id}/reconnect",
            allow_retry=False,
        )

    # ---- system update / metrics ----

    async def get_system_update(self) -> list[SystemUpdateEntry]:
        """
        Return the per-central CCU system-update state.

        Wire: ``GET /system/update``. Firmware fields stay ``None``
        until the daemon has observed the CCU's update endpoint.
        """
        payload = await self._transport.request(method="GET", path="/system/update")
        return [SystemUpdateEntry.model_validate(e) for e in (payload or [])]

    async def install_system_update(self, *, central: str | None = None) -> None:
        """
        Trigger the CCU system-update install (admin).

        Wire: ``POST /system/update/install?central=<name>``. Without
        ``central`` the daemon resolves its default central. Not
        retried — a duplicated trigger could double-run the CCU update.
        """
        await self._transport.request(
            method="POST",
            path="/system/update/install",
            params={"central": central} if central else None,
            allow_retry=False,
        )

    async def get_hub_metrics(self) -> list[HubMetricsEntry]:
        """
        Return per-central hub metrics (health, latency, event age).

        Wire: ``GET /system/metrics``. Metric fields are ``None`` until
        the daemon has observed them.
        """
        payload = await self._transport.request(method="GET", path="/system/metrics")
        return [HubMetricsEntry.model_validate(m) for m in (payload or [])]

    # ---- CCU repair surface ----

    async def list_system_ccus(self) -> list[SystemCCUEntry]:
        """
        Wire: ``GET /system/ccu``. Repair / config-flow read-out.

        The daemon wraps the list in an ``{"entries": [...]}`` envelope;
        unwrap it, while tolerating a bare list for forward-compatibility.
        """
        payload = await self._transport.request(method="GET", path="/system/ccu")
        entries = payload.get("entries", []) if isinstance(payload, dict) else (payload or [])
        return [SystemCCUEntry.model_validate(c) for c in entries]

    # ---- lifecycle / status (admin) ----

    async def get_system_status(self) -> dict[str, Any]:
        """Recent system-status events. Wire: ``GET /system/status``."""
        payload = await self._transport.request(method="GET", path="/system/status")
        return dict(payload or {})

    async def restart(self) -> dict[str, Any]:
        """
        Trigger a graceful daemon shutdown/restart (admin).

        Wire: ``POST /system/restart``. Not retried.
        """
        payload = await self._transport.request(method="POST", path="/system/restart", allow_retry=False)
        return dict(payload or {})

    async def get_startup_capture(self) -> StartupCaptureConfig:
        """
        Read the startup-capture toggle (admin).

        Wire: ``GET /system/startup-capture``.
        """
        payload = await self._transport.request(method="GET", path="/system/startup-capture")
        return StartupCaptureConfig.model_validate(payload)

    async def set_startup_capture(self, *, config: StartupCaptureConfig) -> StartupCaptureConfig:
        """
        Persist the startup-capture toggle (admin).

        Wire: ``PUT /system/startup-capture``.
        """
        payload = await self._transport.request(
            method="PUT",
            path="/system/startup-capture",
            json_body=config.model_dump(mode="json", exclude_none=True),
            allow_retry=True,
        )
        return StartupCaptureConfig.model_validate(payload)
