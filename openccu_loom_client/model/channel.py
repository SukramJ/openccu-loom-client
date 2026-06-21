# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Channel domain model — wraps ChannelSummary."""

from __future__ import annotations

from typing import TYPE_CHECKING

from openccu_loom_types.rest import ChannelSummary

from openccu_loom_client.operations.devices import DevicesOperations

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
    def name(self) -> str | None:
        """Return the user-defined channel name, or ``None`` when unset."""
        return self._summary.name

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

    # ---- channel groups (HA sub-device split) ----

    @property
    def group_no(self) -> int | None:
        """
        Return the channel-group number, or ``None`` outside any group.

        Mirrors aiohomematic's ``Channel.group_no`` (the group master's
        channel number). The daemon omits the field for ungrouped
        channels, which this property maps to ``None``.
        """
        group_no = getattr(self._summary, "group_no", None)
        return group_no or None

    @property
    def is_group_master(self) -> bool:
        """Return whether this channel is the master of its channel group."""
        return bool(getattr(self._summary, "is_group_master", False))

    @property
    def is_in_multi_group(self) -> bool:
        """
        Return whether the channel's group has more than one member.

        Mirrors aiohomematic's ``Channel.is_in_multi_group`` — the HA
        integration uses it to decide whether a data point moves onto a
        sub-device of the parent device.
        """
        return bool(getattr(self._summary, "is_in_multi_group", False))

    @property
    def room(self) -> str | None:
        """
        Return the resolved room (group-master fallback applied), or ``None``.

        The daemon resolves the channel's single room and falls back to
        the group master's room — the same chain aiohomematic's
        ``Channel.room`` walks. Daemons older than api 1.6.0 omit the
        field; the property degrades to ``None`` (callers fall back to
        the device room).
        """
        return getattr(self._summary, "room", None) or None

    @property
    def group_master(self) -> GroupMasterView | None:
        """
        Return the aiohomematic-shaped view of the channel group's master.

        The HA integration reads ``channel.group_master.name`` /
        ``.room`` / ``.group_no`` to name and place the channel group's
        sub-device. ``name`` follows aiohomematic's
        ``ChannelNameData.channel_name`` semantics (device-name prefix
        stripped, default channel names reduce to the bare number), so
        sub-devices are named identically on both backends. Returns
        ``None`` when the channel is not part of a group or the master
        channel is not loaded.
        """
        group_no = self.group_no
        if group_no is None:
            return None
        master = (
            self if self.number == group_no else self._store.get_channel(address=self.device_address, number=group_no)
        )
        if master is None:
            return None
        return GroupMasterView(master=master)

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

    async def reload_channel_config(self) -> None:
        """Re-pull this channel's paramset descriptions and master values from the CCU."""
        transport = self._store.transport
        if transport is None:
            msg = "LoomStore has no transport bound — cannot reach device operations"
            raise RuntimeError(msg)
        await DevicesOperations(transport=transport).reload_channel_config(
            address=self.device_address, channel=self.number
        )

    def __repr__(self) -> str:
        """Return the debug representation."""
        return f"Channel(address={self.address!r}, number={self.number}, paramset_key={self.paramset_key!r})"


class GroupMasterView:
    """
    aiohomematic-shaped read view of a channel group's master channel.

    The HA integration's sub-device split consumes exactly three
    attributes of aiohomematic's master channel: ``group_no``, ``room``
    and ``name`` (the ``ChannelNameData.channel_name`` form). This view
    adapts the loom :class:`Channel` to that contract without changing
    the raw wire semantics of :attr:`Channel.name`.
    """

    __slots__ = ("_master",)

    def __init__(self, *, master: Channel) -> None:
        """Wrap the master channel."""
        self._master = master

    @property
    def group_no(self) -> int | None:
        """Return the group number (the master's channel number)."""
        return self._master.group_no

    @property
    def room(self) -> str | None:
        """Return the master channel's resolved room, or ``None``."""
        return self._master.room

    @property
    def name(self) -> str:
        """
        Return the master's display name in aiohomematic channel-name form.

        Reproduces ``_get_base_name_from_channel_or_device`` +
        ``ChannelNameData._get_channel_name``: a CCU-default channel
        name reads ``<device name>:<no>`` and reduces to the bare
        number after the device prefix is stripped; a user-assigned
        name loses a leading device-name prefix (and a left-over ``:``)
        but is otherwise kept verbatim.
        """
        master = self._master
        device = master.device
        device_name = device.name if device is not None else ""
        raw = master.name
        if device is not None:
            default_name = f"{device.model} {master.address}"
            if not raw or raw == default_name:
                raw = f"{device_name}:{master.number}"
        if not raw:
            return ""
        if device_name and raw.startswith(device_name):
            return raw.replace(device_name, "").strip().removeprefix(":")
        return raw.strip()
