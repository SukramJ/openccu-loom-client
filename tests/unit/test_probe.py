# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
The pre-login probe against the mock daemon.

``GET /info`` is answered without a credential by the daemon, and by an
openccu-lite box's gate when the daemon sits behind one. The probe tells the
two apart by the rule in ``boxgate.py`` and reports a gate answer as a result,
not an error: it is how a caller learns to ask for a box credential.
"""

from __future__ import annotations

import json
import socket
from typing import TYPE_CHECKING

import pytest

from openccu_loom_client import (
    DaemonProbe,
    LoomAuthError,
    LoomHttpError,
    LoomTransportError,
    ProbeOutcome,
    probe_daemon,
)

if TYPE_CHECKING:
    from tests.helpers.mock_daemon import MockDaemon

_INFO_PATH = "/api/v1/info"

_INFO_13_5 = {
    "version": "0.86.0",
    "api_version": "13.5.0",
    "commit": "deadbeef",
    "capabilities": [
        "rest.v1",
        "ws.broadcasts.v1",
        "errors.problem_details.v1",
        "auth.pairing.v1",
        "auth.occulite_token.v1",
        "auth.occulite_sso.v1",
    ],
    "deployment": {"kind": "lite-addon", "ingress_path": "/addons/loom/"},
}

_INFO_OLD = {
    "version": "0.80.0",
    "api_version": "12.1.0",
    "capabilities": ["rest.v1", "ws.broadcasts.v1", "auth.oidc.v1"],
}


async def _probe(mock_daemon: MockDaemon) -> DaemonProbe:
    return await probe_daemon(host=mock_daemon.host, port=mock_daemon.port, tls=False)


def _assert_no_credential(mock_daemon: MockDaemon) -> None:
    """Assert the probe sent one request without a credential in any form; it runs before any login."""
    assert len(mock_daemon.requests) == 1, "the probe sends exactly one request"
    request = mock_daemon.requests[0]
    assert request.method == "GET"
    assert request.path == _INFO_PATH
    headers = {key.lower(): value for key, value in request.headers.items()}
    assert "authorization" not in headers
    assert "cookie" not in headers
    assert headers.get("accept") == "application/json"


async def test_a_13_5_daemon_describes_its_deployment_and_login_paths(mock_daemon: MockDaemon) -> None:
    mock_daemon.get(_INFO_PATH, payload=_INFO_13_5)

    probe = await _probe(mock_daemon)

    assert probe.reached is ProbeOutcome.DAEMON
    assert probe.api_version == "13.5.0"
    assert probe.version == "0.86.0"
    assert probe.capabilities == frozenset(_INFO_13_5["capabilities"])
    assert probe.login_paths == frozenset({"pairing", "occulite_token", "occulite_sso"})
    assert probe.deployment_kind == "lite-addon"
    assert probe.ingress_path == "/addons/loom/"
    _assert_no_credential(mock_daemon)


async def test_an_older_daemon_has_no_deployment(mock_daemon: MockDaemon) -> None:
    """Before api 13.5.0 there is no ``deployment``; that reads as unknown, not as an error."""
    mock_daemon.get(_INFO_PATH, payload=_INFO_OLD)

    probe = await _probe(mock_daemon)

    assert probe.reached is ProbeOutcome.DAEMON
    assert probe.api_version == "12.1.0"
    assert probe.deployment_kind is None
    assert probe.ingress_path is None
    assert probe.login_paths == frozenset({"oidc"})


async def test_an_unknown_deployment_kind_is_kept_verbatim(mock_daemon: MockDaemon) -> None:
    """``kind`` is an open vocabulary for a client: a newer daemon's value is not rejected."""
    mock_daemon.get(_INFO_PATH, payload={**_INFO_13_5, "deployment": {"kind": "future-box"}})

    probe = await _probe(mock_daemon)

    assert probe.deployment_kind == "future-box"
    assert probe.ingress_path is None


async def test_a_gate_401_in_plain_json_is_a_box(mock_daemon: MockDaemon) -> None:
    mock_daemon.get(
        _INFO_PATH,
        status=401,
        payload={"error": "unauthenticated", "message": "login required"},
    )

    probe = await _probe(mock_daemon)

    assert probe.reached is ProbeOutcome.BOX_GATE
    assert probe.api_version == ""
    assert probe.version == ""
    assert probe.capabilities == frozenset()
    assert probe.login_paths == frozenset()
    assert probe.deployment_kind is None
    assert probe.ingress_path is None
    _assert_no_credential(mock_daemon)


async def test_a_redirect_to_the_box_login_is_a_box(mock_daemon: MockDaemon) -> None:
    """The probe follows no redirect: the 302 itself is the answer."""
    mock_daemon.get(_INFO_PATH, status=302, headers={"Location": "/login"})

    probe = await _probe(mock_daemon)

    assert probe.reached is ProbeOutcome.BOX_GATE
    _assert_no_credential(mock_daemon)


async def test_a_problem_json_401_is_the_daemon_and_raises(mock_daemon: MockDaemon) -> None:
    """The daemon never answers ``/info`` with 401; when it does, it is not a box and not a success."""
    problem = {
        "type": "https://openccu-loom.dev/errors/unauthorized",
        "title": "Unauthorized",
        "status": 401,
        "code": "unauthorized",
    }
    mock_daemon.get(
        _INFO_PATH,
        status=401,
        body=json.dumps(problem).encode(),
        content_type="application/problem+json",
    )

    with pytest.raises(LoomAuthError):
        await _probe(mock_daemon)


async def test_a_server_error_raises_the_http_error(mock_daemon: MockDaemon) -> None:
    mock_daemon.get(_INFO_PATH, status=500, body=b"boom", content_type="text/plain")

    with pytest.raises(LoomHttpError) as excinfo:
        await _probe(mock_daemon)
    assert excinfo.value.status == 500


async def test_a_200_that_is_not_an_object_raises(mock_daemon: MockDaemon) -> None:
    mock_daemon.get(_INFO_PATH, payload=["rest.v1"])

    with pytest.raises(LoomTransportError):
        await _probe(mock_daemon)


async def test_a_200_without_api_version_raises(mock_daemon: MockDaemon) -> None:
    mock_daemon.get(_INFO_PATH, payload={"version": "0.86.0", "capabilities": []})

    with pytest.raises(LoomTransportError):
        await _probe(mock_daemon)


async def test_a_200_that_is_not_json_raises(mock_daemon: MockDaemon) -> None:
    mock_daemon.get(_INFO_PATH, body=b"<html>hello</html>", content_type="text/html")

    with pytest.raises(LoomTransportError):
        await _probe(mock_daemon)


async def test_an_unreachable_daemon_raises_the_transport_error() -> None:
    """A refused connection arrives as this package's error, not aiohttp's."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with pytest.raises(LoomTransportError):
        await probe_daemon(host="127.0.0.1", port=port, tls=False)


async def test_the_base_path_reaches_a_daemon_behind_ingress(mock_daemon: MockDaemon) -> None:
    mock_daemon.get("/addons/loom/api/v1/info", payload=_INFO_13_5)

    probe = await probe_daemon(host=mock_daemon.host, port=mock_daemon.port, tls=False, base_path="/addons/loom/api/v1")

    assert probe.reached is ProbeOutcome.DAEMON
    assert mock_daemon.requests[0].path == "/addons/loom/api/v1/info"
