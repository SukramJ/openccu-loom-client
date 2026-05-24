# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Aiohomematic-compatible Protocol surface.

These ``typing.Protocol`` classes are pure type markers — the HA
integration uses them with ``isinstance`` checks to decide which
platform-side wrapper to spawn for a given data-point. The
implementations live in ``openccu_loom_client.model.*``; we list the
relevant methods/properties here so type checkers can resolve them.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class DeviceProtocol(Protocol):
    """Minimum surface of a device that HA-side code touches."""

    @property
    def address(self) -> str: ...
    @property
    def name(self) -> str: ...
    @property
    def model(self) -> str: ...
    @property
    def available(self) -> bool: ...


@runtime_checkable
class ChannelEventGroupProtocol(Protocol):
    """Channel + the set of trigger events it can emit."""

    @property
    def address(self) -> str: ...
    @property
    def number(self) -> int: ...


@runtime_checkable
class CombinedDataPointProtocol(Protocol):
    """Data-point variants that aggregate multiple wire DPs into one (HA-side number, etc.)."""

    @property
    def value(self) -> Any: ...
    @property
    def min(self) -> Any: ...
    @property
    def max(self) -> Any: ...


@runtime_checkable
class ClimateWeekProfileDataPointProtocol(Protocol):
    """Climate channel that owns a week-schedule."""

    @property
    def device_address(self) -> str: ...
    @property
    def channel_number(self) -> int: ...


@runtime_checkable
class ScheduleChannelSwitchProtocol(Protocol):
    """Switch-style channel that can enable/disable its weekly schedule."""

    @property
    def device_address(self) -> str: ...
    @property
    def channel_number(self) -> int: ...


__all__ = [
    "ChannelEventGroupProtocol",
    "ClimateWeekProfileDataPointProtocol",
    "CombinedDataPointProtocol",
    "DeviceProtocol",
    "ScheduleChannelSwitchProtocol",
]
