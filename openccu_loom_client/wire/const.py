# SPDX-License-Identifier: MIT
# Copyright (C) 2026 SukramJ.

"""Contract identity of the daemon build these bindings were generated from."""

from typing import Final

# The last version the separate `openccu-loom-types` distribution
# published before these modules were folded in. Kept so a consumer that
# recorded a generation lineage can still resolve it; the client's own
# version is openccu_loom_client.const.VERSION.
VERSION: Final = "0.5.10"

# Contract identity of the daemon build these types were generated
# from. Stamped by script/gen/stamp_const.py (run via `make generate`);
# do not edit by hand. A client compares SCHEMA_DIGEST against the
# `schema_digest` field of `GET /api/v1/info`: equality means the
# types match the daemon build exactly; inequality means they were
# generated from a different build — fall back to DAEMON_API_VERSION
# vs `api_version` for compatibility reasoning.
SCHEMA_DIGEST: Final = "sha256:904689fccfc2111f6979404165d065cdc7f8185047eb4a1edafe23c2e228f51d"
DAEMON_API_VERSION: Final = "13.4.0"
