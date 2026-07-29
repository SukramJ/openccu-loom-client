# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""System-level REST operations: snapshot, health, diagnostics, interfaces."""

from __future__ import annotations

from typing import Any

from openccu_loom_types.rest import (
    AddonUpdateStatus,
    Health,
    HubDataPoints,
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

    async def get_snapshot(self, *, include: str | None = None) -> Snapshot:
        """
        One-shot dump of every device / program / sysvar / interface.

        Wire: ``GET /snapshot``. The HA bootstrap path calls this once
        at connect time to seed the local :class:`LoomStore`.

        ``include`` opts into the daemon's nested snapshot shape via the
        ``?include=`` query parameter. Pass ``"data_points"`` (which the
        daemon expands to channels + data points) to receive the full
        devices→channels→data-points graph in this single response —
        :attr:`Snapshot.device_channels` is then populated, letting
        :meth:`LoomClient.bootstrap` skip the per-channel data-point
        fan-out. With ``include=None`` the flat summary shape is returned
        (devices / programs / sysvars / interfaces only), unchanged.

        (The daemon additionally offers NDJSON streaming via
        ``Accept: application/x-ndjson``; this client consumes the
        nested JSON envelope, not the stream.)
        """
        params = {"include": include} if include else None
        payload = await self._transport.request(method="GET", path="/snapshot", params=params)
        return Snapshot.model_validate(payload)

    # ---- interfaces ----

    async def list_interfaces(self) -> list[InterfaceState]:
        """List all CCU interfaces and their state. Wire: ``GET /interfaces``."""
        return await self._request_list(method="GET", path="/interfaces", model=InterfaceState)

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
        return await self._request_list(method="GET", path="/system/update", model=SystemUpdateEntry)

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

    # ---- add-on self-update ----

    async def get_addon_update_status(self) -> AddonUpdateStatus:
        """
        Self-update status of the daemon's own CCU add-on package.

        Wire: ``GET /system/addon-update``. Only meaningful on platforms
        with the firmware-side installer (OpenCCU / RaspberryMatic);
        elsewhere ``supported`` is ``False`` and the check/install verbs
        answer 404. Daemons older than api 3.3.0 answer 404 here too.
        """
        payload = await self._transport.request(method="GET", path="/system/addon-update")
        return AddonUpdateStatus.model_validate(payload)

    async def check_addon_update(self) -> None:
        """
        Trigger an immediate add-on update check against the release feed.

        Wire: ``POST /system/addon-update/check`` (202). Observe the
        result via :meth:`get_addon_update_status` or the
        ``addon_update.state_changed`` broadcast. Not retried.
        """
        await self._transport.request(method="POST", path="/system/addon-update/check", allow_retry=False)

    async def install_addon_update(self) -> None:
        """
        Download, verify and install the latest add-on package (admin).

        Wire: ``POST /system/addon-update/install`` (202). The daemon
        restarts as part of the install. Not retried — while an install
        is already running the daemon answers 409.
        """
        await self._transport.request(method="POST", path="/system/addon-update/install", allow_retry=False)

    async def get_hub_metrics(self) -> list[HubMetricsEntry]:
        """
        Return per-central hub metrics (health, latency, event age).

        Wire: ``GET /system/metrics``. Metric fields are ``None`` until
        the daemon has observed them.
        """
        return await self._request_list(method="GET", path="/system/metrics", model=HubMetricsEntry)

    async def get_hub_data_points(self) -> list[HubDataPoints]:
        """
        Return the aggregated hub-singleton snapshot, one entry per central.

        Wire: ``GET /hub/data-points``. Collapses the per-endpoint message /
        inbox / metrics / connectivity / install-mode fan-out into a single
        round-trip; alarm/service carry the **count only** (bodies stay on the
        list endpoints) and ``update`` the flags only (firmware strings stay on
        ``get_system_update``).
        """
        return await self._request_list(method="GET", path="/hub/data-points", model=HubDataPoints)

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
            json_body=self._to_json_body(config),
            allow_retry=True,
        )
        return StartupCaptureConfig.model_validate(payload)
