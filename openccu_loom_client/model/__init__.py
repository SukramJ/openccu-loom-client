# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Domain models — store-aware wrappers around the wire-type Pydantic models.

The Pydantic models in ``openccu_loom_types.rest`` are flat data
containers. These classes add the navigation graph (Device → Channels
→ DataPoints), the lifecycle hook into the store, and the
:meth:`DataPoint.send_value` action that round-trips back to the
daemon's REST surface.

Each wrapper holds a back-reference to its :class:`LoomStore` so callers
can navigate from any entity without having to thread the store
through.
"""

from __future__ import annotations

from openccu_loom_client.model.channel import Channel
from openccu_loom_client.model.custom_data_point import CustomDataPoint
from openccu_loom_client.model.data_point import DataPoint
from openccu_loom_client.model.device import Device
from openccu_loom_client.model.program import Program
from openccu_loom_client.model.sysvar import Sysvar

__all__ = ["Channel", "CustomDataPoint", "DataPoint", "Device", "Program", "Sysvar"]
