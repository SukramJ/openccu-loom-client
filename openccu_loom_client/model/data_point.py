# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""DataPoint domain model — wraps DataPointSummary."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from openccu_loom_types.rest import DataPointSummary, Operations, Source, UiHint

if TYPE_CHECKING:
    from openccu_loom_client.model.channel import Channel
    from openccu_loom_client.model.device import Device
    from openccu_loom_client.store import LoomStore


# Priority values accepted by the daemon's SetValueRequest.value.priority
# field (see openapi.yaml -> SetValueRequest). The client narrows the
# wire-side string enum here so type checkers can flag typos at call
# sites without importing from openccu_loom_types.rest.
SetValuePriority = str  # "critical" | "high" | "default" | "low"


class DataPoint:
    """
    Store-aware wrapper around one (device, channel, parameter) triple.

    The wire-side :class:`DataPointSummary` is mutable on this wrapper:
    when the daemon emits a ``datapoint.value_changed`` broadcast the
    store calls :meth:`_apply_value` which model-copies the summary
    with the new value. Consumers read the freshest value via
    :attr:`value` and never have to touch the summary.
    """

    __slots__ = (
        "_channel_number",
        "_device_address",
        "_store",
        "_summary",
    )

    def __init__(
        self,
        *,
        summary: DataPointSummary,
        device_address: str,
        channel_number: int,
        store: LoomStore,
    ) -> None:
        """Bind this data point to its summary, channel coordinates, and store."""
        self._summary = summary
        self._device_address = device_address
        self._channel_number = channel_number
        self._store = store

    # ---- raw view ----

    @property
    def summary(self) -> DataPointSummary:
        """Return the wire-side summary backing this data point."""
        return self._summary

    # ---- delegated properties ----

    @property
    def parameter(self) -> str:
        """Return the parameter name of this data point."""
        return self._summary.parameter

    @property
    def value(self) -> Any:
        """
        Return the current value of this data point.

        An unobserved data point (the daemon has never seen a wire value
        nor a cache entry for it) reads ``None`` so consumers render
        "unknown" — mirroring aiohomematic's ``NO_CACHE_ENTRY`` semantics.
        Passing through the wire default (0/False) would fabricate a
        plausible-looking measurement.
        """
        if self._summary.observed is False:
            return None
        return self._summary.value

    @property
    def parameter_label(self) -> str | None:
        """Return the human-readable parameter label, if any."""
        return self._summary.parameter_label

    @property
    def type(self) -> str | None:
        """Wire-side type token: BOOL / INTEGER / FLOAT / ENUM / STRING / ACTION."""
        return self._summary.type

    @property
    def unit(self) -> str | None:
        """Return the unit of this data point's value, if any."""
        return self._summary.unit

    @property
    def value_list(self) -> tuple[str, ...]:
        """Return the enum value list for this data point."""
        return tuple(self._summary.value_list or ())

    @property
    def min(self) -> Any:
        """Return the minimum allowed value of this data point."""
        return self._summary.min

    @property
    def max(self) -> Any:
        """Return the maximum allowed value of this data point."""
        return self._summary.max

    @property
    def default(self) -> Any:
        """Return the default value of this data point."""
        return self._summary.default

    @property
    def operations(self) -> Operations:
        """Return the read/write/event operations flags of this data point."""
        return self._summary.operations

    @property
    def is_readable(self) -> bool:
        """Return whether this data point supports the read operation."""
        return self._summary.operations.read

    @property
    def is_writable(self) -> bool:
        """Return whether this data point supports the write operation."""
        return self._summary.operations.write

    @property
    def emits_events(self) -> bool:
        """Return whether this data point emits value-changed events."""
        return self._summary.operations.event

    @property
    def source(self) -> Source | None:
        """Return the source of this data point's value, if known."""
        return self._summary.source

    @property
    def is_observed(self) -> bool:
        """True once any value (cache, live, or stale) was observed."""
        return self._summary.observed

    @property
    def ui_hint(self) -> UiHint | None:
        """Return the UI rendering hint for this data point, if any."""
        return self._summary.ui_hint

    # ---- identity / graph ----

    @property
    def device_address(self) -> str:
        """Return the address of the owning device."""
        return self._device_address

    @property
    def channel_number(self) -> int:
        """Return the channel number this data point belongs to."""
        return self._channel_number

    @property
    def channel_address(self) -> str:
        """Full channel address (``device:channel``)."""
        return f"{self._device_address}:{self._channel_number}"

    @property
    def device(self) -> Device | None:
        """Return the owning device from the store, or None if absent."""
        return self._store.get_device(address=self._device_address)

    @property
    def channel(self) -> Channel | None:
        """Return the owning channel from the store, or None if absent."""
        return self._store.get_channel(address=self._device_address, number=self._channel_number)

    # ---- actions ----

    async def send_value(
        self,
        *,
        value: Any,
        priority: SetValuePriority | None = None,
    ) -> None:
        """
        Write ``value`` back to the daemon (and onward to the CCU).

        Wire path: ``PUT /devices/{addr}/channels/{no}/data-points/{param}/value``
        with a :class:`SetValueRequest` body. The store owns the
        transport reference so this method stays a one-liner.
        """
        await self._store.set_value(
            address=self._device_address,
            channel=self._channel_number,
            parameter=self.parameter,
            value=value,
            priority=priority,
        )

    # ---- mutation hooks (called by the store) ----

    def _replace_summary(self, *, summary: DataPointSummary) -> None:
        """Replace the backing summary in place (called by the store)."""
        self._summary = summary

    def __repr__(self) -> str:
        """Return a debug representation including address and current value."""
        return f"DataPoint({self._device_address}:{self._channel_number}.{self.parameter} = {self.value!r})"
