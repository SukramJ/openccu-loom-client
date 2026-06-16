# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Drive the ``godevccu-e2e`` helper binary for device-backed e2e tests.

The helper (``cmd/godevccu-e2e`` in the daemon repo) boots a CCU
simulator seeded with a fixed device set, prints its resolved ports as
one JSON line on stdout, and exposes an HTTP control API. This wrapper
launches it, reads the ports, and offers :meth:`set_value` /
:meth:`fire_event` so a test can stimulate CCU-side events that the
daemon then broadcasts over WebSocket.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
import json
from pathlib import Path
import signal
import subprocess
import tempfile
import time
from typing import IO, Any
import urllib.request

_PORTS_TIMEOUT_S = 20.0
_TERM_GRACE_S = 5.0


@dataclass(slots=True)
class GodevccuPorts:
    """Listener ports reported by the helper on startup."""

    xml_rpc_port: int
    json_rpc_port: int
    control_port: int


class GodevccuDriver:
    """A running ``godevccu-e2e`` subprocess plus its control client."""

    def __init__(self, proc: subprocess.Popen[bytes], ports: GodevccuPorts, log: IO[str]) -> None:
        """Wrap an already-started helper process and its resolved ports."""
        self._proc = proc
        self._log = log
        self.ports = ports

    @property
    def control_base(self) -> str:
        """Base URL of the helper's HTTP control API."""
        return f"http://127.0.0.1:{self.ports.control_port}"

    def set_value(self, *, address: str, value_key: str, value: Any) -> None:
        """Set a datapoint value on the simulator (drives ``value_changed``)."""
        self._post("/set_value", {"address": address, "value_key": value_key, "value": value})

    def fire_event(self, *, interface_id: str, address: str, value_key: str, value: Any) -> None:
        """Fire a CCU-side event (drives e.g. ``device.trigger`` keypresses)."""
        self._post(
            "/fire_event",
            {
                "interface_id": interface_id,
                "address": address,
                "value_key": value_key,
                "value": value,
            },
        )

    def _post(self, path: str, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        req = urllib.request.Request(  # noqa: S310
            f"{self.control_base}{path}",
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5.0) as resp:  # noqa: S310
            if resp.status >= 300:
                raise RuntimeError(f"control {path} -> {resp.status}")

    def stop(self) -> None:
        """Stop the helper: SIGTERM, then SIGKILL after a grace period."""
        try:
            if self._proc.poll() is None:
                self._proc.send_signal(signal.SIGTERM)
                try:
                    self._proc.wait(timeout=_TERM_GRACE_S)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        self._proc.wait(timeout=_TERM_GRACE_S)
        finally:
            self._log.close()


def _parse_ports(line: str) -> GodevccuPorts | None:
    # The simulator also emits slog JSON to the same stream; the ports
    # line is the only object carrying "control_port", so match on that.
    line = line.strip()
    if not line.startswith("{") or "control_port" not in line:
        return None
    raw = json.loads(line)
    return GodevccuPorts(
        xml_rpc_port=raw["xml_rpc_port"],
        json_rpc_port=raw["json_rpc_port"],
        control_port=raw["control_port"],
    )


def start_godevccu(*, binary: Path, timeout: float = _PORTS_TIMEOUT_S) -> GodevccuDriver:
    """
    Launch the helper and block until it reports its ports JSON line.

    The helper's stdout+stderr are redirected to a temp file rather than
    a pipe: the simulator is chatty, and an unread OS pipe would fill its
    buffer and freeze the simulator mid-request. We poll the file for the
    ports line. Raises :class:`RuntimeError` if the helper exits before
    reporting, or :class:`TimeoutError` if it never does within ``timeout``.
    """
    log = tempfile.NamedTemporaryFile(prefix="godevccu-e2e-", suffix=".log", mode="w+", delete=False)
    proc = subprocess.Popen(  # noqa: S603
        [str(binary)],
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    log_path = Path(log.name)
    deadline = time.monotonic() + timeout
    seen = 0
    while time.monotonic() < deadline:
        text = log_path.read_text()
        for line in text[seen:].splitlines():
            ports = _parse_ports(line)
            if ports is not None:
                return GodevccuDriver(proc, ports, log)
        seen = len(text)
        if proc.poll() is not None:  # exited before reporting ports
            proc.wait()
            log.close()
            raise RuntimeError(f"godevccu-e2e exited before reporting ports:\n{text}")
        time.sleep(0.1)
    proc.kill()
    log.close()
    raise TimeoutError(f"godevccu-e2e did not report ports within {timeout}s")
