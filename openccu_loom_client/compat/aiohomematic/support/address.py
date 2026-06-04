# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Address-string helpers — mirrors ``aiohomematic.support.address``."""

from __future__ import annotations


def get_device_address(*, address: str) -> str:
    """
    Strip the trailing ``:channel`` segment from a channel address.

    ``"VCU0001:3"`` → ``"VCU0001"``. A bare device address is
    returned unchanged.
    """
    return address.split(":", 1)[0]


__all__ = ["get_device_address"]
