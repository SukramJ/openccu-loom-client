# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Device domain model — wraps DeviceSummary / DeviceDetail."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

from openccu_loom_types.rest import Availability, DeviceSummary, Firmware

if TYPE_CHECKING:
    from collections.abc import Iterator

    from openccu_loom_client.model.channel import Channel
    from openccu_loom_client.store import LoomStore


class Device:
    """
    Store-aware wrapper around one CCU device.

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
        """Wrap the given device summary against the owning store."""
        self._summary = summary
        self._store = store
        self._firmware: Firmware | None = None
        self._availability: Availability | None = None

    # ---- raw view ----

    @property
    def summary(self) -> DeviceSummary:
        """
        The underlying wire-side summary record.

        Mutated in place when the daemon's view advances (e.g. ``name``
        changes via PATCH, ``available`` flips via lifecycle event).
        """
        return self._summary

    # ---- delegated properties ----

    @property
    def address(self) -> str:
        """Return the device address."""
        return self._summary.address

    @property
    def ise_id(self) -> int | None:
        """Return the CCU-internal numeric device id, or ``None`` if unset."""
        return self._summary.ise_id

    @property
    def name(self) -> str:
        """Return the device name."""
        return self._summary.name

    @property
    def model(self) -> str:
        """Return the device model."""
        return self._summary.model

    @property
    def interface(self) -> str:
        """Return the interface this device is reached through."""
        return self._summary.interface

    @property
    def interface_id(self) -> str | None:
        """Return the interface id, or ``None`` if unknown."""
        return self._summary.interface_id

    @property
    def manufacturer(self) -> str | None:
        """Return the manufacturer, or ``None`` if unknown."""
        return self._summary.manufacturer

    @property
    def available(self) -> bool:
        """Return whether the device is currently available."""
        return self._summary.available

    @property
    def rooms(self) -> tuple[str, ...]:
        """Defensive tuple view so callers can't mutate the wire model."""
        return tuple(self._summary.rooms or ())

    @property
    def firmware_detail(self) -> Firmware | None:
        """Return the firmware detail record, or ``None`` until detail is attached."""
        return self._firmware

    @property
    def availability(self) -> Availability | None:
        """Return the availability detail, or ``None`` until detail is attached."""
        return self._availability

    # ---- aiohomematic-compat surface (read by homematicip_local entities) ----

    @property
    def firmware(self) -> str:
        """Return the installed firmware version (aiohomematic exposes this as ``sw_version``)."""
        if self._firmware is not None and self._firmware.Current:
            return self._firmware.Current
        return "0.0"

    @property
    def identifier(self) -> str:
        """Return the HA device identifier (``address@interface_id``, matching aiohomematic)."""
        return f"{self.address}@{self.interface_id}"

    @property
    def available_firmware(self) -> str | None:
        """Return the firmware version available for install, or ``None``."""
        return self._firmware.Available if self._firmware is not None else None

    @property
    def firmware_update_state(self) -> str | None:
        """Return the CCU's firmware-update state token, or ``None``."""
        return self._firmware.UpdateState if self._firmware is not None else None

    @property
    def central_info(self) -> SimpleNamespace:
        """Return the owning central, exposing its HA-facing ``name`` (the via-device)."""
        return SimpleNamespace(name=self._store.central_name)

    @property
    def config_provider(self) -> SimpleNamespace:
        """
        Return a minimal aiohomematic-shaped config provider.

        The HA schedule entities read ``device.config_provider.config.locale``
        to translate the schedule name; the locale is the one the integration
        configured on the store (HA's UI language), defaulting to English.
        """
        return SimpleNamespace(config=SimpleNamespace(locale=self._store.locale))

    @property
    def room(self) -> str | None:
        """Return the single assigned room, or ``None`` unless exactly one is set."""
        rooms = self.rooms
        return rooms[0] if len(rooms) == 1 else None

    @property
    def has_sub_devices(self) -> bool:
        """
        Return whether the device splits into multiple sub-devices.

        Mirrors aiohomematic's ``Device.has_sub_devices``: ``False``
        with at most one channel group; otherwise ``True`` when at
        least two groups carry more than one member channel. Group
        membership comes from the daemon's per-channel ``group_no``
        (the same profile-derived grouping aiohomematic builds from
        ``DeviceProfileRegistry``).
        """
        group_sizes: dict[int, int] = {}
        for channel in self.channels:
            if (group_no := channel.group_no) is not None:
                group_sizes[group_no] = group_sizes.get(group_no, 0) + 1
        if len(group_sizes) <= 1:
            return False
        return sum(1 for size in group_sizes.values() if size > 1) > 1

    @property
    def model_description(self) -> str:
        """Return the human-readable model description (the model for the loom backend)."""
        return self.model

    @property
    def week_profile_data_point(self) -> None:
        """Return the device's climate week-profile data point (not modelled for loom)."""
        return None

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
        """Replace the wire summary in place when the daemon's view advances."""
        self._summary = summary

    def _attach_detail(
        self,
        *,
        firmware: Firmware | None,
        availability: Availability | None,
    ) -> None:
        """Attach the detail-only firmware and availability records."""
        self._firmware = firmware
        self._availability = availability

    def __repr__(self) -> str:
        """Return the debug representation."""
        return f"Device(address={self.address!r}, model={self.model!r}, name={self.name!r})"
