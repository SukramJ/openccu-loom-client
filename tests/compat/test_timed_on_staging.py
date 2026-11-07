# SPDX-License-Identifier: MIT
# Copyright (C) 2026 SukramJ.

"""
The ``*_set_on_time`` services reach the daemon as one bounded switch-on.

``homematicip_local`` implements ``switch_set_on_time``, ``light_set_on_time``
and ``valve_set_on_time`` as two steps: ``set_timer_on_time`` *without*
``await`` (``switch.py``, ``light.py``, ``valve.py``), then an ordinary
``turn_on`` / ``open`` in a separate service call. These tests replay exactly
that sequence against the compat twins and record what goes to the daemon.

The regression they pin: ``set_timer_on_time`` used to be a coroutine on the
twins, so the integration's un-awaited call created it and dropped it. Nothing
was sent, the following ``turn_on`` went out unbounded, and a circulation pump
meant to run 900 s ran until someone switched it off by hand.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from openccu_loom_client.compat.aiohomematic.model import custom as _custom
from openccu_loom_client.store import LoomStore
from openccu_loom_client.wire.enums import DataPointCategory
from openccu_loom_client.wire.rest import CustomDPSummary

_DEVICE = "VCU0000001"

Call = tuple[str, dict[str, Any] | None]


def _twin(class_name: str, category: DataPointCategory, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, list[Call]]:
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
        name="cdp", category=category, channel_no=3, supported_operations=["turn_on"], unique_id="loom_test_cdp"
    )
    cls = getattr(_custom, class_name)
    return cls(summary=summary, device_address=_DEVICE, store=LoomStore()), calls


def _stage_like_the_integration(dp: Any, *, on_time: float) -> None:
    # The integration's own call, un-awaited. A coroutine here is the defect:
    # it is never run, so the on-time never leaves the process.
    result = dp.set_timer_on_time(on_time=on_time)
    assert result is None, (
        f"{type(dp).__name__}.set_timer_on_time returned {type(result).__name__}; homematicip_local calls it "
        "without await, so a coroutine is dropped unrun and the on-time is lost"
    )


async def test_switch_set_on_time_then_turn_on_is_one_bounded_switch_on(monkeypatch: pytest.MonkeyPatch) -> None:
    dp, calls = _twin("CustomDpSwitch", DataPointCategory.Switch, monkeypatch)

    _stage_like_the_integration(dp, on_time=900)
    assert calls == [], "staging must not reach the daemon on its own"
    assert dp.timer_on_time == 900

    await dp.turn_on()

    assert calls == [("turn_on_for", {"seconds": 900.0})]
    assert dp.timer_on_time is None


async def test_switch_staged_on_time_is_consumed_by_one_switch_on(monkeypatch: pytest.MonkeyPatch) -> None:
    dp, calls = _twin("CustomDpSwitch", DataPointCategory.Switch, monkeypatch)

    _stage_like_the_integration(dp, on_time=600)
    await dp.turn_on()
    await dp.turn_on()

    assert calls == [("turn_on_for", {"seconds": 600.0}), ("turn_on", None)]


async def test_switch_turn_off_drops_a_staged_on_time(monkeypatch: pytest.MonkeyPatch) -> None:
    dp, calls = _twin("CustomDpSwitch", DataPointCategory.Switch, monkeypatch)

    _stage_like_the_integration(dp, on_time=900)
    await dp.turn_off()
    await dp.turn_on()

    assert calls == [("turn_off", None), ("turn_on", None)]


async def test_switch_explicit_on_time_wins_over_a_staged_one(monkeypatch: pytest.MonkeyPatch) -> None:
    dp, calls = _twin("CustomDpSwitch", DataPointCategory.Switch, monkeypatch)

    _stage_like_the_integration(dp, on_time=900)
    await dp.turn_on(on_time=5)
    await dp.turn_on()

    assert calls == [("turn_on_for", {"seconds": 5.0}), ("turn_on", None)]


@pytest.mark.parametrize("on_time", [0, -1])
async def test_switch_non_positive_on_time_is_an_unbounded_switch_on(
    monkeypatch: pytest.MonkeyPatch, on_time: float
) -> None:
    dp, calls = _twin("CustomDpSwitch", DataPointCategory.Switch, monkeypatch)

    _stage_like_the_integration(dp, on_time=on_time)
    await dp.turn_on()

    assert calls == [("turn_on", None)]


async def test_valve_set_on_time_then_open_sends_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    dp, calls = _twin("CustomDpIpIrrigationValve", DataPointCategory.Valve, monkeypatch)

    _stage_like_the_integration(dp, on_time=600)
    await dp.open()
    await dp.open()

    # `seconds`, not `duration`: the daemon reads a bare number under the
    # `duration` alias as milliseconds.
    assert calls == [("open", {"seconds": 600.0}), ("open", None)]


async def test_valve_close_drops_a_staged_on_time(monkeypatch: pytest.MonkeyPatch) -> None:
    dp, calls = _twin("CustomDpIpIrrigationValve", DataPointCategory.Valve, monkeypatch)

    _stage_like_the_integration(dp, on_time=600)
    await dp.close()
    await dp.open()

    assert calls == [("close", None), ("open", None)]


async def test_light_set_on_time_then_plain_turn_on_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    dp, calls = _twin("CustomDpDimmer", DataPointCategory.Light, monkeypatch)

    _stage_like_the_integration(dp, on_time=120)
    await dp.turn_on()
    await dp.turn_on()

    assert calls == [("set_timer_on_time", {"seconds": 120.0}), ("turn_on", None), ("turn_on", None)]


async def test_light_on_time_with_brightness_is_reported_not_staged_on_the_daemon(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    dp, calls = _twin("CustomDpDimmer", DataPointCategory.Light, monkeypatch)

    _stage_like_the_integration(dp, on_time=120)
    with caplog.at_level(logging.WARNING):
        await dp.turn_on(brightness=128)

    # Staging it on the daemon here would leave it pending for some later
    # switch-on: the daemon's `set_level` does not consume it.
    assert calls == [("set_level", {"brightness": 128})]
    assert "on-time 120.0 s not applied" in caplog.text
    assert dp.timer_on_time is None


async def test_event_is_a_coroutine_like_the_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    # homematicip_local's MQTT consumer passes `dp.event(...)` to
    # `hass.create_task`, which rejects anything that is not a coroutine.
    dp, _ = _twin("CustomDpSwitch", DataPointCategory.Switch, monkeypatch)
    result = dp.event(value=True)
    assert result is not None
    await result
