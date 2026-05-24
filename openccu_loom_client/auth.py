# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Authentication methods supported by the openccu-loom daemon.

The daemon's OpenAPI surface (`securitySchemes` in `assets/openapi.yaml`)
declares three schemes:

- HTTP Basic — username/password against the daemon's local user store.
- HTTP Bearer — long-lived API token issued via the daemon's CLI or
  ``POST /auth/tokens`` (admin-only).
- Session cookie — issued by ``POST /auth/login`` and refreshed by the
  daemon; carried as a regular cookie thereafter.

Each method is a small object that knows how to decorate an aiohttp
request kwargs dict with the right headers / cookies, plus expose its
identity for logging.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from base64 import b64encode
from dataclasses import dataclass

# How many trailing characters of an opaque secret are exposed in the
# log identity hint. Mirrors the daemon's own convention for listing
# tokens via GET /auth/tokens (last six chars only).
_TOKEN_FINGERPRINT_LENGTH = 6


class AuthMethod(ABC):
    """Strategy interface for outbound HTTP/WS request authentication."""

    @abstractmethod
    def apply_to_headers(self, headers: dict[str, str]) -> None:
        """Mutate ``headers`` in place with whatever this method needs.

        Called once per REST request and once for the WebSocket upgrade
        handshake.
        """

    @property
    @abstractmethod
    def identity_hint(self) -> str:
        """Human-readable hint identifying this credential.

        Used in log lines and error messages — must NEVER include the
        secret itself.
        """


@dataclass(frozen=True, slots=True)
class BasicAuth(AuthMethod):
    """HTTP Basic auth — username + password."""

    username: str
    password: str

    def apply_to_headers(self, headers: dict[str, str]) -> None:
        token = b64encode(f"{self.username}:{self.password}".encode()).decode("ascii")
        headers["Authorization"] = f"Basic {token}"

    @property
    def identity_hint(self) -> str:
        return f"basic:{self.username}"


@dataclass(frozen=True, slots=True)
class BearerAuth(AuthMethod):
    """HTTP Bearer auth — API token issued by the daemon.

    Tokens are opaque from the client's perspective; the daemon
    validates them against its token store. ``label`` is just a local
    name carried in logs so multiple bearer tokens can be told apart
    without inspecting the secret.
    """

    token: str
    label: str = "bearer"

    def apply_to_headers(self, headers: dict[str, str]) -> None:
        headers["Authorization"] = f"Bearer {self.token}"

    @property
    def identity_hint(self) -> str:
        # Last six chars only — same convention the daemon uses when
        # listing tokens via GET /auth/tokens.
        suffix = (
            self.token[-_TOKEN_FINGERPRINT_LENGTH:]
            if len(self.token) >= _TOKEN_FINGERPRINT_LENGTH
            else "******"
        )
        return f"bearer:{self.label}:…{suffix}"


@dataclass(frozen=True, slots=True)
class SessionAuth(AuthMethod):
    """Cookie-based session issued by POST /auth/login.

    The cookie value is supplied by the caller after they've performed
    the login round-trip; this auth method just attaches it to outgoing
    requests. (The login flow itself lives in the high-level client,
    not here, because it requires a transport to run.)
    """

    cookie_value: str
    cookie_name: str = "openccu_loom_session"

    def apply_to_headers(self, headers: dict[str, str]) -> None:
        existing = headers.get("Cookie", "")
        pair = f"{self.cookie_name}={self.cookie_value}"
        headers["Cookie"] = f"{existing}; {pair}" if existing else pair

    @property
    def identity_hint(self) -> str:
        return f"session:{self.cookie_name}"
