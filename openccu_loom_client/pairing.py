# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Client token pairing against the daemon (its ADR 0076).

Instead of copying a bearer token by hand, the client asks the daemon
unauthenticated, both sides independently derive the same six-digit code,
a human shows that code to the daemon's administrator, and the
administrator approves by typing it into the daemon's tokens panel. The
approved token arrives on the next poll — exactly once.

The code is bound to the TLS certificate this client actually saw: it is
six digits of ``SHA-256(nonce ‖ client_nonce ‖ fingerprint)``, where the
client commits to ``client_nonce`` (as its SHA-256) before it learns the
daemon's ``nonce`` and reveals it with the first poll. A daemon answer
naming a different certificate than the connection served aborts the
attempt — the code would authenticate the interceptor.

Pairing runs BEFORE any credential exists, so it takes the transport
knobs directly instead of a :class:`~openccu_loom_client.config.LoomConfig`
(whose ``auth`` is required); the approved token then feeds one::

    session = await start_pairing(
        host="loom.local",
        app="openccu-loom-client",
        instance=socket.gethostname(),
        role="operator",
        purpose="Home Assistant device control",
    )
    show_to_user(session.code)          # the six digits
    result = await session.wait()       # blocks until decided / expired
    if result.state == "approved":
        config = LoomConfig(host="loom.local", auth=BearerAuth(token=result.token))
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import secrets
from typing import Final

import aiohttp

from openccu_loom_client.config import (
    DEFAULT_BASE_PATH,
    DEFAULT_HTTP_PORT,
    DEFAULT_HTTPS_PORT,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
)
from openccu_loom_client.exceptions import (
    LoomPairingSlowDownError,
    LoomTransportError,
    http_error_from_problem,
    parse_problem,
)
from openccu_loom_client.wire.rest import PairingAnswer, PairingResult

__all__ = [
    "PAIRING_FINGERPRINT_MISMATCH",
    "PairingSession",
    "derive_pairing_code",
    "start_pairing",
]

# One long poll per wait() iteration; the daemon caps at 30.
_LONG_POLL_SECONDS: Final = 25
# Ceiling for one HTTP round trip including the long poll.
_POLL_TIMEOUT_SECONDS: Final = 40.0

PAIRING_FINGERPRINT_MISMATCH: Final = (
    "pairing aborted: the certificate the daemon reports differs from the one this "
    "connection saw — possible interception"
)


def derive_pairing_code(*, nonce: bytes, client_nonce: bytes, fingerprint: bytes) -> str:
    """
    Compute the six digits both sides derive.

    First four bytes of ``SHA-256(nonce ‖ client_nonce ‖ fingerprint)``,
    big-endian, modulo one million, zero-padded — the daemon's
    ``internal/pairing.Code`` verbatim.
    """
    digest = hashlib.sha256(nonce + client_nonce + fingerprint).digest()
    return f"{int.from_bytes(digest[:4], 'big') % 1_000_000:06d}"


class PairingFingerprintMismatchError(LoomTransportError):
    """The daemon's answer names a certificate this connection did not serve."""


@dataclass(slots=True, kw_only=True)
class PairingSession:
    """One running pairing attempt; created by :func:`start_pairing`."""

    code: str
    """The six digits to show the human who talks to the administrator."""

    expires_in: int
    """Seconds the request stays valid without a decision."""

    _base: str
    _verify_tls: bool
    _answer: PairingAnswer
    _client_nonce: bytes
    _interval: float
    _revealed: bool = field(default=False)

    def _connector(self) -> aiohttp.TCPConnector:
        return aiohttp.TCPConnector(ssl=self._verify_tls)

    async def wait(self) -> PairingResult:
        """
        Poll until the administrator decides or the request expires.

        Returns the terminal :class:`PairingResult`; ``state`` is
        ``approved`` (with ``token``, delivered exactly once),
        ``rejected`` or ``expired``. Transport errors raise.
        """
        params: dict[str, str] = {"wait": str(_LONG_POLL_SECONDS)}
        if not self._revealed:
            params["client_nonce"] = self._client_nonce.hex()
        headers = {"Authorization": f"Pairing {self._answer.poll}"}
        timeout = aiohttp.ClientTimeout(total=_POLL_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(connector=self._connector(), timeout=timeout) as session:
            while True:
                async with session.get(
                    f"{self._base}/pairing/{self._answer.id}", params=params, headers=headers
                ) as resp:
                    payload = await resp.json(content_type=None)
                    if resp.status != 200:
                        error = http_error_from_problem(
                            status=resp.status,
                            problem=parse_problem(payload=payload),
                            raw_body=None,
                            method="GET",
                            url=str(resp.url),
                        )
                        if isinstance(error, LoomPairingSlowDownError):
                            await asyncio.sleep(self._interval)
                            continue
                        raise error
                self._revealed = True
                params.pop("client_nonce", None)
                result = PairingResult.model_validate(payload)
                if result.state != "pending":
                    return result

    async def withdraw(self) -> None:
        """Give the request up; the admin card drops it immediately."""
        headers = {"Authorization": f"Pairing {self._answer.poll}"}
        timeout = aiohttp.ClientTimeout(total=DEFAULT_REQUEST_TIMEOUT_SECONDS)
        async with (
            aiohttp.ClientSession(connector=self._connector(), timeout=timeout) as session,
            session.delete(f"{self._base}/pairing/{self._answer.id}", headers=headers) as resp,
        ):
            if resp.status not in (204, 404):
                payload = await resp.json(content_type=None)
                raise http_error_from_problem(
                    status=resp.status,
                    problem=parse_problem(payload=payload),
                    raw_body=None,
                    method="DELETE",
                    url=str(resp.url),
                )


def _seen_fingerprint(*, resp: aiohttp.ClientResponse) -> bytes:
    """SHA-256 of the peer certificate this response's connection saw, or b''."""
    conn = resp.connection
    if conn is None or conn.transport is None:
        return b""
    ssl_object = conn.transport.get_extra_info("ssl_object")
    if ssl_object is None:
        return b""
    der = ssl_object.getpeercert(binary_form=True)
    if not der:
        return b""
    return hashlib.sha256(der).digest()


async def start_pairing(
    *,
    host: str,
    app: str,
    role: str,
    port: int | None = None,
    tls: bool = True,
    verify_tls: bool = True,
    app_version: str = "",
    instance: str = "",
    name: str = "",
    purpose: str = "",
) -> PairingSession:
    """
    Ask the daemon for a token; returns the session carrying the code.

    ``role`` is ``viewer`` or ``operator`` — the daemon never pairs
    ``admin``. Raises the mapped problem exception when the daemon
    refuses (:class:`~openccu_loom_client.exceptions.LoomPairingOffError`,
    :class:`~openccu_loom_client.exceptions.LoomPairingNotLocalError`,
    rate limits), and :class:`PairingFingerprintMismatchError` when the
    daemon's answer names a certificate this connection did not see.
    """
    resolved_port = port if port is not None else (DEFAULT_HTTPS_PORT if tls else DEFAULT_HTTP_PORT)
    scheme = "https" if tls else "http"
    base = f"{scheme}://{host}:{resolved_port}{DEFAULT_BASE_PATH}"
    client_nonce = secrets.token_bytes(32)
    commit = hashlib.sha256(client_nonce).hexdigest()
    ask = {
        "app": app,
        "app_version": app_version,
        "instance": instance,
        "name": name,
        "role": role,
        "purpose": purpose,
        "commit": commit,
    }
    timeout = aiohttp.ClientTimeout(total=DEFAULT_REQUEST_TIMEOUT_SECONDS)
    async with (
        aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=verify_tls), timeout=timeout) as session,
        session.post(f"{base}/pairing", json=ask) as resp,
    ):
        seen = _seen_fingerprint(resp=resp)
        payload = await resp.json(content_type=None)
        if resp.status != 202:
            raise http_error_from_problem(
                status=resp.status,
                problem=parse_problem(payload=payload),
                raw_body=None,
                method="POST",
                url=str(resp.url),
            )
    answer = PairingAnswer.model_validate(payload)
    reported = bytes.fromhex(answer.fingerprint) if answer.fingerprint else b""
    # The CODE always uses the fingerprint the daemon binds to (its own
    # certificate, empty over plain HTTP or behind a TLS-terminating
    # proxy) — both sides must hash the same bytes. The check below is
    # what makes that safe: when the daemon names a certificate, it must
    # be the one this connection saw. An empty `reported` with TLS in
    # front means a proxy terminates it; the binding lapses like over
    # plain HTTP, and transport trust is the proxy operator's setup.
    if reported and seen and reported != seen:
        # The code binds to the certificate; a daemon naming a different
        # one than this connection served means something terminated TLS
        # in between. Withdrawing is best-effort — the request expires on
        # its own either way.
        session_stub = PairingSession(
            code="",
            expires_in=answer.expires_in,
            _base=base,
            _verify_tls=verify_tls,
            _answer=answer,
            _client_nonce=client_nonce,
            _interval=float(answer.interval),
        )
        try:
            await session_stub.withdraw()
        finally:
            pass
        raise PairingFingerprintMismatchError(PAIRING_FINGERPRINT_MISMATCH)
    return PairingSession(
        code=derive_pairing_code(nonce=bytes.fromhex(answer.nonce), client_nonce=client_nonce, fingerprint=reported),
        expires_in=answer.expires_in,
        _base=base,
        _verify_tls=verify_tls,
        _answer=answer,
        _client_nonce=client_nonce,
        _interval=float(answer.interval),
    )
