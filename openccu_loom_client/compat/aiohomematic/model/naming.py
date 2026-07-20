# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
aiohomematic-schema display names for loom data points.

The HA integration renders ``dp.translated_name`` (device prefix
stripped) as the entity name.

Generic AND custom DPs need no composition here: the daemon is the
single naming authority (>= 0.45.0) and ships the fully composed name
in the wire ``translated_name`` — for generic DPs including the
ambiguity-gated `` chN`` multi-channel marker and the channel-level
collapse for label-omitted primary parameters, for custom DPs the
``ch<no>``/``vch<no>`` channel-group markers and button-lock postfix
labels. The compat layer renders both verbatim.

What remains here is the channel-derived display name for event
groups, which the wire does not carry; it strips the device-name
prefix exactly like aiohomematic's ``DataPointNameData`` so HA's
``has_entity_name`` rendering matches the reference backend.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from openccu_loom_client.model import Device
    from openccu_loom_client.store import LoomStore

_ADDRESS_SEPARATOR: Final = ":"


def has_channel_no_suffix(*, name: str) -> bool:
    """Return whether a channel name ends in ``:<int>`` (aiohomematic check)."""
    if name.count(_ADDRESS_SEPARATOR) == 1:
        try:
            int(name.split(_ADDRESS_SEPARATOR)[1])
        except ValueError:
            return False
        return True
    return False


def channel_base_name(*, store: LoomStore, device: Device, channel_no: int) -> str:
    """
    Return the display base name of one channel.

    A CCU-default channel name (``<model> <address>:<no>``) reads as
    ``<device name>:<no>`` — exactly aiohomematic's
    ``_get_base_name_from_channel_or_device``; a user-renamed channel
    keeps its full custom name.
    """
    channel = store.get_channel(address=device.address, number=channel_no)
    raw = channel.name if channel is not None else None
    default_name = f"{device.model} {device.address}{_ADDRESS_SEPARATOR}{channel_no}"
    if not raw or raw == default_name:
        return f"{device.name}{_ADDRESS_SEPARATOR}{channel_no}"
    return raw


def channel_display_name(*, store: LoomStore, device: Device, channel_no: int) -> str:
    """
    Return the channel-derived display name (event groups).

    Mirrors ``get_event_name``'s channel part as read through
    ``ChannelNameData``: a default-named channel renders ``ch<no>``
    (empty on channel 0), a user-renamed channel keeps its custom name
    minus the device-name prefix.
    """
    base = channel_base_name(store=store, device=device, channel_no=channel_no)
    if has_channel_no_suffix(name=base):
        return "" if channel_no == 0 else f"ch{channel_no}"
    name = base
    if device.name and name.startswith(device.name):
        name = name.replace(device.name, "").strip()
        if name.startswith(_ADDRESS_SEPARATOR):
            name = name[1:]
    return name.strip()


__all__ = [
    "channel_base_name",
    "channel_display_name",
    "has_channel_no_suffix",
]
