# SPDX-License-Identifier: MIT
# Copyright (C) 2026 SukramJ.

"""
Transitions and sound parameters reach the daemon under the daemon's own keys.

``homematicip_local`` hands a light's ``ATTR_TRANSITION`` to ``turn_on`` and
``turn_off`` as ``ramp_time`` and the ``play_sound`` service's fields to
``play_sound`` under their service names. The daemon reads a transition as
``transition`` (capability ``cdp.transition.v1``) and the sound player's
parameters as ``soundfile_index`` / ``tone``, ``duration``, ``transition`` and
``repetitions_index``. These tests record what the compat twins send.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from openccu_loom_client.capabilities import Capability
from openccu_loom_client.compat.aiohomematic.model import custom as _custom
from openccu_loom_client.store import LoomStore
from openccu_loom_client.wire.enums import DataPointCategory
from openccu_loom_client.wire.rest import CustomDPSummary

_DEVICE = "VCU0000001"
_CURRENT = (Capability.CDP_BOUNDED_ON.value, Capability.CDP_TRANSITION.value)
_BEFORE_TRANSITION = (Capability.CDP_BOUNDED_ON.value,)

Call = tuple[str, dict[str, Any] | None]


def _daemon(*capabilities: str) -> Any:
    # Only `info.capabilities` is read; invoke is recorded, never sent.
    return SimpleNamespace(info=SimpleNamespace(capabilities=list(capabilities)))


def _twin(
    class_name: str,
    category: DataPointCategory,
    monkeypatch: pytest.MonkeyPatch,
    *,
    daemon: tuple[str, ...],
    flags: dict[str, bool] | None = None,
) -> tuple[Any, list[Call]]:
    calls: list[Call] = []

    async def _record(
        self: LoomStore,
        *,
        address: str,
        name: str,
        operation: str,
        params: dict[str, Any] | None = None,
        priority: str | None = None,
    ) -> None:
        calls.append((operation, params))

    monkeypatch.setattr(LoomStore, "invoke_custom_data_point", _record)
    summary = CustomDPSummary(
        name="cdp",
        category=category,
        channel_no=3,
        supported_operations=["turn_on"],
        unique_id="loom_test_cdp",
        capabilities=flags or {},
    )
    cls = getattr(_custom, class_name)
    return cls(summary=summary, device_address=_DEVICE, store=LoomStore(transport=_daemon(*daemon))), calls


def _light(
    monkeypatch: pytest.MonkeyPatch, *, daemon: tuple[str, ...] = _CURRENT, transition: bool = True
) -> tuple[Any, list[Call]]:
    return _twin(
        "CustomDpDimmer",
        DataPointCategory.Light,
        monkeypatch,
        daemon=daemon,
        flags={"dimmable": True, "transition": transition},
    )


def _sound_player(monkeypatch: pytest.MonkeyPatch, *, daemon: tuple[str, ...] = _CURRENT) -> tuple[Any, list[Call]]:
    return _twin("CustomDpSoundPlayer", DataPointCategory.Siren, monkeypatch, daemon=daemon)


# ---- light ----


async def test_light_plain_switch_on_takes_the_transition(monkeypatch: pytest.MonkeyPatch) -> None:
    dp, calls = _light(monkeypatch)

    await dp.turn_on(ramp_time=0.5)

    assert calls == [("turn_on", {"transition": 0.5})]


async def test_light_switch_on_with_brightness_is_one_set_level_with_the_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dp, calls = _light(monkeypatch)

    await dp.turn_on(brightness=128, ramp_time=0.5)

    assert calls == [("set_level", {"state": "ON", "brightness": 128, "transition": 0.5})]


async def test_light_bounded_switch_on_carries_both_timers(monkeypatch: pytest.MonkeyPatch) -> None:
    dp, calls = _light(monkeypatch)

    await dp.turn_on(on_time=60, ramp_time=2)

    assert calls == [("turn_on", {"seconds": 60.0, "transition": 2.0})]


async def test_light_switch_off_fades_out_through_set_level(monkeypatch: pytest.MonkeyPatch) -> None:
    dp, calls = _light(monkeypatch)

    await dp.turn_off(ramp_time=2)

    assert calls == [("set_level", {"state": "OFF", "transition": 2.0})]


async def test_light_without_transition_capability_drops_the_transition(monkeypatch: pytest.MonkeyPatch) -> None:
    # The integration declares LightEntityFeature.TRANSITION for every light;
    # a transition sent to one that reports none is refused by the daemon and
    # would fail the whole switch.
    dp, calls = _light(monkeypatch, transition=False)

    await dp.turn_on(ramp_time=0.5)
    await dp.turn_on(brightness=128, ramp_time=0.5)
    await dp.turn_off(ramp_time=2)

    assert calls == [
        ("turn_on", None),
        ("set_level", {"state": "ON", "brightness": 128}),
        ("turn_off", None),
    ]


async def test_light_on_a_daemon_before_the_transition_capability_keeps_the_old_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dp, calls = _light(monkeypatch, daemon=_BEFORE_TRANSITION)

    await dp.turn_on(ramp_time=0.5)
    await dp.turn_on(brightness=128, ramp_time=0.5)
    await dp.turn_off(ramp_time=2)

    assert calls == [
        ("turn_on", None),
        ("set_level", {"state": "ON", "brightness": 128}),
        ("turn_off", None),
    ]


@pytest.mark.parametrize("ramp_time", [0, -1, None])
async def test_light_without_a_positive_transition_sends_none(
    monkeypatch: pytest.MonkeyPatch, ramp_time: float | None
) -> None:
    dp, calls = _light(monkeypatch)

    await dp.turn_on(ramp_time=ramp_time)
    await dp.turn_off(ramp_time=ramp_time)

    assert calls == [("turn_on", None), ("turn_off", None)]


async def test_sound_player_led_takes_the_transition(monkeypatch: pytest.MonkeyPatch) -> None:
    dp, calls = _twin(
        "CustomDpSoundPlayerLed",
        DataPointCategory.Light,
        monkeypatch,
        daemon=_CURRENT,
        flags={"dimmable": True, "color": True, "transition": True},
    )

    await dp.turn_on(brightness=255, on_time=10, ramp_time=1)

    assert calls == [("set_level", {"state": "ON", "brightness": 255, "seconds": 10.0, "transition": 1.0})]


# ---- sound player ----


async def test_play_sound_maps_every_service_field(monkeypatch: pytest.MonkeyPatch) -> None:
    dp, calls = _sound_player(monkeypatch)

    await dp.play_sound(soundfile=3, volume=0.5, on_time=10, ramp_time=1, repetitions=2)

    assert calls == [
        (
            "turn_on",
            {"soundfile_index": 3, "volume": 0.5, "duration": 10, "transition": 1, "repetitions_index": 2},
        )
    ]


async def test_play_sound_sends_a_named_soundfile_as_tone(monkeypatch: pytest.MonkeyPatch) -> None:
    dp, calls = _sound_player(monkeypatch)

    await dp.play_sound(soundfile="SOUNDFILE_001")

    assert calls == [("turn_on", {"tone": "SOUNDFILE_001"})]


async def test_play_sound_on_a_daemon_before_the_transition_capability_sends_ramp_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dp, calls = _sound_player(monkeypatch, daemon=_BEFORE_TRANSITION)

    await dp.play_sound(soundfile=3, on_time=10, ramp_time=1, repetitions=-1)

    assert calls == [("turn_on", {"soundfile_index": 3, "duration": 10, "ramp_time": 1, "repetitions_index": -1})]


async def test_play_sound_without_fields_sends_no_params(monkeypatch: pytest.MonkeyPatch) -> None:
    dp, calls = _sound_player(monkeypatch)

    await dp.play_sound()

    assert calls == [("turn_on", None)]
