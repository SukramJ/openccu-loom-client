# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Tests for the problem+json → exception mapping."""

from __future__ import annotations

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
from openccu_loom_client.wire.rest import Code, Problem


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


class TestAiohomematicHierarchy:
    """
    Loom failures must be catchable as aiohomematic failures.

    ``homematicip_local`` wraps every backend call in
    ``except BaseHomematicException`` (imported from the *real* aiohomematic,
    not the compat shim) and maps it to a typed websocket error. A loom
    exception outside that hierarchy escapes the handler and reaches the config
    panel as a generic ``unknown_error``, losing the error code and the daemon's
    problem+json title.
    """

    def test_loom_exceptions_are_aiohomematic_exceptions(self) -> None:
        from aiohomematic.exceptions import BaseHomematicException

        from openccu_loom_client.exceptions import BaseLoomException, LoomTransportError

        assert issubclass(BaseLoomException, BaseHomematicException)
        assert isinstance(LoomTransportError("boom"), BaseHomematicException)
        assert isinstance(
            http_error_from_problem(
                status=404,
                problem=_make_problem(Code.not_found, status=404),
                raw_body=None,
                method="GET",
                url="/devices/X",
            ),
            BaseHomematicException,
        )

    def test_message_survives_the_aiohomematic_base(self) -> None:
        """Aiohomematic's base eats a leading ``name`` arg — str(err) must stay the message."""
        from openccu_loom_client.exceptions import LoomTransportError

        err = LoomTransportError("connection refused")
        assert str(err) == "connection refused"
        assert err.name == "LoomTransportError"

        http_err = http_error_from_problem(
            status=404,
            problem=_make_problem(Code.not_found, status=404, title="device gone"),
            raw_body=None,
            method="PUT",
            url="/devices/X/paramsets/MASTER",
        )
        # The handler forwards str(err) as the websocket error message.
        assert str(http_err) == "[404] PUT /devices/X/paramsets/MASTER: device gone"

    def test_handler_maps_loom_error_to_typed_code(self) -> None:
        """Simulate homematicip_local's handler: typed code + daemon title, not unknown_error."""
        from aiohomematic.exceptions import BaseHomematicException

        def handler() -> tuple[str, str]:
            try:
                raise http_error_from_problem(
                    status=404,
                    problem=_make_problem(Code.not_found, status=404, title="device gone"),
                    raw_body=None,
                    method="PUT",
                    url="/devices/X",
                )
            except BaseHomematicException as err:
                return ("write_failed", str(err))
            except Exception:
                return ("unknown_error", "")

        code, message = handler()
        assert code == "write_failed"
        assert "device gone" in message
