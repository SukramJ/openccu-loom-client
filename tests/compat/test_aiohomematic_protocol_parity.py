# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Check compat data-point twins against aiohomematic's protocols.

Cross-package validation: do the compat shim's data-point classes
structurally satisfy ``aiohomematic``'s ``@runtime_checkable``
data-point protocols?

The Home Assistant component ``homematicip_local`` (and
``aiohomematic-config``) dispatch on ``isinstance(dp, SomeProtocol)``
where the protocols come from the real ``aiohomematic`` package. On the
openccu-loom backend the data points handed to the platforms are the
compat shim's ``Dp*`` / ``CustomDp*`` / ``Sysvar*`` / ``Program*``
twins. For protocol-based dispatch to work, those twins must
*structurally* expose every member each protocol declares.

This test constructs one instance of each compat class and checks it
against the corresponding aiohomematic protocol. It needs ``aiohomematic``
installed (a dev/test dependency, but not a core runtime dependency of
this package), so it skips cleanly when absent. The default dev setup
covers it::

    pip install -e '.[dev]'
    pytest tests/compat

The twins satisfy the protocols via the shared ``_protocol_surface``
mixins (``_GenericProtocolSurface`` / ``_CustomProtocolSurface`` /
``_SysvarProtocolSurface`` / ``_ProgramProtocolSurface``). If a protocol
member is dropped from a twin, the corresponding case fails with a
message listing the missing members. (Daemon-sourced *values* — accurate
per-parameter ``data_point_type``, rooms, translations — are a separate
Strategy-B refinement; this test guards structural satisfaction only.)
"""

from __future__ import annotations

from typing import Any

import pytest

aiohomematic = pytest.importorskip("aiohomematic")

from aiohomematic.interfaces.model import (  # noqa: E402 — after importorskip
    CustomDataPointProtocol,
    GenericDataPointProtocol,
    GenericProgramDataPointProtocol,
    GenericSysvarDataPointProtocol,
)
from openccu_loom_types.enums import DataPointCategory  # noqa: E402
from openccu_loom_types.rest import (  # noqa: E402
    CustomDPSummary,
    DataPointSummary,
    Operations,
    ProgramSummary,
    SysvarSummary,
)

from openccu_loom_client.compat.aiohomematic.model import (  # noqa: E402
    custom as _custom,
    generic as _generic,
    hub as _hub,
)
from openccu_loom_client.store import LoomStore  # noqa: E402

_DEVICE = "VCU0000001"
_OPS = Operations(read=True, write=True, event=True)


def _store() -> LoomStore:
    return LoomStore()


def _generic_instance(class_name: str) -> Any:
    summary = DataPointSummary(
        parameter="STATE", observed=True, operations=_OPS, type="BOOL", value=True, unique_id="loom_test_state"
    )
    cls = getattr(_generic, class_name)
    return cls(summary=summary, device_address=_DEVICE, channel_number=1, store=_store())


def _custom_instance(class_name: str) -> Any:
    summary = CustomDPSummary(
        name="cdp",
        category=DataPointCategory.Switch,
        channel_no=1,
        supported_operations=["set"],
        unique_id="loom_test_cdp",
    )
    cls = getattr(_custom, class_name)
    return cls(summary=summary, device_address=_DEVICE, store=_store())


def _sysvar_instance(class_name: str) -> Any:
    summary = SysvarSummary(name="sv", value_type="BOOL", observed=True, value=True, unique_id="loom_test_sv")
    cls = getattr(_hub, class_name)
    return cls(summary=summary, store=_store())


def _program_instance(class_name: str) -> Any:
    summary = ProgramSummary(id="1000", name="prog", unique_id="loom_test_prog")
    cls = getattr(_hub, class_name)
    return cls(summary=summary, store=_store())


# (compat class name, instance builder, aiohomematic protocol the platforms check against)
_CASES: list[tuple[str, Any, Any]] = [
    *(
        (n, _generic_instance, GenericDataPointProtocol)
        for n in (
            "DpSwitch",
            "DpBinarySensor",
            "DpSensor",
            "DpSelect",
            "DpText",
            "DpAction",
            "DpButton",
            "DpActionSelect",
        )
    ),
    *(
        (n, _custom_instance, CustomDataPointProtocol)
        for n in (
            "CustomDpSwitch",
            "CustomDpDimmer",
            "CustomDpCover",
            "CustomDpBlind",
            "CustomDpIpBlind",
            "CustomDpGarage",
            "CustomDpIpThermostat",
            "CustomDpSoundPlayer",
            "CustomDpSoundPlayerLed",
            "CustomDpIpIrrigationValve",
            "CustomDpTextDisplay",
            "CustomDpIpFixedColorLight",
        )
    ),
    *(
        (n, _sysvar_instance, GenericSysvarDataPointProtocol)
        for n in (
            "SysvarDpSwitch",
            "SysvarDpBinarySensor",
            "SysvarDpNumber",
            "SysvarDpSensor",
            "SysvarDpText",
            "SysvarDpSelect",
        )
    ),
    *((n, _program_instance, GenericProgramDataPointProtocol) for n in ("ProgramDpButton", "ProgramDpSwitch")),
]


def _protocol_attrs(protocol: Any) -> frozenset[str]:
    attrs = getattr(protocol, "__protocol_attrs__", None)
    if attrs is None:
        from typing import _get_protocol_attrs  # type: ignore[attr-defined]

        attrs = _get_protocol_attrs(protocol)
    return frozenset(attrs)


def _missing_members(instance: Any, protocol: Any) -> list[str]:
    missing: list[str] = []
    for attr in sorted(_protocol_attrs(protocol)):
        try:
            present = hasattr(instance, attr)
        except Exception as exc:  # a raising getter counts as "not satisfied"
            missing.append(f"{attr} (raises {type(exc).__name__})")
            continue
        if not present:
            missing.append(attr)
    return missing


@pytest.mark.parametrize(
    ("class_name", "builder", "protocol"),
    _CASES,
    ids=[f"{name}->{proto.__name__}" for name, _, proto in _CASES],
)
def test_compat_dp_satisfies_aiohomematic_protocol(class_name: str, builder: Any, protocol: Any) -> None:
    instance = builder(class_name)
    assert isinstance(instance, protocol), (
        f"{class_name} does not satisfy {protocol.__name__}; missing members: {_missing_members(instance, protocol)}"
    )


# --- drift guard: the upstream protocol surface itself ---

# Snapshot of every ``@runtime_checkable`` protocol exported by
# ``aiohomematic.interfaces.model`` at the pinned aiohomematic version.
# HA dispatches on these via ``isinstance``. The cases above structurally
# verify the four the loom twins must satisfy; the remaining protocols
# describe aiohomematic's own central/device/channel objects, which the
# loom backend does not hand to the platforms. If aiohomematic ADDS or
# REMOVES a model protocol, this test fails so a human decides whether a
# compat twin (and a parity case above) needs to follow — turning silent,
# runtime-only HA breakage into a loud CI failure. Bump the aiohomematic
# version bound (pyproject) and this snapshot together. See todo.md P1.
_KNOWN_AIOHM_MODEL_PROTOCOLS: frozenset[str] = frozenset(
    {
        "BaseDataPointProtocol",
        "BaseParameterDataPointProtocol",
        "CalculatedDataPointProtocol",
        "CallbackDataPointProtocol",
        "ChannelEventGroupProtocol",
        "ChannelProtocol",
        "ClimateWeekProfileDataPointProtocol",
        "CombinedDataPointProtocol",
        "CustomDataPointProtocol",
        "DeviceChannelAccessProtocol",
        "DeviceDescriptionProviderProtocol",
        "DeviceDetailsProviderProtocol",
        "DeviceIdentityProtocol",
        "DeviceProtocol",
        "DeviceRemovalInfoProtocol",
        "GenericDataPointProtocol",
        "GenericEventProtocol",
        "GenericHubDataPointProtocol",
        "GenericInstallModeDataPointProtocol",
        "GenericProgramDataPointProtocol",
        "GenericSysvarDataPointProtocol",
        "HubBinarySensorDataPointProtocol",
        "HubProtocol",
        "HubSensorDataPointProtocol",
        "ParameterVisibilityProviderProtocol",
        "ParamsetDescriptionProviderProtocol",
        "ScheduleChannelSwitchProtocol",
        "TaskSchedulerProtocol",
        "WeekProfileDataPointProtocol",
        "WeekProfileProtocol",
    }
)


def _runtime_checkable_model_protocols() -> frozenset[str]:
    from aiohomematic.interfaces import model

    names: set[str] = set()
    for name in dir(model):
        obj = getattr(model, name)
        if (
            isinstance(obj, type)
            and getattr(obj, "_is_protocol", False)
            and getattr(obj, "_is_runtime_protocol", False)
        ):
            names.add(name)
    return frozenset(names)


def test_aiohomematic_model_protocol_surface_has_not_drifted() -> None:
    current = _runtime_checkable_model_protocols()
    added = current - _KNOWN_AIOHM_MODEL_PROTOCOLS
    removed = _KNOWN_AIOHM_MODEL_PROTOCOLS - current
    assert not (added or removed), (
        "aiohomematic.interfaces.model runtime_checkable protocols drifted — review whether the "
        "compat twins / parity cases need to follow, then update the snapshot.\n"
        f"  added (new upstream protocols): {sorted(added)}\n"
        f"  removed (gone upstream): {sorted(removed)}"
    )
