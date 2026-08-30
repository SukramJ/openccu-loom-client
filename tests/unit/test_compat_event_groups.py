# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Device-trigger event groups: read from the daemon, gated locally on usage."""

from __future__ import annotations

from types import SimpleNamespace

from openccu_loom_client.compat.aiohomematic.model.event_group import ChannelEventGroup, build_event_groups
from openccu_loom_client.wire.enums import DeviceTriggerEventType


def _group(
    parameter: str = "PRESS_SHORT", unique_id: str = "loom_event_group_keypress_vcu1769958_1"
) -> ChannelEventGroup:
    channel = SimpleNamespace(address="VCU1769958:1", device=SimpleNamespace(name="Switch", available=True))
    return ChannelEventGroup(
        channel=channel,
        event_type=DeviceTriggerEventType.Keypress,
        events=(SimpleNamespace(parameter=parameter),),
        unique_id=unique_id,
    )


def _dp(parameter: str, usage: str | None) -> SimpleNamespace:
    return SimpleNamespace(parameter=parameter, emits_events=True, summary=SimpleNamespace(usage=usage))


def _declared(kind: str, parameters: list[str], unique_id: str) -> SimpleNamespace:
    return SimpleNamespace(kind=kind, parameters=parameters, unique_id=unique_id)


def _channel(address: str, data_points: list[SimpleNamespace], declared: list[SimpleNamespace]) -> SimpleNamespace:
    return SimpleNamespace(
        address=address,
        data_points=data_points,
        summary=SimpleNamespace(event_groups=declared),
    )


def test_unique_id_is_the_daemons_key_verbatim() -> None:
    """
    The key is served, not rebuilt.

    It used to be recomputed from the namespace, the flavour slug and the
    channel id — byte-identical to the daemon's answer, which is exactly why
    the duplication stayed invisible. A value that differs from the daemon's
    must now surface here rather than being silently reconstructed into
    agreement.
    """
    assert _group(unique_id="loom_event_group_keypress_deadbeef_9").unique_id == "loom_event_group_keypress_deadbeef_9"


def test_event_types_lowercased() -> None:
    assert _group(parameter="PRESS_LONG").event_types == ("press_long",)


def test_register_unregister() -> None:
    group = _group()
    assert group.is_registered is False
    group.register()
    assert group.is_registered is True
    group.unregister()
    assert group.is_registered is False


def test_groups_follow_the_daemons_declaration() -> None:
    """Flavour, membership and key all come from ChannelSummary.event_groups."""
    channel = _channel(
        "VCU100:2",
        [_dp("PRESS_SHORT", "event"), _dp("SEQUENCE_OK", "event")],
        [
            _declared("keypress", ["PRESS_SHORT"], "loom_event_group_keypress_vcu100_2"),
            _declared("impulse", ["SEQUENCE_OK"], "loom_event_group_impulse_vcu100_2"),
        ],
    )
    store = SimpleNamespace(devices=[SimpleNamespace(channels=[channel])])

    groups = sorted(build_event_groups(store=store, central_id=""), key=lambda g: g.unique_id)
    assert [g.unique_id for g in groups] == [
        "loom_event_group_impulse_vcu100_2",
        "loom_event_group_keypress_vcu100_2",
    ]
    assert [g.translation_key for g in groups] == ["impulse", "keypress"]


def test_unknown_flavour_is_skipped_not_guessed() -> None:
    """
    A kind this client does not model yet spawns nothing.

    Inventing a DeviceTriggerEventType for it would create an entity Home
    Assistant has no translation for; skipping keeps the daemon free to add a
    flavour before the client models it.
    """
    channel = _channel(
        "VCU100:3",
        [_dp("SOMETHING_NEW", "event")],
        [_declared("something_new", ["SOMETHING_NEW"], "loom_event_group_something_new_vcu100_3")],
    )
    store = SimpleNamespace(devices=[SimpleNamespace(channels=[channel])])
    assert build_event_groups(store=store, central_id="") == ()


def test_build_event_groups_skips_suppressed_usage() -> None:
    """
    no_create/ignored DPs never feed a group (HmIP-PS click suppression).

    This gate stays local on purpose: the daemon groups every event source a
    channel has, while the reference stack never spawns an event for a
    suppressed parameter. It is a consumer-side visibility rule, not a fact
    about the device — so a group whose members are all suppressed does not
    materialise even though the daemon declares it.
    """
    suppressed = _channel(
        "VCU100:1",
        [_dp("PRESS_SHORT", "no_create"), _dp("PRESS_LONG", "ignored")],
        [_declared("keypress", ["PRESS_SHORT", "PRESS_LONG"], "loom_event_group_keypress_vcu100_1")],
    )
    live = _channel(
        "VCU100:2",
        [_dp("PRESS_SHORT", "event"), _dp("PRESS_LONG", None)],
        [_declared("keypress", ["PRESS_SHORT", "PRESS_LONG"], "loom_event_group_keypress_vcu100_2")],
    )
    store = SimpleNamespace(devices=[SimpleNamespace(channels=[suppressed, live])])

    groups = build_event_groups(store=store, central_id="")
    assert [g.channel.address for g in groups] == ["VCU100:2"]
    assert set(groups[0].event_types) == {"press_long", "press_short"}
