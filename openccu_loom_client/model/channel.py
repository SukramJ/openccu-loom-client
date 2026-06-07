# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Channel domain model — wraps ChannelSummary."""

from __future__ import annotations

from typing import TYPE_CHECKING

from openccu_loom_types.rest import ChannelSummary

if TYPE_CHECKING:
    from collections.abc import Iterator

    from openccu_loom_client.model.data_point import DataPoint
    from openccu_loom_client.model.device import Device
    from openccu_loom_client.store import LoomStore


class Channel:
    """Store-aware wrapper around one channel of one device."""

    __slots__ = ("_store", "_summary")

    def __init__(self, *, summary: ChannelSummary, store: LoomStore) -> None:
        """Wrap the given channel summary against the owning store."""
        self._summary = summary
        self._store = store

    @property
    def summary(self) -> ChannelSummary:
        """Return the underlying wire-side summary record."""
        return self._summary

    @property
    def address(self) -> str:
        """The full channel address, e.g. ``"VCU0001:1"``."""
        return self._summary.address

    @property
    def number(self) -> int:
        """Return the channel number."""
        return self._summary.number

    @property
    def paramset_key(self) -> str:
        """Return the canonical (input) paramset key of this channel."""
        return self._summary.paramset_key

    @property
    def paramset_keys(self) -> tuple[str, ...]:
        """Return the paramsets this channel exposes (e.g. ``VALUES``, ``MASTER``)."""
        return tuple(self._summary.paramset_keys or ())

    @property
    def channel_type(self) -> str | None:
        """Return the OCCU channel-type string, or ``None`` if unset."""
        return self._summary.type

    @property
    def type_label(self) -> str | None:
        """Return the localised channel-type label, or ``None`` if unset."""
        return self._summary.type_label

    @property
    def custom_dp_name(self) -> str | None:
        """Return the custom data-point name, or ``None`` if unset."""
        return self._summary.custom_dp_name

    # ---- graph navigation ----

    @property
    def device_address(self) -> str:
        """The owning device's address (channel-address minus ``:N``)."""
        return self._summary.address.split(":", 1)[0]

    @property
    def device(self) -> Device | None:
        """Return the parent Device, if it's loaded in the store."""
        return self._store.get_device(address=self.device_address)

    @property
    def data_points(self) -> Iterator[DataPoint]:
        """Iterate this channel's data points."""
        return iter(
            self._store.data_points_of(
                address=self.device_address,
                channel=self.number,
            )
        )

    def get_data_point(self, *, parameter: str) -> DataPoint | None:
        """Return one data point by parameter name, or ``None`` if absent."""
        return self._store.get_data_point(
            address=self.device_address,
            channel=self.number,
            parameter=parameter,
        )

    def __repr__(self) -> str:
        """Return the debug representation."""
        return (
            f"Channel(address={self.address!r}, number={self.number}, "
            f"paramset_key={self.paramset_key!r})"
        )
