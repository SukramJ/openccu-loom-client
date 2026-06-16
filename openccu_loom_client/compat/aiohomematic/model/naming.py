# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
aiohomematic-schema display names for loom data points.

The HA integration renders ``dp.translated_name`` (device prefix
stripped) as the entity name, so the loom twins must build the same
strings aiohomematic's ``model/support.py`` produces:

* generic DPs — ``get_data_point_name_data``: the (possibly renamed)
  CCU channel name, the parameter translation (suppressed when the
  daemon marks the label omitted) and a `` chN`` postfix when the
  parameter lives on several channels of the device.
* custom DPs — ``get_custom_data_point_name``: the channel name with a
  ``ch<no>``/``vch<no>`` marker for primary/secondary channels of a
  channel group; the device's *only* primary channel collapses to the
  bare device name. Primary channels come from aiohomematic's
  ``DeviceProfileRegistry`` (the same source the ccu twin uses).

All helpers strip the device-name prefix exactly like aiohomematic's
``DataPointNameData`` so HA's ``has_entity_name`` rendering matches the
reference backend bit for bit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from aiohomematic.const import DataPointCategory as AioDataPointCategory
from aiohomematic.model.custom import DeviceProfileRegistry

if TYPE_CHECKING:
    from collections.abc import Iterable

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


def generic_translated_name(
    *,
    store: LoomStore,
    device: Device,
    channel_no: int,
    parameter: str,
    translation: str | None,
    label_omitted: bool,
) -> str | None:
    """
    Build a generic DP's translated display name (aiohomematic schema).

    ``translation`` is the daemon's locale-resolved entity label — it
    already carries the channel-name part of aiohomematic's
    ``get_data_point_name_data`` composition (a renamed channel ships
    e.g. "Messwertkanal Strom"), so only the `` chN`` multi-channel
    postfix is appended here. ``label_omitted`` marks the channel's
    primary parameter whose label is suppressed (CCU translation ``""``)
    — its name reduces to the channel name plus the postfix. The postfix
    applies when the parameter exists on several channels of the device
    (never on channel 0).
    """
    c_postfix = ""
    if channel_no != 0 and store.is_parameter_in_multiple_channels(address=device.address, parameter=parameter):
        c_postfix = f" ch{channel_no}"
    if label_omitted:
        base = channel_base_name(store=store, device=device, channel_no=channel_no)
        c_name = base.split(_ADDRESS_SEPARATOR)[0] if has_channel_no_suffix(name=base) else base
        name = f"{c_name}{c_postfix}".strip()
        return strip_device_prefix(name=name, device_name=device.name)
    if translation:
        # Some daemon builds already append the multi-channel marker —
        # never double it ("Eingangsspannung ch10" stays as-is).
        if c_postfix and translation.endswith(c_postfix.strip()):
            c_postfix = ""
        return strip_device_prefix(name=f"{translation}{c_postfix}".strip(), device_name=device.name)
    # No daemon translation: suppress the name instead of fabricating an
    # English parameter title in a localised deployment.
    return None


def _registry_channel_info(
    *,
    model: str,
    category_token: str,
    channel_no: int,
    fallback_channels: Iterable[int],
) -> tuple[bool, bool]:
    """
    Return ``(is_primary, is_multi_channel)`` for one CDP channel.

    Primary channels come from aiohomematic's ``DeviceProfileRegistry``
    (the configured channels per (model, category) — the exact source of
    the ccu twin's ``CDP_PRIMARY`` usage). Unknown models fall back to
    treating the lowest same-category CDP channel as primary.
    """
    channels: list[int] = []
    try:
        category = AioDataPointCategory(category_token)
    except ValueError:
        category = None
    if category is not None:
        for config in DeviceProfileRegistry.get_configs(model=model, category=category):
            channels.extend(ch for ch in config.channels if ch is not None)
    if channels:
        return channel_no in channels, len(channels) > 1
    fallback = sorted(set(fallback_channels))
    is_primary = bool(fallback) and channel_no == fallback[0]
    return is_primary, len(fallback) > 1


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
    is_primary, is_multi = _registry_channel_info(
        model=device.model,
        category_token=category_token,
        channel_no=channel_no,
        fallback_channels=fallback_channels,
    )
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
    "generic_translated_name",
    "has_channel_no_suffix",
    "strip_device_prefix",
]
