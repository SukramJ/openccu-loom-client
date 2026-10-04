# SPDX-License-Identifier: MIT
# Copyright (C) 2026 SukramJ.

"""
Held devices — the daemon's ``pending_creation`` inbox, in aiohomematic's shape.

The daemon holds a newly paired device back before building it: the device is
announced on the CCU, but it is absent from the snapshot and from the device
stream, and the only place it shows up is ``GET /inbox`` with
``pending_creation: true``. aiohomematic has exactly this state for its own
delayed device creation and announces it as a ``DeviceLifecycleEvent`` of type
``DELAYED`` carrying the interface id and the new addresses; the consumer turns
each address into a fixable repair issue and answers it with
``device_coordinator.add_new_devices_manually``.

:class:`HeldDeviceAnnouncer` produces that event from the inbox. It keeps the
set of addresses it has announced, so a ``hub.inbox_changed`` push that leaves
the held set unchanged announces nothing, and an address leaves the set when it
leaves the held state (accepted, removed, or created).

A held device is never accepted without a name — that is what the hold exists
for. A consumer may still confirm one without a name (``homematicip_local``
auto-confirms every ``DELAYED`` device with an empty name for a while after its
own initial setup), so such a confirmation is *declined*: the address is
dropped from the announced set and one delayed re-sync is scheduled, which
announces it again once the consumer has had time to leave that window.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
import logging
from typing import TYPE_CHECKING, Final

from openccu_loom_client.compat.aiohomematic._upstream import DeviceLifecycleEvent, DeviceLifecycleEventType

if TYPE_CHECKING:
    from openccu_loom_client.client import LoomClient
    from openccu_loom_client.compat.aiohomematic._upstream import EventBus as AioEventBus
    from openccu_loom_client.wire.rest import InboxDevice

_LOGGER: Final = logging.getLogger(__name__)

# How long a declined confirmation waits before the held addresses are
# announced again. Shorter than the integration's auto-confirm window on
# purpose: an announcement that lands inside it is declined once more and the
# cycle repeats, so the address reaches the consumer within this interval of
# the window closing.
HELD_RESYNC_DELAY_SECONDS: Final = 120.0


def wire_interface_id(*, central: str | None, interface: str | None) -> str:
    """
    Return the daemon's wire interface id for an inbox entry, or ``""``.

    The inbox carries the central name and the *bare* interface separately
    (``PublishPendingDevices`` strips the central prefix). The device stream —
    and therefore the ``CREATED`` lifecycle event and the consumer's device
    identifiers — use the joined wire id. The join rule is the daemon's
    ``hmtypes.NewWireInterfaceID``: ``<central>-<interface>``, the bare
    interface when the central is unnamed.
    """
    if not interface:
        return ""
    return f"{central}-{interface}" if central else interface


def is_held(*, entry: InboxDevice) -> bool:
    """Return whether an inbox entry is a device the daemon holds back unbuilt."""
    # None on a daemon that predates the hold — read as "not held".
    return bool(entry.pending_creation)


class HeldDeviceAnnouncer:
    """Publish one ``DELAYED`` lifecycle event per interface for newly held devices."""

    def __init__(
        self,
        *,
        client: LoomClient,
        ha_bus: AioEventBus,
        resync_delay_seconds: float | None = None,
    ) -> None:
        """
        Bind the client whose inbox is read and the aiohomematic bus the events go to.

        ``resync_delay_seconds`` defaults to :data:`HELD_RESYNC_DELAY_SECONDS`,
        read when the timer starts so a test can shorten it.
        """
        self._client = client
        self._ha_bus = ha_bus
        self._resync_delay_seconds = resync_delay_seconds
        self._announced: set[str] = set()
        # Addresses whose confirmation came without a name, waiting for the
        # delayed re-sync. One timer serves all of them.
        self._declined: set[str] = set()
        self._resync_task: asyncio.Task[None] | None = None
        # Pushes and the re-bootstrap hook can overlap; two concurrent syncs
        # reading the same inbox would both see an address as new.
        self._lock = asyncio.Lock()

    @property
    def announced(self) -> frozenset[str]:
        """Return the addresses announced and still held."""
        return frozenset(self._announced)

    def matches_central(self, *, central: str | None) -> bool:
        """Return whether a central tag refers to this central (the hub coordinator's rule)."""
        store = self._client.store
        return not central or central in (store.central_name, store.central_id)

    @property
    def resync_pending(self) -> bool:
        """Return whether a delayed re-sync is scheduled."""
        return self._resync_task is not None and not self._resync_task.done()

    def forget(self, *, address: str) -> None:
        """Drop an address, so it is announced again should it be held again."""
        self._announced.discard(address)

    def decline(self, *, address: str) -> None:
        """
        Record a nameless confirmation of a held address and schedule its re-announcement.

        Nothing is sent to the daemon; the device stays held. The address is
        announced again by the delayed re-sync, not by the next inbox push, so
        an unrelated inbox change does not re-announce it into the same
        auto-confirm window.
        """
        self._announced.discard(address)
        self._declined.add(address)
        self._schedule_resync()

    async def aclose(self) -> None:
        """Cancel a scheduled re-sync and wait for it to finish."""
        self._declined.clear()
        if (task := self._resync_task) is None:
            return
        self._resync_task = None
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    def _schedule_resync(self) -> None:
        if self.resync_pending:
            return
        self._resync_task = asyncio.create_task(self._resync_after_delay(), name="loom-held-devices-resync")

    def _cancel_resync(self) -> None:
        if (task := self._resync_task) is not None:
            self._resync_task = None
            task.cancel()

    async def _resync_after_delay(self) -> None:
        delay = self._resync_delay_seconds
        await asyncio.sleep(HELD_RESYNC_DELAY_SECONDS if delay is None else delay)
        # Cleared before the sync so a decline it provokes can schedule anew.
        self._resync_task = None
        await self.sync(include_declined=True)

    async def held_addresses(self) -> dict[str, str]:
        """Read the inbox and return this central's held addresses mapped to their wire interface id."""
        entries = await self._client.hub.list_inbox_devices()
        held: dict[str, str] = {}
        for entry in entries:
            if not is_held(entry=entry) or not self.matches_central(central=entry.central):
                continue
            if interface_id := wire_interface_id(central=entry.central, interface=entry.interface):
                held[entry.address] = interface_id
        return held

    async def sync(self, *, include_declined: bool = False) -> None:
        """
        Read the inbox and announce the held addresses not announced yet.

        Declined addresses are left to the delayed re-sync, which passes
        ``include_declined``. Best-effort: a failed read is logged and leaves
        the announced set as it was; a re-sync that fails is scheduled again.
        """
        async with self._lock:
            try:
                held = await self.held_addresses()
            except Exception:
                _LOGGER.debug("reading the inbox for held devices failed", exc_info=True)
                if include_declined and self._declined:
                    self._schedule_resync()
                return
            self._announced.intersection_update(held)
            # An address that left the held state is no longer waiting.
            self._declined.intersection_update(held)
            new_by_interface: dict[str, list[str]] = {}
            for address, interface_id in held.items():
                if address in self._announced or (address in self._declined and not include_declined):
                    continue
                new_by_interface.setdefault(interface_id, []).append(address)
                self._declined.discard(address)
            for interface_id, addresses in new_by_interface.items():
                # Marked announced before the publish, not after it: the
                # consumer answers the event from inside its handler, so a
                # nameless confirmation reaches decline() while publish() is
                # still running. Updating afterwards would put the declined
                # address back into the announced set, and the re-sync would
                # then skip it for good. decline() is synchronous and does not
                # take the lock this sync holds.
                self._announced.update(addresses)
                await self._ha_bus.publish(
                    event=DeviceLifecycleEvent(
                        timestamp=datetime.now(tz=UTC),
                        event_type=DeviceLifecycleEventType.DELAYED,
                        device_addresses=tuple(addresses),
                        interface_id=interface_id,
                    )
                )
            # Read after the publishes, so a decline issued during one keeps
            # the re-sync it scheduled.
            if not self._declined:
                self._cancel_resync()
