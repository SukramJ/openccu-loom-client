# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
``aiohomematic.model.calculated`` — daemon-computed data points.

The daemon derives calculated data points (``WINDOW_OPEN``, dew point,
apparent temperature, …) from a channel's generic parameters and ships
them via ``GET /devices/{addr}/channels/{no}/calc-dps``; value changes
ride the regular ``datapoint.value_changed`` stream with the calculated
parameter name. The classes here subclass the generic ``Dp*`` twins so
the HA platforms treat them like any sensor/binary sensor — only the
``unique_id`` differs: it carries the ``calculated`` prefix, mirroring
aiohomematic's ``calculated_<address>_<channel>_<parameter>`` keys.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from openccu_loom_client.compat.aiohomematic.model.generic import DpBinarySensor, DpSensor
from openccu_loom_client.model import DataPoint
from openccu_loom_client.wire.rest import CalculatedDPSummary, DataPointSummary

if TYPE_CHECKING:
    from openccu_loom_client.store import LoomStore


class _CalculatedKeyMixin:
    """unique_id override: the ``calculated`` prefix segregates the keys."""

    if TYPE_CHECKING:
        # Host attributes the concrete ``CalculatedDp*`` twin provides (via
        # ``DpSensor`` / ``DpBinarySensor`` → ``DataPoint``).
        summary: DataPointSummary
        parameter: str
        value: Any
        device_address: str
        channel_number: int
        _store: LoomStore

    # Daemon verdict on the derived value, carried by the calc-dps record
    # (``available``, daemon API 3.13.0). Defaults to True so a record from an
    # older daemon — where the field is absent — keeps the previous behaviour
    # instead of silencing every calculated entity.
    _calculated_available: bool = True

    def apply_calculated_availability(self, *, available: bool) -> None:
        """
        Record the daemon's verdict on the derived value.

        Called wherever the client reads a calc-dps record: the bootstrap
        attach and the explicit re-read behind ``load_data_point_value``.
        """
        self._calculated_available = available

    @property
    def is_valid(self) -> bool:
        """
        Return whether the derived value is a confirmed reading.

        The generic rule — "a value is present" — cannot answer this for a
        calculated data point. It is computed from a channel's ordinary
        readings, and those can be read-but-unusable: the CCU flags a
        measurement fault through the paired ``…_STATUS`` parameter, or the
        reading falls outside the bounds the device declares. The derived
        number keeps updating right through such a fault, so only the daemon's
        ``available`` flag distinguishes a dew point from a dew point computed
        off a thermometer stuck at OVERFLOW.

        Home Assistant restores an entity's previous state exactly when this
        reads False, and stops rendering the live value — which is the point:
        a wrong number is worse than the last known good one.
        """
        return self._calculated_available and self.value is not None

    @property
    def name(self) -> str:
        """
        Return the calc DP's display name from the daemon's label.

        Generic DPs name themselves from the daemon's per-parameter label
        (``parameter_label``); calculated DPs carry their full locale-aware
        label in ``translated_name`` instead. Without this override the
        generic fallback returns the raw parameter (e.g. ``DEW_POINT``),
        which the HA integration then re-injects into the composed entity
        name — so the calc entity would read ``… DEW_POINT`` where the
        direct-CCU twin reads ``… Dew Point``.
        """
        translated = self.summary.translated_name
        return translated or self.parameter

    @property
    def unique_id(self) -> str:
        """Return the daemon-owned canonical key (``loom_calculated_…``), carried via the summary (J1)."""
        return self.summary.unique_id

    async def load_data_point_value(self, *, call_source: Any = None) -> None:
        """Re-read this calculated DP from the daemon's calc-dps endpoint."""
        await self._store.refresh_calculated_data_point(
            address=self.device_address,
            channel=self.channel_number,
            name=self.parameter,
        )


class CalculatedDpSensor(_CalculatedKeyMixin, DpSensor):
    """Daemon-calculated sensor (dew point, apparent temperature, …)."""


class CalculatedDpBinarySensor(_CalculatedKeyMixin, DpBinarySensor):
    """Daemon-calculated binary sensor (window open, alarms, …)."""


def synthesize_summary(*, calc: CalculatedDPSummary) -> DataPointSummary:
    """
    Project a calculated-DP wire record onto the generic summary shape.

    Calculated DPs are read-only and eventful; binary ones get a BOOL
    type token so the binary-sensor value conversion applies.
    """
    return DataPointSummary.model_validate(
        {
            "parameter": calc.name,
            # The daemon ships the canonical key on the calculated record (J1);
            # carry it through so the projected summary keys identically.
            "unique_id": calc.unique_id,
            "value": calc.value,
            "observed": calc.observed,
            "modified_at": calc.modified_at,
            "operations": {"read": True, "write": False, "event": True},
            "category": calc.category or "sensor",
            "type": "BOOL" if calc.category == "binary_sensor" else None,
            # daemon api 1.5.0 ships the locale-aware label for calc DPs
            # (same chain as generic DPs); the generic naming path picks
            # it up like any other daemon label.
            "translated_name": calc.translated_name,
        }
    )


def make_calculated_data_point(
    *,
    summary: CalculatedDPSummary,
    device_address: str,
    channel_number: int,
    store: LoomStore,
) -> DataPoint:
    """Build the categorised calculated data point for one wire record."""
    cls: type[CalculatedDpSensor | CalculatedDpBinarySensor] = (
        CalculatedDpBinarySensor if calc_is_binary(summary=summary) else CalculatedDpSensor
    )
    dp = cls(
        summary=synthesize_summary(calc=summary),
        device_address=device_address,
        channel_number=channel_number,
        store=store,
    )
    # `available` has no slot on the generic summary shape — generic DPs do not
    # carry a per-value verdict — so it rides on the instance instead of
    # through `synthesize_summary`.
    dp.apply_calculated_availability(available=summary.available)
    return dp


def calc_is_binary(*, summary: CalculatedDPSummary) -> bool:
    """Return whether the calculated DP reads as a binary sensor."""
    return summary.category == "binary_sensor"


__all__ = [
    "CalculatedDpBinarySensor",
    "CalculatedDpSensor",
    "make_calculated_data_point",
]
