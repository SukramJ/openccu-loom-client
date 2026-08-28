# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Device domain model — wraps DeviceSummary / DeviceDetail."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from openccu_loom_types.rest import DeviceAvailability, DeviceFirmware, DeviceSummary

from openccu_loom_client.model.device_client import DeviceClient
from openccu_loom_client.operations.devices import DevicesOperations

if TYPE_CHECKING:
    from collections.abc import Iterator
    from datetime import datetime

    from openccu_loom_client.model.channel import Channel
    from openccu_loom_client.model.data_point import DataPoint
    from openccu_loom_client.store import LoomStore


@dataclass(frozen=True, slots=True)
class AvailabilityInfo:
    """
    Bundled availability information for a device.

    Structural twin of aiohomematic's ``AvailabilityInfo`` (same member names).
    ``homematicip_local``'s CCU-dashboard views read ``.is_reachable`` off
    ``Device.availability``; the daemon's wire record spells the same fields in
    PascalCase. Declared here rather than imported so the *core* model stays free
    of aiohomematic internals — the compat layer is where that coupling belongs.
    """

    is_reachable: bool
    last_updated: datetime | None
    battery_level: float | None
    low_battery: bool | None
    signal_strength: int | None


class _ChannelsView:
    """
    Mapping-like view over a device's channels.

    Iterates the channels in number order (as the bare ``channels``
    property used to) and additionally resolves a channel by its full
    address so the HA integration's ``device.channels.get(channel_address)``
    keeps working on the loom backend.
    """

    __slots__ = ("_device_address", "_store")

    def __init__(self, *, store: LoomStore, device_address: str) -> None:
        """Bind the view to one device's channels in the owning store."""
        self._store = store
        self._device_address = device_address

    def __iter__(self) -> Iterator[Channel]:
        """Iterate this device's channels in number order."""
        return iter(self._store.channels_of(address=self._device_address))

    def get(self, channel_address: str, /) -> Channel | None:
        """Return the channel by full address (``ABC:1``), or ``None`` if absent (dict-like)."""
        _device, _, channel = channel_address.partition(":")
        if not channel:
            return None
        return self._store.get_channel(address=self._device_address, number=int(channel))


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
        "_client",
        "_firmware",
        "_forced_availability",
        "_store",
        "_summary",
    )

    def __init__(self, *, summary: DeviceSummary, store: LoomStore) -> None:
        """Wrap the given device summary against the owning store."""
        self._summary = summary
        self._store = store
        self._firmware: DeviceFirmware | None = None
        self._availability: DeviceAvailability | None = None
        self._client: DeviceClient | None = None
        self._forced_availability: Any = None

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
    def sub_model(self) -> str | None:
        """
        Return the device sub-model, or ``None``.

        Mirrors aiohomematic's ``Device.sub_model`` — the config panel's form
        generator takes it to pick model-variant-specific field metadata.
        """
        return self._summary.sub_model

    @property
    def icon(self) -> str | None:
        """
        Return the icon filename for the device model, or ``None``.

        Mirrors aiohomematic's ``Device.icon`` / ``DeviceIdentityProtocol.icon``.
        The daemon resolves the model→icon mapping server-side and ships the
        bare PNG filename on the device summary (empty when no artwork is
        known), so — unlike aiohomematic, which looks the model up in a
        bundled translation table — the client just surfaces it.
        """
        return self._summary.model_icon or None

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
        """Return whether the device is currently available (honouring a forced override)."""
        if self._forced_availability is not None:
            forced = str(getattr(self._forced_availability, "value", self._forced_availability)).upper()
            if "FALSE" in forced:
                return False
            if "TRUE" in forced:
                return True
        return self._summary.available

    @property
    def rooms(self) -> tuple[str, ...]:
        """Defensive tuple view so callers can't mutate the wire model."""
        return tuple(self._summary.rooms or ())

    @property
    def firmware_detail(self) -> DeviceFirmware | None:
        """Return the firmware detail record, or ``None`` until detail is attached."""
        return self._firmware

    @property
    def availability(self) -> AvailabilityInfo:
        """
        Return bundled availability information for the device.

        Mirrors aiohomematic's ``Device.availability`` — an ``AvailabilityInfo``
        record with snake_case members. The CCU dashboard's device-statistics and
        signal-quality views read ``.is_reachable`` off it; the daemon's wire
        record spells the same fields in PascalCase, and before the device detail
        is attached there is no record at all, so an unknown device degrades to
        "reachable if the summary says so".
        """
        detail = self._availability
        if detail is None:
            return AvailabilityInfo(
                is_reachable=self.available,
                last_updated=None,
                battery_level=None,
                low_battery=None,
                signal_strength=None,
            )
        return AvailabilityInfo(
            is_reachable=detail.IsReachable if detail.IsReachable is not None else self.available,
            last_updated=detail.LastUpdated,
            battery_level=detail.BatteryLevel,
            low_battery=detail.LowBattery,
            signal_strength=detail.SignalStrength,
        )

    @property
    def availability_detail(self) -> DeviceAvailability | None:
        """Return the raw wire availability record, or ``None`` until detail is attached."""
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
    def firmware_updatable(self) -> bool:
        """
        Return whether a firmware update can be applied to this device.

        Mirrors aiohomematic's ``Device.firmware_updatable`` — the CCU-dashboard
        firmware overview and the device statistics both gate on it. Prefers the
        daemon's firmware record; before the device detail is attached the
        summary's ``updatable`` + ``update_available`` pair carries the same
        verdict.
        """
        if self._firmware is not None and self._firmware.Updatable is not None:
            return bool(self._firmware.Updatable)
        return bool(self._summary.updatable and self._summary.update_available)

    async def update_firmware(self, *, refresh_after_update_intervals: tuple[int, ...] = ()) -> bool:
        """
        Start an OTA firmware update.

        Mirrors aiohomematic's ``Device.update_firmware`` (which the HA firmware
        overview calls and tests for a truthy result). The daemon owns the
        post-update refresh cadence, so ``refresh_after_update_intervals`` is
        accepted for signature parity and not scheduled client-side. The
        transport raises on a refusal, so reaching the return means the OTA was
        accepted.
        """
        await self._store.update_device_firmware(address=self.address)
        return True

    @property
    def update_status(self) -> str | None:
        """
        Return the daemon's derived firmware update status, or ``None`` (K3).

        One of ``up_to_date`` / ``update_available`` / ``installing``. The
        daemon collapses the raw, interface-specific CCU firmware phases
        (``DeriveDeviceUpdateStatus``), so the client no longer classifies
        the raw state tokens itself.
        """
        status = self._summary.update_status
        return status.value if status is not None else None

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
        """
        Return the device's room (aiohomematic ``Device.room`` semantics).

        The single assigned device room wins; with zero or several
        rooms the maintenance channel's resolved room (group-master
        fallback included, daemon api 1.6.0 ``room`` field) decides.
        ``None`` when neither source yields a unique room.
        """
        rooms = self.rooms
        if len(rooms) == 1:
            return rooms[0]
        maintenance = self._store.get_channel(address=self.address, number=0)
        if maintenance is not None:
            return maintenance.room
        return None

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
    def week_profile_data_point(self) -> Any:
        """Return the device's climate week-profile data point, or ``None`` if it has none."""
        return self._store.get_week_profile_data_point(address=self.address)

    @property
    def client(self) -> DeviceClient:
        """
        Return the aiohomematic-compatible interface client for this device.

        Lazily built against the store's transport; the HA integration's
        service handlers call ``device.client.set_value`` / ``get_paramset``
        / ``put_paramset`` / link operations through it.
        """
        if self._client is None:
            transport = self._store.transport
            if transport is None:
                msg = "LoomStore has no transport bound — cannot build a device client"
                raise RuntimeError(msg)
            self._client = DeviceClient(transport=transport, device_address=self.address)
        return self._client

    def set_forced_availability(self, *, forced_availability: Any) -> None:
        """
        Force the device's reported availability (aiohomematic parity).

        The daemon owns the measured availability, so this only overrides
        the locally reported :attr:`available`; ``FORCE_TRUE`` / ``FORCE_FALSE``
        pin the value, anything else (``NOT_SET``) clears the override.
        """
        self._forced_availability = forced_availability

    async def reload_device_config(self) -> None:
        """Re-pull this device's paramset descriptions and master values from the CCU."""
        await self._devices_ops().reload_device_config(address=self.address)

    async def export_device_definition(self) -> bytes:
        """Return an aiohomematic-compatible device-definition archive (raw zip bytes)."""
        return await self._devices_ops().export_device_definition(address=self.address)

    def _devices_ops(self) -> DevicesOperations:
        """Build the device-operations façade against the store's transport."""
        transport = self._store.transport
        if transport is None:
            msg = "LoomStore has no transport bound — cannot reach device operations"
            raise RuntimeError(msg)
        return DevicesOperations(transport=transport)

    # ---- graph navigation ----

    @property
    def channels(self) -> _ChannelsView:
        """Return a mapping-like view over this device's channels."""
        return _ChannelsView(store=self._store, device_address=self.address)

    def get_generic_data_point(
        self,
        *,
        channel_address: str | None = None,
        parameter: str | None = None,
        **_kwargs: Any,
    ) -> DataPoint | None:
        """
        Find one of the device's data points by parameter, or ``None``.

        Mirrors aiohomematic's ``Device.get_generic_data_point`` lookup surface.
        The CCU dashboard's signal-quality view calls it with ``parameter`` only
        (``RSSI_DEVICE`` / ``RSSI_PEER``, which sit on the maintenance channel),
        so an unqualified parameter is searched across the device's channels;
        ``channel_address`` narrows it to a single channel. The reference's
        ``paramset_key`` / ``state_path`` selectors are accepted for signature
        parity and ignored — the loom data point is keyed by channel+parameter
        and carries neither.
        """
        if parameter is None:
            return None
        if channel_address is not None:
            channel = self.get_channel(channel_address=channel_address)
            channels = [channel] if channel is not None else []
        else:
            channels = list(self.channels)
        for channel in channels:
            for data_point in channel.data_points:
                if data_point.parameter == parameter:
                    return data_point
        return None

    def get_channel(self, *, number: int | None = None, channel_address: str | None = None) -> Channel | None:
        """
        Return one channel by number, or by full channel address.

        ``channel_address`` is aiohomematic's lookup key (``Device.get_channel``
        takes ``"ABC1234567:3"``), which ``homematicip_local``'s config/link
        handlers pass straight through; ``number`` is the loom-internal form.
        A foreign or malformed address resolves to ``None``, like the reference's
        keyed dict lookup.
        """
        if channel_address is not None:
            address, _, channel = channel_address.partition(":")
            if address != self.address or not channel.isdigit():
                return None
            number = int(channel)
        if number is None:
            return None
        return self._store.get_channel(address=self.address, number=number)

    # ---- mutation hooks (called by the store) ----

    def _update_summary(self, *, summary: DeviceSummary) -> None:
        """Replace the wire summary in place when the daemon's view advances."""
        self._summary = summary

    def _attach_detail(
        self,
        *,
        firmware: DeviceFirmware | None,
        availability: DeviceAvailability | None,
    ) -> None:
        """Attach the detail-only firmware and availability records."""
        self._firmware = firmware
        self._availability = availability

    def __repr__(self) -> str:
        """Return the debug representation."""
        return f"Device(address={self.address!r}, model={self.model!r}, name={self.name!r})"
