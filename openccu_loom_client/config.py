# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Connection configuration for the openccu-loom daemon."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openccu_loom_client.auth import AuthMethod


# Default daemon ports. The daemon's default config publishes:
# - HTTP REST + WS on 8080 (cleartext)
# - HTTPS REST + WSS on 8443 (TLS)
# See `config.example.yaml` in the daemon repo.
DEFAULT_HTTP_PORT = 8080
DEFAULT_HTTPS_PORT = 8443

# Daemon's REST surface is mounted at /api/v1 per `assets/openapi.yaml`.
DEFAULT_BASE_PATH = "/api/v1"

# Conservative request timeout. Most REST operations on the daemon
# are sub-second; the longest leg is /snapshot (one-shot JSON blob,
# size-dependent — separate timeout in the snapshot caller).
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0


@dataclass(slots=True, kw_only=True)
class LoomConfig:
    """
    Configuration for connecting to one openccu-loom daemon.

    All wire-level transport, auth and resilience knobs live here so the
    rest of the client never has to thread them through call sites.
    """

    host: str
    auth: AuthMethod
    port: int | None = None
    tls: bool = True
    verify_tls: bool = True
    base_path: str = DEFAULT_BASE_PATH
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS
    # Extra headers attached to every REST request (e.g. for tracing).
    extra_headers: dict[str, str] = field(default_factory=dict)
    # Client name advertised in `User-Agent` so the daemon can attribute
    # request load in audit logs. Override per deployment if running
    # multiple HA instances against one daemon.
    user_agent: str = "openccu-loom-client"

    def __post_init__(self) -> None:
        """Default the port from the TLS flag when not explicitly set."""
        if self.port is None:
            object.__setattr__(
                self,
                "port",
                DEFAULT_HTTPS_PORT if self.tls else DEFAULT_HTTP_PORT,
            )

    @property
    def http_base_url(self) -> str:
        """Full REST base URL including scheme, host, port and `/api/v1`."""
        scheme = "https" if self.tls else "http"
        return f"{scheme}://{self.host}:{self.port}{self.base_path}"

    @property
    def ws_url(self) -> str:
        """WebSocket URL for the /events endpoint."""
        scheme = "wss" if self.tls else "ws"
        return f"{scheme}://{self.host}:{self.port}{self.base_path}/events"
