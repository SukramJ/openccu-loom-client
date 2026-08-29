# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Operations on per-device aggregated Custom Data Points."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from openccu_loom_client.operations._base import _OperationsBase
from openccu_loom_client.wire.rest import CustomDPDetail, CustomDPSummary


class CustomDataPointsOperations(_OperationsBase):
    """
    Wraps the daemon's per-device CDP surface.

    The catalogue (which CDPs a device exposes, their supported
    operations) is fetched via :meth:`list_for_device`; the live
    state arrives on the WebSocket
    ``custom_data_point.state_changed`` broadcast.
    """

    async def list_for_device(self, *, address: str) -> list[CustomDPSummary]:
        """Wire: ``GET /devices/{addr}/cdps``."""
        return await self._request_list(method="GET", path=f"/devices/{address}/cdps", model=CustomDPSummary)

    async def get(self, *, address: str, name: str) -> CustomDPDetail:
        """
        Wire: ``GET /devices/{addr}/cdps/{name}``.

        Returns a *detail* record, not a summary. The endpoint answers with
        four fields — ``name``, ``category``, ``channel_no``, ``state`` — and
        not with the summary the list endpoint returns: no
        ``supported_operations``, no ``unique_id``, no ``capabilities``.

        This method used to validate ``CustomDPSummary`` and therefore raised
        against every real response. Nothing called it, so nothing noticed
        until the store began delegating to it in 2026.8.33.

        The model was hand-written for as long as the daemon declared no
        schema for this operation. It declares one as of api 7.24.0
        (SukramJ/openccu-loom#648), so ``CustomDPDetail`` is generated like
        everything else under ``wire/``.
        """
        payload = await self._transport.request(method="GET", path=f"/devices/{address}/cdps/{quote(name, safe='')}")
        return CustomDPDetail.model_validate(payload)

    async def invoke(
        self,
        *,
        address: str,
        name: str,
        operation: str,
        params: dict[str, Any] | None = None,
        priority: str | None = None,
    ) -> None:
        """
        Run one CDP operation by path-segment name.

        Wire: ``POST /devices/{addr}/cdps/{name}/{operation}`` with
        a :class:`CustomDPInvokeRequest` (``{params, priority}``).

        Operations are not retried by default — the same CDP call
        twice can cause the CCU to double-fire (e.g. ``open`` on a
        bistable cover). Callers that want retries for idempotent
        operations (``set_temperature``, etc.) can opt in via the
        underlying transport.
        """
        body: dict[str, Any] = {}
        if params is not None:
            body["params"] = params
        if priority is not None:
            body["priority"] = priority
        # Always send a JSON body, even an empty one: the daemon parses the
        # body strictly and answers a bodyless POST with 400 "Invalid JSON:
        # EOF", so `body or None` broke every operation that takes no
        # parameters — turn_on, a cover's open, a siren's stop. Nothing
        # noticed because nothing called this until the store started
        # delegating to it; the store had always sent `{}` and said why.
        await self._transport.request(
            method="POST",
            path=f"/devices/{address}/cdps/{quote(name, safe='')}/{quote(operation, safe='')}",
            json_body=body,
            allow_retry=False,
        )
