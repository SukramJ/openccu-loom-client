# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from openccu_loom_client import BearerAuth, LoomConfig
from tests.helpers import MockDaemon


@pytest.fixture
def config() -> LoomConfig:
    """
    Return a LoomConfig pointing at localhost so URL-building tests stay cheap.

    Tests that need an actual transport use the ``mock_daemon`` fixture,
    whose ``config`` points the client at a live in-process server.
    """
    return LoomConfig(
        host="loom.test",
        port=8080,
        tls=False,
        auth=BearerAuth(token="testtoken1234", label="test"),
        request_timeout_seconds=1.0,
    )


@pytest.fixture
async def mock_daemon() -> AsyncIterator[MockDaemon]:
    """Start an in-process mock of the daemon's REST surface and tear it down."""
    daemon = MockDaemon()
    await daemon.start()
    try:
        yield daemon
    finally:
        await daemon.stop()
