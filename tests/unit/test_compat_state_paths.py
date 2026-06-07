# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""MQTT state-path synthesis / parsing matches the aiohomematic format."""

from __future__ import annotations

import pytest

from openccu_loom_client.compat.aiohomematic.central.state_paths import (
    device_state_path,
    parse_device_state_path,
    parse_sysvar_state_path,
    sysvar_state_path,
)


def test_device_state_path_format() -> None:
    assert (
        device_state_path(address="vcu1769958", channel=3, parameter="state")
        == "device/status/VCU1769958/3/STATE"
    )


def test_sysvar_state_path_format() -> None:
    assert sysvar_state_path(name="TargetTemperature") == "sysvar/status/TargetTemperature"


def test_device_state_path_roundtrip() -> None:
    path = device_state_path(address="VCU1769958", channel=3, parameter="STATE")
    assert parse_device_state_path(path) == ("VCU1769958", 3, "STATE")


def test_sysvar_state_path_roundtrip() -> None:
    path = sysvar_state_path(name="Presence")
    assert parse_sysvar_state_path(path) == "Presence"


@pytest.mark.parametrize(
    "bad",
    ["sysvar/status/x", "device/status/a/notint/b", "device/status/a/1", "nonsense"],
)
def test_parse_device_rejects_non_device_paths(bad: str) -> None:
    assert parse_device_state_path(bad) is None


@pytest.mark.parametrize("bad", ["device/status/a/1/b", "sysvarstatus/x", ""])
def test_parse_sysvar_rejects_non_sysvar_paths(bad: str) -> None:
    assert parse_sysvar_state_path(bad) is None
