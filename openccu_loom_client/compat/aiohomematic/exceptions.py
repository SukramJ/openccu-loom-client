# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Aiohomematic-compatible exception aliases."""

from __future__ import annotations

from openccu_loom_client.exceptions import (
    BaseLoomException,
    LoomAuthError,
    LoomTransportError,
    LoomValidationError,
)

# Direct aliases — homematicip_local uses both classes as ``except``
# targets, never as instance constructors that depend on the original
# signature.
BaseHomematicException = BaseLoomException
AuthFailure = LoomAuthError
NoConnectionException = LoomTransportError
ValidationException = LoomValidationError


__all__ = [
    "AuthFailure",
    "BaseHomematicException",
    "NoConnectionException",
    "ValidationException",
]
