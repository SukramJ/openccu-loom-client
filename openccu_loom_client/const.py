# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Package-wide constants.

Single source of truth for the package version (CalVer:
``YYYY.M.MICRO``). ``pyproject.toml`` reads this attribute via
setuptools' dynamic-version directive, and ``_version.py`` re-exports
it as ``__version__`` — so the string lives in exactly one place.
"""

from __future__ import annotations

from typing import Final

VERSION: Final = "2026.8.24"
