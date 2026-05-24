# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Sysvar domain model — wraps SysvarSummary."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from openccu_loom_types.rest import SysvarSummary

if TYPE_CHECKING:
    from openccu_loom_client.store import LoomStore


class Sysvar:
    """Store-aware wrapper around one CCU system variable.

    Sysvars are hub-level typed values exposed by the daemon —
    similar to data-points, but not tied to a specific device.
    HA surfaces them as sensors / switches / numbers depending on
    ``value_type``.
    """

    __slots__ = ("_store", "_summary")

    def __init__(self, *, summary: SysvarSummary, store: LoomStore) -> None:
        self._summary = summary
        self._store = store

    @property
    def summary(self) -> SysvarSummary:
        return self._summary

    @property
    def name(self) -> str:
        return self._summary.name

    @property
    def value(self) -> Any:
        return self._summary.value

    @property
    def value_type(self) -> str | None:
        """``BOOL`` / ``INTEGER`` / ``FLOAT`` / ``STRING`` / ``ENUM``."""
        return self._summary.value_type

    @property
    def unit(self) -> str | None:
        return self._summary.unit

    @property
    def description(self) -> str | None:
        return self._summary.description

    @property
    def value_list(self) -> tuple[str, ...]:
        return tuple(self._summary.value_list or ())

    @property
    def is_observed(self) -> bool:
        return self._summary.observed

    async def set_value(self, value: Any) -> None:
        """Write a new runtime value to this sysvar.

        Wire: ``PUT /sysvars/{name}`` via :meth:`LoomStore.set_sysvar`.
        """
        await self._store.set_sysvar(name=self.name, value=value)

    def _replace_summary(self, summary: SysvarSummary) -> None:
        self._summary = summary

    def __repr__(self) -> str:
        return f"Sysvar(name={self.name!r}, value={self.value!r}, type={self.value_type!r})"
