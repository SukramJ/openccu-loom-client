# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Exception hierarchy for the openccu-loom client.

The daemon emits RFC 9457 `application/problem+json` for every error
response. The `type` field is one of a fixed set of URIs declared in
``openccu_loom_types.rest.Code`` — this module maps each URI to a
typed Python exception so consumers can ``except`` on the failure
class rather than parse the status code or URI themselves.
"""

from __future__ import annotations

from typing import Any

from openccu_loom_types.rest import Code, Problem

# Stable problem-type URI prefix the daemon uses for its error catalogue.
# See assets/openapi.yaml -> components.schemas.Problem.type.enum.
_PROBLEM_URI_PREFIX = "https://openccu-loom.dev/errors/"


class BaseLoomException(Exception):
    """Base class for all exceptions raised by this package."""


class LoomTransportError(BaseLoomException):
    """
    Network-level failure before the daemon got to respond.

    Examples: connection refused, DNS failure, TLS handshake error,
    timeout. The daemon never saw the request.
    """


class LoomHttpError(BaseLoomException):
    """
    The daemon answered with a non-2xx status.

    Carries the parsed RFC 9457 problem document when one was supplied;
    if the response body wasn't problem+json (e.g. an upstream proxy
    returned plain HTML) ``problem`` is None and ``raw_body`` carries
    the bytes for diagnostics.
    """

    def __init__(
        self,
        *,
        status: int,
        problem: Problem | None = None,
        raw_body: bytes | None = None,
        method: str | None = None,
        url: str | None = None,
    ) -> None:
        """Store the HTTP status, parsed problem, and request context."""
        self.status = status
        self.problem = problem
        self.raw_body = raw_body
        self.method = method
        self.url = url
        super().__init__(self._format_message())

    def _format_message(self) -> str:
        where = f"{self.method} {self.url}" if self.method and self.url else "<request>"
        if self.problem is not None:
            title = self.problem.title or self.problem.type
            return f"[{self.status}] {where}: {title}"
        return f"[{self.status}] {where}: <no problem+json body>"


# Per-code subclasses — one per stable URI. Consumers can `except
# LoomNotFoundError` instead of checking status==404 or type-URI.


class LoomValidationError(LoomHttpError):
    """422-class validation failure (input did not match schema)."""


class LoomBadRequestError(LoomHttpError):
    """400-class malformed request (parameter parse error, etc.)."""


class LoomNotFoundError(LoomHttpError):
    """The addressed device / channel / sysvar / program does not exist."""


class LoomConflictError(LoomHttpError):
    """State conflict — e.g. concurrent modification of a paramset."""


class LoomAuthError(LoomHttpError):
    """Missing or invalid credentials (401)."""


class LoomForbiddenError(LoomHttpError):
    """Authenticated identity lacks the required role/scope (403)."""


class LoomUnsupportedError(LoomHttpError):
    """The operation is not supported on this interface or device."""


class LoomRateLimitedError(LoomHttpError):
    """Daemon refused due to throttling — retry after ``retry_after_seconds``."""


class LoomServiceUnreadyError(LoomHttpError):
    """Daemon is still warming up (e.g. initial CCU sync not done)."""


class LoomUpstreamUnavailableError(LoomHttpError):
    """The daemon reached the CCU but got an error back (502/503-class)."""


class LoomInternalError(LoomHttpError):
    """Daemon-internal failure that doesn't match any other category."""


# URI -> exception class. The mapping is exhaustive against
# `openccu_loom_types.rest.Code`; tests assert that every Code enum
# value has a binding here so the mapping stays in sync with the
# wire contract.
_CODE_TO_EXCEPTION: dict[Code, type[LoomHttpError]] = {
    Code.validation: LoomValidationError,
    Code.bad_request: LoomBadRequestError,
    Code.not_found: LoomNotFoundError,
    Code.conflict: LoomConflictError,
    Code.unauthorized: LoomAuthError,
    Code.forbidden: LoomForbiddenError,
    Code.unsupported: LoomUnsupportedError,
    Code.rate_limited: LoomRateLimitedError,
    Code.service_unready: LoomServiceUnreadyError,
    Code.upstream_unavailable: LoomUpstreamUnavailableError,
    Code.internal: LoomInternalError,
}


def _problem_code(*, problem: Problem) -> Code | None:
    """
    Extract the ``Code`` enum value from a Problem.

    The wire contract carries two parallel signals:

    1. ``Problem.code`` (optional) — the short code as a Code enum,
       set by the daemon for every error it produces.
    2. ``Problem.type`` (required) — the full URI under the daemon's
       error namespace, RFC 9457-conforming.

    We prefer ``code`` because it's direct (no string parsing) and
    fall back to URI parsing for resilience — older daemon versions
    or proxied responses might omit ``code``. Returns ``None`` only
    when neither path identifies a known code.
    """
    if problem.code is not None:
        return problem.code
    type_uri = problem.type.value if hasattr(problem.type, "value") else str(problem.type)
    if not type_uri.startswith(_PROBLEM_URI_PREFIX):
        return None
    tail = type_uri[len(_PROBLEM_URI_PREFIX) :]
    try:
        return Code(tail)
    except ValueError:
        return None


def http_error_from_problem(
    *,
    status: int,
    problem: Problem | None,
    raw_body: bytes | None,
    method: str,
    url: str,
) -> LoomHttpError:
    """
    Pick the most specific exception class for an HTTP failure.

    The status code is included for diagnostics, but the type URI is
    the authoritative dispatcher — the daemon contract guarantees that
    the URI carries the semantic category (per ADR-0020).
    """
    cls: type[LoomHttpError] = LoomHttpError
    if problem is not None:
        code = _problem_code(problem=problem)
        if code is not None:
            cls = _CODE_TO_EXCEPTION.get(code, LoomHttpError)
    return cls(
        status=status,
        problem=problem,
        raw_body=raw_body,
        method=method,
        url=url,
    )


def parse_problem(*, payload: Any) -> Problem | None:
    """
    Parse a JSON payload into a ``Problem``; return None on mismatch.

    Defensive against upstream proxies that swallow the body or replace
    it with their own HTML — we never want a generator-style parse
    error to mask the real HTTP failure that triggered it.
    """
    if not isinstance(payload, dict):
        return None
    try:
        return Problem.model_validate(payload)
    except Exception:  # noqa: BLE001 # pragma: no cover - pydantic catches everything specific
        return None
