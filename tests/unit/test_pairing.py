# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Client token pairing (the daemon's ADR 0076) against the mock daemon."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import pytest

from openccu_loom_client import LoomPairingOffError, LoomTransportError, PairingFingerprintMismatchError, start_pairing
from openccu_loom_client.pairing import derive_pairing_code

if TYPE_CHECKING:
    from tests.helpers.mock_daemon import MockDaemon


def _knobs(mock_daemon: MockDaemon) -> dict[str, object]:
    return {"host": mock_daemon.host, "port": mock_daemon.port, "tls": False}


def test_derive_pairing_code_matches_the_daemon() -> None:
    """
    Cross-language pin of the shared derivation.

    The daemon's ``internal/pairing`` golden-vector test hashes the same
    inputs; six digits, stable, and equal for equal inputs on both sides
    of the wire is the whole protocol.
    """
    nonce = bytes.fromhex("00112233445566778899aabbccddeeff")
    client_nonce = bytes.fromhex("ffeeddccbbaa99887766554433221100" * 2)
    fingerprint = bytes.fromhex("0123456789abcdef" * 8)
    digest = hashlib.sha256(nonce + client_nonce + fingerprint).digest()
    expected = f"{int.from_bytes(digest[:4], 'big') % 1_000_000:06d}"
    code = derive_pairing_code(nonce=nonce, client_nonce=client_nonce, fingerprint=fingerprint)
    assert code == expected
    assert len(code) == 6
    assert code.isdigit()
    # The empty fingerprint (plain HTTP / TLS-terminating proxy) is a
    # legal input, not an error.
    assert len(derive_pairing_code(nonce=nonce, client_nonce=client_nonce, fingerprint=b"")) == 6


async def test_pairing_happy_path(mock_daemon: MockDaemon) -> None:
    """Ask → code → approved poll carries the token; reveal rides the first poll."""
    nonce_hex = "aa" * 16
    mock_daemon.post(
        "/api/v1/pairing",
        status=202,
        payload={
            "id": "req1",
            "poll": "secret-poll",
            "nonce": nonce_hex,
            "expires_in": 300,
            "interval": 2,
            "fingerprint": "",
        },
    )
    mock_daemon.get("/api/v1/pairing/req1", payload={"state": "pending"})
    mock_daemon.get(
        "/api/v1/pairing/req1",
        payload={"state": "approved", "token": "tok-abc", "subject": "app-ha", "role": "operator"},
    )

    session = await start_pairing(app="app", instance="ha", role="operator", **_knobs(mock_daemon))
    assert session.code.isdigit() and len(session.code) == 6

    result = await session.wait()
    assert result.state == "approved"
    assert result.token == "tok-abc"

    polls = [r for r in mock_daemon.requests if r.path.startswith("/api/v1/pairing/req1")]
    assert polls[0].query.get("client_nonce"), "first poll must reveal the client nonce"
    assert "client_nonce" not in polls[1].query, "the reveal must not repeat"
    assert polls[0].headers.get("Authorization") == "Pairing secret-poll"


async def test_pairing_off_raises_the_branchable_error(mock_daemon: MockDaemon) -> None:
    """A daemon with pairing switched off answers pairing_off — do not retry."""
    mock_daemon.post(
        "/api/v1/pairing",
        status=503,
        payload={
            "type": "https://openccu-loom.dev/errors/pairing_off",
            "title": "Pairing is switched off",
            "status": 503,
            "code": "pairing_off",
        },
        content_type="application/problem+json",
    )
    with pytest.raises(LoomPairingOffError):
        await start_pairing(app="app", role="operator", **_knobs(mock_daemon))


async def test_pairing_slow_down_keeps_polling(mock_daemon: MockDaemon) -> None:
    """pairing_slow_down is 'poll less often', not an abort."""
    mock_daemon.post(
        "/api/v1/pairing",
        status=202,
        payload={
            "id": "req2",
            "poll": "p2",
            "nonce": "bb" * 16,
            "expires_in": 300,
            "interval": 0,
            "fingerprint": "",
        },
    )
    mock_daemon.get(
        "/api/v1/pairing/req2",
        status=429,
        payload={
            "type": "https://openccu-loom.dev/errors/pairing_slow_down",
            "title": "Poll less often",
            "status": 429,
            "code": "pairing_slow_down",
        },
        content_type="application/problem+json",
    )
    mock_daemon.get("/api/v1/pairing/req2", payload={"state": "rejected"})

    session = await start_pairing(app="app", role="viewer", **_knobs(mock_daemon))
    result = await session.wait()
    assert result.state == "rejected"
    assert result.token is None


async def test_fingerprint_mismatch_aborts_and_withdraws(
    mock_daemon: MockDaemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A daemon naming a certificate this connection did not see is an interceptor sign."""
    mock_daemon.post(
        "/api/v1/pairing",
        status=202,
        payload={
            "id": "req3",
            "poll": "p3",
            "nonce": "cc" * 16,
            "expires_in": 300,
            "interval": 2,
            "fingerprint": "dd" * 32,
        },
    )
    mock_daemon.delete("/api/v1/pairing/req3", status=204)
    monkeypatch.setattr("openccu_loom_client.pairing.seen_fingerprint", lambda *, resp: b"\xee" * 32)
    with pytest.raises(PairingFingerprintMismatchError):
        await start_pairing(app="app", role="operator", **_knobs(mock_daemon))
    withdrawals = [r for r in mock_daemon.requests if r.method == "DELETE"]
    assert len(withdrawals) == 1, "the poisoned request must be withdrawn"


async def test_an_unreachable_daemon_is_a_transport_error() -> None:
    """
    A connection failure raises LoomTransportError, the class callers map to cannot_connect.

    The raw aiohttp error used to escape, and a caller catching the package's
    exceptions — Home Assistant's config flow does — had no branch for it.
    """
    with pytest.raises(LoomTransportError):
        await start_pairing(host="127.0.0.1", port=1, tls=False, app="app", role="operator")


async def test_poll_and_withdraw_against_a_vanished_daemon_are_transport_errors(mock_daemon: MockDaemon) -> None:
    """The running session's poll and withdrawal map a lost daemon the same way."""
    mock_daemon.post(
        "/api/v1/pairing",
        status=202,
        payload={"id": "req1", "poll": "p", "nonce": "aa" * 16, "expires_in": 300, "interval": 2, "fingerprint": ""},
    )
    session = await start_pairing(app="app", role="operator", **_knobs(mock_daemon))
    # Point the running session at a port nothing listens on, as if the
    # daemon went away between the ask and the poll.
    session._base = "http://127.0.0.1:1/api/v1"
    with pytest.raises(LoomTransportError):
        await session.wait()
    with pytest.raises(LoomTransportError):
        await session.withdraw()
