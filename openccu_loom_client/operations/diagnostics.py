# SPDX-License-Identifier: MIT
# Copyright (C) 2026 SukramJ.

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

from typing import TYPE_CHECKING, Any

from openccu_loom_client.operations._base import _OperationsBase
from openccu_loom_client.wire.rest import AuditEntry, MQTTReloadResponse, ValuesCacheStats

if TYPE_CHECKING:
    from datetime import datetime


class DiagnosticsOperations(_OperationsBase):
    """Diagnostics, log control, capture/recording, cache + audit."""

    # ---- composite dump ----

    async def get_diagnostics(self) -> dict[str, Any]:
        """Composite diagnostics dump. Wire: ``GET /diagnostics``."""
        payload = await self._transport.request(method="GET", path="/diagnostics")
        return dict(payload or {})

    async def get_wiring(self) -> list[dict[str, Any]]:
        """
        Return the seams the running daemon declared as it wired them.

        Wire: ``GET /diagnostics/wiring`` (admin, daemon api 7.7.0+).

        Each entry names the seam, the collaborator, whether it is a
        per-central observer or a once-only ordered attachment, the boot
        marks it must precede or follow, and what stops working when it
        is absent. ``violations`` lists the ordering constraints that
        were already broken when the seam attached — normally empty; a
        non-empty list is a wiring defect the daemon reports about
        itself, and nothing else about it looks wrong.

        An empty list is a valid answer and means the daemon wired none
        of them. It is not an error, which is why this endpoint never
        reports "unavailable" instead: a caller has to be able to tell
        "wired nothing" from "cannot say".

        A daemon older than api 7.7.0 has no such route and answers 404.
        """
        payload = await self._transport.request(method="GET", path="/diagnostics/wiring")
        if not isinstance(payload, list):
            return []
        return [dict(entry) for entry in payload if isinstance(entry, dict)]

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

    async def list_incidents(self) -> list[dict[str, Any]]:
        """
        Diagnostic incidents, newest first. Wire: ``GET /incidents``.

        The daemon answers with a JSON array of ``Incident`` (at most its
        default limit of 500); any other body reads as no incidents.
        """
        payload = await self._transport.request(method="GET", path="/incidents")
        return payload if isinstance(payload, list) else []

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

    async def list_device_audit(
        self,
        *,
        address: str,
        op: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[AuditEntry]:
        """
        Change history of one device and its channels, newest first.

        Wire: ``GET /devices/{address}/audit``. Open to the operator role,
        where :meth:`list_audit` is admin-only. The address matches exactly
        (the device address, or one of its channels). ``op`` filters by
        action substring; ``since`` is inclusive and ``until`` exclusive;
        ``limit`` (daemon default 1000, cap 10000) and ``offset`` page the
        durable history. ``since`` / ``until`` must be timezone-aware: the
        daemon reads RFC 3339 and refuses a time without an offset. Bound to this client's central like every
        device-addressed call, so rows another CCU recorded for a repeated
        address are left out.
        """
        params: dict[str, Any] = {}
        if op is not None:
            params["op"] = op
        if since is not None:
            params["since"] = _rfc3339(value=since, name="since")
        if until is not None:
            params["until"] = _rfc3339(value=until, name="until")
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        return await self._request_list(
            method="GET", path=f"/devices/{address}/audit", model=AuditEntry, params=params or None
        )


def _rfc3339(*, value: datetime, name: str) -> str:
    """Render an aware ``datetime`` as RFC 3339; refuse a naive one before it reaches the wire."""
    if value.tzinfo is None or value.utcoffset() is None:
        msg = f"{name} must be timezone-aware (the daemon reads RFC 3339)"
        raise ValueError(msg)
    return value.isoformat()
