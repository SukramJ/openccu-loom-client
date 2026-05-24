# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Week-profile data-point type marker."""

from __future__ import annotations

from openccu_loom_client.model import DataPoint


class WeekProfileDataPoint(DataPoint):
    """A DP that exposes a thermostat week-program on its channel.

    HA-side code matches against this class to decide which channels
    own a climate schedule. The actual schedule is fetched via the
    daemon's ``/devices/{addr}/channels/{n}/week_profile`` endpoint
    once HA needs to display or write it.
    """


__all__ = ["WeekProfileDataPoint"]
