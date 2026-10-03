# SPDX-License-Identifier: MIT
# Copyright (C) 2026 SukramJ.

"""
Client pairing against an openccu-lite box, for a token that opens the gate.

In box-ingress mode (:class:`~openccu_loom_client.config.BoxIngressConfig`)
the client needs a box API token holding the add-on's gate scope. Instead of
storing a box account's password, the client asks the box for one: the box's
administrator sees a six-digit code on the box's status page, compares it
with the one the client shows, and approves. The token arrives on the next
poll — exactly once.

Wire form, from occulited's ``docs/system-api.md`` (*Client pairing — the
program's side*), at the box root rather than under the ingress prefix:

- ``POST /api/auth/v1/pairing/request {app, app_version?, instance?, name?,
  addons: [<id>], purpose?, commit}`` (open, local networks only) answers
  ``202 {id, poll, nonce, expires_in, interval, fingerprint}``;
- ``GET /api/auth/v1/pairing/request/{id}?client_nonce=<hex>&wait=<s>`` with
  ``Authorization: Pairing <poll>`` answers ``{state}``, and once
  ``approved`` also ``{token, name, scopes, access, addons}``;
- ``DELETE`` on the same path withdraws (204);
- refusals carry ``{"error": <code>}`` — ``pairing-off``, ``not-local``,
  ``limit``, ``invalid``, ``forbidden`` — and polling faster than
  ``interval`` without ``wait`` is ``429 slow_down``.

The code is derived exactly as the daemon's own pairing derives it
(:func:`~openccu_loom_client.pairing.derive_pairing_code`), bound to the TLS
certificate this client saw; a box answer naming a different certificate
aborts the attempt.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
import hashlib
from http import HTTPStatus
import json
import secrets
from typing import Any, Final

import aiohttp

from openccu_loom_client import pairing
from openccu_loom_client.config import DEFAULT_BOX_HTTP_PORT, DEFAULT_BOX_HTTPS_PORT, DEFAULT_REQUEST_TIMEOUT_SECONDS
from openccu_loom_client.exceptions import LoomBoxPairingError, LoomTransportError

__all__ = [
    "DEFAULT_BOX_ADDON_ID",
    "BoxPairingResult",
    "BoxPairingSession",
    "start_box_pairing",
]

# The add-on id the daemon is installed under on an openccu-lite box: the
# `id` of the daemon repository's packaging/ccu-addon/ccu/openccu-lite.json.
# The box names the gate scope after it (`addon:openccu-loom`).
DEFAULT_BOX_ADDON_ID: Final = "openccu-loom"

_PAIRING_PATH: Final = "/api/auth/v1/pairing/request"
# One long poll per wait() iteration; the box caps at 30.
_LONG_POLL_SECONDS: Final = 25
# Ceiling for one HTTP round trip including the long poll.
_POLL_TIMEOUT_SECONDS: Final = 40.0
# Bound on box-controlled text heading into an exception message.
_ERROR_CODE_MAX: Final = 64


@dataclass(frozen=True, slots=True, kw_only=True)
class BoxPairingResult:
    """The terminal answer of a box pairing request."""

    state: str
    """``approved``, ``rejected`` or ``expired``."""

    token: str = field(default="", repr=False)
    """The box API token — set only when ``approved``, and delivered once."""

    name: str = ""
    """The name the box gave the token."""

    scopes: tuple[str, ...] = ()
    """The scopes the token holds, as the box stored them."""


@dataclass(slots=True, kw_only=True)
class BoxPairingSession:
    """One running box pairing attempt; created by :func:`start_box_pairing`."""

    code: str
    """The six digits the box administrator compares on the box's status page."""

    expires_in: int
    """Seconds the request stays valid without a decision."""

    _base: str
    _verify_tls: bool
    _request_id: str
    _poll: str = field(repr=False)
    _client_nonce: bytes = field(repr=False)
    _interval: float
    _revealed: bool = False

    def _connector(self) -> aiohttp.TCPConnector:
        return aiohttp.TCPConnector(ssl=self._verify_tls)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Pairing {self._poll}", "Accept": "application/json"}

    async def wait(self) -> BoxPairingResult:
        """
        Poll until the box administrator decides or the request expires.

        Returns the terminal :class:`BoxPairingResult`. A refusal raises
        :class:`~openccu_loom_client.exceptions.LoomBoxPairingError`, an
        unreachable box :class:`~openccu_loom_client.exceptions.LoomTransportError`.
        """
        url = f"{self._base}{_PAIRING_PATH}/{self._request_id}"
        timeout = aiohttp.ClientTimeout(total=_POLL_TIMEOUT_SECONDS)
        try:
            async with aiohttp.ClientSession(connector=self._connector(), timeout=timeout) as session:
                while True:
                    params = {"wait": str(_LONG_POLL_SECONDS)}
                    if not self._revealed:
                        # The first poll reveals the client's half of the code;
                        # the request appears on the box only after it.
                        params["client_nonce"] = self._client_nonce.hex()
                    async with session.get(url, params=params, headers=self._headers(), allow_redirects=False) as resp:
                        status = resp.status
                        payload = _decode(raw=await resp.read())
                    if status == HTTPStatus.TOO_MANY_REQUESTS and _error_code(payload=payload) == "slow_down":
                        await asyncio.sleep(self._interval)
                        continue
                    if status != HTTPStatus.OK:
                        raise _refusal(status=status, payload=payload, what="pairing poll")
                    self._revealed = True
                    state = payload.get("state") if isinstance(payload, dict) else None
                    if state == "pending":
                        continue
                    return _result(state=state, payload=payload)
        except (aiohttp.ClientError, TimeoutError) as exc:
            msg = f"box pairing poll at {self._base} failed: {type(exc).__name__}"
            raise LoomTransportError(msg) from exc

    async def withdraw(self) -> None:
        """Give the request up; the box drops it from its status page at once."""
        url = f"{self._base}{_PAIRING_PATH}/{self._request_id}"
        timeout = aiohttp.ClientTimeout(total=DEFAULT_REQUEST_TIMEOUT_SECONDS)
        try:
            async with (
                aiohttp.ClientSession(connector=self._connector(), timeout=timeout) as session,
                session.delete(url, headers=self._headers(), allow_redirects=False) as resp,
            ):
                status = resp.status
                payload = _decode(raw=await resp.read())
        except (aiohttp.ClientError, TimeoutError) as exc:
            msg = f"box pairing withdrawal at {self._base} failed: {type(exc).__name__}"
            raise LoomTransportError(msg) from exc
        if status not in (HTTPStatus.NO_CONTENT, HTTPStatus.NOT_FOUND):
            raise _refusal(status=status, payload=payload, what="pairing withdrawal")


async def start_box_pairing(
    *,
    host: str,
    app: str,
    port: int | None = None,
    tls: bool = True,
    verify_tls: bool = True,
    addon: str = DEFAULT_BOX_ADDON_ID,
    app_version: str = "",
    instance: str = "",
    name: str = "",
    purpose: dict[str, str] | None = None,
) -> BoxPairingSession:
    """
    Ask an openccu-lite box for a token that opens the gate to ``addon``.

    ``host``/``port``/``tls`` name the box's own web server (the port defaults
    to 443 with TLS and to 80 without), exactly as in
    :class:`~openccu_loom_client.config.BoxIngressConfig`. The request asks
    for the add-on's gate scope only — no box API access. ``purpose`` is the
    program's own words per area, passed through as the box documents it.

    Raises :class:`~openccu_loom_client.exceptions.LoomBoxPairingError` when
    the box refuses (``code`` names why), :class:`LoomTransportError` when it
    cannot be reached, and
    :class:`~openccu_loom_client.pairing.PairingFingerprintMismatchError`
    when the box's answer names a certificate this connection did not see.
    """
    resolved_port = port if port is not None else (DEFAULT_BOX_HTTPS_PORT if tls else DEFAULT_BOX_HTTP_PORT)
    base = f"{'https' if tls else 'http'}://{host}:{resolved_port}"
    client_nonce = secrets.token_bytes(32)
    ask: dict[str, Any] = {
        "app": app,
        "addons": [addon],
        "commit": hashlib.sha256(client_nonce).hexdigest(),
    }
    # The optional fields go out only when set, so the box sees no empty strings.
    ask |= {
        key: value for key, value in (("app_version", app_version), ("instance", instance), ("name", name)) if value
    }
    if purpose:
        ask["purpose"] = purpose
    timeout = aiohttp.ClientTimeout(total=DEFAULT_REQUEST_TIMEOUT_SECONDS)
    try:
        async with (
            aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=verify_tls), timeout=timeout) as session,
            session.post(
                f"{base}{_PAIRING_PATH}", json=ask, headers={"Accept": "application/json"}, allow_redirects=False
            ) as resp,
        ):
            seen = pairing.seen_fingerprint(resp=resp)
            status = resp.status
            payload = _decode(raw=await resp.read())
    except (aiohttp.ClientError, TimeoutError) as exc:
        msg = f"box pairing request at {base} failed: {type(exc).__name__}"
        raise LoomTransportError(msg) from exc
    if status != HTTPStatus.ACCEPTED:
        raise _refusal(status=status, payload=payload, what="pairing request")
    if not isinstance(payload, dict):
        msg = f"box pairing request at {base} answered without a request"
        raise LoomBoxPairingError(message=msg, status=status)
    try:
        request_id = str(payload["id"])
        poll = str(payload["poll"])
        nonce = bytes.fromhex(str(payload["nonce"]))
        reported = bytes.fromhex(str(payload.get("fingerprint") or ""))
        expires_in = int(payload.get("expires_in", 0))
        interval = float(payload.get("interval", 2))
    except (KeyError, ValueError, TypeError) as exc:
        msg = f"box pairing request at {base} answered a malformed request"
        raise LoomBoxPairingError(message=msg, status=status) from exc
    session_obj = BoxPairingSession(
        code=pairing.derive_pairing_code(nonce=nonce, client_nonce=client_nonce, fingerprint=reported),
        expires_in=expires_in,
        _base=base,
        _verify_tls=verify_tls,
        _request_id=request_id,
        _poll=poll,
        _client_nonce=client_nonce,
        _interval=interval,
    )
    # The code binds to the certificate the box names (empty over plain HTTP).
    # When it names one, it must be the one this connection saw — otherwise
    # something terminated TLS in between and the code would authenticate it.
    if reported and seen and reported != seen:
        # Best effort: the request expires on its own either way.
        with contextlib.suppress(LoomTransportError, LoomBoxPairingError):
            await session_obj.withdraw()
        raise pairing.PairingFingerprintMismatchError(pairing.PAIRING_FINGERPRINT_MISMATCH)
    return session_obj


def _decode(*, raw: bytes) -> Any:
    """Decode a JSON body, or return ``None`` for anything else."""
    try:
        return json.loads(raw.decode("utf-8")) if raw else None
    except UnicodeDecodeError, json.JSONDecodeError:
        return None


def _error_code(*, payload: Any) -> str:
    """Return the box's short ``error`` code, bounded, or ``""``."""
    code = payload.get("error") if isinstance(payload, dict) else None
    return code[:_ERROR_CODE_MAX] if isinstance(code, str) else ""


def _refusal(*, status: int, payload: Any, what: str) -> LoomBoxPairingError:
    """Build the error for a box answer that is not the expected one."""
    code = _error_code(payload=payload)
    msg = f"box {what} refused with status {status}"
    if code:
        msg = f"{msg} ({code})"
    return LoomBoxPairingError(message=msg, code=code, status=status)


def _result(*, state: Any, payload: Any) -> BoxPairingResult:
    """Build the terminal result from a non-pending poll answer."""
    if state not in ("approved", "rejected", "expired"):
        msg = f"box pairing poll answered an unknown state {str(state)[:_ERROR_CODE_MAX]!r}"
        raise LoomBoxPairingError(message=msg)
    if state != "approved":
        return BoxPairingResult(state=state)
    token = payload.get("token")
    if not isinstance(token, str) or not token:
        msg = "box pairing was approved but the answer carried no token"
        raise LoomBoxPairingError(message=msg)
    scopes = payload.get("scopes")
    return BoxPairingResult(
        state=state,
        token=token,
        name=str(payload.get("name") or ""),
        scopes=tuple(s for s in scopes if isinstance(s, str)) if isinstance(scopes, list) else (),
    )
