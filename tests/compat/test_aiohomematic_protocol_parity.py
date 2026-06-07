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
installed (it is not a runtime/dev dependency of this package), so it
skips cleanly when absent. Install the opt-in extra to run it::

    pip install -e '.[compat-test]'
    pytest tests/compat

The cases are currently ``xfail``: the categorized data-point-model port
onto ``LoomStore`` (the ``_MODEL_PORT_TODO`` workstream) is incomplete,
so the twins do not yet satisfy the full protocol surface. Each failure
message lists the exact missing members — the executable to-do list.
When a class starts satisfying its protocol the case ``xpass``es, which
is the signal to drop its ``xfail``.
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
        parameter="STATE", observed=True, operations=_OPS, type="BOOL", value=True
    )
    cls = getattr(_generic, class_name)
    return cls(summary=summary, device_address=_DEVICE, channel_number=1, store=_store())


def _custom_instance(class_name: str) -> Any:
    summary = CustomDPSummary(
        name="cdp", category=DataPointCategory.Switch, channel_no=1, supported_operations=["set"]
    )
    cls = getattr(_custom, class_name)
    return cls(summary=summary, device_address=_DEVICE, store=_store())


def _sysvar_instance(class_name: str) -> Any:
    summary = SysvarSummary(name="sv", value_type="BOOL", observed=True, value=True)
    cls = getattr(_hub, class_name)
    return cls(summary=summary, store=_store())


def _program_instance(class_name: str) -> Any:
    summary = ProgramSummary(id="1000", name="prog")
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
    *(
        (n, _program_instance, GenericProgramDataPointProtocol)
        for n in ("ProgramDpButton", "ProgramDpSwitch")
    ),
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


@pytest.mark.xfail(
    reason="compat data-point twins do not yet structurally satisfy aiohomematic's "
    "runtime_checkable protocols — categorized-data-point-model port (_MODEL_PORT_TODO)",
    strict=False,
)
@pytest.mark.parametrize(
    ("class_name", "builder", "protocol"),
    _CASES,
    ids=[f"{name}->{proto.__name__}" for name, _, proto in _CASES],
)
def test_compat_dp_satisfies_aiohomematic_protocol(
    class_name: str, builder: Any, protocol: Any
) -> None:
    instance = builder(class_name)
    assert isinstance(instance, protocol), (
        f"{class_name} does not satisfy {protocol.__name__}; "
        f"missing members: {_missing_members(instance, protocol)}"
    )
