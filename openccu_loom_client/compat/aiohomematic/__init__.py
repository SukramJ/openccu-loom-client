# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
``aiohomematic``-compatible namespace backed by openccu-loom-client.

This package mirrors the import paths the ``homematicip_local`` Home
Assistant integration uses today, so the cutover from a direct CCU
client (aiohomematic) to a daemon-mediated one (openccu-loom-client)
can land one file at a time.

The shim is intentionally surface-only: it re-exports types, aliases
classes and stubs entry points. Anything that needs daemon-mediated
behaviour (``send_value``, ``execute``, …) routes through the underlying
``openccu_loom_client`` machinery.

It does **not** make ``from aiohomematic.* import …`` resolve to this
package. There is no namespace aliasing anywhere here — no ``sys.modules``
assignment, no ``__path__`` manipulation, no ``entry_points`` — and
``homematicip_local`` imports the explicit
``openccu_loom_client.compat.aiohomematic.*`` path at every one of its call
sites. A module in this tree that nothing imports by that explicit path is
therefore unreachable rather than merely unused, which is why thirteen of
them were removed rather than kept for a caller that could not arrive.

Versions follow the host package, so downstream conditional checks
on ``aiohomematic.__version__`` keep working.
"""

from __future__ import annotations

from openccu_loom_client._version import __version__
from openccu_loom_client.compat.aiohomematic import ccu_translations

__all__ = [
    # General
    "__version__",
    "ccu_translations",
]
