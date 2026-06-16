# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
``aiohomematic.client`` shim — only ``InterfaceConfig`` is used externally.

In the original library, ``InterfaceConfig`` configured one of the
CCU-side XML-RPC interfaces (port, callback host, …). The daemon now
manages every interface server-side, so this dataclass is a leaf
stub: HA still builds one per configured interface, but the values
are forwarded to the daemon as opaque config, not used to open
sockets.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from openccu_loom_types.enums import Interface


@dataclass(slots=True)
class InterfaceConfig:
    """
    Per-interface configuration carried from HA's config entry.

    Surface parity with ``aiohomematic.client.InterfaceConfig`` so the
    config-flow code path stays unchanged during the cutover. The
    daemon picks up the actual port binding from its own config — these
    fields are kept for documentation / telemetry only.
    """

    central_name: str
    interface: Interface
    port: int | None = None
    remote_path: str | None = None


__all__: Final = [
    # General
    "Interface",
    "InterfaceConfig",
]
