# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Shared pytest fixtures."""

from __future__ import annotations

import pytest

from openccu_loom_client import BearerAuth, LoomConfig


@pytest.fixture
def config() -> LoomConfig:
    """A LoomConfig pointing at localhost so URL-building tests stay
    cheap. Tests that need an actual transport use this together with
    aioresponses or a real aiohttp TestServer."""
    return LoomConfig(
        host="loom.test",
        port=8080,
        tls=False,
        auth=BearerAuth(token="testtoken1234", label="test"),
        request_timeout_seconds=1.0,
    )
