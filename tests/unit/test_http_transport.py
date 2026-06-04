# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""HTTP-transport tests using aioresponses to mock the daemon."""

from __future__ import annotations

import pytest
from aioresponses import aioresponses

from openccu_loom_client import (
    LoomAuthError,
    LoomConfig,
    LoomNotFoundError,
    LoomTransportError,
    LoomUpstreamUnavailableError,
)
from openccu_loom_client.transport import HttpTransport

_INFO_RESPONSE = {
    "version": "1.2.3",
    "api_version": "1.0.0",
    "commit": "deadbeef",
    "build_date": "2026-05-24T10:00:00Z",
    "started_at": "2026-05-24T10:01:00Z",
    # uptime is ISO-8601 duration per the daemon schema.
    "uptime": "PT60S",
    # capabilities is a closed enum on the wire — these values come
    # straight from openccu_loom_types.rest.Capability.
    "capabilities": ["rest.v1", "ws.broadcasts.v1", "errors.problem_details.v1"],
}


@pytest.fixture
def transport(config: LoomConfig) -> HttpTransport:
    return HttpTransport(config, backoff_sequence=(0.0, 0.0))


class TestConnect:
    async def test_connect_reads_info_and_records_it(self, transport: HttpTransport) -> None:
        with aioresponses() as mock:
            mock.get("http://loom.test:8080/api/v1/info", payload=_INFO_RESPONSE)
            info = await transport.connect()
        assert info.version == "1.2.3"
        assert info.api_version == "1.0.0"
        assert transport.info is not None
        await transport.close()

    async def test_required_capability_missing_raises(self, transport: HttpTransport) -> None:
        with aioresponses() as mock:
            mock.get("http://loom.test:8080/api/v1/info", payload=_INFO_RESPONSE)
            # matter.bridge.v1 is in the Capability enum but not in our
            # fixture response — the daemon doesn't expose it.
            with pytest.raises(LoomTransportError, match="missing required capabilities"):
                await transport.connect(required_capabilities=["matter.bridge.v1"])
        await transport.close()

    async def test_required_capability_present_succeeds(self, transport: HttpTransport) -> None:
        with aioresponses() as mock:
            mock.get("http://loom.test:8080/api/v1/info", payload=_INFO_RESPONSE)
            await transport.connect(required_capabilities=["ws.broadcasts.v1"])
        await transport.close()


class TestRequest:
    async def test_get_2xx_returns_json(self, transport: HttpTransport) -> None:
        with aioresponses() as mock:
            mock.get("http://loom.test:8080/api/v1/info", payload=_INFO_RESPONSE)
            mock.get(
                "http://loom.test:8080/api/v1/devices",
                payload={"items": [], "page": 1, "per_page": 50, "total": 0},
            )
            await transport.connect()
            result = await transport.request("GET", "/devices")
        assert result["total"] == 0
        await transport.close()

    async def test_204_returns_none(self, transport: HttpTransport) -> None:
        with aioresponses() as mock:
            mock.get("http://loom.test:8080/api/v1/info", payload=_INFO_RESPONSE)
            mock.delete("http://loom.test:8080/api/v1/devices/X", status=204)
            await transport.connect()
            result = await transport.request("DELETE", "/devices/X")
        assert result is None
        await transport.close()

    async def test_404_with_problem_json_raises_not_found(self, transport: HttpTransport) -> None:
        with aioresponses() as mock:
            mock.get("http://loom.test:8080/api/v1/info", payload=_INFO_RESPONSE)
            mock.get(
                "http://loom.test:8080/api/v1/devices/UNKNOWN",
                status=404,
                payload={
                    "type": "https://openccu-loom.dev/errors/not_found",
                    "title": "Device not found",
                    "status": 404,
                },
            )
            await transport.connect()
            with pytest.raises(LoomNotFoundError) as ei:
                await transport.request("GET", "/devices/UNKNOWN")
        assert ei.value.status == 404
        assert ei.value.problem is not None
        await transport.close()

    async def test_401_raises_auth_error(self, transport: HttpTransport) -> None:
        with aioresponses() as mock:
            mock.get("http://loom.test:8080/api/v1/info", payload=_INFO_RESPONSE)
            mock.get(
                "http://loom.test:8080/api/v1/devices",
                status=401,
                payload={
                    "type": "https://openccu-loom.dev/errors/unauthorized",
                    "title": "Missing or invalid token",
                    "status": 401,
                },
            )
            await transport.connect()
            with pytest.raises(LoomAuthError):
                await transport.request("GET", "/devices")
        await transport.close()


class TestRetry:
    async def test_get_retries_on_upstream_unavailable_then_succeeds(
        self, transport: HttpTransport
    ) -> None:
        with aioresponses() as mock:
            mock.get("http://loom.test:8080/api/v1/info", payload=_INFO_RESPONSE)
            # First call: upstream unavailable. Second: succeeds.
            mock.get(
                "http://loom.test:8080/api/v1/devices",
                status=502,
                payload={
                    "type": "https://openccu-loom.dev/errors/upstream_unavailable",
                    "title": "CCU unreachable",
                    "status": 502,
                },
            )
            mock.get(
                "http://loom.test:8080/api/v1/devices",
                payload={"items": [], "page": 1, "per_page": 50, "total": 0},
            )
            await transport.connect()
            result = await transport.request("GET", "/devices")
        assert result["total"] == 0
        await transport.close()

    async def test_get_gives_up_after_backoff_exhausted(self, transport: HttpTransport) -> None:
        with aioresponses() as mock:
            mock.get("http://loom.test:8080/api/v1/info", payload=_INFO_RESPONSE)
            for _ in range(5):
                mock.get(
                    "http://loom.test:8080/api/v1/devices",
                    status=502,
                    payload={
                        "type": "https://openccu-loom.dev/errors/upstream_unavailable",
                        "title": "CCU unreachable",
                        "status": 502,
                    },
                )
            await transport.connect()
            with pytest.raises(LoomUpstreamUnavailableError):
                await transport.request("GET", "/devices")
        await transport.close()

    async def test_post_does_not_retry_by_default(self, transport: HttpTransport) -> None:
        with aioresponses() as mock:
            mock.get("http://loom.test:8080/api/v1/info", payload=_INFO_RESPONSE)
            mock.post(
                "http://loom.test:8080/api/v1/programs/p1/execute",
                status=502,
                payload={
                    "type": "https://openccu-loom.dev/errors/upstream_unavailable",
                    "title": "CCU unreachable",
                    "status": 502,
                },
            )
            await transport.connect()
            with pytest.raises(LoomUpstreamUnavailableError):
                await transport.request("POST", "/programs/p1/execute")
        await transport.close()


class TestLifecycle:
    async def test_request_without_connect_raises(self, transport: HttpTransport) -> None:
        with pytest.raises(LoomTransportError, match="not connected"):
            await transport.request("GET", "/devices")

    async def test_context_manager_closes_session(self, config: LoomConfig) -> None:
        with aioresponses() as mock:
            mock.get("http://loom.test:8080/api/v1/info", payload=_INFO_RESPONSE)
            async with HttpTransport(config, backoff_sequence=()) as t:
                assert t.info is not None
        # After __aexit__, info should be cleared.
        assert t.info is None
