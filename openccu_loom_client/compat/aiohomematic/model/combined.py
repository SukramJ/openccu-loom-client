# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
``aiohomematic.model.combined`` — multi-parameter data points.

:class:`CombinedDurationDp` folds a channel's ``DURATION_VALUE`` +
``DURATION_UNIT`` pair into one seconds-typed number entity (category
``number``), mirroring aiohomematic's combined timer: reads convert the
raw value through the unit factor (0 = s, 1 = min, 2 = h), writes pin
``DURATION_UNIT`` to seconds and send the integer value. The unique_id
carries the ``combined`` prefix
(``loom_combined_<address>_<channel>_duration``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from openccu_loom_types.rest import DataPointSummary

from openccu_loom_client.canonical import canonical_unique_id
from openccu_loom_client.compat.aiohomematic.model.generic import BaseDpNumber

if TYPE_CHECKING:
    from openccu_loom_client.model import DataPoint, Device
    from openccu_loom_client.store import LoomStore

PARAMETER_DURATION_VALUE: Final = "DURATION_VALUE"
PARAMETER_DURATION_UNIT: Final = "DURATION_UNIT"

# DURATION_UNIT → seconds factor. The wire value is the raw ENUM index
# (0 = seconds, 1 = minutes, 2 = hours); the option strings cover
# daemons that resolve the index before shipping.
_FACTOR_BY_UNIT: Final[dict[Any, int]] = {
    0: 1,
    1: 60,
    2: 3600,
    "S": 1,
    "M": 60,
    "H": 3600,
    "SECONDS": 1,
    "MINUTES": 60,
    "HOURS": 3600,
}


def _synthesize_summary(*, value_dp: DataPoint, translated_name: str | None = None) -> DataPointSummary:
    """Project the DURATION pair onto one seconds-typed summary."""
    return DataPointSummary.model_validate(
        {
            "parameter": "DURATION",
            # Internal scaffolding only — CombinedDurationDp.unique_id computes
            # the real `loom_combined_…` key (no daemon key for this synthetic
            # entity). A valid non-empty key satisfies the required field.
            "unique_id": value_dp.summary.unique_id,
            "type": "FLOAT",
            "unit": "s",
            "min": value_dp.min,
            "max": value_dp.max,
            "observed": True,
            "operations": {"read": True, "write": True, "event": False},
            # the daemon's calc-dps surface carries the locale-aware
            # label for the suppressed DURATION entry; reusing it here
            # names the combined number like the reference ("Zeitdauer").
            "translated_name": translated_name,
        }
    )


class CombinedDurationDp(BaseDpNumber):
    """Seconds-typed number combining ``DURATION_VALUE`` + ``DURATION_UNIT``."""

    def __init__(
        self,
        *,
        store: LoomStore,
        device: Device,
        channel_no: int,
        translated_name: str | None = None,
    ) -> None:
        """Bind the combined data point to its channel's DURATION pair."""
        value_dp = store.get_data_point(address=device.address, channel=channel_no, parameter=PARAMETER_DURATION_VALUE)
        if value_dp is None:
            msg = f"{device.address}:{channel_no} has no {PARAMETER_DURATION_VALUE} data point"
            raise ValueError(msg)
        super().__init__(
            summary=_synthesize_summary(value_dp=value_dp, translated_name=translated_name),
            device_address=device.address,
            channel_number=channel_no,
            store=store,
        )

    # ---- identity ----

    @property
    def unique_id(self) -> str:
        """Return ``loom_combined_<address>_<channel>_duration``."""
        return canonical_unique_id(
            serial_suffix=self._store.serial_suffix,
            address=f"{self.device_address}:{self.channel_number}",
            parameter="duration",
            prefix="combined",
        )

    # ---- underlying data points ----

    def _source_dp(self, *, parameter: str) -> DataPoint | None:
        """Return one of the underlying DURATION data points from the store."""
        return self._store.get_data_point(address=self.device_address, channel=self.channel_number, parameter=parameter)

    # ---- value ----

    @property
    def value(self) -> float | None:
        """Return the duration in seconds, derived from value × unit factor."""
        value_dp = self._source_dp(parameter=PARAMETER_DURATION_VALUE)
        if value_dp is None or (raw := value_dp.value) is None:
            return None
        factor = 1
        if (unit_dp := self._source_dp(parameter=PARAMETER_DURATION_UNIT)) is not None:
            factor = _FACTOR_BY_UNIT.get(unit_dp.summary.value, 1)
        return float(raw) * factor

    @value.setter
    def value(self, new_value: Any) -> None:
        """Ignore optimistic writes — the value always derives from the live pair."""
        del new_value

    @property
    def is_valid(self) -> bool:
        """Return whether the underlying duration value has been observed."""
        return self.value is not None

    async def send_value(self, *, value: Any, **_kwargs: Any) -> None:
        """Write the duration in seconds: pin the unit to seconds, then the value."""
        await self._store.set_value(
            address=self.device_address,
            channel=self.channel_number,
            parameter=PARAMETER_DURATION_UNIT,
            value=0,
        )
        await self._store.set_value(
            address=self.device_address,
            channel=self.channel_number,
            parameter=PARAMETER_DURATION_VALUE,
            value=int(value),
        )

    async def load_data_point_value(self, *, call_source: Any = None) -> None:
        """Re-read both underlying DURATION data points from the daemon."""
        del call_source
        for parameter in (PARAMETER_DURATION_VALUE, PARAMETER_DURATION_UNIT):
            await self._store.refresh_data_point(
                address=self.device_address,
                channel=self.channel_number,
                parameter=parameter,
            )


def channel_has_duration_pair(*, store: LoomStore, address: str, channel_no: int) -> bool:
    """Return whether a channel carries both DURATION_VALUE and DURATION_UNIT."""
    return all(
        store.get_data_point(address=address, channel=channel_no, parameter=parameter) is not None
        for parameter in (PARAMETER_DURATION_VALUE, PARAMETER_DURATION_UNIT)
    )


__all__ = [
    "PARAMETER_DURATION_UNIT",
    "PARAMETER_DURATION_VALUE",
    "CombinedDurationDp",
    "channel_has_duration_pair",
]
