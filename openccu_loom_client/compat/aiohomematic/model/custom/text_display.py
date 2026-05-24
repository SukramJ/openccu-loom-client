# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Re-export so ``from aiohomematic.model.custom.text_display import …`` works."""

from __future__ import annotations

from openccu_loom_client.compat.aiohomematic.model.custom import CustomDpTextDisplay

__all__ = ["CustomDpTextDisplay"]
