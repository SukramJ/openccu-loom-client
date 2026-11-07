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
from types import SimpleNamespace
from typing import Any

import pytest

from openccu_loom_client.compat.aiohomematic.model import custom as _custom
from openccu_loom_client.store import LoomStore
from openccu_loom_client.wire.enums import DataPointCategory
from openccu_loom_client.wire.rest import CustomDPSummary

_DEVICE = "VCU0000001"

Call = tuple[str, dict[str, Any] | None]


def _daemon(*capabilities: str) -> Any:
    # Only `info.capabilities` is read; invoke is recorded, never sent.
    return SimpleNamespace(info=SimpleNamespace(capabilities=list(capabilities)))


def _twin(
    class_name: str,
    category: DataPointCategory,
    monkeypatch: pytest.MonkeyPatch,
    *,
    capabilities: tuple[str, ...] = (),
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
        name="cdp", category=category, channel_no=3, supported_operations=["turn_on"], unique_id="loom_test_cdp"
    )
    cls = getattr(_custom, class_name)
    store = LoomStore(transport=_daemon(*capabilities)) if capabilities else LoomStore()
    return cls(summary=summary, device_address=_DEVICE, store=store), calls


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


async def test_light_on_an_older_daemon_bounds_a_plain_turn_on_through_its_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dp, calls = _twin("CustomDpDimmer", DataPointCategory.Light, monkeypatch)

    _stage_like_the_integration(dp, on_time=120)
    await dp.turn_on()
    await dp.turn_on()

    assert calls == [("set_timer_on_time", {"seconds": 120.0}), ("turn_on", None), ("turn_on", None)]


async def test_light_on_an_older_daemon_reports_an_on_time_it_cannot_carry(
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


_BOUNDED_ON = "cdp.bounded_on.v1"


async def test_light_plain_turn_on_carries_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    dp, calls = _twin("CustomDpDimmer", DataPointCategory.Light, monkeypatch, capabilities=(_BOUNDED_ON,))

    _stage_like_the_integration(dp, on_time=120)
    await dp.turn_on()
    await dp.turn_on()

    assert calls == [("turn_on", {"seconds": 120.0}), ("turn_on", None)]


async def test_light_turn_on_with_brightness_carries_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    dp, calls = _twin("CustomDpDimmer", DataPointCategory.Light, monkeypatch, capabilities=(_BOUNDED_ON,))

    _stage_like_the_integration(dp, on_time=120)
    await dp.turn_on(brightness=128)

    assert calls == [("set_level", {"brightness": 128, "seconds": 120.0})]


async def test_light_bounded_switch_on_comes_after_the_colour(monkeypatch: pytest.MonkeyPatch) -> None:
    dp, calls = _twin("CustomDpDimmer", DataPointCategory.Light, monkeypatch, capabilities=(_BOUNDED_ON,))

    await dp.turn_on(on_time=30, hs_color=(120.0, 50.0))

    # The colour write may switch the light on itself; the bound has to be in
    # the last write, or that switch-on stays unbounded.
    assert calls == [("set_color", {"hue": 120.0, "saturation": 50.0}), ("turn_on", {"seconds": 30.0})]


def test_store_reports_a_capability_only_with_evidence() -> None:
    assert LoomStore().has_capability(capability=_BOUNDED_ON) is False
    assert LoomStore(transport=_daemon()).has_capability(capability=_BOUNDED_ON) is False
    assert LoomStore(transport=_daemon(_BOUNDED_ON)).has_capability(capability=_BOUNDED_ON) is True
    no_handshake = SimpleNamespace(info=None)
    assert LoomStore(transport=no_handshake).has_capability(capability=_BOUNDED_ON) is False  # type: ignore[arg-type]
