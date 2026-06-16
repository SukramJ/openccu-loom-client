# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Fixtures for the e2e suite — the real daemon as a subprocess.

Two tiers, both opt-in and skipped unless the relevant binary is
pointed at via env var:

* **Tier A** (``daemon_no_ccu`` / ``client_no_ccu``) — daemon booted
  with ``centrals: []``. Needs only ``LOOM_DAEMON_BIN``. Covers
  connect/handshake, ``/info`` capabilities, ``/health``, auth, and the
  WebSocket upgrade.
* **Tier B** (``daemon_with_ccu`` / ``client_with_ccu`` / ``godevccu``)
  — additionally spins up the ``godevccu-e2e`` simulator via
  ``GODEVCCU_E2E_BIN`` and points the daemon at it, so device, value and
  trigger flows can be exercised against real simulated hardware.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
import os
from pathlib import Path

import pytest

from openccu_loom_client import BasicAuth, LoomClient, LoomConfig
from openccu_loom_client.model import DataPoint
from tests.helpers.daemon_process import DaemonHandle, free_port, start_daemon, stop_daemon, wait_for_devices
from tests.helpers.godevccu_driver import GodevccuDriver, start_godevccu

ADMIN_USER = "admin"
ADMIN_PW = "e2e-test-password"


def _bin_from_env(var: str) -> Path:
    raw = os.environ.get(var)
    if not raw:
        pytest.skip(f"set {var} to the built binary to run these e2e tests")
    path = Path(raw).expanduser().resolve()
    if not path.exists():
        pytest.skip(f"{var} points at a missing binary: {path}")
    return path


@pytest.fixture(scope="session")
def daemon_binary() -> Path:
    """Path to the built daemon binary (``LOOM_DAEMON_BIN``)."""
    return _bin_from_env("LOOM_DAEMON_BIN")


@pytest.fixture(scope="session")
def godevccu_binary() -> Path:
    """Path to the built godevccu-e2e helper (``GODEVCCU_E2E_BIN``)."""
    return _bin_from_env("GODEVCCU_E2E_BIN")


def _write_config(tmp: Path, *, rest_port: int, ui_port: int, centrals_block: str) -> Path:
    """
    Render a minimal daemon config YAML pointing at the given ports.

    All keys sit at column 0 and ``centrals_block`` is spliced in as a
    ready-to-use top-level block (``"centrals: []"`` or a full list), so
    no dedent/indent juggling can corrupt the YAML nesting.
    """
    cfg = (
        "locale: en\n"
        f"data_dir: {tmp / 'var'}\n"
        "logging:\n"
        "  level: debug\n"
        "  format: json\n"
        "north:\n"
        "  rest:\n"
        f'    listen: ":{rest_port}"\n'
        "    csrf_enabled: false\n"
        "    auth:\n"
        "      basic_enabled: true\n"
        "      users:\n"
        f'        {ADMIN_USER}: "{ADMIN_PW}"\n'
        "  ui:\n"
        f'    listen: ":{ui_port}"\n'
        # The CCU pushes value events to this callback; pin a reachable
        # host so the simulator's echo (and thus value_changed
        # broadcasts) reaches the daemon.
        "callback:\n"
        "  public_host: 127.0.0.1\n"
        f"{centrals_block}\n"
    )
    path = tmp / "config.yaml"
    path.write_text(cfg)
    return path


def find_writable_bool_dp(client: LoomClient) -> DataPoint:
    """
    Return a writable boolean ``STATE`` data point from the store.

    The seeded device set has a STATE on both the thermostat (read-only)
    and the switch (writable); requiring ``BOOL`` + ``is_writable`` picks
    the switch so write-back tests actually succeed. Skips if none exists.
    """
    for dp in client.store.data_points:
        if dp.parameter == "STATE" and dp.type == "BOOL" and dp.is_writable:
            return dp
    pytest.skip("no writable BOOL STATE data point in the simulated device set")


def device_address_by_model(client: LoomClient, model: str) -> str:
    """
    Return the address of the first device with the given model.

    godevccu assigns ``VCU…`` addresses derived from a fixed serial, but
    tests resolve by model (``HmIP-BSM`` …) rather than hard-coding the
    derived address. Skips if no such device was seeded.
    """
    for device in client.store.devices:
        if device.model == model:
            return device.address
    pytest.skip(f"no {model} device in the simulated device set")


def _client_for(handle: DaemonHandle) -> LoomClient:
    config = LoomConfig(
        host="127.0.0.1",
        port=handle.rest_port,
        tls=False,
        auth=BasicAuth(username=ADMIN_USER, password=ADMIN_PW),
        request_timeout_seconds=10.0,
    )
    return LoomClient(config=config)


# ---- Tier A: daemon without a CCU ----


@pytest.fixture
def daemon_no_ccu(daemon_binary: Path, tmp_path: Path) -> Iterator[DaemonHandle]:
    """Boot the daemon with ``centrals: []`` (no CCU required)."""
    rest, ui = free_port(), free_port()
    cfg = _write_config(tmp_path, rest_port=rest, ui_port=ui, centrals_block="centrals: []")
    handle = start_daemon(
        binary=daemon_binary,
        config_path=cfg,
        log_path=tmp_path / "daemon.log",
        rest_port=rest,
        ui_port=ui,
    )
    try:
        yield handle
    finally:
        stop_daemon(handle.proc)


@pytest.fixture
async def client_no_ccu(daemon_no_ccu: DaemonHandle) -> AsyncIterator[LoomClient]:
    """Yield a connected :class:`LoomClient` bound to the no-CCU daemon."""
    client = _client_for(daemon_no_ccu)
    await client.connect()
    try:
        yield client
    finally:
        await client.close()


# ---- Tier B: daemon backed by the godevccu simulator ----


@pytest.fixture
def godevccu(godevccu_binary: Path) -> Iterator[GodevccuDriver]:
    """Start the godevccu CCU simulator with the seeded default devices."""
    driver = start_godevccu(binary=godevccu_binary)
    try:
        yield driver
    finally:
        driver.stop()


@pytest.fixture
def daemon_with_ccu(daemon_binary: Path, godevccu: GodevccuDriver, tmp_path: Path) -> Iterator[DaemonHandle]:
    """Boot the daemon pointed at the running godevccu simulator."""
    rest, ui = free_port(), free_port()
    centrals = (
        "centrals:\n"
        "  - name: ccu-e2e\n"
        "    host: 127.0.0.1\n"
        f"    port: {godevccu.ports.xml_rpc_port}\n"
        f"    json_rpc_port: {godevccu.ports.json_rpc_port}\n"
        "    username: Admin\n"
        '    password: ""\n'
        "    interfaces:\n"
        "      - HmIP-RF"
    )
    cfg = _write_config(tmp_path, rest_port=rest, ui_port=ui, centrals_block=centrals)
    handle = start_daemon(
        binary=daemon_binary,
        config_path=cfg,
        log_path=tmp_path / "daemon.log",
        rest_port=rest,
        ui_port=ui,
    )
    try:
        # /health turns 200 before the CCU ingest finishes; block until
        # the simulated devices are actually in the daemon's store so a
        # single bootstrap() in the test sees them.
        wait_for_devices(port=rest, username=ADMIN_USER, password=ADMIN_PW)
        yield handle
    finally:
        stop_daemon(handle.proc)


@pytest.fixture
async def client_with_ccu(daemon_with_ccu: DaemonHandle) -> AsyncIterator[LoomClient]:
    """Yield a connected :class:`LoomClient` bound to the godevccu-backed daemon."""
    client = _client_for(daemon_with_ccu)
    await client.connect()
    try:
        yield client
    finally:
        await client.close()
