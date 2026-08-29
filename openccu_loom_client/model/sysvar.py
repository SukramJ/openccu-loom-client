# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Sysvar domain model — wraps SysvarSummary."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from openccu_loom_client.wire.rest import SysvarSummary

if TYPE_CHECKING:
    from openccu_loom_client.model.channel import Channel
    from openccu_loom_client.store import LoomStore


class Sysvar:
    """
    Store-aware wrapper around one CCU system variable.

    Sysvars are hub-level typed values exposed by the daemon —
    similar to data-points, but not tied to a specific device.
    HA surfaces them as sensors / switches / numbers depending on
    ``value_type``.
    """

    __slots__ = ("_store", "_summary")

    def __init__(self, *, summary: SysvarSummary, store: LoomStore) -> None:
        """Bind this wrapper to its wire summary and owning store."""
        self._summary = summary
        self._store = store

    @property
    def summary(self) -> SysvarSummary:
        """Return the backing wire summary."""
        return self._summary

    @property
    def name(self) -> str:
        """Return the sysvar's name."""
        return self._summary.name

    @property
    def value(self) -> Any:
        """Return the sysvar's current runtime value."""
        return self._summary.value

    @property
    def value_type(self) -> str | None:
        """``BOOL`` / ``INTEGER`` / ``FLOAT`` / ``STRING`` / ``ENUM``."""
        return self._summary.value_type

    @property
    def unit(self) -> str | None:
        """Return the value's unit, or ``None`` if dimensionless."""
        return self._summary.unit

    @property
    def description(self) -> str | None:
        """Return the sysvar's description, or ``None`` if unset."""
        return self._summary.description

    @property
    def value_list(self) -> tuple[str, ...]:
        """Return the enum value labels, empty for non-enum sysvars."""
        return tuple(self._summary.value_list or ())

    @property
    def is_observed(self) -> bool:
        """Return whether the daemon streams updates for this sysvar."""
        return self._summary.observed

    # ---- device attachment ----

    @property
    def channel_address(self) -> str | None:
        """
        Return the canonical ``"ADDR:idx"`` channel this sysvar is linked to.

        The daemon resolves the link from the explicit CCU WebUI channel
        assignment ("Kanalzuordnung") or, failing that, from a device
        identifier matched in the variable name. ``None`` means the
        variable belongs to no device — HA consumers then attach the
        entity to the central hub device instead of a physical device.
        """
        return self._summary.channel or None

    @property
    def device_address(self) -> str | None:
        """
        Return the device part of :attr:`channel_address` (before ``":"``).

        HA consumers use it to group the entity under the owning physical
        device; ``None`` together with :attr:`channel_address` (the entity
        belongs on the central hub device).
        """
        return self._summary.device_address or None

    @property
    def channel(self) -> Channel | None:
        """
        Return the linked :class:`Channel` from the store, or ``None``.

        ``None`` when the sysvar is not linked to a device (hub-level
        entity) or the linked channel is not loaded in the store.
        """
        if (channel_address := self.channel_address) is None:
            return None
        return self._store.get_channel_by_address(channel_address=channel_address)

    async def set_value(self, *, value: Any) -> None:
        """
        Write a new runtime value to this sysvar.

        Wire: ``PUT /sysvars/{name}`` via :meth:`LoomStore.set_sysvar`.
        """
        await self._store.set_sysvar(name=self.name, value=value)

    def _replace_summary(self, *, summary: SysvarSummary) -> None:
        self._summary = summary

    def __repr__(self) -> str:
        """Return a debug representation with name, value and type."""
        return f"Sysvar(name={self.name!r}, value={self.value!r}, type={self.value_type!r})"
