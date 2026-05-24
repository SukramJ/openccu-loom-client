# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""``aiohomematic.model``-compatible model surface.

Contains alias modules for ``data_point``, ``event``, ``custom``,
``generic``, ``hub``, ``schedule_models``, ``update`` and
``week_profile_data_point``. Each ships type markers and minimal
behavioural shims so HA-side ``isinstance`` checks and property
reads keep resolving against the new openccu-loom-client model
classes.
"""
