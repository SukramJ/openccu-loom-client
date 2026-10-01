# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Connection configuration for the openccu-loom daemon."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openccu_loom_client.auth import AuthMethod


# The daemon's default port, for cleartext and TLS alike.
#
# It serves ONE listener: `internal/config/bootstrap.go:148` and
# `internal/config/config.go:1915` both default `North.REST.Listen` to
# ":8119", the Home Assistant add-on's `rest_port` option defaults to 8119,
# and ADR 0044 records why the second listener was retired — HA Ingress does
# not forward arbitrary ports, so REST, SPA and the bootstrap surface were
# collapsed onto one. TLS is a property of that listener, not a second port.
#
# The two names are kept because `LoomConfig` picks between them, and because
# a future split would want them back; today they hold the same value.
#
# Previously 8080/8443, citing a `config.example.yaml` that does not exist in
# the daemon repository. 8080 was the pre-0.13.0 default — the daemon's
# add-on changelog records the move — and 8443 has no counterpart in the
# daemon at all.
DEFAULT_HTTP_PORT = 8119
DEFAULT_HTTPS_PORT = 8119

# Daemon's REST surface is mounted at /api/v1 per `assets/openapi.yaml`.
DEFAULT_BASE_PATH = "/api/v1"

# Conservative request timeout. Most REST operations on the daemon
# are sub-second; the longest leg is /snapshot (one-shot JSON blob,
# size-dependent — separate timeout in the snapshot caller).
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0

# Where an openccu-lite box mounts the daemon behind its web server. The box
# strips this prefix before forwarding, so the daemon itself still serves its
# API at ``base_path``.
DEFAULT_BOX_INGRESS_PATH_PREFIX = "/addons/loom"

# Ports the box's web server listens on; TLS terminates at the box.
DEFAULT_BOX_HTTPS_PORT = 443
DEFAULT_BOX_HTTP_PORT = 80


@dataclass(slots=True, kw_only=True)
class BoxIngressConfig:
    """
    Reach the daemon through an openccu-lite box's ingress instead of directly.

    The box serves the daemon at ``https://<box>/addons/loom/`` and fronts
    that path with a session gate: a request without a valid box session
    never reaches the daemon, the gate answers a redirect to the box's login
    page instead. These credentials are the box account the client logs in
    with to obtain that session; they open the gate and nothing more. The
    daemon behind it still authenticates the request through
    :attr:`LoomConfig.auth`.

    ``port`` defaults to 443 with :attr:`LoomConfig.tls` and to 80 without.
    """

    username: str
    # Kept out of the dataclass repr so a config dump or a traceback that
    # captures locals never carries the box password.
    password: str = field(repr=False)
    port: int | None = None
    path_prefix: str = DEFAULT_BOX_INGRESS_PATH_PREFIX


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
    # Ask the daemon to withhold devices whose onboarding is unfinished
    # (daemon ≥ 0.66.1). It answers `GET /snapshot?released_only=true` and a
    # `released_only: true` subscribe frame by dropping every frame about a
    # device the wizard has not released yet.
    #
    # Defaulted on because of what this package is: an ecosystem backend, and
    # the whole point of the daemon's release step is that an operator names
    # and places a device BEFORE an ecosystem adopts it — Home Assistant keeps
    # the entity ids it first saw, so adopting early makes the naming stick to
    # the wrong ones. A consumer building a configuration surface instead (the
    # role the daemon's own Config UI has) sets this False and sees everything.
    #
    # One flag for both planes on purpose: the daemon's contract says to pair
    # the REST query with the WS subscribe option "or the two drift" — a
    # snapshot without the device but a live push about it, or the reverse.
    # Older daemons ignore the unknown query parameter and the unknown frame
    # field, so this is inert against them.
    released_only: bool = True
    # How long bootstrap waits for the daemon to finish its southbound
    # bring-up before walking the snapshot anyway. Waiting matters because
    # `GET /snapshot` answers 200 with empty lists while the central is still
    # in `waiting_for_ccu` and never 5xx — bootstrapping early "succeeds" into
    # an empty model, and a consumer spawns no entities at all.
    #
    # It is bounded, and a timeout is not an error: the walk runs regardless
    # and the daemon's resync re-bootstraps once the CCU arrives. So the only
    # thing this trades is how long a caller's own setup blocks — Home
    # Assistant, for one, logs a config entry that takes minutes. Lower it
    # where a fast, possibly-empty start beats a slow, complete one; 0 skips
    # the wait entirely.
    readiness_wait_seconds: float = 180.0
    # Route every request through an openccu-lite box's ingress (see
    # :class:`BoxIngressConfig`). When set, ``host`` names the box, ``tls``
    # describes the box's listener, and ``port`` is unused — the box port
    # comes from :attr:`BoxIngressConfig.port`.
    box_ingress: BoxIngressConfig | None = None

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
        """
        Full REST base URL including scheme, host, port and `/api/v1`.

        In box-ingress mode the box port and the ingress path prefix take the
        place of the daemon port.
        """
        scheme = "https" if self.tls else "http"
        return f"{scheme}://{self._authority}{self._ingress_prefix}{self.base_path}"

    @property
    def ws_url(self) -> str:
        """WebSocket URL for the /events endpoint (through the box in ingress mode)."""
        scheme = "wss" if self.tls else "ws"
        return f"{scheme}://{self._authority}{self._ingress_prefix}{self.base_path}/events"

    @property
    def box_api_base_url(self) -> str | None:
        """
        Return the box's own API base — scheme, host and box port, no prefix.

        The box's login and logout live at the box root, not under the ingress
        prefix, so the session manager builds its URLs from this. ``None``
        unless :attr:`box_ingress` is set.
        """
        if self.box_ingress is None:
            return None
        scheme = "https" if self.tls else "http"
        return f"{scheme}://{self._authority}"

    @property
    def _authority(self) -> str:
        """Return ``host:port`` of the listener requests go to."""
        if self.box_ingress is None:
            return f"{self.host}:{self.port}"
        box_port = self.box_ingress.port
        if box_port is None:
            box_port = DEFAULT_BOX_HTTPS_PORT if self.tls else DEFAULT_BOX_HTTP_PORT
        return f"{self.host}:{box_port}"

    @property
    def _ingress_prefix(self) -> str:
        """Return the ingress path prefix (no trailing slash), or ``""`` outside ingress mode."""
        if self.box_ingress is None:
            return ""
        return self.box_ingress.path_prefix.rstrip("/")

    def create_central_url(self) -> str:
        """
        Return the central's base URL, without the API path.

        Scheme, authority and — in box-ingress mode — the ingress prefix.
        Mirrors aiohomematic's ``CentralConfig.create_central_url`` so the
        compat surface satisfies ``CentralConfigProtocol``. Consumers like
        homematicip_local's device-icon handler append their own path, so
        this deliberately omits ``base_path`` (unlike :attr:`http_base_url`).

        Box-ingress caveat: a consumer builds its OWN requests from this URL,
        outside this client's transport, so they carry no ``?sid=`` — the
        box's gate turns them away. The URL still points at the ingress (the
        daemon's direct port may be firewalled), but such side-channel
        fetches only work when the consumer brings a box session of its own.
        """
        scheme = "https" if self.tls else "http"
        return f"{scheme}://{self._authority}{self._ingress_prefix}"
