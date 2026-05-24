# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""``aiohomematic.model.event`` — re-export of ``ClickEvent``."""

from __future__ import annotations

from openccu_loom_client.compat.aiohomematic.central.events import DeviceTriggerEvent as ClickEvent

__all__ = ["ClickEvent"]
