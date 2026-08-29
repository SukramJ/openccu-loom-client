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

from typing import TYPE_CHECKING, Any, ClassVar, cast

from openccu_loom_client.canonical import canonical_unique_id, hub_slug
from openccu_loom_client.compat.aiohomematic._upstream import PROGRAM_ADDRESS, SYSVAR_ADDRESS
from openccu_loom_client.compat.aiohomematic.model._protocol_surface import (
    _ProgramProtocolSurface,
    _SysvarProtocolSurface,
)
from openccu_loom_client.compat.aiohomematic.model.hub._surface import _HubEntitySurface
from openccu_loom_client.model import Program, Sysvar
from openccu_loom_client.wire.enums import DataPointCategory

if TYPE_CHECKING:
    from openccu_loom_client.store import LoomStore


def sysvar_unique_id(*, serial_suffix: str, name: str) -> str:
    """
    Canonical HA unique id for a sysvar, matched by the refresh bridge.

    ``loom_<serial>_sysvar_<hub-slug(name)>``: the daemon ``name`` is the
    CCU legacy name, ``hub_slug`` is python-slugify (the contract's slug
    rule), and the serial suffix fills the central-id slot.
    """
    return canonical_unique_id(serial_suffix=serial_suffix, address=SYSVAR_ADDRESS, parameter=hub_slug(name=name))


def program_unique_id(*, serial_suffix: str, name: str) -> str:
    """
    Canonical HA unique id for a program.

    Keyed on ``hub_slug(legacy_name)`` (not the program id), with the
    serial suffix in the central-id slot — ``loom_<serial>_program_<slug>``.
    """
    return canonical_unique_id(serial_suffix=serial_suffix, address=PROGRAM_ADDRESS, parameter=hub_slug(name=name))


# ---- sysvars ----


class _SysvarEntitySurface(_HubEntitySurface, _SysvarProtocolSurface):
    if TYPE_CHECKING:
        # Host attributes from ``Sysvar`` not already declared on the protocol
        # mixins (name / value_list / set_value come from _SysvarProtocolSurface).
        _store: LoomStore
        value_type: str | None

    @property
    def unique_id(self) -> str:
        """Return the daemon-owned canonical HA unique id for this sysvar (J1)."""
        return self.summary.unique_id

    @property
    def data_type(self) -> str | None:
        """Return the sysvar value type."""
        return self.value_type

    @property
    def values(self) -> tuple[str, ...]:
        """Return the allowed value list for an enum sysvar."""
        return self.value_list

    @property
    def value(self) -> Any:
        """
        Return the sysvar value, LIST indices resolved to option strings.

        The CCU stores LIST sysvars as a numeric index; HA's enum sensor
        rejects a state that is not in its options list, so the index is
        mapped to its option (mirroring aiohomematic).
        """
        raw = Sysvar.value.fget(self)  # type: ignore[attr-defined]
        value_list: tuple[str, ...] = self.value_list
        if value_list and raw is not None:
            try:
                idx = int(raw)
            except TypeError, ValueError:
                return raw
            if 0 <= idx < len(value_list):
                return value_list[idx]
        return raw

    async def send_variable(self, *, value: Any) -> None:
        """Write a new value back to the sysvar."""
        await self.set_value(value=value)


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
        """Return the daemon-owned canonical HA unique id for this program (J1)."""
        return self.summary.unique_id

    @property
    def available(self) -> bool:
        """
        Return ``False`` while the CCU would refuse to run this program.

        This is the one hub entity whose availability is not constant: a
        deactivated program ignores its triggers and rejects a manual
        run, so pressing the button could only fail. The daemon reports
        that as ``execute_available`` (api 3.12.0) rather than leaving it
        to be re-derived here — and it fails open, so an older daemon or
        an unobserved flag leaves the button pressable.

        The sibling switch stays available on purpose (see
        :class:`ProgramDpSwitch`).
        """
        return self.execute_available

    async def press(self) -> None:
        """Trigger the program (HA button press)."""
        await self.execute()


class ProgramDpSwitch(_HubEntitySurface, _ProgramProtocolSurface, Program):
    """
    Program exposed as an HA switch (toggle 'active').

    Deliberately *not* gated on :attr:`Program.execute_available`, unlike
    the sibling button: this switch is what turns a deactivated program
    back on, so making it unavailable exactly when the program is off
    would strip out the only control that can recover it.
    """

    _category: ClassVar[DataPointCategory] = DataPointCategory.HubSwitch

    @property
    def unique_id(self) -> str:
        """Return the daemon-owned canonical HA unique id for this program (J1)."""
        return self.summary.unique_id

    @property
    def value(self) -> bool | None:
        """
        Return the program's activity flag — the switch's on/off state.

        ``None`` while the CCU has not reported the flag; Home Assistant
        renders that as unknown rather than off, which is the honest answer
        for a program whose state nobody has read yet.
        """
        return self.summary.active

    async def turn_on(self) -> None:
        """Activate the program on the CCU."""
        await self._store.set_program_enabled(program_id=self.id, active=True)

    async def turn_off(self) -> None:
        """Deactivate the program on the CCU."""
        await self._store.set_program_enabled(program_id=self.id, active=False)


# ---- updates ----


class HmUpdate:
    """
    Type marker for the HA update platform.

    The daemon exposes per-device firmware metadata on
    :class:`openccu_loom_client.model.Device.firmware_detail` (populated by
    ``GET /devices/{addr}``). Wiring HA update entities to it is part of
    the custom/update data-point workstream.
    """


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


_SYSVAR_EXTENDED_BY_TYPE: dict[str, type[Sysvar]] = {
    "ALARM": SysvarDpSwitch,
    "LOGIC": SysvarDpSwitch,
    "LIST": SysvarDpSelect,
    "FLOAT": SysvarDpNumber,
    "INTEGER": SysvarDpNumber,
    "STRING": SysvarDpText,
}


def resolve_sysvar_class(*, value_type: str | None, has_value_list: bool, extended: bool = False) -> type[Sysvar]:
    """
    Pick the ``SysvarDp*`` class from the sysvar's value type.

    Mirrors aiohomematic's mapping: ALARM/LOGIC read as binary sensors,
    everything else as a read-only sensor — unless the variable carries
    the extended description marker, which unlocks the writable flavour
    (switch/select/number/text).
    """
    del has_value_list  # LIST without the extended marker is a sensor too
    token = (value_type or "").upper()
    if extended and (cls := _SYSVAR_EXTENDED_BY_TYPE.get(token)):
        return cls
    return _SYSVAR_BY_TYPE.get(token, SysvarDpSensor)


def make_sysvar_data_point(*, summary: Any, store: Any, enabled_default: bool = False) -> Sysvar:
    """Build the categorised ``SysvarDp*`` wrapper for a sysvar summary."""
    cls = resolve_sysvar_class(
        value_type=summary.value_type,
        has_value_list=bool(summary.value_list),
        extended=bool(getattr(summary, "is_extended", False)),
    )
    dp: Any = cls(summary=summary, store=store)
    dp.set_enabled_default(enabled=enabled_default)
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
    button.set_enabled_default(enabled=enabled_default)
    switch = ProgramDpSwitch(summary=summary, store=store)
    switch.set_enabled_default(enabled=enabled_default)
    return (button, switch)


def make_program_data_point(*, summary: Any, store: Any) -> Program:
    """Build the ``ProgramDpButton`` wrapper for a program summary."""
    return ProgramDpButton(summary=summary, store=store)


__all__ = [
    # General
    "DataPointCategory",
    "HmUpdate",
    "Program",
    "ProgramDpButton",
    "ProgramDpSwitch",
    "Sysvar",
    "SysvarDpBinarySensor",
    "SysvarDpNumber",
    "SysvarDpSelect",
    "SysvarDpSensor",
    "SysvarDpSwitch",
    "SysvarDpText",
    "canonical_unique_id",
    "hub_slug",
    "make_program_data_point",
    "make_program_data_points",
    "make_sysvar_data_point",
    "program_unique_id",
    "resolve_sysvar_class",
    "sysvar_unique_id",
]
