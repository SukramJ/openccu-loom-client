# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Common ground for all operation modules: just the transport handle."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openccu_loom_client.transport.http import HttpTransport


class _OperationsBase:
    """Holds the transport handle the concrete modules use."""

    __slots__ = ("_transport",)

    def __init__(self, *, transport: HttpTransport) -> None:
        self._transport = transport
