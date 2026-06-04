# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Constants + enums — surface that ``aiohomematic.const`` used to expose.

The values come from ``openccu_loom_types.enums`` where possible (so
the wire contract stays the single source of truth). The few aliases
that aren't enums (config-key strings, regex patterns, scalar
defaults) are declared here verbatim — they shadow whatever default
aiohomematic chose, but the daemon is the authoritative side now so
the client-side defaults only matter for the HA-config-flow shape.
"""

from __future__ import annotations

import re
from typing import Final

# ---- enum re-exports (single source of truth: openccu_loom_types) ----
from openccu_loom_types.enums import (
    Backend,
    CacheType,
    CalculatedParameter,
    CallSource,
    CCUType,
    CentralState,
    ClientState,
    DataPointCategory,
    DataPointType,
    DataPointUsage,
    DescriptionMarker,
    DeviceFirmwareState,
    DeviceProfile,
    DeviceTriggerEventType,
    FailureReason,
    Field,
    ForcedDeviceAvailability,
    HubValueType,
    IncidentSeverity,
    IncidentType,
    IntegrationIssueSeverity,
    IntegrationIssueType,
    Interface,
    Manufacturer,
    OptionalSettings,
    Parameter,
    ParameterStatus,
    ParameterType,
    ParamsetKey,
    ProductGroup,
    ProgramTrigger,
)

# ---- config-key constants ----

CONF_USERNAME: Final = "username"
CONF_PASSWORD: Final = "password"
CONF_HOST: Final = "host"
CONF_PORT: Final = "port"

# ---- address / identifier conventions ----

# Aiohomematic's wire-side conventions. The daemon's REST surface
# follows the same address shape (``ABC1234567:CHANNEL``), so we
# can reuse these patterns verbatim for HA-side validation.
IDENTIFIER_SEPARATOR: Final = "@"
DEVICE_ADDRESS_PATTERN: Final = re.compile(r"^[A-Z]{3}[0-9A-F]+$")
CHANNEL_ADDRESS_PATTERN: Final = re.compile(r"^[A-Z]{3}[0-9A-F]+:\d+$")

# ---- defaults: the daemon makes these decisions now, but the HA
# config-flow still exposes them as user-tunable knobs and reads
# the defaults from this module. The values here are the minimum
# set homematicip_local references.

DEFAULT_MULTIPLIER: Final = 1.0
DEFAULT_ENABLE_PROGRAM_SCAN: Final = True
DEFAULT_ENABLE_SYSVAR_SCAN: Final = True
DEFAULT_USE_GROUP_CHANNEL_FOR_COVER_STATE: Final = False
DEFAULT_INTERFACES_REQUIRING_PERIODIC_REFRESH: Final[frozenset[str]] = frozenset()
DEFAULT_OPTIONAL_SETTINGS: Final[frozenset[str]] = frozenset()
DEFAULT_PROGRAM_MARKERS: Final[tuple[str, ...]] = ()
DEFAULT_SYSVAR_MARKERS: Final[tuple[str, ...]] = ()
DEFAULT_UN_IGNORES: Final[tuple[str, ...]] = ()

# Listen-on/port sentinels — the daemon owns the actual binding, so
# these are mostly placeholders for HA-side validation logic that
# previously compared against the aiohomematic defaults.
IP_ANY_V4: Final = "0.0.0.0"  # nosec B104 — sentinel only; the daemon owns the actual binding
PORT_ANY: Final = 0

# ---- HA-categorisation helpers ----

# Aiohomematic exposes a tuple of every DP category HA exposes a
# platform for. We mirror it from the enum's members so it stays
# in sync without manual maintenance.
CATEGORIES: Final = tuple(DataPointCategory)

# Click + DP event subtypes the daemon emits as DeviceTriggerEventType.
# Aiohomematic split keypress into Short/Long/Double/etc.; the daemon
# collapses these into one ``Keypress`` value and carries the subtype
# inside the event payload itself, so the wire surface is leaner.
CLICK_EVENTS: Final = (DeviceTriggerEventType.Keypress,)
DATA_POINT_EVENTS: Final = (
    DeviceTriggerEventType.Impulse,
    DeviceTriggerEventType.DeviceError,
)

# Sysvar state-path root used in MQTT topic generation.
SYSVAR_STATE_PATH_ROOT: Final = "sysvar"


# ---- schedule helpers ----


class ScheduleProfile:
    """Placeholder for aiohomematic's ScheduleProfile enum.

    The daemon owns the schedule semantic; HA only needs the
    profile-id strings (``P1`` … ``P6``). Materialise them here so
    HA-side service-call validators keep working.
    """

    P1: Final = "P1"
    P2: Final = "P2"
    P3: Final = "P3"
    P4: Final = "P4"
    P5: Final = "P5"
    P6: Final = "P6"


class WeekdayStr:
    """Weekday-string constants used by schedule services."""

    MONDAY: Final = "MONDAY"
    TUESDAY: Final = "TUESDAY"
    WEDNESDAY: Final = "WEDNESDAY"
    THURSDAY: Final = "THURSDAY"
    FRIDAY: Final = "FRIDAY"
    SATURDAY: Final = "SATURDAY"
    SUNDAY: Final = "SUNDAY"


# ---- runtime config dataclasses ----


class ScheduleTimerConfig:
    """Per-config-entry scheduling tunable (HA-only, daemon doesn't see this)."""

    def __init__(self, *, sys_scan_interval: int = 300) -> None:
        self.sys_scan_interval = sys_scan_interval


class TimeoutConfig:
    """Per-config-entry network timeouts. Forwarded to LoomConfig in the cutover."""

    def __init__(
        self,
        *,
        command_retry_max_attempts: int = 3,
        command_throttle_interval: float = 0.0,
    ) -> None:
        self.command_retry_max_attempts = command_retry_max_attempts
        self.command_throttle_interval = command_throttle_interval


class SystemInformation:
    """Stand-in for aiohomematic's system info bundle."""

    def __init__(
        self,
        *,
        serial: str | None = None,
        version: str | None = None,
        available_interfaces: tuple[str, ...] = (),
    ) -> None:
        self.serial = serial
        self.version = version
        self.available_interfaces = available_interfaces


# ---- helper: default port per interface ----

# Aiohomematic-era defaults — the daemon binds however its config
# tells it to, but homematicip_local's config-flow uses these to
# pre-fill the per-interface port field. Keyed on the daemon's
# Interface-enum string values (``HmIP-RF``, ``BidCos-RF``, …).
_DEFAULT_PORTS: Final[dict[Interface, tuple[int, int]]] = {
    Interface.HmIPRF: (2010, 42010),
    Interface.BidCosRF: (2001, 42001),
    Interface.BidCosWired: (2000, 42000),
    Interface.VirtualDevices: (9292, 9292),
}


def get_interface_default_port(*, interface: Interface, tls: bool = False) -> int:
    """Best-effort default port lookup, kept for HA-config-flow compat.

    Returns 0 when the interface family doesn't have a fixed default
    (CUxD, …). The daemon will surface its actual binding on
    ``GET /interfaces`` after the cutover.
    """
    entry = _DEFAULT_PORTS.get(interface)
    if entry is None:
        return 0
    return entry[1] if tls else entry[0]


__all__ = [
    "CATEGORIES",
    "CHANNEL_ADDRESS_PATTERN",
    "CLICK_EVENTS",
    "CONF_HOST",
    "CONF_PASSWORD",
    "CONF_PORT",
    "CONF_USERNAME",
    "DATA_POINT_EVENTS",
    "DEFAULT_ENABLE_PROGRAM_SCAN",
    "DEFAULT_ENABLE_SYSVAR_SCAN",
    "DEFAULT_INTERFACES_REQUIRING_PERIODIC_REFRESH",
    "DEFAULT_MULTIPLIER",
    "DEFAULT_OPTIONAL_SETTINGS",
    "DEFAULT_PROGRAM_MARKERS",
    "DEFAULT_SYSVAR_MARKERS",
    "DEFAULT_UN_IGNORES",
    "DEFAULT_USE_GROUP_CHANNEL_FOR_COVER_STATE",
    "DEVICE_ADDRESS_PATTERN",
    "IDENTIFIER_SEPARATOR",
    "IP_ANY_V4",
    "PORT_ANY",
    "SYSVAR_STATE_PATH_ROOT",
    "Backend",
    "CCUType",
    "CacheType",
    "CalculatedParameter",
    "CallSource",
    "CentralState",
    "ClientState",
    "DataPointCategory",
    "DataPointType",
    "DataPointUsage",
    "DescriptionMarker",
    "DeviceFirmwareState",
    "DeviceProfile",
    "DeviceTriggerEventType",
    "FailureReason",
    "Field",
    "ForcedDeviceAvailability",
    "HubValueType",
    "IncidentSeverity",
    "IncidentType",
    "IntegrationIssueSeverity",
    "IntegrationIssueType",
    "Interface",
    "Manufacturer",
    "OptionalSettings",
    "Parameter",
    "ParameterStatus",
    "ParameterType",
    "ParamsetKey",
    "ProductGroup",
    "ProgramTrigger",
    "ScheduleProfile",
    "ScheduleTimerConfig",
    "SystemInformation",
    "TimeoutConfig",
    "WeekdayStr",
    "get_interface_default_port",
]
