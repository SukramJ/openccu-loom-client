# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
aiohomematic-schema display names for loom data points.

The HA integration renders ``dp.translated_name`` (device prefix
stripped) as the entity name.

* generic DPs need no composition here: the daemon is the single
  naming authority (>= 0.45.0) and ships the fully composed name —
  including the ambiguity-gated `` chN`` multi-channel marker and the
  channel-level collapse for label-omitted primary parameters — in the
  wire ``translated_name``. The compat layer renders it verbatim.
* custom DPs — ``get_custom_data_point_name``: the channel name with a
  ``ch<no>``/``vch<no>`` marker for primary/secondary channels of a
  channel group; the device's *only* primary channel collapses to the
  bare device name. Primary channels come from the daemon's
  ``ChannelSummary.is_custom_dp_primary`` marker (K1 — the daemon owns
  the device profile), falling back to the lowest same-category CDP
  channel when the daemon leaves it unmarked.

All helpers strip the device-name prefix exactly like aiohomematic's
``DataPointNameData`` so HA's ``has_entity_name`` rendering matches the
reference backend bit for bit.
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


def strip_device_prefix(*, name: str, device_name: str) -> str | None:
    """Strip the leading device name (``DataPointNameData`` semantics), ``None`` if empty."""
    if device_name and name.startswith(device_name):
        name = name[len(device_name) :].lstrip()
    return name or None


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


def custom_name_parts(
    *,
    store: LoomStore,
    device: Device,
    channel_no: int,
    category_token: str,
    postfix: str = "",
    ignore_multiple_channels_for_name: bool = False,
) -> tuple[str | None, str]:
    """
    Build a custom DP's ``(translated_name, parameter_name)`` pair.

    Mirrors aiohomematic's ``get_custom_data_point_name``:

    * the device's only primary channel (or a postfix-carrying CDP such
      as a button lock) renders the postfix alone — usually nothing, so
      the entity collapses to the device name;
    * grouped channels get a ``ch<no>`` (primary) / ``vch<no>``
      (secondary) marker;
    * a user-renamed channel (no ``:<int>`` suffix) keeps its name.
    """
    base = channel_base_name(store=store, device=device, channel_no=channel_no)
    device_name = device.name
    postfix_title = postfix.replace("_", " ").title() if postfix else ""
    if not has_channel_no_suffix(name=base):
        return strip_device_prefix(name=base, device_name=device_name), postfix_title
    c_name, no_token = base.split(_ADDRESS_SEPARATOR)
    fallback_channels = [
        cdp.summary.channel_no
        for cdp in store.custom_data_points_of(address=device.address)
        if str(getattr(cdp.category, "value", cdp.category) or "") == category_token
    ]
    # K1: the daemon owns the device profile and marks the primary CDP channel
    # (ChannelSummary.is_custom_dp_primary), replacing aiohomematic's
    # DeviceProfileRegistry. ``is_multi`` is whether the category has more than
    # one *primary* channel (the registry's len(configs) > 1), NOT the raw CDP
    # count. When the daemon leaves the channel unmarked (unknown profile), fall
    # back to treating the lowest same-category CDP channel as the sole primary.
    channel = store.get_channel(address=device.address, number=channel_no)
    primary_marker = channel.is_custom_dp_primary if channel is not None else None
    if primary_marker is None:
        sorted_channels = sorted(set(fallback_channels))
        is_primary = bool(sorted_channels) and channel_no == sorted_channels[0]
        is_multi = len(sorted_channels) > 1
    else:
        is_primary = primary_marker
        primary_channels = [
            ch
            for ch in set(fallback_channels)
            if (c := store.get_channel(address=device.address, number=ch)) is not None and c.is_custom_dp_primary
        ]
        is_multi = len(primary_channels) > 1
    if (is_primary and not is_multi) or ignore_multiple_channels_for_name:
        parameter_name = postfix_title
    else:
        marker = "ch" if is_primary else "vch"
        parameter_name = f"{marker}{no_token}"
    name = f"{c_name} {parameter_name}".strip() if parameter_name else c_name
    return strip_device_prefix(name=name, device_name=device_name), parameter_name


__all__ = [
    "channel_base_name",
    "channel_display_name",
    "custom_name_parts",
    "has_channel_no_suffix",
    "strip_device_prefix",
]
