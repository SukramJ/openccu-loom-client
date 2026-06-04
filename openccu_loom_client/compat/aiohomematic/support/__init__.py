# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Aiohomematic-compatible ``support`` helpers."""

from __future__ import annotations

import socket


def find_free_port() -> int:
    """
    Return a currently-free TCP port on the local machine.

    Re-implementation of ``aiohomematic.support.find_free_port``.
    The daemon owns its own bindings now — this helper survives only
    because HA's config-flow still threads it through legacy code
    paths that may be cleaned up in the cutover.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return int(s.getsockname()[1])


def to_bool(value: object) -> bool:
    """
    Coerce a CCU-typed value into a Python bool.

    Mirrors the lenient parsing aiohomematic does for legacy reasons:
    truthy strings (``"true"``, ``"1"``, ``"on"``, ``"yes"``) → True,
    everything else (incl. ``None``) → False.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "on", "yes"}
    return False


__all__ = ["find_free_port", "to_bool"]
