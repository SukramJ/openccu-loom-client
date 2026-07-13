# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Diagnostics + observability REST operations.

Covers the admin diagnostics surface: per-subsystem log-level
overrides, log backfill, RAM-buffered capture, the RPC-session
recorder, incidents, Prometheus metrics, the persistent VALUES-cache
admin, the MQTT-stack reload, and the audit ledger.

Most of these endpoints carry free-form JSON bodies in the daemon's
OpenAPI (``additionalProperties: true``), so they are typed as
``dict``. The ones that have a published schema (``ValuesCacheStats``,
``MQTTReloadResponse``, ``AuditEntry``) return their model.

The live SSE log tail (``GET /diagnostics/logs/stream``) is a
long-lived ``text/event-stream`` and is intentionally not wrapped by
this request/response transport; use the ``logs`` backfill instead.
Binary archive downloads use :meth:`HttpTransport.request_bytes`.
"""

from __future__ import annotations

from typing import Any

from openccu_loom_types.rest import AuditEntry, MQTTReloadResponse, ValuesCacheStats

from openccu_loom_client.operations._base import _OperationsBase


class DiagnosticsOperations(_OperationsBase):
    """Diagnostics, log control, capture/recording, cache + audit."""

    # ---- composite dump ----

    async def get_diagnostics(self) -> dict[str, Any]:
        """Composite diagnostics dump. Wire: ``GET /diagnostics``."""
        payload = await self._transport.request(method="GET", path="/diagnostics")
        return dict(payload or {})

    # ---- log levels ----

    async def get_log_level(self) -> dict[str, Any]:
        """Return the current global default log level. Wire: ``GET /diagnostics/log-level``."""
        payload = await self._transport.request(method="GET", path="/diagnostics/log-level")
        return dict(payload or {})

    async def set_log_level(self, *, level: str) -> dict[str, Any]:
        """
        Change the global default log level (admin).

        Wire: ``PUT /diagnostics/log-level``.
        """
        payload = await self._transport.request(
            method="PUT",
            path="/diagnostics/log-level",
            json_body={"level": level},
            allow_retry=True,
        )
        return dict(payload or {})

    async def get_log_levels(self) -> dict[str, Any]:
        """Active per-subsystem overrides. Wire: ``GET /diagnostics/log-levels``."""
        payload = await self._transport.request(method="GET", path="/diagnostics/log-levels")
        return dict(payload or {})

    async def set_log_level_override(self, *, path: str, level: str) -> None:
        """
        Install a per-subsystem override (admin).

        Wire: ``PUT /diagnostics/log-levels/{path}``.
        """
        await self._transport.request(
            method="PUT",
            path=f"/diagnostics/log-levels/{path}",
            json_body={"level": level},
            allow_retry=True,
        )

    async def remove_log_level_override(self, *, path: str) -> None:
        """
        Remove a per-subsystem override (admin).

        Wire: ``DELETE /diagnostics/log-levels/{path}``.
        """
        await self._transport.request(method="DELETE", path=f"/diagnostics/log-levels/{path}")

    # ---- log records ----

    async def get_logs(self, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Backfill recent log records (admin). Wire: ``GET /diagnostics/logs``."""
        payload = await self._transport.request(method="GET", path="/diagnostics/logs", params=params)
        return dict(payload or {})

    # ---- RAM-buffered capture ----

    async def start_capture(self, *, options: dict[str, Any] | None = None) -> dict[str, Any]:
        """Start a RAM-buffered log capture (admin). Wire: ``POST /diagnostics/capture``."""
        payload = await self._transport.request(
            method="POST", path="/diagnostics/capture", json_body=options or None, allow_retry=False
        )
        return dict(payload or {})

    async def list_captures(self) -> dict[str, Any]:
        """List active + archived captures (admin). Wire: ``GET /diagnostics/capture``."""
        payload = await self._transport.request(method="GET", path="/diagnostics/capture")
        return dict(payload or {})

    async def get_capture(self, *, capture_id: str) -> dict[str, Any]:
        """Capture metadata (admin). Wire: ``GET /diagnostics/capture/{id}``."""
        payload = await self._transport.request(method="GET", path=f"/diagnostics/capture/{capture_id}")
        return dict(payload or {})

    async def stop_capture(self, *, capture_id: str) -> dict[str, Any]:
        """Stop a running capture (admin). Wire: ``POST /diagnostics/capture/{id}/stop``."""
        payload = await self._transport.request(
            method="POST",
            path=f"/diagnostics/capture/{capture_id}/stop",
            allow_retry=False,
        )
        return dict(payload or {})

    async def download_capture(self, *, capture_id: str) -> bytes:
        """
        Download a capture archive (tar.gz, admin).

        Wire: ``GET /diagnostics/capture/{id}/download``.
        """
        return await self._transport.request_bytes(method="GET", path=f"/diagnostics/capture/{capture_id}/download")

    # ---- RPC session recorder ----

    async def start_rpc_recording(self, *, options: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """
        Start the XML/JSON/BIN-RPC session recorder (admin).

        Wire: ``POST /diagnostics/rpc-recording``.
        """
        payload = await self._transport.request(
            method="POST",
            path="/diagnostics/rpc-recording",
            json_body=options or None,
            allow_retry=False,
        )
        return list(payload or [])

    async def list_rpc_recordings(self) -> list[dict[str, Any]]:
        """RPC-recorder status per central (admin). Wire: ``GET /diagnostics/rpc-recording``."""
        payload = await self._transport.request(method="GET", path="/diagnostics/rpc-recording")
        return list(payload or [])

    async def stop_rpc_recording(self, *, options: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """
        Stop the RPC session recorder (admin).

        Wire: ``POST /diagnostics/rpc-recording/stop``.
        """
        payload = await self._transport.request(
            method="POST",
            path="/diagnostics/rpc-recording/stop",
            json_body=options or None,
            allow_retry=False,
        )
        return list(payload or [])

    async def download_rpc_recording(self, *, central: str) -> bytes:
        """
        Download a recorded RPC trace (admin).

        Wire: ``GET /diagnostics/rpc-recording/{central}/download``.
        """
        return await self._transport.request_bytes(method="GET", path=f"/diagnostics/rpc-recording/{central}/download")

    # ---- incidents / metrics ----

    async def clear_cache(self, *, kind: str = "global") -> None:
        """
        Clear the CCU-derivable caches and let the daemon re-pull them fresh.

        Wire: ``POST /admin/cache/clear`` with a ``CacheClearRequest``. This is
        the daemon analogue of aiohomematic's ``clear_all`` (device + paramset
        descriptions, device details, data cache) — a values-cache reset alone
        only covers a fraction of it.
        """
        await self._transport.request(
            method="POST",
            path="/admin/cache/clear",
            json_body={"kind": kind},
            allow_retry=False,
        )

    async def clear_incidents(self) -> None:
        """
        Drop the daemon's recorded incidents.

        Wire: ``DELETE /incidents``. The daemon owns the incident store, so the
        integration panel's "clear incidents" action has to reach it — a
        client-side no-op left the list unchanged and the button dead.
        """
        await self._transport.request(method="DELETE", path="/incidents", allow_retry=True)

    async def list_incidents(self) -> dict[str, Any]:
        """Diagnostic incidents. Wire: ``GET /incidents``."""
        payload = await self._transport.request(method="GET", path="/incidents")
        return dict(payload or {})

    async def get_metrics(self) -> bytes:
        """Prometheus metrics (text exposition). Wire: ``GET /metrics``."""
        return await self._transport.request_bytes(method="GET", path="/metrics")

    # ---- persistent VALUES cache ----

    async def get_values_cache_stats(self) -> ValuesCacheStats:
        """
        Return persistent VALUES-cache statistics (admin).

        Wire: ``GET /admin/values-cache/stats``.
        """
        payload = await self._transport.request(method="GET", path="/admin/values-cache/stats")
        return ValuesCacheStats.model_validate(payload)

    async def reset_values_cache(self) -> None:
        """
        Drop every row from the persistent cache (admin).

        Wire: ``POST /admin/values-cache/reset``.
        """
        await self._transport.request(method="POST", path="/admin/values-cache/reset", allow_retry=False)

    async def reset_device_values_cache(self, *, address: str) -> None:
        """
        Drop cache rows for one device (admin).

        Wire: ``POST /devices/{addr}/values-cache/reset``.
        """
        await self._transport.request(method="POST", path=f"/devices/{address}/values-cache/reset", allow_retry=False)

    # ---- MQTT ----

    async def reload_mqtt(self) -> MQTTReloadResponse:
        """
        Tear down + rebuild the MQTT stack (admin).

        Wire: ``POST /admin/mqtt/reload``.
        """
        payload = await self._transport.request(method="POST", path="/admin/mqtt/reload", allow_retry=False)
        return MQTTReloadResponse.model_validate(payload)

    # ---- audit ----

    async def list_audit(self) -> list[AuditEntry]:
        """Recent change history (FIFO buffer). Wire: ``GET /audit``."""
        return await self._request_list(method="GET", path="/audit", model=AuditEntry)
