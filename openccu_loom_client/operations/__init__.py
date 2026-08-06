# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
High-level REST operation modules — one per daemon resource group.

Each module is a thin pythonic facade over the corresponding section
of the daemon's OpenAPI surface. They share a small helper base class
that wires the transport reference; the actual methods stay close to
the wire so callers can reason about the round-trips that happen.
"""

from __future__ import annotations

from openccu_loom_client.operations.alarm import AlarmOperations
from openccu_loom_client.operations.auth import AuthOperations
from openccu_loom_client.operations.backup import BackupOperations
from openccu_loom_client.operations.centrals import CentralsOperations
from openccu_loom_client.operations.config import ConfigOperations
from openccu_loom_client.operations.custom_data_points import CustomDataPointsOperations
from openccu_loom_client.operations.datapoints import DataPointsOperations
from openccu_loom_client.operations.devices import DevicesOperations
from openccu_loom_client.operations.diagnostics import DiagnosticsOperations
from openccu_loom_client.operations.groups import GroupsOperations
from openccu_loom_client.operations.hub import HubOperations
from openccu_loom_client.operations.i18n import I18nOperations
from openccu_loom_client.operations.links import LinksOperations
from openccu_loom_client.operations.matter import MatterOperations
from openccu_loom_client.operations.schedules import SchedulesOperations
from openccu_loom_client.operations.security import SecurityOperations
from openccu_loom_client.operations.sessions import SessionsOperations
from openccu_loom_client.operations.system import SystemOperations
from openccu_loom_client.operations.users import UsersOperations
from openccu_loom_client.operations.visibility import VisibilityOperations

__all__ = [
    # General
    "AlarmOperations",
    "AuthOperations",
    "BackupOperations",
    "CentralsOperations",
    "ConfigOperations",
    "CustomDataPointsOperations",
    "DataPointsOperations",
    "DevicesOperations",
    "DiagnosticsOperations",
    "GroupsOperations",
    "HubOperations",
    "I18nOperations",
    "LinksOperations",
    "MatterOperations",
    "SchedulesOperations",
    "SecurityOperations",
    "SessionsOperations",
    "SystemOperations",
    "UsersOperations",
    "VisibilityOperations",
]
