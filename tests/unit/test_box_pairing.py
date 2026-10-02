# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Box pairing: the client asking an openccu-lite box for a gate token.

``FakeBoxPairing`` plays the program's side of the box's client pairing as
occulited's ``docs/system-api.md`` (*Client pairing — the program's side*)
describes it: ``POST /api/auth/v1/pairing/request`` answers 202 with the
request id, poll secret and nonce; polls on ``…/request/{id}`` carry
``Authorization: Pairing <poll>``, the first one reveals ``client_nonce``
(checked against the commitment), and the administrator's decision arrives
once. Over plain HTTP the fingerprint is empty.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
import hashlib
import secrets
from typing import Any

from aiohttp import web
import pytest

from openccu_loom_client import BoxPairingResult, LoomBoxPairingError, LoomTransportError, start_box_pairing
from openccu_loom_client.pairing import derive_pairing_code

_PATH = "/api/auth/v1/pairing/request"
_TOKEN = "olt_0123456789abcdef0123456789abcdef"


@dataclass(slots=True)
class FakeBoxPairing:
    """In-process fake of the box's pairing routes."""

    port: int = 0
    asks: list[dict[str, Any]] = field(default_factory=list)
    polls: list[dict[str, str]] = field(default_factory=list)
    withdrawn: list[str] = field(default_factory=list)
    # What the next request answers instead of 202, as (status, body).
    refuse: tuple[int, dict[str, Any]] | None = None
    # The administrator's decision the poll delivers after `pending_polls`
    # pending answers; None keeps the request pending.
    decision: str | None = "approved"
    pending_polls: int = 1
    # Answer this many polls with 429 slow_down first.
    slow_downs: int = 0
    nonce: bytes = field(default_factory=lambda: secrets.token_bytes(16))
    poll_secret: str = "poll-secret"
    commit: str = ""
    revealed: bytes | None = None
    _runner: web.AppRunner | None = None

    async def start(self) -> FakeBoxPairing:
        app = web.Application()
        app.router.add_post(_PATH, self._request)
        app.router.add_get(f"{_PATH}/{{id}}", self._poll)
        app.router.add_delete(f"{_PATH}/{{id}}", self._withdraw)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host="127.0.0.1", port=0)
        await site.start()
        self.port = self._runner.addresses[0][1]
        return self

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _request(self, request: web.Request) -> web.Response:
        ask = await request.json()
        self.asks.append(ask)
        if self.refuse is not None:
            status, body = self.refuse
            return web.json_response(body, status=status)
        self.commit = ask["commit"]
        return web.json_response(
            {
                "id": "req-1",
                "poll": self.poll_secret,
                "nonce": self.nonce.hex(),
                "expires_in": 300,
                "interval": 0.01,
                "fingerprint": "",
            },
            status=202,
        )

    async def _poll(self, request: web.Request) -> web.Response:
        if request.headers.get("Authorization") != f"Pairing {self.poll_secret}":
            return web.json_response({"error": "forbidden"}, status=403)
        self.polls.append(dict(request.query))
        if (cn := request.query.get("client_nonce")) is not None:
            if hashlib.sha256(bytes.fromhex(cn)).hexdigest() != self.commit:
                return web.json_response({"error": "invalid"}, status=422)
            self.revealed = bytes.fromhex(cn)
        if self.revealed is None:
            return web.json_response({"error": "not-revealed"}, status=422)
        if self.slow_downs:
            self.slow_downs -= 1
            return web.json_response({"error": "slow_down"}, status=429, headers={"Retry-After": "1"})
        if self.pending_polls:
            self.pending_polls -= 1
            return web.json_response({"state": "pending"})
        if self.decision == "approved":
            return web.json_response(
                {
                    "state": "approved",
                    "token": _TOKEN,
                    "name": "homeassistant-ha",
                    "scopes": ["addon:openccu-loom"],
                    "access": {},
                    "addons": ["openccu-loom"],
                }
            )
        return web.json_response({"state": self.decision})

    async def _withdraw(self, request: web.Request) -> web.Response:
        self.withdrawn.append(request.match_info["id"])
        return web.Response(status=204)

    def code(self) -> str:
        """Return the code the box's status page shows for the revealed request."""
        assert self.revealed is not None
        return derive_pairing_code(nonce=self.nonce, client_nonce=self.revealed, fingerprint=b"")


@pytest.fixture
async def box() -> AsyncIterator[FakeBoxPairing]:
    fake = await FakeBoxPairing().start()
    try:
        yield fake
    finally:
        await fake.stop()


async def _start(box: FakeBoxPairing, **kwargs: Any) -> Any:
    return await start_box_pairing(host="127.0.0.1", port=box.port, tls=False, app="homematicip_local", **kwargs)


class TestBoxPairing:
    async def test_asks_for_the_addon_scope_and_commits_to_its_nonce(self, box: FakeBoxPairing) -> None:
        await _start(box, instance="ha", name="Home Assistant")
        (ask,) = box.asks
        assert ask["app"] == "homematicip_local"
        assert ask["addons"] == ["openccu-loom"]
        assert ask["instance"] == "ha"
        assert ask["name"] == "Home Assistant"
        # Only the add-on's pages are asked for — no box API access.
        assert "access" not in ask
        assert len(ask["commit"]) == 64

    async def test_approved_pairing_delivers_the_token_and_both_sides_show_one_code(self, box: FakeBoxPairing) -> None:
        session = await _start(box)
        result = await session.wait()
        assert result == BoxPairingResult(
            state="approved", token=_TOKEN, name="homeassistant-ha", scopes=("addon:openccu-loom",)
        )
        assert session.code == box.code()
        # The client's half is revealed once, on the first poll, and only then.
        assert "client_nonce" in box.polls[0]
        assert all("client_nonce" not in p for p in box.polls[1:])
        assert _TOKEN not in repr(result)

    @pytest.mark.parametrize("decision", ["rejected", "expired"])
    async def test_a_refused_or_expired_request_carries_no_token(self, box: FakeBoxPairing, decision: str) -> None:
        box.decision = decision
        result = await (await _start(box)).wait()
        assert result == BoxPairingResult(state=decision)

    async def test_slow_down_is_waited_out(self, box: FakeBoxPairing) -> None:
        box.slow_downs = 2
        result = await (await _start(box)).wait()
        assert result.state == "approved"

    @pytest.mark.parametrize(
        ("status", "code"),
        [(403, "pairing-off"), (403, "not-local"), (429, "limit"), (422, "invalid")],
    )
    async def test_box_refusals_name_their_code(self, box: FakeBoxPairing, status: int, code: str) -> None:
        box.refuse = (status, {"error": code})
        with pytest.raises(LoomBoxPairingError) as err:
            await _start(box)
        assert err.value.code == code
        assert err.value.status == status

    async def test_withdraw_drops_the_request(self, box: FakeBoxPairing) -> None:
        session = await _start(box)
        await session.withdraw()
        assert box.withdrawn == ["req-1"]

    async def test_unreachable_box_is_a_transport_error(self) -> None:
        with pytest.raises(LoomTransportError):
            await start_box_pairing(host="127.0.0.1", port=1, tls=False, app="x")
