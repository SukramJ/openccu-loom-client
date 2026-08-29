# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Device-trigger event-group classification + unique_id format."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from openccu_loom_client.compat.aiohomematic.model.event_group import ChannelEventGroup, _trigger_type
from openccu_loom_client.wire.enums import DeviceTriggerEventType


@pytest.mark.parametrize(
    ("parameter", "expected"),
    [
        ("PRESS_SHORT", DeviceTriggerEventType.Keypress),
        ("PRESS_LONG", DeviceTriggerEventType.Keypress),
        ("SEQUENCE_OK", DeviceTriggerEventType.Impulse),
        ("ERROR", DeviceTriggerEventType.DeviceError),
        ("SENSOR_ERROR", DeviceTriggerEventType.DeviceError),
        ("STATE", None),
        ("LEVEL", None),
    ],
)
def test_trigger_classification(parameter: str, expected: DeviceTriggerEventType | None) -> None:
    assert _trigger_type(parameter=parameter) == expected


def _group(parameter: str = "PRESS_SHORT", central_id: str = "") -> ChannelEventGroup:
    channel = SimpleNamespace(address="VCU1769958:1", device=SimpleNamespace(name="Switch", available=True))
    return ChannelEventGroup(
        channel=channel,
        event_type=DeviceTriggerEventType.Keypress,
        events=(SimpleNamespace(parameter=parameter),),
        central_id=central_id,
    )


def test_unique_id_format() -> None:
    # event_group_{short}_{channel_unique_id} — matches aiohomematic.
    assert _group().unique_id == "loom_event_group_keypress_vcu1769958_1"


def test_event_types_lowercased() -> None:
    assert _group(parameter="PRESS_LONG").event_types == ("press_long",)


def test_register_unregister() -> None:
    group = _group()
    assert group.is_registered is False
    group.register()
    assert group.is_registered is True
    group.unregister()
    assert group.is_registered is False


def test_build_event_groups_skips_suppressed_usage() -> None:
    """no_create/ignored DPs never feed a group (HmIP-PS click suppression)."""
    from openccu_loom_client.compat.aiohomematic.model.event_group import build_event_groups

    def _dp(parameter: str, usage: str | None) -> SimpleNamespace:
        return SimpleNamespace(
            parameter=parameter,
            emits_events=True,
            summary=SimpleNamespace(usage=usage),
        )

    suppressed_channel = SimpleNamespace(
        address="VCU100:1",
        data_points=[_dp("PRESS_SHORT", "no_create"), _dp("PRESS_LONG", "ignored")],
    )
    live_channel = SimpleNamespace(
        address="VCU100:2",
        data_points=[_dp("PRESS_SHORT", "event"), _dp("PRESS_LONG", None)],
    )
    device = SimpleNamespace(channels=[suppressed_channel, live_channel])
    store = SimpleNamespace(devices=[device])

    groups = build_event_groups(store=store, central_id="")
    assert [g.channel.address for g in groups] == ["VCU100:2"]
    assert set(groups[0].event_types) == {"press_long", "press_short"}
