# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Aiohomematic-compatible CCU-translation helpers.

In the original library this module mapped device models to icon
names. The daemon now ships these mappings server-side and surfaces
them on device records — keeping the helper alive only to satisfy
HA-side imports until the call sites move to the new API.
"""

from __future__ import annotations


def get_device_icon(*, model: str) -> str | None:
    """
    Return ``None`` until the cutover wires the daemon's mapping.

    The daemon's ``DeviceSummary`` doesn't currently include an icon
    field; once it does this helper will fetch from the
    :class:`LoomStore`. Returning ``None`` lets HA fall back to its
    default device icon, which is the same behaviour HA exhibits
    when aiohomematic doesn't know a model.
    """
    return None


__all__ = ["get_device_icon"]
