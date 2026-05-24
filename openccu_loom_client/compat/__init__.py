# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Compatibility shims for downstream consumers mid-migration.

Currently only ``aiohomematic`` (one sub-package) — it exposes the
same import paths the ``homematicip_local`` HA component uses today,
so the cutover can happen one file at a time instead of as one big
breaking change.
"""
