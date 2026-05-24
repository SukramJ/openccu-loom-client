# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Async Python REST + WebSocket client for the openccu-loom daemon.

The public surface intentionally mirrors what the
`homematicip_local` Home Assistant integration imports from
`aiohomematic` today — see `compat/aiohomematic/` for the namespace
shim that ships alongside this package.

Stable wire types are re-exported from the sister package
``openccu-loom-types`` (Pydantic models + enums, regenerated on every
daemon release). This package adds the transport, event-bus and
domain-wrapper layers on top.
"""

from __future__ import annotations

from openccu_loom_client._version import __version__
from openccu_loom_client.auth import BasicAuth, BearerAuth, SessionAuth
from openccu_loom_client.client import LoomClient
from openccu_loom_client.config import LoomConfig
from openccu_loom_client.exceptions import (
    BaseLoomException,
    LoomAuthError,
    LoomBadRequestError,
    LoomConflictError,
    LoomForbiddenError,
    LoomHttpError,
    LoomNotFoundError,
    LoomRateLimitedError,
    LoomServiceUnreadyError,
    LoomTransportError,
    LoomUnsupportedError,
    LoomUpstreamUnavailableError,
    LoomValidationError,
)
from openccu_loom_client.store import LoomStore

__all__ = [
    "BaseLoomException",
    "BasicAuth",
    "BearerAuth",
    "LoomAuthError",
    "LoomBadRequestError",
    "LoomClient",
    "LoomConfig",
    "LoomConflictError",
    "LoomForbiddenError",
    "LoomHttpError",
    "LoomNotFoundError",
    "LoomRateLimitedError",
    "LoomServiceUnreadyError",
    "LoomStore",
    "LoomTransportError",
    "LoomUnsupportedError",
    "LoomUpstreamUnavailableError",
    "LoomValidationError",
    "SessionAuth",
    "__version__",
]
