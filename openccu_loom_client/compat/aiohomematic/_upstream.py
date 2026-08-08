# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Single seam onto the ``aiohomematic`` internals the compat layer reuses.

The compat shim couples to ``aiohomematic`` *internals* — not a stable
public API (``aiohomematic-contract`` was withdrawn, upstream #3221). Routing
every such symbol through this one module gives that coupling a single,
grep-able surface: the place to reason about the ``aiohomematic<2026.7``
version bound, the protocol drift-guard
(``tests/compat/test_aiohomematic_protocol_parity.py``), and the eventual
selective-reuse rollback.

The one exception is the bit-identical routing-key contract — it stays
isolated in :mod:`openccu_loom_client.canonical` (``generate_unique_id`` &
friends), the documented routing seam, because it is the *critical* surface
and is consumed below the compat layer.

Consumers import their symbols from here, keeping their local names (some
alias the aiohomematic events/enums ``Aio*`` to disambiguate from the
identically-named loom wire types). Only the import *source* moves; provenance
stays explicit at the use site.
"""

from __future__ import annotations

from aiohomematic.async_support import Looper
from aiohomematic.central.coordinators.link import DeviceLink, LinkableChannel
from aiohomematic.central.events import (
    CentralStateChangedEvent,
    DataPointsCreatedEvent,
    DataPointStateChangedEvent,
    DeviceLifecycleEvent,
    DeviceLifecycleEventType,
    DeviceRemovedEvent,
    DeviceTriggerEvent,
    EventBus,
    OptimisticRollbackEvent,
)
from aiohomematic.central.health import CentralHealth
from aiohomematic.const import (
    HUB_ADDRESS,
    INSTALL_MODE_ADDRESS,
    PROGRAM_ADDRESS,
    SYSVAR_ADDRESS,
    AlarmMessageData,
    CallSource,
    CCUType,
    CentralState,
    ClientState,
    DataPointCategory,
    DataPointKey,
    DataPointUsage,
    DeviceTriggerEventType,
    InboxDeviceData,
    Interface,
    ParamsetKey,
    ScheduleField,
    ScheduleProfile,
    ServiceMessageData,
    SystemInformation,
    WeekdayStr,
)
from aiohomematic.model.custom import ClimateMode, ClimateProfile
from aiohomematic.model.schedule_models import TargetChannelInfo
from aiohomematic.parameter_tools import validate_paramset

__all__ = [
    "HUB_ADDRESS",
    "INSTALL_MODE_ADDRESS",
    "PROGRAM_ADDRESS",
    "SYSVAR_ADDRESS",
    "AlarmMessageData",
    "CCUType",
    "CallSource",
    "CentralHealth",
    "CentralState",
    "CentralStateChangedEvent",
    "ClientState",
    "ClimateMode",
    "ClimateProfile",
    "DataPointCategory",
    "DataPointKey",
    "DataPointStateChangedEvent",
    "DataPointUsage",
    "DataPointsCreatedEvent",
    "DeviceLifecycleEvent",
    "DeviceLifecycleEventType",
    "DeviceLink",
    "DeviceRemovedEvent",
    "DeviceTriggerEvent",
    "DeviceTriggerEventType",
    "EventBus",
    "InboxDeviceData",
    "Interface",
    "LinkableChannel",
    "Looper",
    "OptimisticRollbackEvent",
    "ParamsetKey",
    "ScheduleField",
    "ScheduleProfile",
    "ServiceMessageData",
    "SystemInformation",
    "TargetChannelInfo",
    "WeekdayStr",
    "validate_paramset",
]
