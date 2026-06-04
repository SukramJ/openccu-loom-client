# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Configuration REST operations.

Covers the sanitized read-only ``/config`` plus the admin
config-management surface (``/config/schema``, ``/config/effective``,
and per-section read/replace/delete under ``/config/sections``).
"""

from __future__ import annotations

from typing import Any

from openccu_loom_types.rest import ConfigSnapshot, ConfigSnapshotResponse, SchemaResponse

from openccu_loom_client.operations._base import _OperationsBase


class ConfigOperations(_OperationsBase):
    """Effective config read-out + admin section management."""

    async def get_config(self) -> ConfigSnapshot:
        """Sanitized effective configuration. Wire: ``GET /config``."""
        payload = await self._transport.request("GET", "/config")
        return ConfigSnapshot.model_validate(payload)

    async def get_schema(self) -> SchemaResponse:
        """
        Config field schema (section list + typed descriptors).

        Wire: ``GET /config/schema``.
        """
        payload = await self._transport.request("GET", "/config/schema")
        return SchemaResponse.model_validate(payload)

    async def get_effective(self) -> ConfigSnapshotResponse:
        """
        Return the merged effective config with source annotations (admin).

        Wire: ``GET /config/effective``.
        """
        payload = await self._transport.request("GET", "/config/effective")
        return ConfigSnapshotResponse.model_validate(payload)

    async def get_section(self, *, section: str) -> dict[str, Any]:
        """Read one config section (admin). Wire: ``GET /config/sections/{section}``."""
        payload = await self._transport.request("GET", f"/config/sections/{section}")
        return dict(payload or {})

    async def put_section(self, *, section: str, values: dict[str, Any]) -> dict[str, Any]:
        """
        Replace one config section (admin).

        Wire: ``PUT /config/sections/{section}``. Returns the daemon's
        ack (section, version, updated_at, restart_required).
        """
        payload = await self._transport.request(
            "PUT",
            f"/config/sections/{section}",
            json_body=values,
            allow_retry=True,
        )
        return dict(payload or {})

    async def delete_section(self, *, section: str) -> None:
        """
        Delete one config section (admin).

        Wire: ``DELETE /config/sections/{section}``.
        """
        await self._transport.request("DELETE", f"/config/sections/{section}")
