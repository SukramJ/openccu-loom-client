# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
``aiohomematic.model.generic`` — categorised data-point classes.

Each generic CCU parameter maps to one HA platform entity. The class
a parameter resolves to (and the :class:`DataPointCategory` it carries)
is decided by :func:`make_generic_data_point`, mirroring aiohomematic's
``(type, operations, value_list)`` resolver:

* read-only ``BOOL`` → :class:`DpBinarySensor`, other read-only → :class:`DpSensor`
* writable ``BOOL`` → :class:`DpSwitch`, ``ENUM`` → :class:`DpSelect`,
  ``FLOAT/INTEGER`` → :class:`BaseDpNumber`, ``STRING`` → :class:`DpText`
* write-only ``ACTION`` → :class:`DpButton`; write-only with a value
  list → :class:`DpActionSelect`; write-only numeric →
  :class:`BaseDpActionNumber`; other write-only → :class:`DpAction`

The classes subclass the core :class:`DataPoint`, so the store can hold
the categorised instance (one live object per data point) while HA's
``isinstance`` dispatch still works. The entity-facing surface
(``unique_id``, ``category``, ``register``/``unregister``, ``name`` …)
is added by :class:`_GenericEntitySurface`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar, Final

from openccu_loom_types.enums import DataPointCategory

from openccu_loom_client.compat.aiohomematic.model._protocol_surface import _GenericProtocolSurface
from openccu_loom_client.events.types import data_point_event_key
from openccu_loom_client.model import DataPoint

if TYPE_CHECKING:
    from openccu_loom_client.store import LoomStore

_LOGGER: Final = logging.getLogger(__name__)

_UNSET: Final = object()


class _GenericEntitySurface(_GenericProtocolSurface):
    """
    Entity-facing attributes HA reads off a generic data point.

    Mixed into every ``Dp*`` class. Relies on the host class also being
    a :class:`DataPoint` (so ``value``, ``parameter``, ``min`` … resolve).
    """

    _category: ClassVar[DataPointCategory] = DataPointCategory.Sensor

    # ---- identity ----

    @property
    def category(self) -> DataPointCategory:
        return self._category

    @property
    def unique_id(self) -> str:
        return data_point_event_key(
            serial_suffix=self._store.serial_suffix,  # type: ignore[attr-defined]
            device_address=self.device_address,  # type: ignore[attr-defined]
            channel=self.channel_number,  # type: ignore[attr-defined]
            parameter=self.parameter,  # type: ignore[attr-defined]
        )

    @property
    def name(self) -> str:
        return self.parameter_label or self.parameter  # type: ignore[attr-defined,no-any-return]

    @property
    def full_name(self) -> str:
        device = self.device  # type: ignore[attr-defined]
        device_name = device.name if device is not None else self.device_address  # type: ignore[attr-defined]
        return f"{device_name} {self.name}"

    @property
    def central(self) -> Any:
        # The central reference is not threaded onto data points; the
        # compat layer scopes by unique_id instead. Provided for
        # signature parity.
        return None

    # ---- value / state ----

    def _resolve_enum(self, raw: Any) -> Any:
        """Map an ENUM index to its ``value_list`` option string (mirrors aiohomematic)."""
        value_list: tuple[str, ...] = self.value_list  # type: ignore[attr-defined]
        is_enum: bool = self.type == "ENUM"  # type: ignore[attr-defined]
        if is_enum and value_list and isinstance(raw, int) and 0 <= raw < len(value_list):
            return value_list[raw]
        return raw

    @property
    def value(self) -> Any:
        """
        Return the data point's value, ENUM-resolved.

        An ENUM index is mapped to its ``value_list`` option string so HA's
        sensor/select read a string. A value written by HA (optimistic /
        restored default) takes precedence until the daemon reports a fresh
        one (the store clears the override in ``apply_value_changed``).
        """
        override = getattr(self, "_value_override", _UNSET)
        if override is not _UNSET:
            return override
        return self._resolve_enum(DataPoint.value.fget(self))  # type: ignore[attr-defined]

    @value.setter
    def value(self, new_value: Any) -> None:
        """Store an HA-written value (optimistic); the next daemon update clears it."""
        self._value_override = new_value

    @property
    def default(self) -> Any:
        """Return the parameter's default, ENUM-resolved (HA restores this when unset)."""
        return self._resolve_enum(getattr(self.summary, "default", None))  # type: ignore[attr-defined]

    @property
    def is_valid(self) -> bool:
        return self.value is not None

    @property
    def state_uncertain(self) -> bool:
        # The loom client has no per-value uncertainty signal yet.
        return False

    @property
    def available(self) -> bool:
        device = self.device  # type: ignore[attr-defined]
        return bool(device.available) if device is not None else True

    @property
    def enabled_default(self) -> bool:
        return True

    @property
    def additional_information(self) -> dict[str, Any]:
        return {}

    @property
    def hmtype(self) -> str | None:
        return self.type  # type: ignore[attr-defined,no-any-return]

    @property
    def values(self) -> tuple[str, ...]:
        return self.value_list  # type: ignore[attr-defined,no-any-return]

    @property
    def multiplier(self) -> float:
        return 1.0

    @property
    def modified_at(self) -> Any:
        return getattr(self.summary, "modified_at", None)  # type: ignore[attr-defined]

    @property
    def refreshed_at(self) -> Any:
        return getattr(self.summary, "last_seen_at", None)  # type: ignore[attr-defined]

    # ---- registration bookkeeping ----

    @property
    def is_registered(self) -> bool:
        return getattr(self, "_registered", False)

    def register(self) -> None:
        self._registered = True

    def unregister(self) -> None:
        self._registered = False

    # ---- value load ----

    async def load_data_point_value(self, *, call_source: Any = None) -> None:
        """Re-read this data point's value from the daemon."""
        store: LoomStore = self._store  # type: ignore[attr-defined]
        await store.refresh_data_point(
            address=self.device_address,  # type: ignore[attr-defined]
            channel=self.channel_number,  # type: ignore[attr-defined]
            parameter=self.parameter,  # type: ignore[attr-defined]
        )


# ---- concrete categorised classes ----


class DpSwitch(_GenericEntitySurface, DataPoint):
    """BOOL parameter HA exposes as a switch."""

    _category: ClassVar[DataPointCategory] = DataPointCategory.Switch

    async def turn_on(self, **_kwargs: Any) -> None:
        """Send ``True`` to switch the parameter on."""
        await self.send_value(value=True)

    async def turn_off(self, **_kwargs: Any) -> None:
        """Send ``False`` to switch the parameter off."""
        await self.send_value(value=False)

    async def set_on_time(self, *, on_time: float) -> None:
        """
        Timed-on for a generic switch parameter.

        aiohomematic writes a sibling ``ON_TIME`` parameter; the loom
        client addresses one parameter per data point, so this is a
        no-op placeholder until a paramset-level write is wired.
        """
        _LOGGER.debug(
            "set_on_time(%s) is not yet wired for the loom generic switch %s",
            on_time,
            self.parameter,
        )


# ENUM value lists that HA reads as a binary sensor, mapped to the option
# that means "on". Mirrors aiohomematic's
# ``_BINARY_SENSOR_TRUE_VALUE_DICT_FOR_VALUE_LIST`` (model/support.py) — a
# door ``STATE`` of ``CLOSED`` (index 0) MUST read as ``False``; resolving
# it to the option string would make every contact truthy ("CLOSED" is a
# non-empty string) and permanently "on" in HA.
_BINARY_SENSOR_TRUE_VALUE_BY_VALUE_LIST: Final[dict[tuple[str, ...], str]] = {
    ("CLOSED", "OPEN"): "OPEN",
    ("DRY", "RAIN"): "RAIN",
    ("STABLE", "NOT_STABLE"): "NOT_STABLE",
}


class DpBinarySensor(_GenericEntitySurface, DataPoint):
    """Read-only BOOL parameter (or an ENUM retyped to a binary sensor)."""

    _category: ClassVar[DataPointCategory] = DataPointCategory.BinarySensor

    def _as_bool(self, raw: Any) -> bool | None:
        """Convert the raw wire value to the HA ``is_on`` bool (aiohomematic parity)."""
        if raw is None:
            return None
        if isinstance(raw, bool):
            return raw
        value_list: tuple[str, ...] = self.value_list
        if value_list:
            true_value = _BINARY_SENSOR_TRUE_VALUE_BY_VALUE_LIST.get(tuple(value_list))
            if isinstance(raw, int) and 0 <= raw < len(value_list):
                raw = value_list[raw]
            if true_value is not None and isinstance(raw, str):
                return raw == true_value
        return bool(raw)

    @property
    def value(self) -> bool | None:
        """Return the binary state as ``bool`` (never the ENUM option string)."""
        override = getattr(self, "_value_override", _UNSET)
        if override is not _UNSET:
            return self._as_bool(override)
        return self._as_bool(DataPoint.value.fget(self))  # type: ignore[attr-defined]

    @value.setter
    def value(self, new_value: Any) -> None:
        """Store an HA-written value (optimistic); the next daemon update clears it."""
        self._value_override = new_value

    @property
    def default(self) -> bool | None:
        """Return the parameter default as ``bool`` (HA restores this when unset)."""
        return self._as_bool(getattr(self.summary, "default", None))


class DpSensor(_GenericEntitySurface, DataPoint):
    """Read-only non-BOOL parameter."""

    _category: ClassVar[DataPointCategory] = DataPointCategory.Sensor


class DpSelect(_GenericEntitySurface, DataPoint):
    """ENUM-typed parameter (read+write)."""

    _category: ClassVar[DataPointCategory] = DataPointCategory.Select


class DpText(_GenericEntitySurface, DataPoint):
    """STRING parameter (read+write)."""

    _category: ClassVar[DataPointCategory] = DataPointCategory.Text


class BaseDpNumber(_GenericEntitySurface, DataPoint):
    """Numeric parameter (INTEGER / FLOAT), read+write."""

    _category: ClassVar[DataPointCategory] = DataPointCategory.Number


class DpAction(_GenericEntitySurface, DataPoint):
    """Write-only parameter with no value list."""

    _category: ClassVar[DataPointCategory] = DataPointCategory.Action

    async def send_action(self, value: Any = True) -> None:
        """Send the given value to the write-only parameter."""
        await self.send_value(value=value)


class DpButton(DpAction):
    """Write-only ACTION parameter HA exposes as a momentary button."""

    _category: ClassVar[DataPointCategory] = DataPointCategory.Button

    async def press(self) -> None:
        """Send ``True`` to trigger the momentary button press."""
        await self.send_value(value=True)


class DpActionSelect(DpSelect):
    """ENUM-typed write-only parameter."""

    _category: ClassVar[DataPointCategory] = DataPointCategory.ActionSelect


class BaseDpActionNumber(BaseDpNumber):
    """Numeric write-only parameter."""

    _category: ClassVar[DataPointCategory] = DataPointCategory.ActionNumber


# ---- factory ----

_WRITABLE_BY_TYPE: dict[str, type[DataPoint]] = {
    "BOOL": DpSwitch,
    "ENUM": DpSelect,
    "FLOAT": BaseDpNumber,
    "INTEGER": BaseDpNumber,
    "STRING": DpText,
}


def resolve_generic_class(
    *, type_token: str | None, read: bool, write: bool, has_value_list: bool
) -> type[DataPoint]:
    """
    Pick the ``Dp*`` class for a parameter from its type + operations.

    Mirrors aiohomematic's resolver tree (see module docstring).
    """
    token = (type_token or "").upper()
    if write:
        if token == "ACTION":  # noqa: S105 # nosec B105 — parameter type token, not a secret
            return DpSwitch if read else DpButton
        if not read:  # write-only, typed
            if has_value_list:
                return DpActionSelect
            if token in ("FLOAT", "INTEGER"):
                return BaseDpActionNumber
            return DpAction
        return _WRITABLE_BY_TYPE.get(token, DpText)
    # read-only
    return (
        DpBinarySensor if token == "BOOL" else DpSensor  # noqa: S105 # nosec B105 — type token, not a secret
    )


# The daemon derives the authoritative DataPointCategory from the full
# paramset + CONTROL context; clients spawn entities off ``category``
# rather than re-deriving from raw (type, operations) — see the
# DataPointSummary.category contract in the daemon's openapi.yaml. The
# heuristic resolver below is the fallback only when the daemon omits
# the category (e.g. a DP that does not implement the categorised
# surface).
_CLASS_BY_CATEGORY: dict[str, type[DataPoint]] = {
    cls._category.value: cls
    for cls in (
        DpSwitch,
        DpBinarySensor,
        DpSensor,
        DpSelect,
        DpText,
        BaseDpNumber,
        DpAction,
        DpButton,
        DpActionSelect,
        BaseDpActionNumber,
    )
}


def make_generic_data_point(
    *,
    summary: Any,
    device_address: str,
    channel_number: int,
    store: LoomStore,
) -> DataPoint:
    """Store data-point factory: build the categorised ``Dp*`` instance."""
    cls: type[DataPoint] | None = None
    if category := getattr(summary, "category", None):
        cls = _CLASS_BY_CATEGORY.get(str(category))
    if cls is None:
        ops = summary.operations
        cls = resolve_generic_class(
            type_token=summary.type,
            read=bool(ops.read),
            write=bool(ops.write),
            has_value_list=bool(summary.value_list),
        )
    return cls(
        summary=summary,
        device_address=device_address,
        channel_number=channel_number,
        store=store,
    )


__all__ = [
    "BaseDpActionNumber",
    "BaseDpNumber",
    "DpAction",
    "DpActionSelect",
    "DpBinarySensor",
    "DpButton",
    "DpSelect",
    "DpSensor",
    "DpSwitch",
    "DpText",
    "make_generic_data_point",
    "resolve_generic_class",
]
