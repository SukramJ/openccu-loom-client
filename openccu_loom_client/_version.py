# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Single source of truth for the package version.

Pulled from the installed distribution metadata so the version in
`pyproject.toml` is authoritative and a separate string here can
never drift.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _dist_version

try:
    __version__ = _dist_version("openccu-loom-client")
except PackageNotFoundError:
    # Editable install before `pip install -e .` resolved the dist-info.
    __version__ = "0.0.0+local"
