# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Aiohomematic-compatible ``store`` sub-package.

The original library owned a SQLite-backed persistent store; the
openccu-loom daemon owns its own SQLite + filesystem state now (per
spec). This shim exists only because HA's startup path still calls
``cleanup_files`` to clean up *aiohomematic-era* leftover files
during the cutover.
"""
