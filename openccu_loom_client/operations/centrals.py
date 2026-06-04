# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Central (CCU connection) administration REST operations.

Covers ``/centrals``. The daemon is multi-CCU from day one; each
:class:`CentralRow` is one persisted CCU connection record.
"""

from __future__ import annotations

from openccu_loom_types.rest import CentralRow

from openccu_loom_client.operations._base import _OperationsBase


class CentralsOperations(_OperationsBase):
    """CRUD over the daemon's centrals store (admin)."""

    async def list_centrals(self) -> list[CentralRow]:
        """Wire: ``GET /centrals``."""
        payload = await self._transport.request("GET", "/centrals")
        return [CentralRow.model_validate(c) for c in (payload or [])]

    async def get_central(self, *, name: str) -> CentralRow:
        """Wire: ``GET /centrals/{name}``."""
        payload = await self._transport.request("GET", f"/centrals/{name}")
        return CentralRow.model_validate(payload)

    async def create_central(self, *, central: CentralRow) -> CentralRow:
        """Create a new central. Wire: ``POST /centrals``."""
        payload = await self._transport.request(
            "POST",
            "/centrals",
            json_body=central.model_dump(mode="json", exclude_none=True),
            allow_retry=False,
        )
        return CentralRow.model_validate(payload)

    async def replace_central(self, *, name: str, central: CentralRow) -> None:
        """Replace a central. Wire: ``PUT /centrals/{name}``."""
        await self._transport.request(
            "PUT",
            f"/centrals/{name}",
            json_body=central.model_dump(mode="json", exclude_none=True),
            allow_retry=True,
        )

    async def delete_central(self, *, name: str) -> None:
        """Delete a central. Wire: ``DELETE /centrals/{name}``."""
        await self._transport.request("DELETE", f"/centrals/{name}")
