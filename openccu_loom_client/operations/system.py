# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""System-level REST operations: snapshot, health, diagnostics, interfaces."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from openccu_loom_client.operations._base import _OperationsBase
from openccu_loom_client.wire.rest import (
    AddonUpdateStatus,
    Health,
    HubDataPoints,
    HubMetricsEntry,
    Info,
    InterfaceState,
    Snapshot,
    StartupCaptureConfig,
    StartupCaptureConfigWrite,
    SystemCCUEntry,
    SystemUpdateEntry,
)


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

    async def get_snapshot(
        self, *, include: str | None = None, released_only: bool = False, central: str | None = None
    ) -> Snapshot:
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

        ``released_only`` asks the daemon to omit devices whose onboarding
        the operator has not finished (daemon ≥ 0.66.1). Opt-in and never
        the default on the wire, because the same endpoint serves the
        daemon's Config UI, which must see such a device in order to
        configure it. An ecosystem consumer wants it on — see
        :attr:`LoomConfig.released_only`, which is what
        :meth:`LoomClient.bootstrap` passes. An older daemon ignores the
        unknown parameter and returns everything.

        ``central`` scopes the dump to one CCU. A daemon may mediate
        several, and the snapshot otherwise carries every one of them —
        so each consumer bound to a single CCU pays for, parses and then
        discards the others' whole device tree. Pass the daemon-side
        central name (``interfaces[].central_id``, == ``payload.central``);
        an older daemon ignores the unknown parameter and returns
        everything, which is the pre-scoping behaviour.

        (The daemon additionally offers NDJSON streaming via
        ``Accept: application/x-ndjson``; this client consumes the
        nested JSON envelope, not the stream.)
        """
        params: dict[str, Any] = {}
        if include:
            params["include"] = include
        if released_only:
            params["released_only"] = "true"
        if central:
            params["central"] = central
        payload = await self._transport.request(method="GET", path="/snapshot", params=params or None)
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

    async def install_system_update(self, *, central: str | None = None, backup_first: bool = False) -> None:
        """
        Trigger the CCU system-update install (admin).

        Wire: ``POST /system/update/install?central=<name>``. Without
        ``central`` the daemon resolves its default central. Not
        retried — a duplicated trigger could double-run the CCU update.

        With ``backup_first`` the daemon takes a full CCU backup and only
        starts the update once that backup is durably stored; a failed
        backup aborts and the update does not run. The call then **blocks
        for as long as the backup takes** — minutes on a large
        configuration — because its response is what tells the caller
        whether the safety net exists. Callers on a tight timeout budget
        must raise :attr:`LoomConfig.request_timeout_seconds` accordingly.
        Requires daemon api ≥ 3.11.0; older daemons ignore the body.
        """
        await self._transport.request(
            method="POST",
            path="/system/update/install",
            params={"central": central} if central else None,
            # Omit the body entirely unless asked for, so a pre-3.11.0
            # daemon sees the exact request shape it validated before.
            json_body={"backup_first": True} if backup_first else None,
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

    # ---- CCU maintenance (admin) ----
    #
    # These reboot / power off / re-mode the *CCU hardware*, not the daemon
    # (that is :meth:`restart`). All of them answer 202 the moment the CCU
    # accepted the request and 422 when the central's backend cannot host
    # the action at all (CUxD, Homegear, or a firmware without the method).
    # None are retried: every one has a side effect on real hardware and
    # the daemon does not deduplicate a repeated trigger.

    async def reboot_ccu(self, *, central: str) -> None:
        """
        Reboot one CCU (admin).

        Wire: ``POST /system/ccu/{central}/reboot`` (202). The daemon
        persists the CCU's object model (``system.Save()``) before
        triggering the reboot. The southbound connection to that central
        drops for the duration and recovers on its own once the CCU is
        back — the readiness gate re-runs the bring-up.
        """
        await self._transport.request(
            method="POST",
            path=f"/system/ccu/{quote(central, safe='')}/reboot",
            allow_retry=False,
        )

    async def poweroff_ccu(self, *, central: str) -> None:
        """
        Shut a CCU host down (admin).

        Wire: ``POST /system/ccu/{central}/poweroff`` (202). Nothing
        brings it back on: the central stays in the daemon's waiting
        state until it is switched on physically. That is the intended
        outcome, not a fault. Requires daemon api ≥ 3.9.0.
        """
        await self._transport.request(
            method="POST",
            path=f"/system/ccu/{quote(central, safe='')}/poweroff",
            allow_retry=False,
        )

    async def restart_ccu_safe_mode(self, *, central: str) -> None:
        """
        Restart a CCU into safe mode (admin).

        Wire: ``POST /system/ccu/{central}/safe-mode`` (202). The CCU
        comes back with its ReGa logic layer held down, so a
        configuration that breaks normal startup can be repaired.
        Requires daemon api ≥ 3.9.0.
        """
        await self._transport.request(
            method="POST",
            path=f"/system/ccu/{quote(central, safe='')}/safe-mode",
            allow_retry=False,
        )

    async def restart_ccu_recovery_mode(self, *, central: str) -> None:
        """
        Restart a CCU into its recovery system (admin).

        Wire: ``POST /system/ccu/{central}/recovery-mode`` (202). The CCU
        comes back in the separate recovery system reachable on its own
        address. OpenCCU / RaspberryMatic only — check
        :attr:`SystemCCUEntry.recovery_mode_supported` before offering
        this, a stock CCU3 has no such method and answers 422. Requires
        daemon api ≥ 3.9.0.
        """
        await self._transport.request(
            method="POST",
            path=f"/system/ccu/{quote(central, safe='')}/recovery-mode",
            allow_retry=False,
        )

    async def set_ccu_position(self, *, central: str, longitude: float, latitude: float) -> None:
        """
        Write a CCU's astro reference position (admin).

        Wire: ``PUT /system/ccu/{central}/position`` (204). Every
        sunrise/sunset time the CCU computes — for its own programs and
        for the weekly profiles this client edits — derives from this
        position, so a wrong value skews astro schedules silently rather
        than failing. The daemon reads the values back and compares them,
        so a successful call means the CCU holds exactly what was sent.
        The time zone is read-only (:attr:`SystemCCUEntry.timezone`); it
        is set on the CCU itself.

        Coordinates are decimal degrees. Out-of-range values and centrals
        whose backend has no ReGa path (CUxD, Homegear) answer 422.
        Requires daemon api ≥ 3.8.0.

        Retried: the write is idempotent — the same coordinates land the
        same way, and the daemon confirms by read-back either way.
        """
        await self._transport.request(
            method="PUT",
            path=f"/system/ccu/{quote(central, safe='')}/position",
            json_body={"longitude": longitude, "latitude": latitude},
            allow_retry=True,
        )

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

    async def set_startup_capture(self, *, config: StartupCaptureConfigWrite) -> StartupCaptureConfig:
        """
        Persist the startup-capture toggle (admin).

        Wire: ``PUT /system/startup-capture``. Read and write shapes split
        with api 6.0.0: the write's ``anonymise`` may be omitted and then
        means *true* (the privacy-preserving default), while the response
        always reports the effective value.
        """
        payload = await self._transport.request(
            method="PUT",
            path="/system/startup-capture",
            json_body=self._to_json_body(config),
            allow_retry=True,
        )
        return StartupCaptureConfig.model_validate(payload)
