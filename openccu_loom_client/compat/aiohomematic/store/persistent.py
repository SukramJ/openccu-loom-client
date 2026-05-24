# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Stub for the legacy aiohomematic persistent-store cleanup hook."""

from __future__ import annotations

import logging
from pathlib import Path

_LOGGER = logging.getLogger(__name__)


def cleanup_files(*, storage_directory: str | Path) -> None:
    """No-op — daemon owns its own SQLite cache (greenfield policy).

    Original behaviour deleted stale per-central JSON dumps under
    the aiohomematic storage directory. The openccu-loom daemon
    holds equivalent state itself, so the HA-side cleanup is no
    longer the client's job. We keep the function so the cutover
    doesn't break ``cleanup_files`` import sites; remove these
    call sites in a follow-up.
    """
    _LOGGER.debug(
        "aiohomematic.store.persistent.cleanup_files is a no-op under "
        "openccu-loom-client (greenfield policy — daemon owns its store)"
    )
