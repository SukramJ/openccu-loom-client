# SPDX-License-Identifier: MIT
# Copyright (C) 2026 SukramJ.

"""
Held devices — the daemon's ``pending_creation`` inbox as aiohomematic's DELAYED.

The daemon holds a newly paired device back unbuilt; the only trace of it is an
inbox entry flagged ``pending_creation``. The compat layer announces each such
address once as a ``DeviceLifecycleEvent`` of type ``DELAYED`` (the shape
``homematicip_local`` turns into a fixable repair issue), and answers the
repair's ``add_new_devices_manually`` with accept-then-release.

The announcement is driven through ``start()`` so the subscription under test
is the one production installs; only the network-heavy bring-up is stubbed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from openccu_loom_client.compat.aiohomematic._upstream import DeviceLifecycleEvent, DeviceLifecycleEventType
from openccu_loom_client.compat.aiohomematic.central.events import (
    DeviceLifecycleEventType as CompatDeviceLifecycleEventType,
)
import openccu_loom_client.compat.aiohomematic.central.held_devices as held_devices_module
from openccu_loom_client.events import HubInboxChangedEvent
from openccu_loom_client.exceptions import BaseLoomException
from openccu_loom_client.operations.devices import DevicesOperations
from openccu_loom_client.wire.rest import HubCountChangedPayload, Kind2 as Kind
from tests.helpers import MockDaemon
from tests.unit.test_compat_central_adapter import _BASE, _INFO, _LITE_ENTRY, _make_config, _wait_for


def _held(address: str, *, central: str = "home", interface: str = "HmIP-RF") -> dict[str, Any]:
    return {
        "central": central,
        "address": address,
        "model": "HmIP-PS",
        "interface": interface,
        "pending_creation": True,
    }


def _inbox_push(*, central: str = "home", count: int = 1) -> HubInboxChangedEvent:
    return HubInboxChangedEvent(
        seq=1,
        kind=Kind.change,
        ts="2026-10-04T08:00:00Z",
        payload=HubCountChangedPayload(central=central, count=count),
    )


async def _started(
    *,
    mock_daemon: MockDaemon,
    monkeypatch: pytest.MonkeyPatch,
    inboxes: list[list[dict[str, Any]]],
    on_delayed: Callable[[Any, DeviceLifecycleEvent], Awaitable[None]] | None = None,
) -> tuple[Any, list[DeviceLifecycleEvent]]:
    """
    Start an adapter whose successive ``GET /inbox`` reads answer ``inboxes`` in order (the last repeats).

    ``on_delayed`` is awaited from inside the bus handler for every DELAYED
    event, with the central — the way the integration answers it in place.
    """
    mock_daemon.get(f"{_BASE}/info", payload=_INFO)
    mock_daemon.get(f"{_BASE}/system/ccu", payload=[_LITE_ENTRY])
    mock_daemon.get(f"{_BASE}/interfaces", payload=[])
    for inbox in inboxes:
        mock_daemon.get(f"{_BASE}/inbox", payload=inbox)
    central = await _make_config(mock_daemon=mock_daemon).create_central()
    seen: list[DeviceLifecycleEvent] = []

    async def _record(*, event: DeviceLifecycleEvent) -> None:
        seen.append(event)
        if on_delayed is not None and event.event_type == DeviceLifecycleEventType.DELAYED:
            await on_delayed(central, event)

    # Subscribed before start(), the way the integration does it.
    central.event_bus.subscribe(event_type=DeviceLifecycleEvent, event_key=None, handler=_record)

    async def _noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    for target, attr in (
        (central.client_coordinator, "refresh"),
        (central._client, "wait_until_ready"),
        (central._client, "start_events"),
        (central, "_bootstrap_model"),
        (central.query_facade, "prefetch_un_ignore_candidates"),
        (central, "_emit_data_points_created"),
        (central, "_hub_reconcile_loop"),
    ):
        monkeypatch.setattr(target, attr, _noop)
    await central.start()
    await central._looper.block_till_done()
    return central, seen


def _delayed(seen: list[DeviceLifecycleEvent]) -> list[tuple[str | None, tuple[str, ...]]]:
    return [(e.interface_id, e.device_addresses) for e in seen if e.event_type == DeviceLifecycleEventType.DELAYED]


def _inbox_reads(mock_daemon: MockDaemon) -> int:
    return sum(1 for r in mock_daemon.requests if r.method == "GET" and r.path == f"{_BASE}/inbox")


class TestDelayedAnnouncement:
    """A held address is announced once, on the interface id the device stream uses."""

    async def test_held_device_at_start_is_announced_once(self, mock_daemon: MockDaemon, monkeypatch) -> None:
        central, seen = await _started(mock_daemon=mock_daemon, monkeypatch=monkeypatch, inboxes=[[_held("NEW1")]])
        try:
            # `<central>-<interface>`: the daemon's NewWireInterfaceID join,
            # the same id the device.created frame carries.
            assert _delayed(seen) == [("home-HmIP-RF", ("NEW1",))]
            assert seen[0].key is None, "the integration subscribes with event_key=None"
        finally:
            await central.stop()

    async def test_unchanged_inbox_push_announces_nothing(self, mock_daemon: MockDaemon, monkeypatch) -> None:
        central, seen = await _started(mock_daemon=mock_daemon, monkeypatch=monkeypatch, inboxes=[[_held("NEW1")]])
        try:
            reads = _inbox_reads(mock_daemon)
            await central._client.events.publish(event=_inbox_push())
            assert await _wait_for(lambda: _inbox_reads(mock_daemon) > reads), "the push must re-read the inbox"
            await central._looper.block_till_done()
            assert _delayed(seen) == [("home-HmIP-RF", ("NEW1",))]
        finally:
            await central.stop()

    async def test_new_held_address_is_announced_alone(self, mock_daemon: MockDaemon, monkeypatch) -> None:
        central, seen = await _started(
            mock_daemon=mock_daemon,
            monkeypatch=monkeypatch,
            inboxes=[[_held("NEW1")], [_held("NEW1"), _held("NEW2")]],
        )
        try:
            await central._client.events.publish(event=_inbox_push(count=2))
            assert await _wait_for(lambda: len(_delayed(seen)) == 2)
            await central._looper.block_till_done()
            assert _delayed(seen) == [("home-HmIP-RF", ("NEW1",)), ("home-HmIP-RF", ("NEW2",))]
        finally:
            await central.stop()

    async def test_address_that_left_and_returns_is_announced_again(self, mock_daemon: MockDaemon, monkeypatch) -> None:
        central, seen = await _started(
            mock_daemon=mock_daemon,
            monkeypatch=monkeypatch,
            inboxes=[[_held("NEW1")], [], [_held("NEW1")]],
        )
        try:
            for _ in range(2):
                reads = _inbox_reads(mock_daemon)
                await central._client.events.publish(event=_inbox_push())
                assert await _wait_for(lambda reads=reads: _inbox_reads(mock_daemon) > reads)
            await central._looper.block_till_done()
            assert _delayed(seen) == [("home-HmIP-RF", ("NEW1",)), ("home-HmIP-RF", ("NEW1",))]
        finally:
            await central.stop()

    @pytest.mark.parametrize(
        "entry",
        [
            # A plain CCU inbox entry — waiting for an accept, not held by the daemon.
            {"central": "home", "address": "CCU1", "model": "HmIP-PS", "interface": "HmIP-RF"},
            # Accepted and built, withheld until release — not a DELAYED device.
            {
                "central": "home",
                "address": "MID1",
                "model": "HmIP-PS",
                "interface": "HmIP-RF",
                "awaiting_release": True,
            },
            # Held, but on another central of the same daemon.
            _held("OTHER1", central="other"),
        ],
        ids=["plain-ccu-inbox", "awaiting-release", "foreign-central"],
    )
    async def test_entries_that_are_not_held_here_announce_nothing(
        self, mock_daemon: MockDaemon, monkeypatch, entry: dict[str, Any]
    ) -> None:
        central, seen = await _started(mock_daemon=mock_daemon, monkeypatch=monkeypatch, inboxes=[[entry]])
        try:
            assert _inbox_reads(mock_daemon) >= 1, "the inbox must have been read for this to mean anything"
            assert _delayed(seen) == []
        finally:
            await central.stop()

    async def test_entry_without_the_field_older_daemon(self, mock_daemon: MockDaemon, monkeypatch) -> None:
        """A daemon older than api 13.7.0 never sends `pending_creation`: no event and no error."""
        entry = {"central": "home", "address": "OLD1", "model": "HmIP-PS", "interface": "HmIP-RF"}
        central, seen = await _started(mock_daemon=mock_daemon, monkeypatch=monkeypatch, inboxes=[[entry]])
        try:
            await central._client.events.publish(event=_inbox_push())
            await central._looper.block_till_done()
            assert _delayed(seen) == []
            records = await central.json_rpc_client.get_inbox_devices()
            assert records[0].pending_creation is False
        finally:
            await central.stop()

    def test_compat_enum_carries_upstream_value(self) -> None:
        assert CompatDeviceLifecycleEventType.DELAYED.value == DeviceLifecycleEventType.DELAYED.value


async def _connected(mock_daemon: MockDaemon, *, inbox: list[dict[str, Any]]) -> Any:
    mock_daemon.get(f"{_BASE}/info", payload=_INFO)
    mock_daemon.get(f"{_BASE}/inbox", payload=inbox)
    central = await _make_config(mock_daemon=mock_daemon).create_central()
    await central._client.connect()
    return central


def _writes(mock_daemon: MockDaemon) -> list[tuple[str, str, Any]]:
    return [(r.method, r.path, r.json()) for r in mock_daemon.requests if r.method != "GET"]


class TestAddNewDevicesManually:
    """The repair's fix callback accepts a held device under its name, then releases it."""

    async def test_held_address_is_accepted_with_its_name_then_released(self, mock_daemon: MockDaemon) -> None:
        central = await _connected(mock_daemon, inbox=[_held("NEW1")])
        mock_daemon.post(f"{_BASE}/devices/NEW1/accept", status=202)
        mock_daemon.post(f"{_BASE}/devices/NEW1/release", status=204)
        try:
            await central.device_coordinator.add_new_devices_manually(
                interface_id="home-HmIP-RF", address_names={"NEW1": "Stehlampe"}
            )
            assert _writes(mock_daemon) == [
                ("POST", f"{_BASE}/devices/NEW1/accept", {"name": "Stehlampe"}),
                ("POST", f"{_BASE}/devices/NEW1/release", None),
            ], "a held device must be accepted under its name and then released"
        finally:
            await central._client.close()

    async def test_address_that_is_not_held_with_empty_name_sends_nothing(self, mock_daemon: MockDaemon) -> None:
        """Negative control for the decline: a built device confirmed without a name is left alone."""
        central = await _connected(mock_daemon, inbox=[])
        try:
            await central.device_coordinator.add_new_devices_manually(
                interface_id="home-HmIP-RF", address_names={"VCU1": ""}
            )
            assert _writes(mock_daemon) == []
            assert central._held_devices.resync_pending is False
        finally:
            await central._client.close()

    async def test_failed_release_raises_and_says_the_device_waits(self, mock_daemon: MockDaemon) -> None:
        central = await _connected(mock_daemon, inbox=[_held("NEW1")])
        mock_daemon.post(f"{_BASE}/devices/NEW1/accept", status=202)
        mock_daemon.post(
            f"{_BASE}/devices/NEW1/release",
            status=500,
            payload={"type": "https://openccu-loom.dev/errors/internal", "title": "boom", "status": 500},
        )
        try:
            with pytest.raises(BaseLoomException, match="stays awaiting release"):
                await central.device_coordinator.add_new_devices_manually(
                    interface_id="home-HmIP-RF", address_names={"NEW1": "Stehlampe"}
                )
            assert [w[1] for w in _writes(mock_daemon)] == [
                f"{_BASE}/devices/NEW1/accept",
                f"{_BASE}/devices/NEW1/release",
            ]
        finally:
            await central._client.close()

    async def test_address_that_is_not_held_is_only_renamed(self, mock_daemon: MockDaemon) -> None:
        central = await _connected(mock_daemon, inbox=[])
        mock_daemon.patch(f"{_BASE}/devices/VCU1", payload={"address": "VCU1", "model": "HmIP-PS"})
        try:
            await central.device_coordinator.add_new_devices_manually(
                interface_id="home-HmIP-RF", address_names={"VCU1": "Lamp"}
            )
            writes = _writes(mock_daemon)
            assert [(m, p) for m, p, _ in writes] == [("PATCH", f"{_BASE}/devices/VCU1")]
        finally:
            await central._client.close()

    async def test_accept_device_in_inbox_passes_the_name(self, mock_daemon: MockDaemon) -> None:
        central = await _connected(mock_daemon, inbox=[])
        mock_daemon.post(f"{_BASE}/devices/NEW1/accept", status=202)
        try:
            assert await central.json_rpc_client.accept_device_in_inbox(device_address="NEW1", device_name="Lampe")
            assert await central.json_rpc_client.accept_device_in_inbox(device_address="NEW1")
            assert _writes(mock_daemon) == [
                ("POST", f"{_BASE}/devices/NEW1/accept", {"name": "Lampe"}),
                ("POST", f"{_BASE}/devices/NEW1/accept", None),
            ]
        finally:
            await central._client.close()


class TestNamelessConfirmation:
    """
    A held device is never accepted without a name.

    ``homematicip_local`` auto-confirms every DELAYED device with an empty name
    for a while after its initial setup. That confirmation is declined and the
    device announced again after a delay, until a confirmation carries a name.
    """

    @pytest.mark.parametrize("name", ["", "   "], ids=["empty", "blank"])
    async def test_declined_then_reannounced_then_accepted_with_name(
        self, mock_daemon: MockDaemon, monkeypatch, name: str
    ) -> None:
        monkeypatch.setattr(held_devices_module, "HELD_RESYNC_DELAY_SECONDS", 0.05)
        central, seen = await _started(mock_daemon=mock_daemon, monkeypatch=monkeypatch, inboxes=[[_held("NEW1")]])
        mock_daemon.post(f"{_BASE}/devices/NEW1/accept", status=202)
        mock_daemon.post(f"{_BASE}/devices/NEW1/release", status=204)
        try:
            assert _delayed(seen) == [("home-HmIP-RF", ("NEW1",))]
            await central.device_coordinator.add_new_devices_manually(
                interface_id="home-HmIP-RF", address_names={"NEW1": name}
            )
            assert _writes(mock_daemon) == [], "a held device confirmed without a name must not be accepted"
            assert central._held_devices.resync_pending is True
            assert await _wait_for(lambda: len(_delayed(seen)) == 2), "the declined device must be announced again"
            await central._looper.block_till_done()
            assert _delayed(seen) == [("home-HmIP-RF", ("NEW1",)), ("home-HmIP-RF", ("NEW1",))]
            assert central._held_devices.resync_pending is False, "nothing waits once it was re-announced"

            await central.device_coordinator.add_new_devices_manually(
                interface_id="home-HmIP-RF", address_names={"NEW1": "Stehlampe"}
            )
            assert _writes(mock_daemon) == [
                ("POST", f"{_BASE}/devices/NEW1/accept", {"name": "Stehlampe"}),
                ("POST", f"{_BASE}/devices/NEW1/release", None),
            ]
        finally:
            await central.stop()

    async def test_several_declines_share_one_timer(self, mock_daemon: MockDaemon, monkeypatch) -> None:
        monkeypatch.setattr(held_devices_module, "HELD_RESYNC_DELAY_SECONDS", 60.0)
        central, _ = await _started(
            mock_daemon=mock_daemon, monkeypatch=monkeypatch, inboxes=[[_held("NEW1"), _held("NEW2")]]
        )
        try:
            await central.device_coordinator.add_new_devices_manually(
                interface_id="home-HmIP-RF", address_names={"NEW1": "", "NEW2": ""}
            )
            first = central._held_devices._resync_task
            assert first is not None
            await central.device_coordinator.add_new_devices_manually(
                interface_id="home-HmIP-RF", address_names={"NEW1": ""}
            )
            assert central._held_devices._resync_task is first
        finally:
            await central.stop()

    async def test_timer_is_cancelled_on_stop(self, mock_daemon: MockDaemon, monkeypatch) -> None:
        monkeypatch.setattr(held_devices_module, "HELD_RESYNC_DELAY_SECONDS", 60.0)
        central, seen = await _started(mock_daemon=mock_daemon, monkeypatch=monkeypatch, inboxes=[[_held("NEW1")]])
        await central.device_coordinator.add_new_devices_manually(
            interface_id="home-HmIP-RF", address_names={"NEW1": ""}
        )
        task = central._held_devices._resync_task
        assert task is not None
        assert not task.done()
        await central.stop()
        assert task.cancelled(), "stop() must cancel the pending re-sync"
        assert central._held_devices.resync_pending is False
        assert len(_delayed(seen)) == 1


def _confirm_with(names: list[str]) -> Callable[[Any, DeviceLifecycleEvent], Awaitable[None]]:
    """
    Answer each DELAYED event in place with the next name (the last repeats).

    ``homematicip_local`` confirms from inside its bus handler, so the
    confirmation runs while the announcing ``publish`` is still on the stack.
    """
    answers = iter(names)
    last = names[-1]

    async def _answer(central: Any, event: DeviceLifecycleEvent) -> None:
        name = next(answers, last)
        await central.device_coordinator.add_new_devices_manually(
            interface_id=event.interface_id, address_names=dict.fromkeys(event.device_addresses, name)
        )

    return _answer


class TestConfirmationDuringAnnouncement:
    """A confirmation the consumer issues while the DELAYED event is still being published."""

    async def test_nameless_confirmation_in_handler_is_reannounced(self, mock_daemon: MockDaemon, monkeypatch) -> None:
        monkeypatch.setattr(held_devices_module, "HELD_RESYNC_DELAY_SECONDS", 0.05)
        central, seen = await _started(
            mock_daemon=mock_daemon,
            monkeypatch=monkeypatch,
            inboxes=[[_held("NEW1")]],
            on_delayed=_confirm_with([""]),
        )
        try:
            assert _delayed(seen) == [("home-HmIP-RF", ("NEW1",))]
            assert await _wait_for(lambda: len(_delayed(seen)) >= 2), (
                "a decline issued inside the announcement must still lead to a second announcement"
            )
            assert _delayed(seen)[:2] == [("home-HmIP-RF", ("NEW1",)), ("home-HmIP-RF", ("NEW1",))]
            # The decline runs inside the bus handler after the event is recorded (the handler
            # reads the inbox first), so the re-sync it schedules is awaited, not read at once.
            assert await _wait_for(lambda: central._held_devices.resync_pending), (
                "a consumer that keeps declining keeps a re-sync"
            )
            assert _writes(mock_daemon) == [], "a held device confirmed without a name must not be accepted"
        finally:
            await central.stop()

    async def test_named_confirmation_after_in_handler_decline_accepts(
        self, mock_daemon: MockDaemon, monkeypatch
    ) -> None:
        monkeypatch.setattr(held_devices_module, "HELD_RESYNC_DELAY_SECONDS", 0.05)
        central, seen = await _started(
            mock_daemon=mock_daemon,
            monkeypatch=monkeypatch,
            inboxes=[[_held("NEW1")]],
            on_delayed=_confirm_with(["", "Stehlampe"]),
        )
        mock_daemon.post(f"{_BASE}/devices/NEW1/accept", status=202)
        mock_daemon.post(f"{_BASE}/devices/NEW1/release", status=204)
        try:
            assert await _wait_for(lambda: len(_delayed(seen)) == 2), "the declined device must be announced again"
            # The accept and release run inside the bus handler after the event is recorded,
            # and the looper does not track that handler — wait for both writes.
            assert await _wait_for(lambda: len(_writes(mock_daemon)) >= 2), "the named confirmation must be sent"
            assert _writes(mock_daemon) == [
                ("POST", f"{_BASE}/devices/NEW1/accept", {"name": "Stehlampe"}),
                ("POST", f"{_BASE}/devices/NEW1/release", None),
            ]
            assert central._held_devices.resync_pending is False, "nothing waits once it was accepted"
            assert _delayed(seen) == [("home-HmIP-RF", ("NEW1",)), ("home-HmIP-RF", ("NEW1",))]
        finally:
            await central.stop()

    async def test_silent_consumer_is_announced_once(self, mock_daemon: MockDaemon, monkeypatch) -> None:
        """Negative control: without a confirmation the address stays announced and nothing is scheduled."""
        monkeypatch.setattr(held_devices_module, "HELD_RESYNC_DELAY_SECONDS", 0.05)

        async def _ignore(_central: Any, _event: DeviceLifecycleEvent) -> None:
            return None

        central, seen = await _started(
            mock_daemon=mock_daemon, monkeypatch=monkeypatch, inboxes=[[_held("NEW1")]], on_delayed=_ignore
        )
        try:
            assert _delayed(seen) == [("home-HmIP-RF", ("NEW1",))]
            assert central._held_devices.announced == frozenset({"NEW1"})
            assert central._held_devices.resync_pending is False
        finally:
            await central.stop()


class TestAcceptDeviceBody:
    """The native accept carries the first-time configuration only when one is given."""

    async def test_body_matches_the_wire_request(self, mock_daemon: MockDaemon) -> None:
        mock_daemon.get(f"{_BASE}/info", payload=_INFO)
        mock_daemon.post(f"{_BASE}/devices/NEW1/accept", status=202)
        central = await _make_config(mock_daemon=mock_daemon).create_central()
        await central._client.connect()
        try:
            ops: DevicesOperations = central._client.devices
            await ops.accept_device(
                address="NEW1", name="Lampe", include_channels=True, rooms=["Küche"], functions=["Licht"]
            )
            await ops.accept_device(address="NEW1")
            assert [w[2] for w in _writes(mock_daemon)] == [
                {"name": "Lampe", "include_channels": True, "rooms": ["Küche"], "functions": ["Licht"]},
                None,
            ]
        finally:
            await central._client.close()
