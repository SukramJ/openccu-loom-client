# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Glue layer: WebSocket events → LoomStore + EventBus.

The transport raises typed :class:`LoomEvent` objects via the event
bus; the store has ``apply_*`` methods that mutate the in-memory
graph. This module is the small piece in between that subscribes to
the relevant event types and forwards them to the store.

Kept separate from both store and bus so:

- The store can be unit-tested without a bus.
- The bus can be unit-tested without a store.
- The high-level :class:`LoomClient` wires this glue at construction
  time and tears it down at close.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from openccu_loom_client.events import (
    AlarmCountdownEvent,
    AlarmHealthChangedEvent,
    AlarmPanelChangedEvent,
    AlarmReadinessChangedEvent,
    AlarmStateChangedEvent,
    AlarmTriggeredEvent,
    CustomDataPointStateChangedEvent,
    DataPointValueChangedEvent,
    DeviceAvailabilityChangedEvent,
    DeviceCreatedEvent,
    DeviceRemovedEvent,
    ProgramChangedEvent,
    ProgramExecutedEvent,
    SysvarChangedEvent,
)

if TYPE_CHECKING:
    from openccu_loom_client.events import EventBus, SubscriptionGroup
    from openccu_loom_client.store import LoomStore


def bind_ws_events_to_store(
    *,
    bus: EventBus,
    store: LoomStore,
    group: SubscriptionGroup,
) -> None:
    """
    Subscribe the standard wire→store handlers on ``group``.

    These subscriptions are installed:

    - ``DataPointValueChangedEvent`` → ``store.apply_value_changed``
    - ``CustomDataPointStateChangedEvent`` →
      ``store.apply_custom_data_point_state_changed``
    - ``DeviceCreatedEvent`` → ``store.apply_device_created``
    - ``DeviceRemovedEvent`` → ``store.apply_device_removed``
    - ``DeviceAvailabilityChangedEvent`` →
      ``store.apply_device_availability_changed``
    - ``SysvarChangedEvent`` → ``store.apply_sysvar_changed``
    - ``ProgramExecutedEvent`` → ``store.apply_program_executed``
    - ``ProgramChangedEvent`` → ``store.apply_program_changed``
    - the six ``alarm.*`` store mutations (panel/state/countdown/
      readiness/triggered/health) → ``store.apply_alarm_*``

    All of them are scoped to the supplied :class:`SubscriptionGroup`
    so the high-level client can tear them down with a single
    ``group.cancel()`` on close.

    The group is the caller's choice — typically one per LoomClient
    instance. This bridge wires the store-mutating events only; events
    that translate into HA-facing signals (e.g. the daemon's
    OptimisticRollback broadcast) are bound in the compat refresh
    bridge, not here. The follow-up reconcile for ``device.created`` is
    owned by :class:`~openccu_loom_client.client.LoomClient` (it needs
    the bus), so it subscribes its own handler after this binding.
    """

    async def on_value(event: DataPointValueChangedEvent) -> None:
        store.apply_value_changed(payload=event.payload)

    async def on_cdp_state(event: CustomDataPointStateChangedEvent) -> None:
        store.apply_custom_data_point_state_changed(payload=event.payload)

    async def on_created(event: DeviceCreatedEvent) -> None:
        store.apply_device_created(payload=event.payload)

    async def on_removed(event: DeviceRemovedEvent) -> None:
        store.apply_device_removed(payload=event.payload)

    async def on_availability(event: DeviceAvailabilityChangedEvent) -> None:
        store.apply_device_availability_changed(payload=event.payload)

    async def on_sysvar(event: SysvarChangedEvent) -> None:
        store.apply_sysvar_changed(payload=event.payload)

    async def on_program(event: ProgramExecutedEvent) -> None:
        store.apply_program_executed(payload=event.payload)

    async def on_program_changed(event: ProgramChangedEvent) -> None:
        store.apply_program_changed(payload=event.payload)

    async def on_alarm_panel(event: AlarmPanelChangedEvent) -> None:
        store.apply_alarm_panel_changed(payload=event.payload)

    async def on_alarm_state(event: AlarmStateChangedEvent) -> None:
        store.apply_alarm_state_changed(payload=event.payload)

    async def on_alarm_countdown(event: AlarmCountdownEvent) -> None:
        store.apply_alarm_countdown(payload=event.payload)

    async def on_alarm_readiness(event: AlarmReadinessChangedEvent) -> None:
        store.apply_alarm_readiness_changed(payload=event.payload)

    async def on_alarm_triggered(event: AlarmTriggeredEvent) -> None:
        store.apply_alarm_triggered(payload=event.payload)

    async def on_alarm_health(event: AlarmHealthChangedEvent) -> None:
        store.apply_alarm_health_changed(payload=event.payload)

    group.subscribe(event_type=DataPointValueChangedEvent, handler=on_value)
    group.subscribe(event_type=CustomDataPointStateChangedEvent, handler=on_cdp_state)
    group.subscribe(event_type=DeviceCreatedEvent, handler=on_created)
    group.subscribe(event_type=DeviceRemovedEvent, handler=on_removed)
    group.subscribe(event_type=DeviceAvailabilityChangedEvent, handler=on_availability)
    group.subscribe(event_type=SysvarChangedEvent, handler=on_sysvar)
    group.subscribe(event_type=ProgramExecutedEvent, handler=on_program)
    group.subscribe(event_type=ProgramChangedEvent, handler=on_program_changed)
    group.subscribe(event_type=AlarmPanelChangedEvent, handler=on_alarm_panel)
    group.subscribe(event_type=AlarmStateChangedEvent, handler=on_alarm_state)
    group.subscribe(event_type=AlarmCountdownEvent, handler=on_alarm_countdown)
    group.subscribe(event_type=AlarmReadinessChangedEvent, handler=on_alarm_readiness)
    group.subscribe(event_type=AlarmTriggeredEvent, handler=on_alarm_triggered)
    group.subscribe(event_type=AlarmHealthChangedEvent, handler=on_alarm_health)
