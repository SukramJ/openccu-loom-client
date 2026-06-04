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
    """Store-aware wrapper around one (device, channel, parameter) triple.

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
        self._summary = summary
        self._device_address = device_address
        self._channel_number = channel_number
        self._store = store

    # ---- raw view ----

    @property
    def summary(self) -> DataPointSummary:
        return self._summary

    # ---- delegated properties ----

    @property
    def parameter(self) -> str:
        return self._summary.parameter

    @property
    def value(self) -> Any:
        return self._summary.value

    @property
    def parameter_label(self) -> str | None:
        return self._summary.parameter_label

    @property
    def type(self) -> str | None:
        """Wire-side type token: BOOL / INTEGER / FLOAT / ENUM / STRING / ACTION."""
        return self._summary.type

    @property
    def unit(self) -> str | None:
        return self._summary.unit

    @property
    def value_list(self) -> tuple[str, ...]:
        return tuple(self._summary.value_list or ())

    @property
    def min(self) -> Any:
        return self._summary.min

    @property
    def max(self) -> Any:
        return self._summary.max

    @property
    def default(self) -> Any:
        return self._summary.default

    @property
    def operations(self) -> Operations:
        return self._summary.operations

    @property
    def is_readable(self) -> bool:
        return self._summary.operations.read

    @property
    def is_writable(self) -> bool:
        return self._summary.operations.write

    @property
    def emits_events(self) -> bool:
        return self._summary.operations.event

    @property
    def source(self) -> Source | None:
        return self._summary.source

    @property
    def is_observed(self) -> bool:
        """True once any value (cache, live, or stale) was observed."""
        return self._summary.observed

    @property
    def ui_hint(self) -> UiHint | None:
        return self._summary.ui_hint

    # ---- identity / graph ----

    @property
    def device_address(self) -> str:
        return self._device_address

    @property
    def channel_number(self) -> int:
        return self._channel_number

    @property
    def channel_address(self) -> str:
        """Full channel address (``device:channel``)."""
        return f"{self._device_address}:{self._channel_number}"

    @property
    def device(self) -> Device | None:
        return self._store.get_device(address=self._device_address)

    @property
    def channel(self) -> Channel | None:
        return self._store.get_channel(address=self._device_address, number=self._channel_number)

    # ---- actions ----

    async def send_value(
        self,
        value: Any,
        *,
        priority: SetValuePriority | None = None,
    ) -> None:
        """Write ``value`` back to the daemon (and onward to the CCU).

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

    def _replace_summary(self, summary: DataPointSummary) -> None:
        self._summary = summary

    def __repr__(self) -> str:
        return (
            f"DataPoint({self._device_address}:{self._channel_number}.{self.parameter}"
            f" = {self.value!r})"
        )
