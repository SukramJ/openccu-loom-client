# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Device-trigger event-group classification + unique_id format."""

from __future__ import annotations

from types import SimpleNamespace

from openccu_loom_types.enums import DeviceTriggerEventType
import pytest

from openccu_loom_client.compat.aiohomematic.model.event_group import (
    ChannelEventGroup,
    _trigger_type,
)


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
    assert _trigger_type(parameter) == expected


def _group(parameter: str = "PRESS_SHORT", central_id: str = "") -> ChannelEventGroup:
    channel = SimpleNamespace(
        address="VCU1769958:1", device=SimpleNamespace(name="Switch", available=True)
    )
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
