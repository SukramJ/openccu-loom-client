# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
``aiohomematic``-compatible namespace backed by openccu-loom-client.

This package mirrors the import paths the ``homematicip_local`` Home
Assistant integration uses today, so the cutover from a direct CCU
client (aiohomematic) to a daemon-mediated one (openccu-loom-client)
can land one file at a time.

The shim is intentionally surface-only: it re-exports types, aliases
classes, and stubs entry points so ``from aiohomematic.* import …``
statements resolve. Anything that needs daemon-mediated behaviour
(``send_value``, ``execute``, …) routes through the underlying
``openccu_loom_client`` machinery.

Versions follow the host package, so downstream conditional checks
on ``aiohomematic.__version__`` keep working.
"""

from __future__ import annotations

from openccu_loom_client._version import __version__
from openccu_loom_client.compat.aiohomematic import ccu_translations

__all__ = ["__version__", "ccu_translations"]
