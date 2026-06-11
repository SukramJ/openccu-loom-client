# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
``aiohomematic.model.hub`` — categorised Sysvar / Program data points.

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

from typing import Any, ClassVar, cast

from aiohomematic.const import PROGRAM_ADDRESS, SYSVAR_ADDRESS
from openccu_loom_types.enums import DataPointCategory

from openccu_loom_client.canonical import canonical_unique_id, hub_slug
from openccu_loom_client.compat.aiohomematic.model._protocol_surface import (
    _ProgramProtocolSurface,
    _SysvarProtocolSurface,
)
from openccu_loom_client.model import Program, Sysvar


def sysvar_unique_id(*, serial_suffix: str, name: str) -> str:
    """
    Canonical HA unique id for a sysvar, matched by the refresh bridge.

    ``loom_<serial>_sysvar_<hub-slug(name)>``: the daemon ``name`` is the
    CCU legacy name, ``hub_slug`` is python-slugify (the contract's slug
    rule), and the serial suffix fills the central-id slot.
    """
    return canonical_unique_id(
        serial_suffix=serial_suffix, address=SYSVAR_ADDRESS, parameter=hub_slug(name)
    )


def program_unique_id(*, serial_suffix: str, name: str) -> str:
    """
    Canonical HA unique id for a program.

    Keyed on ``hub_slug(legacy_name)`` (not the program id), with the
    serial suffix in the central-id slot — ``loom_<serial>_program_<slug>``.
    """
    return canonical_unique_id(
        serial_suffix=serial_suffix, address=PROGRAM_ADDRESS, parameter=hub_slug(name)
    )


class _HubEntitySurface:
    """Shared entity surface for hub data points."""

    _category: ClassVar[DataPointCategory] = DataPointCategory.HubSensor

    @classmethod
    def default_category(cls) -> DataPointCategory:
        """Return the HA data-point category for this class."""
        return cls._category

    @property
    def category(self) -> DataPointCategory:
        """Return the HA data-point category of this instance."""
        return self._category

    @property
    def is_registered(self) -> bool:
        """Return whether this hub entity has been registered with HA."""
        return getattr(self, "_registered", False)

    def register(self) -> None:
        """Mark this hub entity as registered with HA."""
        self._registered = True

    def unregister(self) -> None:
        """Mark this hub entity as no longer registered with HA."""
        self._registered = False

    @property
    def enabled_default(self) -> bool:
        """
        Return whether the entity is enabled by default.

        Mirrors aiohomematic: hub entities default to disabled unless a
        configured description marker matched (the resolver sets the
        flag at build time).
        """
        return getattr(self, "_enabled_default", False)

    def set_enabled_default(self, enabled: bool) -> None:
        """Record the marker-resolved enabled_default flag."""
        self._enabled_default = enabled

    @property
    def state_uncertain(self) -> bool:
        """Return whether the current state is considered uncertain."""
        return False


# ---- sysvars ----


class _SysvarEntitySurface(_HubEntitySurface, _SysvarProtocolSurface):
    @property
    def unique_id(self) -> str:
        """Return the canonical HA unique id for this sysvar."""
        return sysvar_unique_id(
            serial_suffix=self._store.serial_suffix,  # type: ignore[attr-defined]
            name=self.name,  # type: ignore[attr-defined]
        )

    @property
    def data_type(self) -> str | None:
        """Return the sysvar value type."""
        return self.value_type  # type: ignore[attr-defined,no-any-return]

    @property
    def values(self) -> tuple[str, ...]:
        """Return the allowed value list for an enum sysvar."""
        return self.value_list  # type: ignore[attr-defined,no-any-return]

    @property
    def value(self) -> Any:
        """
        Return the sysvar value, LIST indices resolved to option strings.

        The CCU stores LIST sysvars as a numeric index; HA's enum sensor
        rejects a state that is not in its options list, so the index is
        mapped to its option (mirroring aiohomematic).
        """
        raw = Sysvar.value.fget(self)  # type: ignore[attr-defined]
        value_list: tuple[str, ...] = self.value_list  # type: ignore[attr-defined]
        if value_list and raw is not None:
            try:
                idx = int(raw)
            except TypeError, ValueError:
                return raw
            if 0 <= idx < len(value_list):
                return value_list[idx]
        return raw

    async def send_variable(self, value: Any) -> None:
        """Write a new value back to the sysvar."""
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


class ProgramDpButton(_HubEntitySurface, _ProgramProtocolSurface, Program):
    """Program triggered as an HA button entity."""

    _category: ClassVar[DataPointCategory] = DataPointCategory.HubButton

    @property
    def unique_id(self) -> str:
        """Return the canonical HA unique id for this program."""
        return program_unique_id(serial_suffix=self._store.serial_suffix, name=self.name)

    async def press(self) -> None:
        """Trigger the program (HA button press)."""
        await self.execute()


class ProgramDpSwitch(_HubEntitySurface, _ProgramProtocolSurface, Program):
    """Program exposed as an HA switch (toggle 'active')."""

    _category: ClassVar[DataPointCategory] = DataPointCategory.HubSwitch

    @property
    def unique_id(self) -> str:
        """Return the canonical HA unique id for this program."""
        return program_unique_id(serial_suffix=self._store.serial_suffix, name=self.name)


# ---- updates ----


class HmUpdate:
    """
    Type marker for the HA update platform.

    The daemon exposes per-device firmware metadata on
    :class:`openccu_loom_client.model.Device.firmware_detail` (populated by
    ``GET /devices/{addr}``). Wiring HA update entities to it is part of
    the custom/update data-point workstream.
    """


def resolve_hub_inclusion(
    *,
    name: str,
    description: str | None,
    is_internal: bool,
    markers: tuple[str, ...],
    include_internal_default: bool,
) -> tuple[bool, bool]:
    """
    Resolve ``(include, enabled_default)`` for a sysvar/program.

    Mirrors aiohomematic's ``_resolve_sysvar_enabled_default``: internal
    entries need the ``INTERNAL`` marker (or the type's
    include-internal default); non-internal entries with configured
    markers are included (and enabled) only when the description starts
    with one of them; without markers everything non-excluded is
    included but disabled by default.
    """
    del name
    enabled_default = False
    if is_internal:
        if markers:
            if "INTERNAL" not in markers:
                return False, enabled_default
            enabled_default = True
        elif not include_internal_default:
            return False, enabled_default
    elif markers:
        desc = description or ""
        if not any(desc.startswith(str(marker)) for marker in markers):
            return False, enabled_default
        enabled_default = True
    return True, enabled_default


# ---- factories ----

# CCU SysvarType → class, mirroring aiohomematic's non-extended default
# (model/hub/__init__.py): ALARM/LOGIC → binary sensor; everything else
# (FLOAT/INTEGER/STRING/LIST) → read-only sensor. The writable variants
# (switch/number/select/text) require the "extended" sysvar marker from
# the CCU variable description, which the daemon does not surface yet.
_SYSVAR_BY_TYPE: dict[str, type[Sysvar]] = {
    "ALARM": SysvarDpBinarySensor,
    "LOGIC": SysvarDpBinarySensor,
}


def resolve_sysvar_class(*, value_type: str | None, has_value_list: bool) -> type[Sysvar]:
    """
    Pick the ``SysvarDp*`` class from the sysvar's value type.

    Mirrors aiohomematic's default mapping: ALARM/LOGIC read as binary
    sensors, every other type (including LIST — ``has_value_list``) as a
    read-only sensor until extended-marker support lands.
    """
    del has_value_list  # LIST without the extended marker is a sensor too
    return _SYSVAR_BY_TYPE.get((value_type or "").upper(), SysvarDpSensor)


def make_sysvar_data_point(*, summary: Any, store: Any, enabled_default: bool = False) -> Sysvar:
    """Build the categorised ``SysvarDp*`` wrapper for a sysvar summary."""
    cls = resolve_sysvar_class(
        value_type=summary.value_type, has_value_list=bool(summary.value_list)
    )
    dp: Any = cls(summary=summary, store=store)
    dp.set_enabled_default(enabled_default)
    return cast("Sysvar", dp)


def make_program_data_points(
    *, summary: Any, store: Any, enabled_default: bool = False
) -> tuple[ProgramDpButton | ProgramDpSwitch, ...]:
    """
    Build both program wrappers for one program summary.

    aiohomematic spawns two entities per CCU program: a button
    (execute) and a switch (toggle ``active``) — they share the
    canonical key; HA unique_ids are scoped per platform.
    """
    button = ProgramDpButton(summary=summary, store=store)
    button.set_enabled_default(enabled_default)
    switch = ProgramDpSwitch(summary=summary, store=store)
    switch.set_enabled_default(enabled_default)
    return (button, switch)


def make_program_data_point(*, summary: Any, store: Any) -> Program:
    """Build the ``ProgramDpButton`` wrapper for a program summary."""
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
    "make_program_data_points",
    "make_sysvar_data_point",
    "program_unique_id",
    "resolve_hub_inclusion",
    "resolve_sysvar_class",
    "sysvar_unique_id",
]
