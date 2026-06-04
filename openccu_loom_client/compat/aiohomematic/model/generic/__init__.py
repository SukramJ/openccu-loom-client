# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""``aiohomematic.model.generic`` — categorised data-point classes.

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

from openccu_loom_client.events.types import data_point_event_key
from openccu_loom_client.model import DataPoint

if TYPE_CHECKING:
    from openccu_loom_client.store import LoomStore

_LOGGER: Final = logging.getLogger(__name__)


class _GenericEntitySurface:
    """Entity-facing attributes HA reads off a generic data point.

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

    @property
    def is_valid(self) -> bool:
        return self.value is not None  # type: ignore[attr-defined]

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
        await self.send_value(value=True)

    async def turn_off(self, **_kwargs: Any) -> None:
        await self.send_value(value=False)

    async def set_on_time(self, *, on_time: float) -> None:
        """Timed-on for a generic switch parameter.

        aiohomematic writes a sibling ``ON_TIME`` parameter; the loom
        client addresses one parameter per data point, so this is a
        no-op placeholder until a paramset-level write is wired.
        """
        _LOGGER.debug(
            "set_on_time(%s) is not yet wired for the loom generic switch %s",
            on_time,
            self.parameter,
        )


class DpBinarySensor(_GenericEntitySurface, DataPoint):
    """Read-only BOOL parameter."""

    _category: ClassVar[DataPointCategory] = DataPointCategory.BinarySensor


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
        await self.send_value(value=value)


class DpButton(DpAction):
    """Write-only ACTION parameter HA exposes as a momentary button."""

    _category: ClassVar[DataPointCategory] = DataPointCategory.Button

    async def press(self) -> None:
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
    """Pick the ``Dp*`` class for a parameter from its type + operations.

    Mirrors aiohomematic's resolver tree (see module docstring).
    """
    token = (type_token or "").upper()
    if write:
        if token == "ACTION":  # nosec B105 — parameter type token, not a secret
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
        DpBinarySensor if token == "BOOL" else DpSensor  # nosec B105 — type token, not a secret
    )


def make_generic_data_point(
    *,
    summary: Any,
    device_address: str,
    channel_number: int,
    store: LoomStore,
) -> DataPoint:
    """Store data-point factory: build the categorised ``Dp*`` instance."""
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
