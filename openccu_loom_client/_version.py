# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Single source of truth for the package version.

The version literal lives in `const.py` (CalVer); this module merely
re-exports it as `__version__` so both `pyproject.toml` (dynamic
version) and `const.VERSION` stay in lock-step and can never drift.
"""

from __future__ import annotations

from openccu_loom_client.const import VERSION as __version__

__all__ = ["__version__"]
