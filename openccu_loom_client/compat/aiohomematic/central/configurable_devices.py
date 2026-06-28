# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
``get_configurable_devices`` result models + builder.

Mirrors aiohomematic's ``ConfigurableDevice`` / ``ConfigurableDeviceChannel``
/ ``MaintenanceData`` dataclasses field-for-field, so HA's config
websocket handler can ``dataclasses.asdict(d)`` them and the frontend
receives the identical JSON shape on the loom backend.

Built synchronously from the live :class:`LoomStore` (the daemon ships
channel ``type``/``type_label``/``paramset_keys`` and the device
``ise_id``/maintenance data points), so no per-channel REST round-trip is
needed. The MASTER-visibility refinement aiohomematic applies (advertise
MASTER only when it has visible, non-internal params) is skipped — it
would need an async paramset-description fetch per channel; advertising
the channel's actual paramset keys is a safe superset.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from openccu_loom_client.compat.aiohomematic.ccu_translations import register_device_icons

if TYPE_CHECKING:
    from openccu_loom_client.model import Device
    from openccu_loom_client.store import LoomStore

# CCU channel-0 maintenance parameter → MaintenanceData field.
_MAINTENANCE_PARAM_TO_FIELD: dict[str, str] = {
    "UNREACH": "unreach",
    "LOW_BAT": "low_bat",
    "LOWBAT": "low_bat",
    "RSSI_DEVICE": "rssi_device",
    "RSSI_PEER": "rssi_peer",
    "DUTY_CYCLE": "dutycycle",
    "DUTYCYCLE": "dutycycle",
    "CONFIG_PENDING": "config_pending",
}


@dataclass(frozen=True, slots=True)
class MaintenanceData:
    """Cached maintenance state from device channel 0."""

    unreach: bool | None = None
    low_bat: bool | None = None
    rssi_device: int | None = None
    rssi_peer: int | None = None
    dutycycle: bool | None = None
    config_pending: bool | None = None


@dataclass(frozen=True, slots=True)
class ConfigurableDeviceChannel:
    """Channel available for configuration with resolved labels."""

    address: str
    channel_type: str
    channel_type_label: str
    channel_name: str
    paramset_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ConfigurableDevice:
    """Device with configurable channels for the configuration UI."""

    address: str
    interface: str
    interface_id: str
    model: str
    model_description: str
    name: str
    firmware: str
    channels: tuple[ConfigurableDeviceChannel, ...]
    maintenance: MaintenanceData


def _maintenance_for(*, device: Device) -> MaintenanceData:
    channel = device.get_channel(number=0)
    if channel is None:
        return MaintenanceData()
    fields: dict[str, Any] = {}
    for dp in channel.data_points:
        field = _MAINTENANCE_PARAM_TO_FIELD.get(dp.parameter)
        if field is not None and dp.value is not None:
            fields[field] = dp.value
    return MaintenanceData(**fields)


def build_configurable_devices(*, store: LoomStore) -> tuple[ConfigurableDevice, ...]:
    """Build the configurable-device descriptors from the live store."""
    # HA's config panel pairs this call with ``get_device_icon(model=...)``
    # per device, so refresh the model→icon lookup from the live store here:
    # it keeps the map current (new/removed devices) with no extra round-trip.
    register_device_icons(devices=store.devices)
    out: list[ConfigurableDevice] = []
    for device in store.devices:
        channels: list[ConfigurableDeviceChannel] = []
        for channel in device.channels:
            keys = channel.paramset_keys
            if not keys:
                continue
            channel_type = channel.channel_type or ""
            channels.append(
                ConfigurableDeviceChannel(
                    address=channel.address,
                    channel_type=channel_type,
                    channel_type_label=channel.type_label or channel_type,
                    # ``name`` rides the wire but is not yet declared on the
                    # ChannelSummary schema, so read it defensively.
                    channel_name=getattr(channel.summary, "name", "") or "",
                    paramset_keys=keys,
                )
            )
        if not channels:
            continue
        out.append(
            ConfigurableDevice(
                address=device.address,
                interface=device.interface,
                interface_id=device.interface_id or "",
                model=device.model,
                model_description=getattr(device.summary, "model_label", "") or "",
                name=device.name,
                firmware=device.firmware,
                channels=tuple(channels),
                maintenance=_maintenance_for(device=device),
            )
        )
    return tuple(out)
