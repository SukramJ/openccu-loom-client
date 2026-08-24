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
        payload = await self._transport.request(method="GET", path="/config")
        return ConfigSnapshot.model_validate(payload)

    async def get_schema(self) -> SchemaResponse:
        """
        Config field schema (section list + typed descriptors).

        Wire: ``GET /config/schema``.
        """
        payload = await self._transport.request(method="GET", path="/config/schema")
        return SchemaResponse.model_validate(payload)

    async def get_effective(self) -> ConfigSnapshotResponse:
        """
        Return the merged effective config with source annotations (admin).

        Wire: ``GET /config/effective``.
        """
        payload = await self._transport.request(method="GET", path="/config/effective")
        return ConfigSnapshotResponse.model_validate(payload)

    async def get_section(self, *, section: str) -> dict[str, Any]:
        """Read one config section (admin). Wire: ``GET /config/sections/{section}``."""
        payload = await self._transport.request(method="GET", path=f"/config/sections/{section}")
        return dict(payload or {})

    async def put_section(self, *, section: str, values: dict[str, Any]) -> dict[str, Any]:
        """
        Replace one config section (admin).

        Wire: ``PUT /config/sections/{section}``. Returns the daemon's
        ack: ``section``, ``version``, ``updated_at``,
        ``restart_required``, and — from daemon api 7.8.0 — ``applied``
        plus an optional ``apply_error``.

        ``applied`` is the answer to a question the ack could not
        previously express: whether the running daemon *took* the change,
        as opposed to merely storing it. False on its own is not a
        failure — most sections have no subsystem that can rebuild itself
        and the value simply takes effect at the next restart. It is
        false with an ``apply_error`` when a subsystem that could have
        taken it refused, and that is the case a caller must not report
        as a plain success: the section is saved, the daemon is still
        doing the old thing, and only this field says so.

        Both keys are absent against a daemon older than 7.8.0. Treat a
        missing ``applied`` as "unknown", not as False.
        """
        payload = await self._transport.request(
            method="PUT",
            path=f"/config/sections/{section}",
            json_body=values,
            allow_retry=True,
        )
        return dict(payload or {})

    async def delete_section(self, *, section: str) -> None:
        """
        Delete one config section (admin).

        Wire: ``DELETE /config/sections/{section}``.
        """
        await self._transport.request(method="DELETE", path=f"/config/sections/{section}")
