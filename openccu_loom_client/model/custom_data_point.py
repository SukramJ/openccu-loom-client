# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
CustomDataPoint — daemon-side aggregated DP (cover, light, climate …).

The daemon collapses the multi-DP wiring (LEVEL + STATE for a switch,
LEVEL + TILT + STATE for a cover, …) into one *Custom Data Point*
keyed by a stable name per device. State arrives as a free-form dict
on the wire (``CustomDataPointStateChangedPayload.state``); operations
are invoked by path segment (``turn_on``, ``set_temperature``,
``open``, …) at ``POST /devices/{addr}/cdps/{name}/{operation}``.

This wrapper holds:

- ``summary`` — catalogue info from ``CustomDPSummary``
  (``supported_operations``, ``kind``, owning channels …)
- ``state`` — the current state dict, mutated in place when the store
  observes a ``CustomDataPointStateChangedPayload``.

The semantic logic (what ``state["level"]`` means, what params
``set_temperature`` takes) lives in the daemon's CDP definitions; this
wrapper is intentionally schema-agnostic so adding a new CDP kind
needs no Python change at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from openccu_loom_types.rest import CustomDPSummary

if TYPE_CHECKING:
    from openccu_loom_client.model.device import Device
    from openccu_loom_client.store import LoomStore


class CustomDataPoint:
    """Store-aware wrapper around one aggregated CDP on one device."""

    __slots__ = (
        "_device_address",
        "_state",
        "_store",
        "_summary",
    )

    def __init__(
        self,
        *,
        summary: CustomDPSummary,
        device_address: str,
        store: LoomStore,
        initial_state: dict[str, Any] | None = None,
    ) -> None:
        """Bind this custom data point to its summary, device, store, and initial state."""
        self._summary = summary
        self._device_address = device_address
        self._store = store
        self._state: dict[str, Any] = dict(initial_state or {})

    # ---- raw view ----

    @property
    def summary(self) -> CustomDPSummary:
        """Return the wire-side summary backing this custom data point."""
        return self._summary

    @property
    def state(self) -> dict[str, Any]:
        """
        Current aggregated state dict.

        Returned as a defensive copy so callers can't mutate the
        store's internal record. Updated in place by
        :meth:`LoomStore.apply_custom_data_point_state_changed`.
        """
        return dict(self._state)

    # ---- delegated properties ----

    @property
    def name(self) -> str:
        """Return the stable name of this custom data point."""
        return self._summary.name

    @property
    def kind(self) -> str | None:
        """CDP kind (e.g. ``"switch"``, ``"cover"``, ``"climate"``) — see daemon catalogue."""
        return self._summary.kind

    @property
    def category(self) -> str | None:
        """Return the category of this custom data point, if any."""
        return self._summary.category

    @property
    def device_address(self) -> str:
        """Return the address of the owning device."""
        return self._device_address

    @property
    def device(self) -> Device | None:
        """Return the owning device from the store, or None if absent."""
        return self._store.get_device(address=self._device_address)

    @property
    def supported_operations(self) -> tuple[str, ...]:
        """Return the operations supported by this custom data point."""
        return tuple(self._summary.supported_operations or ())

    # ---- actions ----

    async def invoke(
        self,
        *,
        operation: str,
        params: dict[str, Any] | None = None,
        priority: str | None = None,
    ) -> None:
        """
        Run one CDP operation.

        Wire: ``POST /devices/{addr}/cdps/{name}/{operation}`` with
        a :class:`CustomDPInvokeRequest` body.

        Operation names come from :attr:`supported_operations`; the
        daemon validates them and 422s on unknown operations. We let
        the caller pass any string so future daemon-side additions
        don't need a client release.
        """
        await self._store.invoke_custom_data_point(
            address=self._device_address,
            name=self.name,
            operation=operation,
            params=params,
            priority=priority,
        )

    # ---- mutation hooks (called by the store) ----

    def _replace_state(self, *, state: dict[str, Any]) -> None:
        """Replace the current state dict in place (called by the store)."""
        self._state = dict(state)

    def _replace_summary(self, *, summary: CustomDPSummary) -> None:
        """Replace the backing summary in place (called by the store)."""
        self._summary = summary

    def __repr__(self) -> str:
        """Return a debug representation including device, name, kind, and state keys."""
        return (
            f"CustomDataPoint(device={self._device_address!r}, name={self.name!r}, "
            f"kind={self.kind!r}, state_keys={sorted(self._state)!r})"
        )
