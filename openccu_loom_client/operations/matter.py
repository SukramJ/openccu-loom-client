# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Matter-bridge administration REST operations (``/matter``).

The Matter bridge is operator-opt-in. This wraps its status, fabric
management, the exposable-device allowlist, and commissioning-window
control.
"""

from __future__ import annotations

from typing import Any

from openccu_loom_types.rest import (
    MatterCommissioningWindowRequest,
    MatterCommissioningWindowResponse,
    MatterCompatibility,
    MatterDiagnosticEventList,
    MatterEndpointList,
    MatterExposureBulkUpdate,
    MatterExposureList,
    MatterExposureUpdate,
    MatterFabricList,
    MatterMdnsDiagnostics,
    MatterSessionList,
    MatterSetupPayload,
    MatterStatus,
)

from openccu_loom_client.operations._base import _OperationsBase


class MatterOperations(_OperationsBase):
    """Matter bridge status, fabrics, allowlist + commissioning."""

    async def get_status(self) -> MatterStatus:
        """Bridge runtime state. Wire: ``GET /matter/status``."""
        payload = await self._transport.request(method="GET", path="/matter/status")
        return MatterStatus.model_validate(payload)

    async def list_fabrics(self) -> MatterFabricList:
        """List commissioned Matter fabrics. Wire: ``GET /matter/fabrics``."""
        payload = await self._transport.request(method="GET", path="/matter/fabrics")
        return MatterFabricList.model_validate(payload)

    async def delete_fabric(self, *, fabric_id: str) -> None:
        """Unpair a Matter fabric (admin). Wire: ``DELETE /matter/fabrics/{id}``."""
        await self._transport.request(method="DELETE", path=f"/matter/fabrics/{fabric_id}")

    async def get_exposable(self) -> MatterExposureList:
        """
        List allowlist state + mappable verdict (admin).

        Wire: ``GET /matter/exposable``.
        """
        payload = await self._transport.request(method="GET", path="/matter/exposable")
        return MatterExposureList.model_validate(payload)

    async def update_exposable(self, *, update: MatterExposureUpdate) -> None:
        """Update one allowlist row (admin). Wire: ``PUT /matter/exposable``."""
        await self._transport.request(
            method="PUT",
            path="/matter/exposable",
            json_body=self._to_json_body(update),
            allow_retry=True,
        )

    async def update_exposable_bulk(self, *, updates: MatterExposureBulkUpdate) -> dict[str, Any]:
        """
        Apply multiple allowlist updates (admin).

        Wire: ``POST /matter/exposable/bulk``. Returns ``{applied: N}``.
        """
        payload = await self._transport.request(
            method="POST",
            path="/matter/exposable/bulk",
            json_body=self._to_json_body(updates),
            allow_retry=False,
        )
        return dict(payload or {})

    async def open_commissioning_window(
        self, *, request: MatterCommissioningWindowRequest | None = None
    ) -> MatterCommissioningWindowResponse:
        """
        Open a Matter commissioning window (admin).

        Wire: ``POST /matter/commissioning/window``.
        """
        body = self._to_json_body(request) if request is not None else None
        payload = await self._transport.request(
            method="POST",
            path="/matter/commissioning/window",
            json_body=body,
            allow_retry=False,
        )
        return MatterCommissioningWindowResponse.model_validate(payload)

    async def close_commissioning_window(self) -> None:
        """
        Close an open commissioning window (admin).

        Wire: ``POST /matter/commissioning/window/close``.
        """
        await self._transport.request(method="POST", path="/matter/commissioning/window/close", allow_retry=False)

    async def share(
        self, *, request: MatterCommissioningWindowRequest | None = None
    ) -> MatterCommissioningWindowResponse:
        """
        Open a commissioning window for a second controller (admin).

        Wire: ``POST /matter/share``.
        """
        body = self._to_json_body(request) if request is not None else None
        payload = await self._transport.request(method="POST", path="/matter/share", json_body=body, allow_retry=False)
        return MatterCommissioningWindowResponse.model_validate(payload)

    async def get_setup_payload(self) -> MatterSetupPayload:
        """
        Bridge QR + manual pairing code (admin, daemon ≥ 0.60.0).

        Wire: ``GET /matter/setup-payload``. Hands out the commissioning
        passcode, so daemons ≥ 0.60.0 answer 403 for non-admin identities.
        """
        payload = await self._transport.request(method="GET", path="/matter/setup-payload")
        return MatterSetupPayload.model_validate(payload)

    async def list_sessions(self) -> MatterSessionList:
        """
        List open secure sessions + id-space occupancy (daemon api ≥ 5.21.0).

        Wire: ``GET /matter/sessions``. ``peer_idle_seconds`` is the
        controller-liveness signal — a controller that went away without
        closing its session stops sending while the session stays open.
        """
        payload = await self._transport.request(method="GET", path="/matter/sessions")
        return MatterSessionList.model_validate(payload)

    async def list_endpoints(self) -> MatterEndpointList:
        """
        Return the assembled bridge topology as a controller sees it (daemon api ≥ 5.22.0).

        Wire: ``GET /matter/endpoints`` — device types + clusters per
        endpoint, with the source device/channel addresses.
        """
        payload = await self._transport.request(method="GET", path="/matter/endpoints")
        return MatterEndpointList.model_validate(payload)

    async def get_mdns_diagnostics(self) -> MatterMdnsDiagnostics:
        """
        Return what the bridge announces over mDNS + discoverability findings (daemon api ≥ 5.22.0).

        Wire: ``GET /matter/mdns``.
        """
        payload = await self._transport.request(method="GET", path="/matter/mdns")
        return MatterMdnsDiagnostics.model_validate(payload)

    async def get_compatibility(self) -> MatterCompatibility:
        """
        Ecosystem classification of the paired fabrics + per-ecosystem findings (daemon api ≥ 5.22.0).

        Wire: ``GET /matter/compatibility``.
        """
        payload = await self._transport.request(method="GET", path="/matter/compatibility")
        return MatterCompatibility.model_validate(payload)

    async def list_diagnostic_events(self) -> MatterDiagnosticEventList:
        """
        Return the bounded pairing/session/discovery event trace (daemon api ≥ 5.33.0).

        Wire: ``GET /matter/events``. A diagnostic, not an audit trail —
        the trace does not survive a daemon restart.
        """
        payload = await self._transport.request(method="GET", path="/matter/events")
        return MatterDiagnosticEventList.model_validate(payload)

    async def force_sync(self) -> None:
        """
        Rebuild the exposed endpoints from the current devices (admin, daemon api ≥ 5.31.0).

        Wire: ``POST /matter/force-sync``. Touches no pairing — the
        alternative to restarting the daemon when the endpoint list has
        drifted from the model.
        """
        await self._transport.request(method="POST", path="/matter/force-sync", allow_retry=False)

    async def factory_reset(self, *, confirm: str) -> None:
        """
        Remove all pairings, returning the bridge to its unpaired state (admin, daemon api ≥ 5.31.0).

        Wire: ``POST /matter/factory-reset``. The daemon requires the
        caller to name the action: ``confirm`` must be the literal
        ``"remove-all-fabrics"`` — deliberately not defaulted here, so no
        call site can unpair an installation without spelling it out. A
        fabric that fails to revoke is reported as an error by the daemon
        rather than silently skipped.
        """
        await self._transport.request(
            method="POST",
            path="/matter/factory-reset",
            json_body={"confirm": confirm},
            allow_retry=False,
        )
