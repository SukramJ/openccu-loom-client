# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Async REST transport for the openccu-loom daemon.

This module wraps :class:`aiohttp.ClientSession` with the daemon
contract specifics:

- RFC 9457 ``application/problem+json`` parsing into typed
  exceptions (see :mod:`openccu_loom_client.exceptions`).
- Retry with exponential backoff for transient upstream failures
  (``upstream_unavailable`` 502, ``service_unready`` 503). Retries
  are bounded and never applied to non-idempotent verbs (POST /
  PATCH) unless the caller explicitly opts in.
- One-shot capability handshake against ``GET /info`` at
  :meth:`HttpTransport.connect`, asserting the daemon's ``api_version``
  is compatible and that any caller-required capabilities are present.

The transport itself is generic — domain-specific operation modules
(``operations/devices.py``, ``operations/datapoints.py`` …) layer
on top in subsequent phases.
"""

from __future__ import annotations

import asyncio
import json
import logging
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, Final, Self

import aiohttp
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

# Backoff schedule (seconds) used on retryable failures. Three
# attempts total (initial + two retries) keeps the worst-case latency
# under ~3.5 seconds while still riding out the typical CCU
# reconnect window the daemon manages on the south-bound side.
_DEFAULT_BACKOFF_SEQUENCE: Final = (0.5, 2.0)


class HttpTransport:
    """REST transport for one openccu-loom daemon.

    Lifecycle: construct → :meth:`connect` (opens the session and runs
    the capability handshake) → use → :meth:`close` (or async-context
    exit). Re-entry after close is allowed: a fresh :meth:`connect`
    re-opens the session.
    """

    def __init__(
        self,
        config: LoomConfig,
        *,
        session: aiohttp.ClientSession | None = None,
        backoff_sequence: tuple[float, ...] = _DEFAULT_BACKOFF_SEQUENCE,
    ) -> None:
        self._config: Final = config
        self._external_session: Final = session
        self._backoff_sequence: Final = backoff_sequence
        self._session: aiohttp.ClientSession | None = session
        self._info: Info | None = None

    # ---- context manager ----

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.close()

    # ---- lifecycle ----

    async def connect(self, *, required_capabilities: Iterable[str] = ()) -> Info:
        """Open the underlying session and run the capability handshake.

        Returns the parsed ``/info`` payload so callers can record the
        daemon's version and capability set without an extra round-trip.
        Raises :class:`LoomTransportError` if the daemon is unreachable
        or returns a non-2xx status.
        """
        if self._session is None or self._session.closed:
            self._session = self._external_session or aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=self._config.verify_tls),
                timeout=aiohttp.ClientTimeout(total=self._config.request_timeout_seconds),
            )

        info_payload = await self.request("GET", "/info")
        self._info = Info.model_validate(info_payload)

        missing = [c for c in required_capabilities if c not in (self._info.capabilities or [])]
        if missing:
            msg = (
                f"daemon at {self._config.host} is missing required capabilities: "
                f"{sorted(missing)} — got {sorted(self._info.capabilities or [])}"
            )
            raise LoomTransportError(msg)

        _LOGGER.info(
            "connected to openccu-loom %s at %s (api_version=%s)",
            self._info.version,
            self._config.host,
            self._info.api_version,
        )
        return self._info

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
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
        headers: dict[str, str] | None = None,
        allow_retry: bool | None = None,
    ) -> Any:
        """Run one REST request against the daemon.

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
        merged_headers = self._build_headers(headers)
        retry = allow_retry if allow_retry is not None else method.upper() in _RETRY_SAFE_METHODS

        attempt_delays = (0.0, *self._backoff_sequence) if retry else (0.0,)
        last_exc: Exception | None = None
        for delay in attempt_delays:
            if delay:
                await asyncio.sleep(delay)
            try:
                return await self._do_once(
                    method=method,
                    url=url,
                    params=params,
                    json_body=json_body,
                    headers=merged_headers,
                )
            except _RETRYABLE_EXCEPTIONS as exc:
                last_exc = exc
                _LOGGER.debug(
                    "retryable failure on %s %s: %s",
                    method,
                    url,
                    exc,
                )
        # Exhausted all attempts — re-raise the last failure.
        assert last_exc is not None
        raise last_exc

    async def request_bytes(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        """Fetch a non-JSON (binary) body — backup / capture downloads.

        Returns the raw response body. Errors still go through the
        ``problem+json`` path, so a 4xx/5xx raises the same typed
        exceptions as :meth:`request`. Not retried by default — these
        endpoints stream sizeable archives where a blind retry is
        wasteful.
        """
        if self._session is None or self._session.closed:
            msg = "HttpTransport not connected — call connect() first"
            raise LoomTransportError(msg)
        url = self._config.http_base_url + path
        merged = self._build_headers(headers)
        merged.setdefault("Accept", "application/octet-stream")
        assert self._session is not None
        try:
            async with self._session.request(method, url, params=params, headers=merged) as resp:
                raw = await resp.read()
                if HTTPStatus.OK <= resp.status < HTTPStatus.MULTIPLE_CHOICES:
                    return raw
                payload = self._decode_json(raw) if raw else None
                problem = parse_problem(payload) if payload is not None else None
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

    def _build_headers(self, extra: dict[str, str] | None) -> dict[str, str]:
        headers: dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": self._config.user_agent,
            **self._config.extra_headers,
        }
        if extra:
            headers.update(extra)
        self._config.auth.apply_to_headers(headers)
        return headers

    async def _do_once(
        self,
        *,
        method: str,
        url: str,
        params: dict[str, Any] | None,
        json_body: Any | None,
        headers: dict[str, str],
    ) -> Any:
        assert self._session is not None
        try:
            async with self._session.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=headers,
            ) as resp:
                if resp.status == HTTPStatus.NO_CONTENT:
                    return None
                raw = await resp.read()
                if HTTPStatus.OK <= resp.status < HTTPStatus.MULTIPLE_CHOICES:
                    return self._decode_json(raw) if raw else None
                # Error path — try problem+json first.
                payload = self._decode_json(raw) if raw else None
                problem = parse_problem(payload) if payload is not None else None
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
