# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Data-point and paramset REST operations."""

from __future__ import annotations

from typing import Any

from openccu_loom_types.rest import Query, ValuesBatchRequest

from openccu_loom_client.operations._base import _OperationsBase


class DataPointsOperations(_OperationsBase):
    """Wraps the daemon's data-point + paramset REST surface."""

    # ---- single-value read / write ----

    async def set_value(
        self,
        *,
        address: str,
        channel: int,
        parameter: str,
        value: Any,
        priority: str | None = None,
    ) -> None:
        """
        Write one data-point value back to the CCU.

        Wire: ``PUT /devices/{addr}/channels/{n}/data-points/{param}/value``
        with a :class:`SetValueRequest`. Idempotent: the daemon
        serializes concurrent writes on the same DP.
        """
        body: dict[str, Any] = {"value": value}
        if priority is not None:
            body["priority"] = priority
        await self._transport.request(
            method="PUT",
            path=f"/devices/{address}/channels/{channel}/data-points/{parameter}/value",
            json_body=body,
            allow_retry=True,
        )

    # ---- bulk read ----

    async def batch_read(
        self,
        *,
        queries: list[tuple[str, int, str]],
    ) -> dict[tuple[str, int, str], Any]:
        """
        Read many data-points in one round-trip.

        Wire: ``POST /devices/values:batch`` with a
        :class:`ValuesBatchRequest`. Returns a dict keyed on the
        (address, channel, parameter) triple. Failures per item are
        carried in the per-result ``error`` field but raised as a
        single ``LoomHttpError`` only when the request as a whole
        rejected (e.g. auth).
        """
        body = ValuesBatchRequest.model_validate(
            {"queries": [Query(address=a, channel=c, parameter=p).model_dump() for (a, c, p) in queries]}
        )
        payload = await self._transport.request(
            method="POST",
            path="/devices/values:batch",
            json_body=body.model_dump(),
            # Reads are idempotent — let the transport retry on transient upstream errors.
            allow_retry=True,
        )
        # The daemon may wrap the items in a ``{"results": [...]}`` envelope
        # or return a bare list. Distinguish explicitly: a dict without
        # ``results`` must yield no items, not iterate its string keys.
        if isinstance(payload, dict):
            results = payload.get("results", [])
        elif isinstance(payload, list):
            results = payload
        else:
            results = []
        out: dict[tuple[str, int, str], Any] = {}
        for item in results:
            key = (item["address"], item["channel"], item["parameter"])
            # Surface a successful result's summary.value; failures
            # surface as the raw error string so the caller can decide.
            if item.get("error"):
                out[key] = {"error": item["error"]}
            elif item.get("summary") is not None:
                out[key] = item["summary"].get("value")
        return out

    # ---- paramset ----

    async def get_paramset(
        self,
        *,
        address: str,
        paramset_key: str,
    ) -> dict[str, Any]:
        """
        Read a whole paramset (VALUES / MASTER / LINK) for a channel.

        Wire: ``GET /devices/{addr}/paramsets/{key}``. The wire shape
        is a free-form dict keyed by parameter name; the daemon
        controls the typing per-parameter.
        """
        payload = await self._transport.request(
            method="GET",
            path=f"/devices/{address}/paramsets/{paramset_key}",
        )
        return dict(payload or {})

    async def put_paramset(
        self,
        *,
        address: str,
        paramset_key: str,
        values: dict[str, Any],
    ) -> None:
        """
        Write a paramset transactionally to the CCU.

        Wire: ``PUT /devices/{addr}/paramsets/{key}``. The daemon
        forwards the whole map as a single CCU call where the
        interface supports it — this is the bulk equivalent of
        :meth:`set_value` and the right path for multi-parameter
        atomic writes (cover position + tilt, light brightness +
        colour, …).
        """
        await self._transport.request(
            method="PUT",
            path=f"/devices/{address}/paramsets/{paramset_key}",
            json_body=values,
            allow_retry=True,
        )
