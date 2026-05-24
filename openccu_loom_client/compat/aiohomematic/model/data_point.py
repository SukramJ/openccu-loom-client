# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""``aiohomematic.model.data_point`` compatibility.

Two symbols are exported:

- :class:`CallbackDataPoint` — direct alias for
  :class:`openccu_loom_client.model.DataPoint`.
- :class:`CallParameterCollector` — gathers multi-parameter writes
  and dispatches them as one paramset PUT. Replaces aiohomematic's
  ``CallParameterCollector`` (which served the same purpose against
  the CCU's XML-RPC ``putParamset`` call).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self

from openccu_loom_client.model import DataPoint
from openccu_loom_client.operations._base import _OperationsBase  # noqa: F401 — re-export below

if TYPE_CHECKING:
    from types import TracebackType

    from openccu_loom_client.operations.datapoints import DataPointsOperations

CallbackDataPoint = DataPoint


class CallParameterCollector:
    """Batch multiple parameter writes into one daemon paramset PUT.

    HA-side code that wants atomic multi-DP writes (cover position +
    tilt, light brightness + colour, …) constructs one of these,
    pushes individual ``(parameter, value)`` pairs onto it, and runs
    ``await collector.send_data()``. Internally we collapse to one
    ``PUT /devices/{addr}/paramsets/{key}`` call so the daemon
    forwards the whole set to the CCU atomically.

    Use as an async-context-manager OR call ``send_data`` directly.
    """

    __slots__ = (
        "_address",
        "_channel",
        "_committed",
        "_datapoints_ops",
        "_paramset_key",
        "_values",
    )

    def __init__(
        self,
        *,
        datapoints_ops: DataPointsOperations,
        address: str,
        channel: int,
        paramset_key: str = "VALUES",
    ) -> None:
        self._datapoints_ops = datapoints_ops
        self._address = address
        self._channel = channel
        self._paramset_key = paramset_key
        self._values: dict[str, Any] = {}
        self._committed = False

    def add_data(self, *, parameter: str, value: Any) -> None:
        """Stage one ``parameter → value`` mapping for the batch."""
        self._values[parameter] = value

    async def send_data(self) -> None:
        """Flush every staged value as one paramset PUT.

        Idempotent: a second call with no new ``add_data`` is a no-op.
        After commit the collector is closed to further mutation.
        """
        if self._committed or not self._values:
            self._committed = True
            return
        await self._datapoints_ops.put_paramset(
            address=self._address,
            paramset_key=self._paramset_key,
            values=dict(self._values),
        )
        self._committed = True

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        # Only flush on clean exit; on error, let the failure propagate.
        if exc_type is None:
            await self.send_data()


__all__ = ["CallParameterCollector", "CallbackDataPoint"]
