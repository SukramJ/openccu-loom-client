# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Tier A e2e — authentication against the real daemon."""

from __future__ import annotations

import pytest

from openccu_loom_client import BasicAuth, LoomAuthError, LoomClient, LoomConfig
from tests.e2e.conftest import ADMIN_PW, ADMIN_USER
from tests.helpers.daemon_process import DaemonHandle

pytestmark = pytest.mark.e2e


def _client(handle: DaemonHandle, *, username: str, password: str) -> LoomClient:
    return LoomClient(
        config=LoomConfig(
            host="127.0.0.1",
            port=handle.rest_port,
            tls=False,
            auth=BasicAuth(username=username, password=password),
            request_timeout_seconds=10.0,
        )
    )


# connect() only runs the public GET /info handshake, so auth is proven
# against a protected endpoint (GET /snapshot — 401 without valid creds).


async def test_valid_credentials_accepted(daemon_no_ccu: DaemonHandle) -> None:
    client = _client(daemon_no_ccu, username=ADMIN_USER, password=ADMIN_PW)
    await client.connect()
    try:
        await client.system.get_snapshot()  # protected; must not raise
    finally:
        await client.close()


async def test_bad_credentials_rejected(daemon_no_ccu: DaemonHandle) -> None:
    client = _client(daemon_no_ccu, username=ADMIN_USER, password="wrong-password")
    await client.connect()  # handshake hits public /info, so this succeeds
    try:
        with pytest.raises(LoomAuthError):
            await client.system.get_snapshot()
    finally:
        await client.close()
