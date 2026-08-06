# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Common ground for all operation modules: the transport handle + helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel

if TYPE_CHECKING:
    from openccu_loom_client.transport.http import HttpTransport

_ModelT = TypeVar("_ModelT", bound=BaseModel)


class _OperationsBase:
    """Holds the transport handle the concrete modules use, plus shared helpers."""

    __slots__ = ("_transport",)

    def __init__(self, *, transport: HttpTransport) -> None:
        self._transport = transport

    async def _request_list(
        self,
        *,
        method: str,
        path: str,
        model: type[_ModelT],
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
        allow_retry: bool | None = None,
    ) -> list[_ModelT]:
        """
        Run a request whose body is a JSON array and validate each item.

        Centralises the null-guard (a daemon returning ``null`` / a non-list
        for an empty collection then yields ``[]`` instead of crashing — the
        B7 fragility class) and the per-item ``model_validate`` the operation
        modules repeat ~20×.
        """
        payload = await self._transport.request(
            method=method, path=path, params=params, json_body=json_body, allow_retry=allow_retry
        )
        items = payload if isinstance(payload, list) else []
        return [model.model_validate(item) for item in items]

    @staticmethod
    def _to_json_body(model: BaseModel) -> Any:
        """
        Serialise a request model to a JSON-ready body (drops unset fields).

        ``by_alias=True`` is not cosmetic: the generator renames every
        field colliding with a Python keyword, so ``AlarmOutput``'s
        ``class`` becomes ``class_`` and ``EnergyResponse``'s ``from``
        becomes ``from_``. Dumping by field name sent the daemon
        ``{"class_": …}`` for a property its schema requires as
        ``class`` — the wire name is the alias, always.
        """
        return model.model_dump(mode="json", by_alias=True, exclude_none=True)
