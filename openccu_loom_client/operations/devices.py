# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Device-scoped REST operations.

Maps to the ``/devices`` and ``/devices/{addr}``-rooted endpoints in
the daemon's OpenAPI surface. Returns parsed Pydantic models from
``openccu_loom_types.rest`` so callers get end-to-end typing.
"""

from __future__ import annotations

from typing import Any

from openccu_loom_types.rest import (
    CalculatedDPDetail,
    CalculatedDPSummary,
    ChannelSummary,
    DataPointSummary,
    DeviceDetail,
    DeviceList,
    DeviceSummary,
    ExportedConfiguration,
)

from openccu_loom_client.operations._base import _OperationsBase


class DevicesOperations(_OperationsBase):
    """Wraps the daemon's device REST surface."""

    # ---- read ----

    async def list_devices(self, *, page: int = 1, per_page: int = 50) -> DeviceList:
        """
        Paginated catalogue of registered devices.

        Wire: ``GET /devices?page=&per_page=``. Returns the full
        envelope (items + page + per_page + total) so callers can
        page through without parsing.
        """
        payload = await self._transport.request(
            method="GET",
            path="/devices",
            params={"page": page, "per_page": per_page},
        )
        return DeviceList.model_validate(payload)

    async def iter_all_devices(self, *, per_page: int = 200) -> list[DeviceSummary]:
        """
        Walk all pages and return one flat device list.

        Use for bootstrap workflows where the caller wants the
        complete catalogue in one go. For large CCUs prefer
        :meth:`SystemOperations.get_snapshot` which returns the
        device list in a single request.
        """
        out: list[DeviceSummary] = []
        page = 1
        while True:
            chunk = await self.list_devices(page=page, per_page=per_page)
            out.extend(chunk.items)
            if len(out) >= chunk.total or not chunk.items:
                return out
            page += 1

    async def get_device_detail(self, *, address: str) -> DeviceDetail:
        """
        One device's full record incl. firmware, availability and channels.

        Wire: ``GET /devices/{addr}``.
        """
        payload = await self._transport.request(method="GET", path=f"/devices/{address}")
        return DeviceDetail.model_validate(payload)

    async def list_channels(self, *, address: str) -> list[ChannelSummary]:
        """
        All channels of a device (without their data-points).

        Wire: ``GET /devices/{addr}/channels``.
        """
        return await self._request_list(method="GET", path=f"/devices/{address}/channels", model=ChannelSummary)

    async def list_data_points(
        self,
        *,
        address: str,
        channel: int,
    ) -> list[DataPointSummary]:
        """
        All data-points of one (device, channel) pair.

        Wire: ``GET /devices/{addr}/channels/{n}/data-points``.
        """
        return await self._request_list(
            method="GET",
            path=f"/devices/{address}/channels/{channel}/data-points",
            model=DataPointSummary,
        )

    async def get_data_point(
        self,
        *,
        address: str,
        channel: int,
        parameter: str,
    ) -> DataPointSummary:
        """
        One data-point record (incl. current value, range, type).

        Wire: ``GET /devices/{addr}/channels/{n}/data-points/{param}``.
        """
        payload = await self._transport.request(
            method="GET",
            path=f"/devices/{address}/channels/{channel}/data-points/{parameter}",
        )
        return DataPointSummary.model_validate(payload)

    # ---- calculated data points ----

    async def list_calculated_data_points(self, *, address: str, channel: int) -> list[CalculatedDPSummary]:
        """
        All calculated (derived) data-points on a channel.

        Wire: ``GET /devices/{addr}/channels/{n}/calc-dps``.
        """
        return await self._request_list(
            method="GET",
            path=f"/devices/{address}/channels/{channel}/calc-dps",
            model=CalculatedDPSummary,
        )

    async def get_calculated_data_point(self, *, address: str, channel: int, name: str) -> CalculatedDPDetail:
        """
        One calculated data-point by name (incl. ``depends_on``).

        Wire: ``GET /devices/{addr}/channels/{n}/calc-dps/{name}``.
        """
        payload = await self._transport.request(
            method="GET",
            path=f"/devices/{address}/channels/{channel}/calc-dps/{name}",
        )
        return CalculatedDPDetail.model_validate(payload)

    # ---- write / lifecycle ----

    async def refresh_all(self) -> None:
        """
        Force a CCU re-pull on every interface.

        Wire: ``POST /devices/refresh``. Async on the daemon side;
        the call returns 202.
        """
        await self._transport.request(method="POST", path="/devices/refresh")

    async def reload_device_config(self, *, address: str) -> None:
        """
        Re-pull a single device's config from its CCU.

        The surgical counterpart to :meth:`refresh_all` (re-pulls descriptions
        and master values for one device). Wire: ``POST /devices/{address}/reload``.
        """
        await self._transport.request(method="POST", path=f"/devices/{address}/reload")

    async def reload_channel_config(self, *, address: str, channel: int) -> None:
        """
        Re-pull a single channel's config from its CCU.

        Wire: ``POST /devices/{address}/channels/{channel}/reload``.
        """
        await self._transport.request(method="POST", path=f"/devices/{address}/channels/{channel}/reload")

    async def export_device_definition(self, *, address: str) -> bytes:
        """
        Export a device definition as an aiohomematic-compatible zip archive.

        Wire: ``GET /devices/{address}/export-definition``. Returns the raw
        archive bytes (the daemon assembles the same layout aiohomematic's
        ``export_device_definition`` writes).
        """
        return await self._transport.request_bytes(method="GET", path=f"/devices/{address}/export-definition")

    async def patch_device(self, *, address: str, name: str) -> None:
        """
        Update a device's mutable metadata (currently just name).

        Wire: ``PATCH /devices/{addr}``.
        """
        await self._transport.request(
            method="PATCH",
            path=f"/devices/{address}",
            json_body={"name": name},
            allow_retry=False,
        )

    async def delete_device(self, *, address: str) -> None:
        """
        Remove a device from the registry (admin operation).

        Wire: ``DELETE /devices/{addr}``.
        """
        await self._transport.request(method="DELETE", path=f"/devices/{address}")

    async def update_firmware(self, *, address: str) -> None:
        """
        Trigger an OTA firmware update for the device.

        Wire: ``POST /devices/{addr}/firmware/update``. Never retried —
        the CCU does not handle duplicate update requests gracefully, so
        a retry mid-flight can leave the radio in an undefined state.
        Callers that need at-most-once delivery across their own retries
        should pass an ``Idempotency-Key`` header via the transport.
        """
        await self._transport.request(
            method="POST",
            path=f"/devices/{address}/firmware/update",
            allow_retry=False,
        )

    async def accept_device(self, *, address: str) -> None:
        """
        Promote a pending pairing candidate into the registry.

        Wire: ``POST /devices/{addr}/accept``.
        """
        await self._transport.request(method="POST", path=f"/devices/{address}/accept", allow_retry=False)

    # ---- UI schema / config snapshots ----

    async def get_ui_schema(
        self,
        *,
        address: str,
        channel: int,
        paramset: str = "VALUES",
        peer: str | None = None,
        locale: str = "en",
        expert: bool | None = None,
    ) -> dict[str, Any]:
        """
        Return the renderable form descriptor for a channel paramset.

        Wire: ``GET /devices/{addr}/channels/{n}/ui-schema``.
        """
        params: dict[str, Any] = {"paramset": paramset, "locale": locale}
        if peer is not None:
            params["peer"] = peer
        if expert is not None:
            params["expert"] = expert
        payload = await self._transport.request(
            method="GET",
            path=f"/devices/{address}/channels/{channel}/ui-schema",
            params=params,
        )
        return dict(payload or {})

    async def export_channel_config(
        self, *, address: str, channel: int, paramset: str = "MASTER"
    ) -> ExportedConfiguration:
        """
        Export a channel paramset as a portable snapshot.

        Wire: ``GET /devices/{addr}/channels/{n}/config/export``.
        """
        payload = await self._transport.request(
            method="GET",
            path=f"/devices/{address}/channels/{channel}/config/export",
            params={"paramset": paramset},
        )
        return ExportedConfiguration.model_validate(payload)

    async def import_channel_config(self, *, address: str, channel: int, configuration: ExportedConfiguration) -> None:
        """
        Import a previously-exported paramset snapshot.

        Wire: ``POST /devices/{addr}/channels/{n}/config/import``.
        """
        await self._transport.request(
            method="POST",
            path=f"/devices/{address}/channels/{channel}/config/import",
            json_body=self._to_json_body(configuration),
            allow_retry=False,
        )
