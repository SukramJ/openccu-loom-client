# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""HTTP-transport tests against an in-process mock daemon."""

from __future__ import annotations

from dataclasses import replace
import logging
import time

import openccu_loom_types
import pytest

from openccu_loom_client import (
    LoomAuthError,
    LoomConfig,
    LoomNotFoundError,
    LoomTransportError,
    LoomUpstreamUnavailableError,
)
from openccu_loom_client.transport import HttpTransport
from tests.helpers import MockDaemon

_INFO_RESPONSE = {
    "version": "1.2.3",
    # Report the exact API version the installed types were generated
    # against so the connect() compatibility guard passes; guard-rejection
    # cases override this inline. See TestApiVersionGuard.
    "api_version": openccu_loom_types.DAEMON_API_VERSION,
    "commit": "deadbeef",
    "build_date": "2026-05-24T10:00:00Z",
    "addon_build": False,
    "started_at": "2026-05-24T10:01:00Z",
    # uptime is ISO-8601 duration per the daemon schema.
    "uptime": "PT60S",
    # capabilities is a closed enum on the wire — these values come
    # straight from openccu_loom_types.rest.Capability.
    "capabilities": ["rest.v1", "ws.broadcasts.v1", "errors.problem_details.v1"],
    # Required field since daemon 0.2.0; empty skips the digest handshake
    # so unrelated tests stay quiet. Digest tests override it explicitly.
    "schema_digest": "",
}


@pytest.fixture
def transport(mock_daemon: MockDaemon) -> HttpTransport:
    """Return a transport pointed at the mock daemon with backoff disabled."""
    return HttpTransport(config=mock_daemon.config, backoff_sequence=(0.0, 0.0))


class TestConnect:
    async def test_connect_reads_info_and_records_it(self, mock_daemon: MockDaemon, transport: HttpTransport) -> None:
        mock_daemon.get("/api/v1/info", payload=_INFO_RESPONSE)
        info = await transport.connect()
        assert info.version == "1.2.3"
        assert info.api_version == openccu_loom_types.DAEMON_API_VERSION
        assert transport.info is not None
        await transport.close()

    async def test_required_capability_missing_raises(self, mock_daemon: MockDaemon, transport: HttpTransport) -> None:
        mock_daemon.get("/api/v1/info", payload=_INFO_RESPONSE)
        # matter.bridge.v1 is in the Capability enum but not in our
        # fixture response — the daemon doesn't expose it.
        with pytest.raises(LoomTransportError, match="missing required capabilities"):
            await transport.connect(required_capabilities=["matter.bridge.v1"])
        await transport.close()

    async def test_required_capability_present_succeeds(
        self, mock_daemon: MockDaemon, transport: HttpTransport
    ) -> None:
        mock_daemon.get("/api/v1/info", payload=_INFO_RESPONSE)
        await transport.connect(required_capabilities=["ws.broadcasts.v1"])
        await transport.close()


_DIGEST_A = "sha256:" + "a" * 64
_DIGEST_B = "sha256:" + "b" * 64


class TestSchemaDigestHandshake:
    """Connect-time digest comparison against openccu-loom-types (daemon ADR 0028)."""

    async def test_digest_mismatch_warns_but_connects(
        self,
        mock_daemon: MockDaemon,
        transport: HttpTransport,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(openccu_loom_types, "SCHEMA_DIGEST", _DIGEST_A, raising=False)
        mock_daemon.get("/api/v1/info", payload={**_INFO_RESPONSE, "schema_digest": _DIGEST_B})
        with caplog.at_level(logging.WARNING):
            await transport.connect()
        assert any("different daemon build" in r.message for r in caplog.records)
        await transport.close()

    async def test_digest_match_is_silent(
        self,
        mock_daemon: MockDaemon,
        transport: HttpTransport,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(openccu_loom_types, "SCHEMA_DIGEST", _DIGEST_A, raising=False)
        mock_daemon.get("/api/v1/info", payload={**_INFO_RESPONSE, "schema_digest": _DIGEST_A})
        with caplog.at_level(logging.WARNING):
            await transport.connect()
        assert not any("different daemon build" in r.message for r in caplog.records)
        await transport.close()

    @pytest.mark.parametrize(
        ("types_digest", "payload_extra"),
        [
            (_DIGEST_A, {}),  # daemon sends an empty digest
            ("", {"schema_digest": _DIGEST_B}),  # types package not stamped
        ],
    )
    async def test_digest_check_skipped_when_either_side_missing(
        self,
        mock_daemon: MockDaemon,
        transport: HttpTransport,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        types_digest: str,
        payload_extra: dict[str, str],
    ) -> None:
        monkeypatch.setattr(openccu_loom_types, "SCHEMA_DIGEST", types_digest, raising=False)
        mock_daemon.get("/api/v1/info", payload={**_INFO_RESPONSE, **payload_extra})
        with caplog.at_level(logging.WARNING):
            await transport.connect()
        assert not any("different daemon build" in r.message for r in caplog.records)
        await transport.close()


class TestApiVersionGuard:
    """connect() hard-fails on an incompatible daemon API version (needs same major, minor ≥ expected)."""

    @staticmethod
    def _expected_major_minor() -> tuple[int, int]:
        parts = openccu_loom_types.DAEMON_API_VERSION.split(".")
        return int(parts[0]), int(parts[1])

    async def test_matching_version_connects(self, mock_daemon: MockDaemon, transport: HttpTransport) -> None:
        # _INFO_RESPONSE already reports DAEMON_API_VERSION → guard passes.
        mock_daemon.get("/api/v1/info", payload=_INFO_RESPONSE)
        await transport.connect()
        await transport.close()

    async def test_newer_minor_same_major_connects(self, mock_daemon: MockDaemon, transport: HttpTransport) -> None:
        major, minor = self._expected_major_minor()
        mock_daemon.get("/api/v1/info", payload={**_INFO_RESPONSE, "api_version": f"{major}.{minor + 5}.0"})
        await transport.connect()
        await transport.close()

    async def test_older_minor_raises(self, mock_daemon: MockDaemon, transport: HttpTransport) -> None:
        # The incident pattern: daemon behind the API version the types expect.
        major, minor = self._expected_major_minor()
        mock_daemon.get("/api/v1/info", payload={**_INFO_RESPONSE, "api_version": f"{major}.{minor - 1}.0"})
        with pytest.raises(LoomTransportError, match="incompatible API version"):
            await transport.connect()

    async def test_newer_major_raises(self, mock_daemon: MockDaemon, transport: HttpTransport) -> None:
        major, _minor = self._expected_major_minor()
        mock_daemon.get("/api/v1/info", payload={**_INFO_RESPONSE, "api_version": f"{major + 1}.0.0"})
        with pytest.raises(LoomTransportError, match="incompatible API version"):
            await transport.connect()

    async def test_older_major_raises(self, mock_daemon: MockDaemon, transport: HttpTransport) -> None:
        major, _minor = self._expected_major_minor()
        mock_daemon.get("/api/v1/info", payload={**_INFO_RESPONSE, "api_version": f"{major - 1}.99.0"})
        with pytest.raises(LoomTransportError, match="incompatible API version"):
            await transport.connect()

    async def test_unparseable_version_skips_guard(self, mock_daemon: MockDaemon, transport: HttpTransport) -> None:
        # A daemon whose api_version isn't dotted-numeric must not hard-fail;
        # the guard degrades to a no-op (the digest handshake still warns).
        mock_daemon.get("/api/v1/info", payload={**_INFO_RESPONSE, "api_version": "experimental"})
        await transport.connect()
        await transport.close()


class TestRequest:
    async def test_get_2xx_returns_json(self, mock_daemon: MockDaemon, transport: HttpTransport) -> None:
        mock_daemon.get("/api/v1/info", payload=_INFO_RESPONSE)
        mock_daemon.get(
            "/api/v1/devices",
            payload={"items": [], "page": 1, "per_page": 50, "total": 0},
        )
        await transport.connect()
        result = await transport.request(method="GET", path="/devices")
        assert result["total"] == 0
        await transport.close()

    async def test_204_returns_none(self, mock_daemon: MockDaemon, transport: HttpTransport) -> None:
        mock_daemon.get("/api/v1/info", payload=_INFO_RESPONSE)
        mock_daemon.delete("/api/v1/devices/X", status=204)
        await transport.connect()
        result = await transport.request(method="DELETE", path="/devices/X")
        assert result is None
        await transport.close()

    async def test_404_with_problem_json_raises_not_found(
        self, mock_daemon: MockDaemon, transport: HttpTransport
    ) -> None:
        mock_daemon.get("/api/v1/info", payload=_INFO_RESPONSE)
        mock_daemon.get(
            "/api/v1/devices/UNKNOWN",
            status=404,
            payload={
                "type": "https://openccu-loom.dev/errors/not_found",
                "title": "Device not found",
                "status": 404,
            },
        )
        await transport.connect()
        with pytest.raises(LoomNotFoundError) as ei:
            await transport.request(method="GET", path="/devices/UNKNOWN")
        assert ei.value.status == 404
        assert ei.value.problem is not None
        await transport.close()

    async def test_401_raises_auth_error(self, mock_daemon: MockDaemon, transport: HttpTransport) -> None:
        mock_daemon.get("/api/v1/info", payload=_INFO_RESPONSE)
        mock_daemon.get(
            "/api/v1/devices",
            status=401,
            payload={
                "type": "https://openccu-loom.dev/errors/unauthorized",
                "title": "Missing or invalid token",
                "status": 401,
            },
        )
        await transport.connect()
        with pytest.raises(LoomAuthError):
            await transport.request(method="GET", path="/devices")
        await transport.close()


class TestRetry:
    async def test_get_retries_on_upstream_unavailable_then_succeeds(
        self, mock_daemon: MockDaemon, transport: HttpTransport
    ) -> None:
        mock_daemon.get("/api/v1/info", payload=_INFO_RESPONSE)
        # First call: upstream unavailable. Second: succeeds.
        mock_daemon.get(
            "/api/v1/devices",
            status=502,
            payload={
                "type": "https://openccu-loom.dev/errors/upstream_unavailable",
                "title": "CCU unreachable",
                "status": 502,
            },
        )
        mock_daemon.get(
            "/api/v1/devices",
            payload={"items": [], "page": 1, "per_page": 50, "total": 0},
        )
        await transport.connect()
        result = await transport.request(method="GET", path="/devices")
        assert result["total"] == 0
        await transport.close()

    async def test_get_gives_up_after_backoff_exhausted(
        self, mock_daemon: MockDaemon, transport: HttpTransport
    ) -> None:
        mock_daemon.get("/api/v1/info", payload=_INFO_RESPONSE)
        # A single 502 stub is reused for every retry until backoff is exhausted.
        mock_daemon.get(
            "/api/v1/devices",
            status=502,
            payload={
                "type": "https://openccu-loom.dev/errors/upstream_unavailable",
                "title": "CCU unreachable",
                "status": 502,
            },
        )
        await transport.connect()
        with pytest.raises(LoomUpstreamUnavailableError):
            await transport.request(method="GET", path="/devices")
        await transport.close()

    async def test_post_does_not_retry_by_default(self, mock_daemon: MockDaemon, transport: HttpTransport) -> None:
        mock_daemon.get("/api/v1/info", payload=_INFO_RESPONSE)
        mock_daemon.post(
            "/api/v1/programs/p1/execute",
            status=502,
            payload={
                "type": "https://openccu-loom.dev/errors/upstream_unavailable",
                "title": "CCU unreachable",
                "status": 502,
            },
        )
        await transport.connect()
        with pytest.raises(LoomUpstreamUnavailableError):
            await transport.request(method="POST", path="/programs/p1/execute")
        await transport.close()


class TestLifecycle:
    async def test_request_without_connect_raises(self, transport: HttpTransport) -> None:
        with pytest.raises(LoomTransportError, match="not connected"):
            await transport.request(method="GET", path="/devices")

    async def test_context_manager_closes_session(self, mock_daemon: MockDaemon) -> None:
        mock_daemon.get("/api/v1/info", payload=_INFO_RESPONSE)
        config: LoomConfig = mock_daemon.config
        async with HttpTransport(config=config, backoff_sequence=()) as t:
            assert t.info is not None
        # After __aexit__, info should be cleared.
        assert t.info is None


class TestDeadlineBudget:
    """N5: retries share one total-deadline budget, not N × per-request timeout."""

    async def test_request_is_bounded_by_the_total_deadline(self, mock_daemon: MockDaemon) -> None:
        mock_daemon.get("/api/v1/info", payload=_INFO_RESPONSE)
        # An endpoint that hangs far past the budget — each attempt would time
        # out on its own; the budget must stop the retries well before N × it.
        mock_daemon.get("/api/v1/devices", payload={"items": []}, delay=1.0)
        cfg = replace(mock_daemon.config, request_timeout_seconds=0.4)
        transport = HttpTransport(config=cfg, backoff_sequence=(0.1, 0.1))
        await transport.connect()
        start = time.monotonic()
        with pytest.raises(LoomTransportError):
            await transport.request(method="GET", path="/devices")
        elapsed = time.monotonic() - start
        await transport.close()
        # Budget ≈ 0.4 s. Without it: 3 × 0.4 + 0.2 backoff ≈ 1.4 s. Assert the
        # total stays near the single budget (generous CI slack).
        assert elapsed < 1.0, f"deadline budget not enforced: {elapsed:.2f}s"
