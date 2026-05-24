# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Matter-bridge administration REST operations (``/matter``).

The Matter bridge is operator-opt-in. This wraps its status, fabric
management, the exposable-device allowlist, and commissioning-window
control.
"""

from __future__ import annotations

from typing import Any

from openccu_loom_types.rest import (
    MatterCommissioningWindowRequest,
    MatterCommissioningWindowResponse,
    MatterExposureBulkUpdate,
    MatterExposureList,
    MatterExposureUpdate,
    MatterFabricList,
    MatterSetupPayload,
    MatterStatus,
)

from openccu_loom_client.operations._base import _OperationsBase


class MatterOperations(_OperationsBase):
    """Matter bridge status, fabrics, allowlist + commissioning."""

    async def get_status(self) -> MatterStatus:
        """Bridge runtime state. Wire: ``GET /matter/status``."""
        payload = await self._transport.request("GET", "/matter/status")
        return MatterStatus.model_validate(payload)

    async def list_fabrics(self) -> MatterFabricList:
        """List commissioned Matter fabrics. Wire: ``GET /matter/fabrics``."""
        payload = await self._transport.request("GET", "/matter/fabrics")
        return MatterFabricList.model_validate(payload)

    async def delete_fabric(self, *, fabric_id: str) -> None:
        """Unpair a Matter fabric (admin). Wire: ``DELETE /matter/fabrics/{id}``."""
        await self._transport.request("DELETE", f"/matter/fabrics/{fabric_id}")

    async def get_exposable(self) -> MatterExposureList:
        """List allowlist state + mappable verdict (admin).

        Wire: ``GET /matter/exposable``.
        """
        payload = await self._transport.request("GET", "/matter/exposable")
        return MatterExposureList.model_validate(payload)

    async def update_exposable(self, *, update: MatterExposureUpdate) -> None:
        """Update one allowlist row (admin). Wire: ``PUT /matter/exposable``."""
        await self._transport.request(
            "PUT",
            "/matter/exposable",
            json_body=update.model_dump(mode="json", exclude_none=True),
            allow_retry=True,
        )

    async def update_exposable_bulk(
        self, *, updates: MatterExposureBulkUpdate
    ) -> dict[str, Any]:
        """Apply multiple allowlist updates (admin).

        Wire: ``POST /matter/exposable/bulk``. Returns ``{applied: N}``.
        """
        payload = await self._transport.request(
            "POST",
            "/matter/exposable/bulk",
            json_body=updates.model_dump(mode="json", exclude_none=True),
            allow_retry=False,
        )
        return dict(payload or {})

    async def open_commissioning_window(
        self, *, request: MatterCommissioningWindowRequest | None = None
    ) -> MatterCommissioningWindowResponse:
        """Open a Matter commissioning window (admin).

        Wire: ``POST /matter/commissioning/window``.
        """
        body = (
            request.model_dump(mode="json", exclude_none=True)
            if request is not None
            else None
        )
        payload = await self._transport.request(
            "POST",
            "/matter/commissioning/window",
            json_body=body,
            allow_retry=False,
        )
        return MatterCommissioningWindowResponse.model_validate(payload)

    async def close_commissioning_window(self) -> None:
        """Close an open commissioning window (admin).

        Wire: ``POST /matter/commissioning/window/close``.
        """
        await self._transport.request(
            "POST", "/matter/commissioning/window/close", allow_retry=False
        )

    async def share(
        self, *, request: MatterCommissioningWindowRequest | None = None
    ) -> MatterCommissioningWindowResponse:
        """Open a commissioning window for a second controller (admin).

        Wire: ``POST /matter/share``.
        """
        body = (
            request.model_dump(mode="json", exclude_none=True)
            if request is not None
            else None
        )
        payload = await self._transport.request(
            "POST", "/matter/share", json_body=body, allow_retry=False
        )
        return MatterCommissioningWindowResponse.model_validate(payload)

    async def get_setup_payload(self) -> MatterSetupPayload:
        """Bridge QR + manual pairing code. Wire: ``GET /matter/setup-payload``."""
        payload = await self._transport.request("GET", "/matter/setup-payload")
        return MatterSetupPayload.model_validate(payload)
