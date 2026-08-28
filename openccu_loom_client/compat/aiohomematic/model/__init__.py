# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
``aiohomematic.model``-compatible model surface.

Contains the modules ``homematicip_local`` imports by their explicit path:
``alarm_panel``, ``custom``, ``generic`` and ``hub``, plus ``update``,
``calculated``, ``combined``, ``event_group``, ``naming`` and
``week_profile`` for this package's own use. Each ships type markers and
minimal behavioural shims so HA-side ``isinstance`` checks and property
reads keep resolving against the openccu-loom-client model classes.
"""
