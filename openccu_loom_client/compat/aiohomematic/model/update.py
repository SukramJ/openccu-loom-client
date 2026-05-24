# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Update-platform type marker."""

from __future__ import annotations

from openccu_loom_client.compat.aiohomematic.model.hub import HmUpdate


class DpUpdate(HmUpdate):
    """HA update entity backed by daemon firmware metadata."""


__all__ = ["DpUpdate"]
