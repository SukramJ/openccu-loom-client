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

from openccu_loom_types.rest import CalculatedDPSummary, DataPointSummary

from openccu_loom_client.canonical import canonical_unique_id
from openccu_loom_client.compat.aiohomematic.model.generic import DpBinarySensor, DpSensor
from openccu_loom_client.model import DataPoint

if TYPE_CHECKING:
    from openccu_loom_client.store import LoomStore


class _CalculatedKeyMixin:
    """unique_id override: the ``calculated`` prefix segregates the keys."""

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
        translated = getattr(self.summary, "translated_name", None)  # type: ignore[attr-defined]
        return translated or self.parameter  # type: ignore[attr-defined,no-any-return]

    @property
    def unique_id(self) -> str:
        """Return ``loom_calculated_<address>_<channel>_<parameter>``."""
        return canonical_unique_id(
            serial_suffix=self._store.serial_suffix,  # type: ignore[attr-defined]
            address=f"{self.device_address}:{self.channel_number}",  # type: ignore[attr-defined]
            parameter=self.parameter,  # type: ignore[attr-defined]
            prefix="calculated",
        )

    async def load_data_point_value(self, *, call_source: Any = None) -> None:
        """Re-read this calculated DP from the daemon's calc-dps endpoint."""
        await self._store.refresh_calculated_data_point(  # type: ignore[attr-defined]
            address=self.device_address,  # type: ignore[attr-defined]
            channel=self.channel_number,  # type: ignore[attr-defined]
            name=self.parameter,  # type: ignore[attr-defined]
        )


class CalculatedDpSensor(_CalculatedKeyMixin, DpSensor):
    """Daemon-calculated sensor (dew point, apparent temperature, …)."""


class CalculatedDpBinarySensor(_CalculatedKeyMixin, DpBinarySensor):
    """Daemon-calculated binary sensor (window open, alarms, …)."""


def synthesize_summary(calc: CalculatedDPSummary) -> DataPointSummary:
    """
    Project a calculated-DP wire record onto the generic summary shape.

    Calculated DPs are read-only and eventful; binary ones get a BOOL
    type token so the binary-sensor value conversion applies.
    """
    return DataPointSummary.model_validate(
        {
            "parameter": calc.name,
            "value": calc.value,
            "observed": calc.observed,
            "modified_at": calc.modified_at,
            "operations": {"read": True, "write": False, "event": True},
            "category": calc.category or "sensor",
            "type": "BOOL" if calc.category == "binary_sensor" else None,
            # daemon api 1.5.0 ships the locale-aware label for calc DPs
            # (same chain as generic DPs); the generic naming path picks
            # it up like any other daemon label.
            "translated_name": getattr(calc, "translated_name", None),
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
    cls: type[DataPoint] = (
        CalculatedDpBinarySensor if calc_is_binary(summary) else CalculatedDpSensor
    )
    return cls(
        summary=synthesize_summary(summary),
        device_address=device_address,
        channel_number=channel_number,
        store=store,
    )


def calc_is_binary(summary: CalculatedDPSummary) -> bool:
    """Return whether the calculated DP reads as a binary sensor."""
    return summary.category == "binary_sensor"


__all__ = [
    "CalculatedDpBinarySensor",
    "CalculatedDpSensor",
    "make_calculated_data_point",
]
