# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Device domain model — wraps DeviceSummary / DeviceDetail."""

from __future__ import annotations

from typing import TYPE_CHECKING

from openccu_loom_types.rest import (
    Availability,
    DeviceSummary,
    Firmware,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from openccu_loom_client.model.channel import Channel
    from openccu_loom_client.store import LoomStore


class Device:
    """Store-aware wrapper around one CCU device.

    Constructed from a :class:`DeviceSummary` (returned by the
    daemon's ``/snapshot`` or ``/devices`` endpoints). Channels and
    optional detail-only fields (firmware, availability) attach later
    when the caller fetches ``GET /devices/{addr}``.
    """

    __slots__ = (
        "_availability",
        "_firmware",
        "_store",
        "_summary",
    )

    def __init__(self, *, summary: DeviceSummary, store: LoomStore) -> None:
        self._summary = summary
        self._store = store
        self._firmware: Firmware | None = None
        self._availability: Availability | None = None

    # ---- raw view ----

    @property
    def summary(self) -> DeviceSummary:
        """The underlying wire-side summary record.

        Mutated in place when the daemon's view advances (e.g. ``name``
        changes via PATCH, ``available`` flips via lifecycle event).
        """
        return self._summary

    # ---- delegated properties ----

    @property
    def address(self) -> str:
        return self._summary.address

    @property
    def name(self) -> str:
        return self._summary.name

    @property
    def model(self) -> str:
        return self._summary.model

    @property
    def interface(self) -> str:
        return self._summary.interface

    @property
    def interface_id(self) -> str | None:
        return self._summary.interface_id

    @property
    def manufacturer(self) -> str | None:
        return self._summary.manufacturer

    @property
    def available(self) -> bool:
        return self._summary.available

    @property
    def rooms(self) -> tuple[str, ...]:
        """Defensive tuple view so callers can't mutate the wire model."""
        return tuple(self._summary.rooms or ())

    @property
    def firmware(self) -> Firmware | None:
        return self._firmware

    @property
    def availability(self) -> Availability | None:
        return self._availability

    # ---- graph navigation ----

    @property
    def channels(self) -> Iterator[Channel]:
        """Iterate this device's channels in number order."""
        return iter(self._store.channels_of(address=self.address))

    def get_channel(self, *, number: int) -> Channel | None:
        """Return one channel by number, or ``None`` if absent."""
        return self._store.get_channel(address=self.address, number=number)

    # ---- mutation hooks (called by the store) ----

    def _update_summary(self, summary: DeviceSummary) -> None:
        self._summary = summary

    def _attach_detail(
        self,
        *,
        firmware: Firmware | None,
        availability: Availability | None,
    ) -> None:
        self._firmware = firmware
        self._availability = availability

    def __repr__(self) -> str:
        return f"Device(address={self.address!r}, model={self.model!r}, name={self.name!r})"
