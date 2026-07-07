# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Async REST transport for the openccu-loom daemon.

This module wraps :class:`aiohttp.ClientSession` with the daemon
contract specifics:

- RFC 9457 ``application/problem+json`` parsing into typed
  exceptions (see :mod:`openccu_loom_client.exceptions`).
- Retry with exponential backoff for transient failures on idempotent
  verbs: the daemon's ``upstream_unavailable`` (502) / ``service_unready``
  (503), **and** any wrapped network/timeout error (``aiohttp.ClientError``
  / ``TimeoutError`` → :class:`LoomTransportError`). All attempts share one
  total-deadline budget (``request_timeout_seconds``), so the worst-case
  wall-clock is that budget — not N × per-request timeout. Never applied to
  non-idempotent verbs (POST / PATCH) unless the caller opts in.
- One-shot capability handshake against ``GET /info`` at
  :meth:`HttpTransport.connect`, asserting the daemon's ``api_version``
  is compatible and that any caller-required capabilities are present.

The transport itself is generic — domain-specific operation modules
(``operations/devices.py``, ``operations/datapoints.py`` …) layer
on top in subsequent phases.
"""

from __future__ import annotations

import asyncio
from http import HTTPStatus
import json
import logging
from typing import TYPE_CHECKING, Any, Final, Self

import aiohttp
import openccu_loom_types
from openccu_loom_types.rest import Info

from openccu_loom_client.exceptions import (
    LoomServiceUnreadyError,
    LoomTransportError,
    LoomUpstreamUnavailableError,
    http_error_from_problem,
    parse_problem,
)

if TYPE_CHECKING:
    from collections.abc import Iterable
    from types import TracebackType

    from openccu_loom_client.config import LoomConfig

_LOGGER: Final = logging.getLogger(__name__)

# Idempotent HTTP verbs that are safe to auto-retry. POST/PATCH are
# excluded by default because they typically carry side effects.
_RETRY_SAFE_METHODS: Final = frozenset({"GET", "HEAD", "PUT", "DELETE"})

# Exception types that justify a retry. Both are wire-level signals
# from the daemon that the failure is transient.
_RETRYABLE_EXCEPTIONS: Final = (
    LoomServiceUnreadyError,
    LoomUpstreamUnavailableError,
    LoomTransportError,
)

# Backoff schedule (seconds) used on retryable failures: three attempts
# total (initial + two retries) ride out the typical CCU reconnect window
# the daemon manages south-bound. The total wall-clock is capped by the
# shared deadline budget in ``request()`` (``request_timeout_seconds``),
# not the sum of per-attempt timeouts.
_DEFAULT_BACKOFF_SEQUENCE: Final = (0.5, 2.0)


class HttpTransport:
    """
    REST transport for one openccu-loom daemon.

    Lifecycle: construct → :meth:`connect` (opens the session and runs
    the capability handshake) → use → :meth:`close` (or async-context
    exit). Re-entry after close is allowed: a fresh :meth:`connect`
    re-opens the session.
    """

    def __init__(
        self,
        *,
        config: LoomConfig,
        session: aiohttp.ClientSession | None = None,
        backoff_sequence: tuple[float, ...] = _DEFAULT_BACKOFF_SEQUENCE,
    ) -> None:
        """Configure the transport; the session opens on :meth:`connect`."""
        self._config: Final = config
        self._external_session: Final = session
        self._backoff_sequence: Final = backoff_sequence
        self._session: aiohttp.ClientSession | None = session
        self._info: Info | None = None

    # ---- context manager ----

    async def __aenter__(self) -> Self:
        """Connect and return the transport."""
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
        /,
    ) -> None:
        """Close the session on context exit."""
        await self.close()

    # ---- lifecycle ----

    async def connect(self, *, required_capabilities: Iterable[str] = ()) -> Info:
        """
        Open the underlying session and run the capability handshake.

        Returns the parsed ``/info`` payload so callers can record the
        daemon's version and capability set without an extra round-trip.
        Raises :class:`LoomTransportError` if the daemon is unreachable
        or returns a non-2xx status.
        """
        created_here = False
        if self._session is None or self._session.closed:
            self._session = self._external_session or aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=self._config.verify_tls),
                timeout=aiohttp.ClientTimeout(total=self._config.request_timeout_seconds),
            )
            created_here = True

        try:
            info_payload = await self.request(method="GET", path="/info")
            self._info = Info.model_validate(info_payload)

            missing = [c for c in required_capabilities if c not in (self._info.capabilities or [])]
            if missing:
                msg = (
                    f"daemon at {self._config.host} is missing required capabilities: "
                    f"{sorted(missing)} — got {sorted(self._info.capabilities or [])}"
                )
                raise LoomTransportError(msg)

            self._check_schema_digest(info_payload=info_payload)
        except BaseException:
            # A failed handshake must not leak the session we just opened
            # (unreachable daemon, capability mismatch, cancellation). Only
            # tear down a session this call created and owns — never a
            # caller-supplied one, and never one a prior connect() opened.
            if created_here and self._external_session is None and self._session is not None:
                await self._session.close()
                self._session = None
            self._info = None
            raise

        _LOGGER.info(
            "connected to openccu-loom %s at %s (api_version=%s)",
            self._info.version,
            self._config.host,
            self._info.api_version,
        )
        return self._info

    def _check_schema_digest(self, *, info_payload: Any) -> None:
        """
        Compare the daemon's ``schema_digest`` with the installed types package.

        The reference value is stamped into ``openccu-loom-types`` at
        generation time (daemon ADR 0028); equality means the generated
        types match the daemon build exactly. A mismatch is logged as a
        warning, not raised: the contract may still be compatible —
        ``api_version`` and the capability set govern hard compatibility.
        Silently skipped when either side predates the digest (old
        daemon or unstamped types package).
        """
        daemon_digest = info_payload.get("schema_digest", "") if isinstance(info_payload, dict) else ""
        types_digest = getattr(openccu_loom_types, "SCHEMA_DIGEST", "")
        if not daemon_digest or not types_digest:
            return
        if daemon_digest != types_digest:
            _LOGGER.warning(
                "openccu-loom-types was generated from a different daemon build: "
                "daemon at %s reports schema_digest=%s, installed types carry %s "
                "(types daemon_api_version=%s, daemon api_version=%s) — "
                "update openccu-loom-types to match the daemon release",
                self._config.host,
                daemon_digest,
                types_digest,
                getattr(openccu_loom_types, "DAEMON_API_VERSION", "?"),
                self._info.api_version if self._info else "?",
            )

    async def close(self) -> None:
        """Tear down the session (only if this transport owns it)."""
        if self._session is None or self._session.closed:
            return
        if self._external_session is None:
            await self._session.close()
        self._session = None
        self._info = None

    @property
    def info(self) -> Info | None:
        """Return the last ``/info`` payload, or ``None`` before connect."""
        return self._info

    # ---- core request ----

    async def request(
        self,
        *,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
        headers: dict[str, str] | None = None,
        allow_retry: bool | None = None,
    ) -> Any:
        """
        Run one REST request against the daemon.

        ``path`` is appended to ``config.http_base_url``. ``params``
        and ``json_body`` are passed through to aiohttp.

        Successful 2xx responses are returned as the decoded JSON body
        (``None`` for 204). Errors raise the most specific subclass of
        :class:`LoomHttpError` derived from the ``problem+json`` ``type``.

        Retry: enabled by default for idempotent verbs, disabled for
        POST/PATCH. Override via ``allow_retry``.
        """
        if self._session is None or self._session.closed:
            msg = "HttpTransport not connected — call connect() first"
            raise LoomTransportError(msg)

        url = self._config.http_base_url + path
        merged_headers = self._build_headers(extra=headers)
        retry = allow_retry if allow_retry is not None else method.upper() in _RETRY_SAFE_METHODS

        # Single total-deadline budget shared across all attempts: each retry's
        # per-request timeout is the *remaining* budget, so the worst case is
        # ``request_timeout_seconds`` overall — not N × timeout. Backoff that
        # would overrun the deadline is skipped.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._config.request_timeout_seconds
        attempt_delays = (0.0, *self._backoff_sequence) if retry else (0.0,)
        last_exc: Exception | None = None
        for delay in attempt_delays:
            remaining = deadline - loop.time()
            if remaining <= 0 or (delay and delay >= remaining):
                break  # no budget left for another attempt (or its backoff)
            if delay:
                await asyncio.sleep(delay)
                remaining = deadline - loop.time()
            try:
                return await self._do_once(
                    method=method,
                    url=url,
                    params=params,
                    json_body=json_body,
                    headers=merged_headers,
                    client_timeout=aiohttp.ClientTimeout(total=remaining),
                )
            except _RETRYABLE_EXCEPTIONS as exc:
                last_exc = exc
                _LOGGER.debug(
                    "retryable failure on %s %s: %s",
                    method,
                    url,
                    exc,
                )
        if last_exc is not None:
            raise last_exc
        # Only reachable if the configured budget was ≤ 0 (no attempt ran).
        msg = f"request to {url}: no time budget ({self._config.request_timeout_seconds}s)"
        raise LoomTransportError(msg)

    async def request_bytes(
        self,
        *,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        total_timeout_seconds: float | None = None,
    ) -> bytes:
        """
        Fetch a non-JSON (binary) body — backup / capture downloads.

        Returns the raw response body. Errors still go through the
        ``problem+json`` path, so a 4xx/5xx raises the same typed
        exceptions as :meth:`request`. Not retried by default — these
        endpoints stream sizeable archives where a blind retry is
        wasteful.

        These archives can take far longer to transfer than a JSON call,
        so this method does *not* inherit the session-wide total timeout
        (default 30s), which would guarantee failure on any sizeable
        download over a slow link. Instead it applies a per-chunk *read*
        timeout (so a genuinely stalled transfer still fails fast) and no
        total cap, unless the caller passes ``total_timeout_seconds`` to
        impose an explicit ceiling. Setting an explicit timeout on the
        request also makes behaviour deterministic when the transport runs
        on a caller-supplied session whose default is unknown.
        """
        if self._session is None or self._session.closed:
            msg = "HttpTransport not connected — call connect() first"
            raise LoomTransportError(msg)
        url = self._config.http_base_url + path
        merged = self._build_headers(extra=headers)
        merged.setdefault("Accept", "application/octet-stream")
        timeout = aiohttp.ClientTimeout(
            total=total_timeout_seconds,
            sock_connect=self._config.request_timeout_seconds,
            sock_read=self._config.request_timeout_seconds,
        )
        assert self._session is not None  # noqa: S101 — narrowed by the connected-state guard above
        try:
            async with self._session.request(method, url, params=params, headers=merged, timeout=timeout) as resp:
                raw = await resp.read()
                if HTTPStatus.OK <= resp.status < HTTPStatus.MULTIPLE_CHOICES:
                    return raw
                payload = self._decode_json(raw) if raw else None
                problem = parse_problem(payload=payload) if payload is not None else None
                raise http_error_from_problem(
                    status=resp.status,
                    problem=problem,
                    raw_body=raw if problem is None else None,
                    method=method,
                    url=url,
                )
        except aiohttp.ClientError as exc:
            msg = f"transport error talking to {url}: {exc}"
            raise LoomTransportError(msg) from exc
        except TimeoutError as exc:
            msg = f"request to {url} timed out after {self._config.request_timeout_seconds}s"
            raise LoomTransportError(msg) from exc

    # ---- internals ----

    def _build_headers(self, *, extra: dict[str, str] | None) -> dict[str, str]:
        headers: dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": self._config.user_agent,
            **self._config.extra_headers,
        }
        if extra:
            headers.update(extra)
        self._config.auth.apply_to_headers(headers=headers)
        return headers

    async def _do_once(
        self,
        *,
        method: str,
        url: str,
        params: dict[str, Any] | None,
        json_body: Any | None,
        headers: dict[str, str],
        client_timeout: aiohttp.ClientTimeout | None = None,
    ) -> Any:
        assert self._session is not None  # noqa: S101 — narrowed by the connected-state guard above
        # ``timeout=None`` in aiohttp DISABLES the timeout; to keep the session
        # default we must omit the kwarg entirely, so pass it only when set.
        extra: dict[str, Any] = {"timeout": client_timeout} if client_timeout is not None else {}
        try:
            async with self._session.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=headers,
                **extra,
            ) as resp:
                if resp.status == HTTPStatus.NO_CONTENT:
                    return None
                raw = await resp.read()
                if HTTPStatus.OK <= resp.status < HTTPStatus.MULTIPLE_CHOICES:
                    return self._decode_json(raw) if raw else None
                # Error path — try problem+json first.
                payload = self._decode_json(raw) if raw else None
                problem = parse_problem(payload=payload) if payload is not None else None
                raise http_error_from_problem(
                    status=resp.status,
                    problem=problem,
                    raw_body=raw if problem is None else None,
                    method=method,
                    url=url,
                )
        except aiohttp.ClientError as exc:
            msg = f"transport error talking to {url}: {exc}"
            raise LoomTransportError(msg) from exc
        except TimeoutError as exc:
            msg = f"request to {url} timed out after {self._config.request_timeout_seconds}s"
            raise LoomTransportError(msg) from exc

    @staticmethod
    def _decode_json(raw: bytes) -> Any:
        # aiohttp's resp.json() guesses the content-type; we decode
        # the raw bytes ourselves so problem+json responses with the
        # right Content-Type but a slightly off body still parse.
        try:
            return json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError, json.JSONDecodeError:
            return None
