# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Async Python REST + WebSocket client for the openccu-loom daemon.

The public surface intentionally mirrors what the
`homematicip_local` Home Assistant integration imports from
`aiohomematic` today — see `compat/aiohomematic/` for the namespace
shim that ships alongside this package.

Stable wire types live in ``openccu_loom_client.wire`` (Pydantic models
+ enums, regenerated from the daemon's contract on every daemon release
and never hand-edited). This package adds the transport, event-bus and
domain-wrapper layers on top.
"""

from __future__ import annotations

from openccu_loom_client._version import __version__
from openccu_loom_client.auth import BasicAuth, BearerAuth, NoAuth, SessionAuth
from openccu_loom_client.boxpairing import BoxPairingResult, BoxPairingSession, start_box_pairing
from openccu_loom_client.capabilities import ALWAYS_ON, Capability
from openccu_loom_client.client import LoomClient
from openccu_loom_client.config import BoxIngressConfig, LoomConfig
from openccu_loom_client.exceptions import (
    BaseLoomException,
    LoomAuthError,
    LoomBadRequestError,
    LoomBoxGateError,
    LoomBoxPairingError,
    LoomBoxTokenError,
    LoomConflictError,
    LoomFeatureUnavailableError,
    LoomForbiddenError,
    LoomHttpError,
    LoomIncompatibleVersionError,
    LoomInternalError,
    LoomNotFoundError,
    LoomPairingNotLocalError,
    LoomPairingOffError,
    LoomPairingSlowDownError,
    LoomRateLimitedError,
    LoomServiceUnreadyError,
    LoomTransportError,
    LoomUnsupportedError,
    LoomUnsupportedOperationError,
    LoomUpstreamUnavailableError,
    LoomValidationError,
)
from openccu_loom_client.pairing import PairingFingerprintMismatchError, PairingSession, start_pairing
from openccu_loom_client.store import LoomStore

__all__ = [
    # General
    "ALWAYS_ON",
    "BaseLoomException",
    "BasicAuth",
    "BearerAuth",
    "BoxIngressConfig",
    "BoxPairingResult",
    "BoxPairingSession",
    "Capability",
    "LoomAuthError",
    "LoomBadRequestError",
    "LoomBoxGateError",
    "LoomBoxPairingError",
    "LoomBoxTokenError",
    "LoomClient",
    "LoomConfig",
    "LoomConflictError",
    "LoomFeatureUnavailableError",
    "LoomForbiddenError",
    "LoomHttpError",
    "LoomIncompatibleVersionError",
    "LoomInternalError",
    "LoomNotFoundError",
    "LoomPairingNotLocalError",
    "LoomPairingOffError",
    "LoomPairingSlowDownError",
    "LoomRateLimitedError",
    "LoomServiceUnreadyError",
    "LoomStore",
    "LoomTransportError",
    "LoomUnsupportedError",
    "LoomUnsupportedOperationError",
    "LoomUpstreamUnavailableError",
    "LoomValidationError",
    "NoAuth",
    "PairingFingerprintMismatchError",
    "PairingSession",
    "SessionAuth",
    "__version__",
    "start_box_pairing",
    "start_pairing",
]
