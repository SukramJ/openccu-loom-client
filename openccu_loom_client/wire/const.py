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
SCHEMA_DIGEST: Final = "sha256:1f2e0d7b544f4c0de575dd76ba4c4ddc1e21760b0381e2ba19eb3d55de52e732"
DAEMON_API_VERSION: Final = "18.0.0"
