# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""``aiohomematic.model.hub`` — categorised Sysvar / Program data points.

Hub entities (CCU system variables and programs) become HA platform
entities. Each class declares its :class:`DataPointCategory` and a
``default_category()`` classmethod (the component looks the category up
from the class it asks for). :func:`make_sysvar_data_point` maps a
sysvar's ``value_type`` to the right ``SysvarDp*`` class; programs
become a :class:`ProgramDpButton`.

The classes subclass :class:`Sysvar` / :class:`Program` so the live
value is read straight off the store-held model.
"""

from __future__ import annotations

from typing import Any, ClassVar

from openccu_loom_types.enums import DataPointCategory

from openccu_loom_client.model import Program, Sysvar


def _clean(text: str) -> str:
    return text.replace(":", "_").replace("-", "_").replace(" ", "_").lower()


def sysvar_unique_id(name: str) -> str:
    """Stable HA unique id for a sysvar, matched by the refresh bridge."""
    return f"sysvar_{_clean(name)}"


def program_unique_id(program_id: str) -> str:
    """Stable HA unique id for a program."""
    return f"program_{_clean(program_id)}"


class _HubEntitySurface:
    """Shared entity surface for hub data points."""

    _category: ClassVar[DataPointCategory] = DataPointCategory.HubSensor

    @classmethod
    def default_category(cls) -> DataPointCategory:
        return cls._category

    @property
    def category(self) -> DataPointCategory:
        return self._category

    @property
    def is_registered(self) -> bool:
        return getattr(self, "_registered", False)

    def register(self) -> None:
        self._registered = True

    def unregister(self) -> None:
        self._registered = False

    @property
    def enabled_default(self) -> bool:
        return True

    @property
    def state_uncertain(self) -> bool:
        return False


# ---- sysvars ----


class _SysvarEntitySurface(_HubEntitySurface):
    @property
    def unique_id(self) -> str:
        return sysvar_unique_id(self.name)  # type: ignore[attr-defined]

    @property
    def data_type(self) -> str | None:
        return self.value_type  # type: ignore[attr-defined,no-any-return]

    @property
    def values(self) -> tuple[str, ...]:
        return self.value_list  # type: ignore[attr-defined,no-any-return]

    async def send_variable(self, value: Any) -> None:
        await self.set_value(value)  # type: ignore[attr-defined]


class SysvarDpSwitch(_SysvarEntitySurface, Sysvar):
    """BOOL sysvar exposed as an HA switch."""

    _category: ClassVar[DataPointCategory] = DataPointCategory.HubSwitch


class SysvarDpBinarySensor(_SysvarEntitySurface, Sysvar):
    """BOOL sysvar exposed as a read-only HA binary_sensor."""

    _category: ClassVar[DataPointCategory] = DataPointCategory.HubBinarySensor


class SysvarDpNumber(_SysvarEntitySurface, Sysvar):
    """Numeric sysvar exposed as an HA number entity."""

    _category: ClassVar[DataPointCategory] = DataPointCategory.HubNumber


class SysvarDpSensor(_SysvarEntitySurface, Sysvar):
    """Sysvar exposed as a read-only HA sensor entity."""

    _category: ClassVar[DataPointCategory] = DataPointCategory.HubSensor


class SysvarDpText(_SysvarEntitySurface, Sysvar):
    """String sysvar exposed as an HA text entity."""

    _category: ClassVar[DataPointCategory] = DataPointCategory.HubText


class SysvarDpSelect(_SysvarEntitySurface, Sysvar):
    """ENUM sysvar exposed as an HA select entity."""

    _category: ClassVar[DataPointCategory] = DataPointCategory.HubSelect


# ---- programs ----


class ProgramDpButton(_HubEntitySurface, Program):
    """Program triggered as an HA button entity."""

    _category: ClassVar[DataPointCategory] = DataPointCategory.HubButton

    @property
    def unique_id(self) -> str:
        return program_unique_id(self.id)

    async def press(self) -> None:
        await self.execute()


class ProgramDpSwitch(_HubEntitySurface, Program):
    """Program exposed as an HA switch (toggle 'active')."""

    _category: ClassVar[DataPointCategory] = DataPointCategory.HubSwitch

    @property
    def unique_id(self) -> str:
        return program_unique_id(self.id)


# ---- updates ----


class HmUpdate:
    """Type marker for the HA update platform.

    The daemon exposes per-device firmware metadata on
    :class:`openccu_loom_client.model.Device.firmware` (populated by
    ``GET /devices/{addr}``). Wiring HA update entities to it is part of
    the custom/update data-point workstream.
    """


# ---- factories ----

_SYSVAR_BY_TYPE: dict[str, type[Sysvar]] = {
    "BOOL": SysvarDpSwitch,
    "ENUM": SysvarDpSelect,
    "FLOAT": SysvarDpNumber,
    "INTEGER": SysvarDpNumber,
    "STRING": SysvarDpText,
}


def resolve_sysvar_class(*, value_type: str | None, has_value_list: bool) -> type[Sysvar]:
    """Pick the ``SysvarDp*`` class from the sysvar's value type.

    BOOL sysvars map to a writable switch (HA users toggle them); other
    types map to number/select/text, defaulting to a read-only sensor.
    """
    if has_value_list:
        return SysvarDpSelect
    return _SYSVAR_BY_TYPE.get((value_type or "").upper(), SysvarDpSensor)


def make_sysvar_data_point(*, summary: Any, store: Any) -> Sysvar:
    cls = resolve_sysvar_class(
        value_type=summary.value_type, has_value_list=bool(summary.value_list)
    )
    return cls(summary=summary, store=store)


def make_program_data_point(*, summary: Any, store: Any) -> Program:
    return ProgramDpButton(summary=summary, store=store)


__all__ = [
    "HmUpdate",
    "ProgramDpButton",
    "ProgramDpSwitch",
    "SysvarDpBinarySensor",
    "SysvarDpNumber",
    "SysvarDpSelect",
    "SysvarDpSensor",
    "SysvarDpSwitch",
    "SysvarDpText",
    "make_program_data_point",
    "make_sysvar_data_point",
    "program_unique_id",
    "resolve_sysvar_class",
    "sysvar_unique_id",
]
