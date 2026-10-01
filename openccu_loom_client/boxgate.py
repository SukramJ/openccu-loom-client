# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Session manager for an openccu-lite box's ingress gate.

An openccu-lite box serves the daemon at ``/addons/loom/`` behind a session
gate. The gate accepts the box session as a ``?sid=`` query parameter — the
form that works for a headless client — and answers a redirect to the box's
login page when the session is missing or no longer valid. The session is the
gate's door-opener only: the daemon behind it authenticates the request
through the client's own ``AuthMethod`` exactly as without the box.

The box's auth API lives at the box root, not under the ingress prefix. Wire
form, as documented in godevccu ``pkg/litefake/CONTRACT.md`` §A.2:

- ``POST /api/auth/v1/login {username, password}`` (open) answers
  ``{sid, user, role, level, account_id, must_change_password}``;
- ``POST /api/auth/v1/logout`` needs ``Authorization: Bearer <sid>``;
- a refusal is ``401 {"error":"unauthenticated","message":"login required"}``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
from http import HTTPStatus
import json
import logging
from typing import TYPE_CHECKING, Final

import aiohttp

from openccu_loom_client.exceptions import LoomBoxLoginError, LoomTransportError

if TYPE_CHECKING:
    from openccu_loom_client.config import BoxIngressConfig, LoomConfig

_LOGGER: Final = logging.getLogger(__name__)

_LOGIN_PATH: Final = "/api/auth/v1/login"
_LOGOUT_PATH: Final = "/api/auth/v1/logout"

# How many leading characters of a session id appear in a log line. Enough to
# tell two sessions apart while debugging, far too few to replay one.
_SID_HINT_LENGTH: Final = 4


def sid_hint(*, sid: str | None) -> str:
    """Return a log-safe hint for a session id — never the full value."""
    if not sid:
        return "<none>"
    return f"{sid[:_SID_HINT_LENGTH]}…"


class BoxGateSession:
    """
    Hold one box session for the gate in front of the daemon.

    Logs in lazily on first use and caches the session id. Callers that saw
    the gate bounce a request call :meth:`relogin` with the session id that
    request carried; concurrent callers bouncing on the same stale session
    share one login rather than each starting their own.

    ``session_getter`` returns the open :class:`aiohttp.ClientSession` the
    login and logout requests ride on — the owning transport's session, so
    TLS settings and connection pooling stay in one place.
    """

    def __init__(
        self,
        *,
        config: LoomConfig,
        session_getter: Callable[[], aiohttp.ClientSession],
    ) -> None:
        """Bind the manager to a config in box-ingress mode."""
        if config.box_ingress is None or config.box_api_base_url is None:
            msg = "BoxGateSession needs LoomConfig.box_ingress"
            raise ValueError(msg)
        self._config: Final = config
        self._box: Final[BoxIngressConfig] = config.box_ingress
        self._api_base: Final[str] = config.box_api_base_url
        self._session_getter: Final = session_getter
        self._sid: str | None = None
        self._lock: Final = asyncio.Lock()

    def __repr__(self) -> str:
        """Return a secret-safe repr: box user and a session hint, never the password or sid."""
        return f"{type(self).__name__}(box={self._api_base}, user={self._box.username}, sid={sid_hint(sid=self._sid)})"

    @property
    def has_session(self) -> bool:
        """Return whether a box session id is currently cached."""
        return self._sid is not None

    async def sid(self) -> str:
        """Return the cached session id, logging in first when there is none."""
        if (current := self._sid) is not None:
            return current
        async with self._lock:
            # Another caller may have logged in while this one waited.
            if (current := self._sid) is not None:
                return current
            return await self._login()

    async def relogin(self, *, stale_sid: str | None) -> str:
        """
        Replace a session the gate refused, once per stale session.

        ``stale_sid`` is the session id the bounced request carried. When the
        cached session already differs from it, another caller has logged in
        since that request left, and its fresh session is returned instead of
        starting yet another login — N concurrent bounces cost one login.
        """
        async with self._lock:
            current = self._sid
            if current is not None and current != stale_sid:
                return current
            self._sid = None
            return await self._login()

    async def logout(self) -> None:
        """
        End the box session, best effort.

        Never raises: a box that is already unreachable has nothing to clean
        up that a later expiry will not, and a shutdown path must not fail on
        it.
        """
        async with self._lock:
            sid, self._sid = self._sid, None
        if sid is None:
            return
        with contextlib.suppress(Exception):
            session = self._session_getter()
            async with session.post(
                f"{self._api_base}{_LOGOUT_PATH}",
                headers={"Authorization": f"Bearer {sid}", "User-Agent": self._config.user_agent},
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=self._config.request_timeout_seconds),
            ) as resp:
                _LOGGER.debug("box logout for %s answered %d", self._box.username, resp.status)

    async def _login(self) -> str:
        """Log in to the box and cache the session id; caller holds the lock."""
        url = f"{self._api_base}{_LOGIN_PATH}"
        try:
            session = self._session_getter()
            async with session.post(
                url,
                json={"username": self._box.username, "password": self._box.password},
                # The daemon credential is deliberately NOT attached: the box
                # is a different realm, and its auth API would take a Bearer
                # header as a credential of its own.
                headers={"Accept": "application/json", "User-Agent": self._config.user_agent},
                # The login is open and answers directly; a redirect here is
                # not part of the contract and must not carry the password on.
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=self._config.request_timeout_seconds),
            ) as resp:
                status = resp.status
                raw = await resp.read()
        except (aiohttp.ClientError, TimeoutError) as exc:
            # A box that cannot be reached is a transport condition, not a
            # credential verdict — keep the classes apart so callers can
            # retry the first and stop on the second (the WS reconnect loop
            # does exactly that).
            msg = f"box login at {self._api_base} failed: {type(exc).__name__}"
            raise LoomTransportError(msg) from exc

        if status != HTTPStatus.OK:
            msg = f"box login at {self._api_base} as {self._box.username!r} refused with status {status}"
            reason = self._error_code(raw=raw)
            if reason:
                msg = f"{msg} ({reason})"
            raise LoomBoxLoginError(msg)

        try:
            payload = json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError, json.JSONDecodeError:
            payload = None
        sid = payload.get("sid") if isinstance(payload, dict) else None
        if not isinstance(sid, str) or not sid:
            msg = f"box login at {self._api_base} answered without a session id"
            raise LoomBoxLoginError(msg)
        self._sid = sid
        _LOGGER.debug("logged in to box %s as %s (sid %s)", self._api_base, self._box.username, sid_hint(sid=sid))
        return sid

    @staticmethod
    def _error_code(*, raw: bytes) -> str:
        """Return the box's short ``error`` code from a refusal body, or ``""``."""
        try:
            payload = json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError, json.JSONDecodeError:
            return ""
        code = payload.get("error") if isinstance(payload, dict) else None
        # Box-controlled text heading into an exception message: bound it.
        return code[:64] if isinstance(code, str) else ""
