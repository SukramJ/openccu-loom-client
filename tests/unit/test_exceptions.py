# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Tests for the problem+json → exception mapping."""

from __future__ import annotations

from openccu_loom_types.rest import Code, Problem
import pytest

from openccu_loom_client.exceptions import (
    _CODE_TO_EXCEPTION,
    LoomAuthError,
    LoomForbiddenError,
    LoomHttpError,
    LoomNotFoundError,
    LoomRateLimitedError,
    LoomServiceUnreadyError,
    LoomUpstreamUnavailableError,
    LoomValidationError,
    http_error_from_problem,
    parse_problem,
)


def _make_problem(code: Code, *, status: int, title: str = "boom") -> Problem:
    """
    Build a Problem that carries BOTH the URI (`type`) and the short `code` field.

    The daemon emits both on every error, and the
    dispatcher must work even if one is missing.
    """
    return Problem.model_validate(
        {
            "type": f"https://openccu-loom.dev/errors/{code.value}",
            "title": title,
            "status": status,
            "code": code.value,
        }
    )


class TestParseProblem:
    def test_parses_minimal_payload(self) -> None:
        payload = {
            "type": "https://openccu-loom.dev/errors/not_found",
            "title": "Device not found",
            "status": 404,
        }
        problem = parse_problem(payload=payload)
        assert problem is not None
        assert problem.type.value.endswith("/not_found")

    def test_returns_none_for_non_dict(self) -> None:
        assert parse_problem(payload="not a dict") is None
        assert parse_problem(payload=None) is None
        assert parse_problem(payload=[1, 2, 3]) is None


class TestErrorMapping:
    def test_mapping_is_exhaustive(self) -> None:
        """Every Code enum value must have a mapped exception class."""
        unmapped = set(Code) - set(_CODE_TO_EXCEPTION)
        assert unmapped == set(), f"unmapped problem codes: {unmapped}"

    @pytest.mark.parametrize(
        ("code", "expected_cls"),
        [
            (Code.validation, LoomValidationError),
            (Code.not_found, LoomNotFoundError),
            (Code.unauthorized, LoomAuthError),
            (Code.forbidden, LoomForbiddenError),
            (Code.rate_limited, LoomRateLimitedError),
            (Code.service_unready, LoomServiceUnreadyError),
            (Code.upstream_unavailable, LoomUpstreamUnavailableError),
        ],
    )
    def test_dispatch_by_code(self, code: Code, expected_cls: type[LoomHttpError]) -> None:
        problem = _make_problem(code, status=500)
        exc = http_error_from_problem(
            status=500,
            problem=problem,
            raw_body=None,
            method="GET",
            url="https://x/api/v1/devices",
        )
        assert isinstance(exc, expected_cls)

    def test_falls_back_to_base_when_no_problem(self) -> None:
        exc = http_error_from_problem(
            status=502,
            problem=None,
            raw_body=b"<html>nginx</html>",
            method="GET",
            url="https://x/api/v1/devices",
        )
        assert type(exc) is LoomHttpError
        assert exc.raw_body == b"<html>nginx</html>"

    def test_falls_back_to_base_when_neither_code_nor_type_resolves(self) -> None:
        # `Problem.type` is a closed enum, so we can't smuggle an unknown
        # URI through model_validate. Construct the failure path by
        # bypassing the helper and passing a None code + missing type
        # attribute via a stub.
        class _Stub:
            code = None
            type = "https://example.com/something-else"
            title = "wat"
            status = 500

        exc = http_error_from_problem(
            status=500,
            problem=_Stub(),  # type: ignore[arg-type]
            raw_body=None,
            method="GET",
            url="https://x/api/v1/foo",
        )
        assert type(exc) is LoomHttpError

    def test_error_message_format(self) -> None:
        problem = _make_problem(Code.not_found, status=404, title="No such device")
        exc = http_error_from_problem(
            status=404,
            problem=problem,
            raw_body=None,
            method="GET",
            url="https://x/api/v1/devices/0001",
        )
        msg = str(exc)
        assert "404" in msg
        assert "No such device" in msg
        assert "GET https://x/api/v1/devices/0001" in msg


class TestPublicExports:
    """B5: every mapped exception is re-exported from the top-level package."""

    def test_every_mapped_exception_is_publicly_exported(self) -> None:
        import openccu_loom_client as pkg

        for cls in set(_CODE_TO_EXCEPTION.values()):
            assert hasattr(pkg, cls.__name__), f"{cls.__name__} not importable from openccu_loom_client"
            assert cls.__name__ in pkg.__all__, f"{cls.__name__} missing from __all__"

    def test_internal_error_is_exported(self) -> None:
        # The specific regression: LoomInternalError was defined + mapped but
        # never re-exported, so `except LoomInternalError` needed a deep import.
        from openccu_loom_client import LoomInternalError

        assert LoomInternalError.__name__ in __import__("openccu_loom_client").__all__
