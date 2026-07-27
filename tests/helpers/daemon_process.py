# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Spawn the real ``openccu-loom`` daemon as a subprocess for e2e tests.

Unlike the in-process :class:`tests.helpers.mock_daemon.MockDaemon`
(which stubs the HTTP surface), this drives the genuine Go binary so
e2e tests exercise the real REST + WebSocket contract end to end.

The daemon prints no "listening" line, so readiness is detected by
polling the unauthenticated ``GET /api/v1/health`` probe until it
returns 200 (mirroring the daemon's own Go harness). On boot failure
the captured log is included in the raised error.
"""

from __future__ import annotations

import base64
import contextlib
from dataclasses import dataclass
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request

_HEALTH_TICK_S = 0.05
_BOOT_TIMEOUT_S = 30.0
_DEVICES_TIMEOUT_S = 30.0
_DEVICES_TICK_S = 0.25
_TERM_GRACE_S = 5.0


def free_port() -> int:
    """Return an OS-assigned free TCP port on localhost."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass(slots=True)
class DaemonHandle:
    """A running daemon subprocess plus the ports it was bound to."""

    proc: subprocess.Popen[bytes]
    rest_port: int
    ui_port: int
    log_path: Path

    @property
    def rest_base(self) -> str:
        """REST base URL including the ``/api/v1`` mount point."""
        return f"http://127.0.0.1:{self.rest_port}/api/v1"


def wait_for_health(*, port: int, timeout: float = _BOOT_TIMEOUT_S) -> None:
    """
    Poll ``GET /api/v1/health`` until it returns 200 or ``timeout`` elapses.

    The probe needs no auth. Raises :class:`TimeoutError` if the daemon
    never becomes healthy in time.
    """
    url = f"http://127.0.0.1:{port}/api/v1/health"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with (
            contextlib.suppress(urllib.error.URLError, ConnectionError, OSError),
            urllib.request.urlopen(url, timeout=1.0) as resp,
        ):
            if resp.status == 200:
                return
        time.sleep(_HEALTH_TICK_S)
    raise TimeoutError(f"daemon not healthy on :{port} within {timeout}s")


def wait_for_devices(
    *,
    port: int,
    username: str,
    password: str,
    timeout: float = _DEVICES_TIMEOUT_S,
) -> None:
    """
    Poll ``GET /api/v1/devices`` until the daemon has ingested ≥1 device.

    A daemon backed by a CCU reports ``/health`` 200 *before* the
    interface finishes its initial device ingest, so device-driven tests
    must wait for the store to fill. Raises :class:`TimeoutError` (with
    the last seen state) if no device appears within ``timeout``.
    """
    url = f"http://127.0.0.1:{port}/api/v1/devices"
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    headers = {"Authorization": f"Basic {token}"}
    deadline = time.monotonic() + timeout
    last = "<no response>"
    while time.monotonic() < deadline:
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=2.0) as resp:  # noqa: S310
                if resp.status == 200:
                    body = json.load(resp)
                    items = body.get("items", []) if isinstance(body, dict) else body
                    if items:
                        return
                    last = "0 devices"
                else:
                    last = f"HTTP {resp.status}"
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            last = repr(exc)
        time.sleep(_DEVICES_TICK_S)
    raise TimeoutError(f"daemon ingested no devices on :{port} within {timeout}s (last: {last})")


def start_daemon(
    *,
    binary: Path,
    config_path: Path,
    log_path: Path,
    rest_port: int,
    ui_port: int,
    boot_timeout: float = _BOOT_TIMEOUT_S,
) -> DaemonHandle:
    """
    Launch ``<binary> run --config <config_path>`` and wait until healthy.

    stdout+stderr are redirected to ``log_path``. On boot failure the
    process is stopped and the captured log is surfaced in the error.
    """
    log_file = log_path.open("wb")
    proc = subprocess.Popen(  # noqa: S603
        [str(binary), "run", "--config", str(config_path)],
        stdout=log_file,
        stderr=subprocess.STDOUT,
        env={**os.environ, "OPENCCU_LOOM_CONFIG": str(config_path)},
    )
    try:
        wait_for_health(port=rest_port, timeout=boot_timeout)
    except Exception as exc:
        stop_daemon(proc)
        log = log_path.read_text(errors="replace") if log_path.exists() else "<no log>"
        raise RuntimeError(f"daemon failed to boot:\n{log}") from exc
    return DaemonHandle(proc=proc, rest_port=rest_port, ui_port=ui_port, log_path=log_path)


def stop_daemon(proc: subprocess.Popen[bytes]) -> None:
    """Stop the daemon: SIGTERM, then SIGKILL after a short grace period."""
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=_TERM_GRACE_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=_TERM_GRACE_S)
