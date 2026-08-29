# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
``aiohomematic.model.update`` — per-device firmware-update data point.

Mirrors aiohomematic's ``DpUpdate``: one update data point per device,
keyed ``<address>_update`` (the ``Update`` pseudo-parameter), surfacing
installed / available firmware off the device record and driving the
daemon's ``POST /devices/{addr}/firmware/update``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar, Final

from openccu_loom_client.canonical import canonical_unique_id
from openccu_loom_client.wire.enums import DataPointCategory

if TYPE_CHECKING:
    from openccu_loom_client.model import Device
    from openccu_loom_client.store import LoomStore

_LOGGER: Final = logging.getLogger(__name__)


class DpUpdate:
    """Firmware-update data point for one device (aiohomematic ``DpUpdate`` twin)."""

    _category: ClassVar[DataPointCategory] = DataPointCategory.Update

    __slots__ = ("_device", "_registered", "_store")

    def __init__(self, *, device: Device, store: LoomStore) -> None:
        """Bind the update data point to its device and store."""
        self._device: Final = device
        self._store: Final = store
        self._registered = False

    @property
    def category(self) -> DataPointCategory:
        """Return the data-point category (``update``)."""
        return self._category

    @property
    def unique_id(self) -> str:
        """Return the canonical key ``loom_<address>_update``."""
        return canonical_unique_id(
            serial_suffix=self._store.serial_suffix,
            address=self._device.address,
            parameter="Update",
        )

    @property
    def device(self) -> Device:
        """Return the owning device."""
        return self._device

    @property
    def name(self) -> str:
        """Return the data-point name."""
        return "Update"

    @property
    def full_name(self) -> str:
        """Return the display name ``<device> Update``."""
        return f"{self._device.name} Update"

    @property
    def translation_key(self) -> str:
        """Return the HA translation key."""
        return "device_update"

    @property
    def available(self) -> bool:
        """Return the owning device's availability."""
        return bool(self._device.available)

    @property
    def firmware(self) -> str | None:
        """Return the installed firmware version."""
        return self._device.firmware

    @property
    def firmware_update_state(self) -> str | None:
        """Return the CCU's firmware-update state token."""
        return self._device.firmware_update_state

    @property
    def in_progress(self) -> bool:
        """Return whether a firmware update is currently installing (K3)."""
        # The daemon's derived update_status collapses the raw, interface-
        # specific CCU phases — consume it instead of classifying state tokens.
        return self._device.update_status == "installing"

    @property
    def latest_firmware(self) -> str | None:
        """
        Return the latest installable firmware (K3).

        The daemon reports ``update_available`` once an install is offered
        (collapsing the HmIP "ready state" vs. BidCos "direct" difference);
        otherwise fall back to the installed version so HA renders "up to date".
        """
        if self._device.update_status == "update_available":
            return self._device.available_firmware or self._device.firmware
        return self._device.firmware

    @property
    def is_registered(self) -> bool:
        """Return whether the entity has been registered with HA."""
        return self._registered

    def register(self) -> None:
        """Mark the entity as registered with HA."""
        self._registered = True

    def unregister(self) -> None:
        """Mark the entity as no longer registered with HA."""
        self._registered = False

    async def refresh_firmware_data(self) -> None:
        """Re-read the device's firmware record from the daemon."""
        await self._store.refresh_device(address=self._device.address)

    async def update_firmware(self, *, refresh_after_update_intervals: tuple[int, ...]) -> bool:
        """Trigger the OTA firmware update via the daemon."""
        del refresh_after_update_intervals  # the daemon owns the refresh cadence
        await self._store.update_device_firmware(address=self._device.address)
        return True


def make_update_data_point(*, device: Any, store: Any) -> DpUpdate:
    """Build the firmware-update data point for one device."""
    return DpUpdate(device=device, store=store)


__all__ = ["DpUpdate", "make_update_data_point"]
