# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
aiohomematic MQTT ``state_path`` synthesis + reverse parsing.

The daemon addresses data points by ``(address, channel, parameter)`` and
has no notion of aiohomematic's MQTT ``state_path`` strings. The HA MQTT
bridge (``homematicip_local/mqtt.py``) builds one topic per ``state_path``
and looks data points back up by it, so the compat layer synthesises the
*exact* aiohomematic format and parses it back.

Formats (see aiohomematic ``model/support.py`` / ``const.py``):

* generic DP : ``device/status/{ADDRESS.upper()}/{CHANNEL}/{PARAMETER.upper()}``
* sysvar     : ``sysvar/status/{NAME}``
"""

from __future__ import annotations

_DEVICE_STATE_PATH_ROOT = "device/status"
_SYSVAR_STATE_PATH_ROOT = "sysvar/status"


def device_state_path(*, address: str, channel: int, parameter: str) -> str:
    """Synthesise the MQTT state path of a generic data point."""
    return f"{_DEVICE_STATE_PATH_ROOT}/{address.upper()}/{channel}/{parameter.upper()}"


def sysvar_state_path(*, name: str) -> str:
    """Synthesise the MQTT state path of a sysvar."""
    return f"{_SYSVAR_STATE_PATH_ROOT}/{name}"


def parse_device_state_path(state_path: str) -> tuple[str, int, str] | None:
    """Parse a generic-DP state path into ``(address, channel, parameter)``."""
    parts = state_path.split("/")
    if len(parts) != 5 or parts[0] != "device" or parts[1] != "status":
        return None
    address, channel, parameter = parts[2], parts[3], parts[4]
    if not channel.isdigit():
        return None
    return address, int(channel), parameter


def parse_sysvar_state_path(state_path: str) -> str | None:
    """Parse a sysvar state path into its name."""
    prefix = f"{_SYSVAR_STATE_PATH_ROOT}/"
    if not state_path.startswith(prefix):
        return None
    return state_path[len(prefix) :]
