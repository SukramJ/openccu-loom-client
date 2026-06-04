# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Climate schedule model stubs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ClimateWeekdaySchedule:
    """
    One day of a climate week-program.

    Aiohomematic carried the slot list (start-time + setpoint pairs)
    here; the daemon's schedule API stores them server-side, so this
    class is mostly a type marker for HA-side service-call schemas.
    """

    weekday: str = ""
    slots: list[dict[str, Any]] = field(default_factory=list)


__all__ = ["ClimateWeekdaySchedule"]
