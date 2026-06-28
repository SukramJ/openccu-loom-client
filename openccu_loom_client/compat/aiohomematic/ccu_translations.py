# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Aiohomematic-compatible CCU-translation helpers.

In the original library this module mapped device models to icon names
from a bundled translation archive. The daemon now ships that mapping
server-side and resolves it per device (``DeviceSummary.model_icon``),
so this helper just folds the live device records into the same
process-wide, central-independent ``model → icon-filename`` lookup that
aiohomematic exposes through :func:`get_device_icon`.

The model→icon mapping is a pure function of the model on the daemon,
so a single accumulating map shared across every central is correct:
the same model always resolves to the same filename, re-registration is
idempotent, and tearing one central down must not evict another's
entries.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from openccu_loom_client.model import Device

# Process-wide ``model.lower() → icon filename`` map, mirroring
# aiohomematic's central-independent ``device_icons`` translation table.
_DEVICE_ICONS: dict[str, str] = {}


def register_device_icons(*, devices: Iterable[Device]) -> None:
    """
    Fold each device's daemon-resolved icon filename into the lookup.

    Reads ``Device.icon`` (the daemon's ``model_icon``) keyed by the
    lower-cased model. Models without artwork (icon ``None``) are
    skipped so :func:`get_device_icon` falls back to HA's default device
    icon — the same behaviour aiohomematic shows for an unknown model.
    """
    for device in devices:
        if icon := device.icon:
            _DEVICE_ICONS[device.model.lower()] = icon


def get_device_icon(*, model: str) -> str | None:
    """Return the icon filename for a device model, or ``None`` if unknown."""
    return _DEVICE_ICONS.get(model.lower())


def clear_device_icons() -> None:
    """Drop every registered icon mapping (test isolation)."""
    _DEVICE_ICONS.clear()


__all__ = ["clear_device_icons", "get_device_icon", "register_device_icons"]
