# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""In-memory pub/sub for typed LoomEvents.

The :class:`EventBus` is the single point that converts the WebSocket
event stream into application-level callbacks. It mirrors the
``aiohomematic`` event-bus contract so the cutover in
``homematicip_local`` is a one-line swap of the import path.

Two main classes:

- :class:`EventBus` — global registry of subscriptions keyed on event
  type + optional ``event_key``. ``publish()`` fans an event out to
  every matching handler sequentially; an exception in one handler is
  logged but does NOT stop the others from running.
- :class:`SubscriptionGroup` — convenience wrapper that tracks a set
  of subscriptions so a controller (HA config-entry, integration test,
  …) can cancel them all together when its lifecycle ends.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, TypeVar

if TYPE_CHECKING:
    from openccu_loom_client.events.types import LoomEvent

_LOGGER: Final = logging.getLogger(__name__)

_EventT = TypeVar("_EventT", bound="LoomEvent")

EventHandler = Callable[["LoomEvent"], Awaitable[None]]
"""Async callable invoked once per matching event."""

UnsubscribeCallback = Callable[[], None]
"""Idempotent zero-arg callable that removes one subscription."""


@dataclass(slots=True)
class _Subscription:
    """One registered handler — tracked in the bus and optionally by a group."""

    event_type: type[LoomEvent]
    event_key: str | None
    handler: EventHandler
    # Set to True by cancel paths so a fan-out in progress can skip
    # handlers whose group has already been torn down.
    cancelled: bool = False


def _event_key_of(event: LoomEvent) -> str | None:
    """Return the routing key for an event, if it has one.

    Convention: events expose an ``event_key`` attribute when they
    want to be filtered by something more specific than the type
    alone (typically the central name, device address, or sysvar
    name). When the attribute is absent or ``None`` the event
    matches every subscriber of its type regardless of
    ``event_key``.
    """
    return getattr(event, "event_key", None)


class EventBus:
    """Process-local pub/sub for :class:`LoomEvent` subclasses."""

    def __init__(self) -> None:
        # event_type → list of active subscriptions. We don't bother
        # with a per-(type, key) index — the daemon emits at most a
        # few thousand events/minute and the subscriber count per
        # type stays in the double digits.
        self._subs: dict[type[LoomEvent], list[_Subscription]] = {}

    def subscribe(
        self,
        *,
        event_type: type[_EventT],
        handler: Callable[[_EventT], Awaitable[None]],
        event_key: str | None = None,
    ) -> UnsubscribeCallback:
        """Register ``handler`` for events of exactly ``event_type``.

        If ``event_key`` is non-None the handler only fires when the
        event's own ``event_key`` (see :func:`_event_key_of`) equals
        the subscribed value. ``event_key=None`` matches every event
        of the type — equivalent to "give me all of them".

        Returns an idempotent ``unsubscribe`` callback. Subsequent
        calls are no-ops.
        """
        sub = _Subscription(
            event_type=event_type,
            event_key=event_key,
            handler=handler,  # type: ignore[arg-type]
        )
        self._subs.setdefault(event_type, []).append(sub)

        def _unsubscribe() -> None:
            if sub.cancelled:
                return
            sub.cancelled = True
            bucket = self._subs.get(event_type)
            if bucket is None:
                return
            with contextlib.suppress(ValueError):
                bucket.remove(sub)
            if not bucket:
                self._subs.pop(event_type, None)

        return _unsubscribe

    async def publish(self, event: LoomEvent) -> None:
        """Fan ``event`` out to all matching subscribers.

        Handlers run sequentially in registration order — same
        contract aiohomematic provides. An exception in one handler
        is logged but does not abort the fan-out.
        """
        bucket = self._subs.get(type(event))
        if not bucket:
            return
        evt_key = _event_key_of(event)
        # Snapshot the list so a handler that unsubscribes mid-fanout
        # doesn't reshape what we're iterating.
        for sub in list(bucket):
            if sub.cancelled:
                continue
            if sub.event_key is not None and sub.event_key != evt_key:
                continue
            try:
                await sub.handler(event)
            except Exception:
                _LOGGER.exception(
                    "event handler raised on %s (event_key=%r)",
                    type(event).__name__,
                    evt_key,
                )

    def create_subscription_group(self, *, name: str) -> SubscriptionGroup:
        """Build a new :class:`SubscriptionGroup` bound to this bus."""
        return SubscriptionGroup(bus=self, name=name)

    # Test / introspection helpers.

    def subscription_count(self, event_type: type[LoomEvent] | None = None) -> int:
        """Total number of active subscriptions, optionally per type."""
        if event_type is None:
            return sum(len(b) for b in self._subs.values())
        return len(self._subs.get(event_type, []))


@dataclass(slots=True)
class SubscriptionGroup:
    """Cancellable bundle of subscriptions on one :class:`EventBus`.

    Mirrors the ``aiohomematic.central.events.SubscriptionGroup``
    surface so ``homematicip_local``'s pattern of "one group per
    config entry, cancel on unload" carries over verbatim.
    """

    bus: EventBus
    name: str
    _members: list[UnsubscribeCallback] = field(default_factory=list, init=False)

    def subscribe(
        self,
        *,
        event_type: type[_EventT],
        handler: Callable[[_EventT], Awaitable[None]],
        event_key: str | None = None,
    ) -> UnsubscribeCallback:
        """Like :meth:`EventBus.subscribe`, but the unsubscribe is
        also remembered by this group so :meth:`cancel` can call it
        on shutdown."""
        unsub = self.bus.subscribe(
            event_type=event_type,
            handler=handler,
            event_key=event_key,
        )
        self._members.append(unsub)
        return unsub

    def cancel(self) -> None:
        """Unsubscribe every handler this group ever registered.

        Idempotent: a second call is a no-op. Individual callbacks
        are also idempotent so concurrent direct unsubscribes from
        a caller can't double-pop the registry.
        """
        members = self._members
        self._members = []
        for unsub in members:
            unsub()

    @property
    def size(self) -> int:
        """Number of active subscriptions in this group."""
        return len(self._members)
