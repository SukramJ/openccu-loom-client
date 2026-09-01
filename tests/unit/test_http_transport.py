# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""HTTP-transport tests against an in-process mock daemon."""

from __future__ import annotations

from dataclasses import replace
import logging
import time

from pydantic import ValidationError
import pytest

from openccu_loom_client import (
    Capability,
    LoomAuthError,
    LoomConfig,
    LoomHttpError,
    LoomIncompatibleVersionError,
    LoomNotFoundError,
    LoomTransportError,
    LoomUpstreamUnavailableError,
    wire,
)
from openccu_loom_client.compat.aiohomematic.central import list_ccus
from openccu_loom_client.transport import HttpTransport
from tests.helpers import MockDaemon

# What the compat pre-flight declares, spelled out here rather than imported
# from the module under test: importing the constant would make the assertion
# agree with whatever that module says, which is no assertion at all.
_PREFLIGHT_TOKENS = ["rest.v1", "errors.problem_details.v1"]

_INFO_RESPONSE = {
    "version": "1.2.3",
    # The version this build's types were generated against. It no longer
    # gates anything — TestApiVersionReporting pins the reporting rule with
    # fixed literals on both sides — but keeping it matched here means
    # unrelated cases produce no version log to reason about.
    "api_version": wire.DAEMON_API_VERSION,
    "commit": "deadbeef",
    "build_date": "2026-05-24T10:00:00Z",
    "addon_build": False,
    "started_at": "2026-05-24T10:01:00Z",
    # uptime is ISO-8601 duration per the daemon schema.
    "uptime": "PT60S",
    # capabilities is a closed enum on the wire — these values come
    # straight from openccu_loom_client.wire.rest.Capability.
    "capabilities": ["rest.v1", "ws.broadcasts.v1", "errors.problem_details.v1"],
    # Required field since daemon 0.2.0; empty skips the digest handshake
    # so unrelated tests stay quiet. Digest tests override it explicitly.
    "schema_digest": "",
    "config_ui_url": "",
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
        assert info.api_version == wire.DAEMON_API_VERSION
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
        monkeypatch.setattr(wire, "SCHEMA_DIGEST", _DIGEST_A, raising=False)
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
        monkeypatch.setattr(wire, "SCHEMA_DIGEST", _DIGEST_A, raising=False)
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
        monkeypatch.setattr(wire, "SCHEMA_DIGEST", types_digest, raising=False)
        mock_daemon.get("/api/v1/info", payload={**_INFO_RESPONSE, **payload_extra})
        with caplog.at_level(logging.WARNING):
            await transport.connect()
        assert not any("different daemon build" in r.message for r in caplog.records)
        await transport.close()


# Fixed literals, deliberately. The predecessor of this class read the
# daemon's answer back out of ``wire.DAEMON_API_VERSION`` and asserted the
# constant against itself, so it stayed green whatever the rule was — it
# measured nothing. Both sides are now written down: the types are pinned to
# _TYPES_API_VERSION via monkeypatch and each case names the daemon's answer.
_TYPES_API_VERSION = "10.1.0"


class TestApiVersionReporting:
    """
    connect() reports an API-version difference and connects anyway.

    Refusing on the number was wrong in both directions. Upward: the daemon's
    major went 7 → 8 → 9 → 10 in one release window and every bump removed
    surface no generated client referenced. Downward: HACS updates the HA
    integration before the daemon, so ``minor(daemon) < minor(types)`` is the
    ordinary state of an additive daemon release. The hard gate is the
    capability handshake — see :class:`TestCapabilityGate`.
    """

    @pytest.fixture(autouse=True)
    def _pin_types_version(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(wire, "DAEMON_API_VERSION", _TYPES_API_VERSION, raising=False)

    @staticmethod
    def _version_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
        return [r for r in caplog.records if "reports API version" in r.getMessage()]

    async def test_exact_match_is_silent(
        self, mock_daemon: MockDaemon, transport: HttpTransport, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_daemon.get("/api/v1/info", payload={**_INFO_RESPONSE, "api_version": "10.1.0"})
        with caplog.at_level(logging.INFO):
            await transport.connect()
        assert self._version_records(caplog) == []
        await transport.close()

    async def test_patch_difference_is_silent(
        self, mock_daemon: MockDaemon, transport: HttpTransport, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Only major and minor are contract-bearing; a patch bump says nothing.
        mock_daemon.get("/api/v1/info", payload={**_INFO_RESPONSE, "api_version": "10.1.7"})
        with caplog.at_level(logging.INFO):
            await transport.connect()
        assert self._version_records(caplog) == []
        await transport.close()

    async def test_daemon_ahead_by_a_minor_connects_and_logs_info(
        self, mock_daemon: MockDaemon, transport: HttpTransport, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_daemon.get("/api/v1/info", payload={**_INFO_RESPONSE, "api_version": "10.4.0"})
        with caplog.at_level(logging.INFO):
            info = await transport.connect()
        assert info.api_version == "10.4.0"
        records = self._version_records(caplog)
        assert [r.levelno for r in records] == [logging.INFO]
        await transport.close()

    async def test_daemon_behind_by_a_minor_connects_and_logs_info(
        self, mock_daemon: MockDaemon, transport: HttpTransport, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The HACS ordering: the integration updates first, the daemon later.
        # This used to hard-fail setup for a contract that was fully present.
        mock_daemon.get("/api/v1/info", payload={**_INFO_RESPONSE, "api_version": "10.0.0"})
        with caplog.at_level(logging.INFO):
            info = await transport.connect()
        assert info.api_version == "10.0.0"
        records = self._version_records(caplog)
        assert [r.levelno for r in records] == [logging.INFO]
        await transport.close()

    @pytest.mark.parametrize("daemon_version", ["11.0.0", "9.5.0"])
    async def test_major_difference_connects_and_warns(
        self,
        mock_daemon: MockDaemon,
        transport: HttpTransport,
        caplog: pytest.LogCaptureFixture,
        daemon_version: str,
    ) -> None:
        mock_daemon.get("/api/v1/info", payload={**_INFO_RESPONSE, "api_version": daemon_version})
        with caplog.at_level(logging.INFO):
            info = await transport.connect()
        assert info.api_version == daemon_version
        records = self._version_records(caplog)
        assert [r.levelno for r in records] == [logging.WARNING]
        await transport.close()

    async def test_unparseable_version_is_silent(
        self, mock_daemon: MockDaemon, transport: HttpTransport, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_daemon.get("/api/v1/info", payload={**_INFO_RESPONSE, "api_version": "experimental"})
        with caplog.at_level(logging.INFO):
            await transport.connect()
        assert self._version_records(caplog) == []
        await transport.close()

    async def test_older_daemon_missing_a_payload_field_still_reports_the_version_first(
        self, mock_daemon: MockDaemon, transport: HttpTransport, caplog: pytest.LogCaptureFixture
    ) -> None:
        # An older daemon is missing whichever payload field this types release
        # added last, and every Info field is required — so model validation
        # still fails, naming that field. The version note is emitted *before*
        # validation so the log names the actual cause next to the symptom.
        old = {k: v for k, v in _INFO_RESPONSE.items() if k != "config_ui_url"}
        mock_daemon.get("/api/v1/info", payload={**old, "api_version": "10.0.0"})
        with caplog.at_level(logging.INFO), pytest.raises(ValidationError):
            await transport.connect()
        assert [r.levelno for r in self._version_records(caplog)] == [logging.INFO]


class TestCapabilityGate:
    """
    The declared capabilities are the only hard compatibility gate at connect().

    Typed as LoomIncompatibleVersionError (a LoomTransportError subclass): an
    absent capability does not clear on its own, and a caller that retries
    "not ready" conditions has to tell that apart from an unreachable host.
    """

    async def test_missing_required_capability_raises(self, mock_daemon: MockDaemon, transport: HttpTransport) -> None:
        mock_daemon.get("/api/v1/info", payload=_INFO_RESPONSE)
        with pytest.raises(LoomIncompatibleVersionError, match="missing required capabilities"):
            await transport.connect(required_capabilities=[Capability.ALARM])
        await transport.close()

    async def test_missing_capability_raises_even_on_an_exact_version_match(
        self, mock_daemon: MockDaemon, transport: HttpTransport, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The negative control for the case above: matching versions do not
        # excuse an absent capability, so the gate is the capability set and
        # not a version comparison wearing a new message.
        monkeypatch.setattr(wire, "DAEMON_API_VERSION", _TYPES_API_VERSION, raising=False)
        mock_daemon.get("/api/v1/info", payload={**_INFO_RESPONSE, "api_version": _TYPES_API_VERSION})
        with pytest.raises(LoomIncompatibleVersionError, match="missing required capabilities"):
            await transport.connect(required_capabilities=[Capability.HISTORY])
        await transport.close()

    async def test_present_capability_connects_across_a_major_difference(
        self, mock_daemon: MockDaemon, transport: HttpTransport, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The whole point of the change: a daemon two majors away still serves
        # a caller whose declared surface it advertises.
        monkeypatch.setattr(wire, "DAEMON_API_VERSION", _TYPES_API_VERSION, raising=False)
        mock_daemon.get("/api/v1/info", payload={**_INFO_RESPONSE, "api_version": "12.0.0"})
        await transport.connect(required_capabilities=[Capability.REST, Capability.WS_BROADCASTS])
        await transport.close()

    async def test_no_declared_capabilities_never_raises(
        self, mock_daemon: MockDaemon, transport: HttpTransport
    ) -> None:
        mock_daemon.get("/api/v1/info", payload={**_INFO_RESPONSE, "capabilities": []})
        await transport.connect()
        await transport.close()


class TestCompatLayerDeclaresItsCapabilities:
    """
    The compat layer's ``connect()`` calls declare what they cannot work without.

    The transport gate above only bites for a caller that names something, so
    a call site passing nothing turns the whole handshake into decoration.
    These pin the declaration through the real entry point rather than
    re-passing the tokens from the test — asserting the effect, not the
    collaboration.

    (Lives beside the gate it makes load-bearing; a dedicated compat test
    module would be the tidier home once one exists.)
    """

    async def test_list_ccus_refuses_a_daemon_without_problem_details(self, mock_daemon: MockDaemon) -> None:
        # errors.problem_details.v1 is what makes http_error_from_problem
        # dispatch LoomAuthError from the problem type URI. Without it the
        # config flow's invalid_auth answer can never be produced, so this
        # pre-flight has no business reporting success.
        mock_daemon.get(
            "/api/v1/info",
            payload={**_INFO_RESPONSE, "capabilities": ["rest.v1", "ws.broadcasts.v1"]},
        )
        mock_daemon.get("/api/v1/system/ccu", payload={"entries": []})
        with pytest.raises(LoomIncompatibleVersionError, match="missing required capabilities"):
            await list_ccus(host=mock_daemon.host, port=mock_daemon.port, token="t")

    async def test_list_ccus_connects_when_its_declared_capabilities_are_present(self, mock_daemon: MockDaemon) -> None:
        # The negative control for the case above: with the same call site and
        # the tokens present, the pre-flight completes — so the failure there
        # is the declaration biting, not list_ccus being broken.
        mock_daemon.get("/api/v1/info", payload=_INFO_RESPONSE)
        mock_daemon.get("/api/v1/system/ccu", payload={"entries": []})
        assert await list_ccus(host=mock_daemon.host, port=mock_daemon.port, token="t") == []

    async def test_list_ccus_does_not_require_the_event_stream(self, mock_daemon: MockDaemon) -> None:
        # Over-declaring is the lockout this change removed, in a new place.
        # A pre-flight that opens no WS must not refuse a daemon over it.
        mock_daemon.get("/api/v1/info", payload={**_INFO_RESPONSE, "capabilities": _PREFLIGHT_TOKENS})
        mock_daemon.get("/api/v1/system/ccu", payload={"entries": []})
        assert await list_ccus(host=mock_daemon.host, port=mock_daemon.port, token="t") == []


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


class TestTransportSecurityHardening:
    """Audit fixes F3 (no daemon-driven redirects) and F6 (bounded download)."""

    async def test_json_request_does_not_follow_redirect(
        self, mock_daemon: MockDaemon, transport: HttpTransport
    ) -> None:
        # A hostile/compromised daemon replies 302 → another endpoint; aiohttp
        # would otherwise re-send the auth header there. With allow_redirects
        # disabled the 3xx surfaces as an error and the target is never hit.
        mock_daemon.get("/api/v1/info", payload=_INFO_RESPONSE)
        mock_daemon.get(
            "/api/v1/devices",
            status=302,
            headers={"Location": "/api/v1/leaked"},
        )
        mock_daemon.get("/api/v1/leaked", payload={"stolen": True})
        await transport.connect()
        # The unfollowed 3xx surfaces as an HTTP-status error, not a 2xx body.
        with pytest.raises(LoomHttpError) as ei:
            await transport.request(method="GET", path="/devices")
        assert ei.value.status == 302
        await transport.close()
        assert not any(r.path == "/api/v1/leaked" for r in mock_daemon.requests), (
            "client followed a daemon-controlled redirect"
        )

    async def test_request_bytes_does_not_follow_redirect(
        self, mock_daemon: MockDaemon, transport: HttpTransport
    ) -> None:
        mock_daemon.get("/api/v1/info", payload=_INFO_RESPONSE)
        mock_daemon.get(
            "/api/v1/backup",
            status=302,
            headers={"Location": "/api/v1/leaked"},
        )
        mock_daemon.get("/api/v1/leaked", body=b"stolen")
        await transport.connect()
        with pytest.raises(LoomHttpError) as ei:
            await transport.request_bytes(method="GET", path="/backup")
        assert ei.value.status == 302
        await transport.close()
        assert not any(r.path == "/api/v1/leaked" for r in mock_daemon.requests)

    async def test_request_bytes_aborts_past_max_bytes(self, mock_daemon: MockDaemon, transport: HttpTransport) -> None:
        mock_daemon.get("/api/v1/info", payload=_INFO_RESPONSE)
        mock_daemon.get("/api/v1/backup", body=b"x" * 4096)
        await transport.connect()
        with pytest.raises(LoomTransportError, match="download cap"):
            await transport.request_bytes(method="GET", path="/backup", max_bytes=1024)
        await transport.close()

    async def test_request_bytes_returns_body_within_cap(
        self, mock_daemon: MockDaemon, transport: HttpTransport
    ) -> None:
        mock_daemon.get("/api/v1/info", payload=_INFO_RESPONSE)
        mock_daemon.get("/api/v1/backup", body=b"x" * 4096)
        await transport.connect()
        raw = await transport.request_bytes(method="GET", path="/backup", max_bytes=1_000_000)
        assert raw == b"x" * 4096
        await transport.close()

    async def test_request_upload_does_not_follow_redirect(
        self, mock_daemon: MockDaemon, transport: HttpTransport
    ) -> None:
        # Same reasoning as the two above, and with more at stake: a
        # followed 3xx would carry the auth header *and* the archive to
        # whatever host the daemon named.
        mock_daemon.get("/api/v1/info", payload=_INFO_RESPONSE)
        mock_daemon.post("/api/v1/backups/upload", status=302, headers={"Location": "/api/v1/leaked"})
        mock_daemon.post("/api/v1/leaked", payload={"stolen": True})
        await transport.connect()
        with pytest.raises(LoomHttpError) as ei:
            await transport.request_upload(
                method="POST",
                path="/backups/upload",
                field_name="file",
                filename="ccu.sbk",
                content=b"SBK",
            )
        assert ei.value.status == 302
        await transport.close()
        assert not any(r.path == "/api/v1/leaked" for r in mock_daemon.requests)


class TestRequestUpload:
    """``request_upload`` — the multipart counterpart to ``request_bytes``."""

    async def test_upload_is_never_retried(self, mock_daemon: MockDaemon, transport: HttpTransport) -> None:
        # A retried upload re-sends the whole archive and, on a route that
        # is not idempotent, can leave a second stored backup behind. The
        # 503 must surface on the first attempt.
        mock_daemon.get("/api/v1/info", payload=_INFO_RESPONSE)
        mock_daemon.post(
            "/api/v1/backups/upload",
            status=503,
            payload={
                "type": "https://openccu-loom.dev/errors/service_unready",
                "title": "Backup storage unavailable",
                "status": 503,
            },
        )
        await transport.connect()
        with pytest.raises(LoomHttpError):
            await transport.request_upload(
                method="POST",
                path="/backups/upload",
                field_name="file",
                filename="ccu.sbk",
                content=b"SBK",
            )
        await transport.close()
        assert sum(1 for r in mock_daemon.requests if r.path == "/api/v1/backups/upload") == 1

    async def test_upload_204_returns_none(self, mock_daemon: MockDaemon, transport: HttpTransport) -> None:
        mock_daemon.get("/api/v1/info", payload=_INFO_RESPONSE)
        mock_daemon.post("/api/v1/backups/upload", status=204)
        await transport.connect()
        assert (
            await transport.request_upload(
                method="POST",
                path="/backups/upload",
                field_name="file",
                filename="ccu.sbk",
                content=b"SBK",
            )
            is None
        )
        await transport.close()
