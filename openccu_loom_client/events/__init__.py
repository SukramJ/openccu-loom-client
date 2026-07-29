# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Typed events emitted by the openccu-loom daemon over WebSocket.

Each ``WsEnvelope`` arriving on the wire is converted into a concrete
:class:`LoomEvent` subclass via :func:`event_from_envelope`. Consumers
subscribe to a specific subclass on the :class:`EventBus` and receive
only matching events — typed all the way through.

Unknown ``type`` strings (forward-compatibility with future daemon
versions) become :class:`UnknownLoomEvent` so the stream never drops
on the floor.
"""

from __future__ import annotations

from openccu_loom_client.events.bus import EventBus, SubscriptionGroup, UnsubscribeCallback
from openccu_loom_client.events.synthetic import (
    DataPointsCreatedEvent,
    OptimisticRollbackEvent,
    new_data_points_created_event,
    new_optimistic_rollback_event,
)
from openccu_loom_client.events.types import (
    AddonUpdateStateChangedEvent,
    AlarmCountdownEvent,
    AlarmHealthChangedEvent,
    AlarmJournalAppendedEvent,
    AlarmNotificationEvent,
    AlarmPanelChangedEvent,
    AlarmReadinessChangedEvent,
    AlarmReminderEvent,
    AlarmStateChangedEvent,
    AlarmTriggeredEvent,
    AlarmWalkTestProgressEvent,
    CentralReadinessChangedEvent,
    CentralStateChangedEvent,
    CustomDataPointStateChangedEvent,
    DataPointValueChangedEvent,
    DeviceCreatedEvent,
    DeviceRemovedEvent,
    HubAlarmMessageCountChangedEvent,
    HubConnectivityChangedEvent,
    HubInboxChangedEvent,
    HubMetricsChangedEvent,
    HubServiceMessageCountChangedEvent,
    HubSystemUpdateChangedEvent,
    InstallModeChangedEvent,
    LoomEvent,
    MatterCommissioningProgressEvent,
    MatterCommissioningWindowOpenedEvent,
    MatterEndpointAssembledEvent,
    MatterExposableChangedEvent,
    MatterFabricAddedEvent,
    MatterFabricRemovedEvent,
    ProgramExecutedEvent,
    SystemStatusChangedEvent,
    SysvarChangedEvent,
    UnknownLoomEvent,
    event_from_envelope,
)

__all__ = [
    # General
    "AddonUpdateStateChangedEvent",
    "AlarmCountdownEvent",
    "AlarmHealthChangedEvent",
    "AlarmJournalAppendedEvent",
    "AlarmNotificationEvent",
    "AlarmPanelChangedEvent",
    "AlarmReadinessChangedEvent",
    "AlarmReminderEvent",
    "AlarmStateChangedEvent",
    "AlarmTriggeredEvent",
    "AlarmWalkTestProgressEvent",
    "CentralReadinessChangedEvent",
    "CentralStateChangedEvent",
    "CustomDataPointStateChangedEvent",
    "DataPointValueChangedEvent",
    "DataPointsCreatedEvent",
    "DeviceCreatedEvent",
    "DeviceRemovedEvent",
    "EventBus",
    "HubAlarmMessageCountChangedEvent",
    "HubConnectivityChangedEvent",
    "HubInboxChangedEvent",
    "HubMetricsChangedEvent",
    "HubServiceMessageCountChangedEvent",
    "HubSystemUpdateChangedEvent",
    "InstallModeChangedEvent",
    "LoomEvent",
    "MatterCommissioningProgressEvent",
    "MatterCommissioningWindowOpenedEvent",
    "MatterEndpointAssembledEvent",
    "MatterExposableChangedEvent",
    "MatterFabricAddedEvent",
    "MatterFabricRemovedEvent",
    "OptimisticRollbackEvent",
    "ProgramExecutedEvent",
    "SubscriptionGroup",
    "SystemStatusChangedEvent",
    "SysvarChangedEvent",
    "UnknownLoomEvent",
    "UnsubscribeCallback",
    "event_from_envelope",
    "new_data_points_created_event",
    "new_optimistic_rollback_event",
]
