#!/usr/bin/env python3
r"""
Stamp const.py with the daemon contract identity.

Computes the canonical contract digest over the daemon repo's schema
assets and extracts the APIVersion constant, then rewrites the
SCHEMA_DIGEST / DAEMON_API_VERSION constants in
openccu_loom_client/wire/const.py. The digest definition mirrors the
daemon's script/generate_schema_digest.go (ADR 0028 there):

    combined = "<path>\\n<sha256-hex of file bytes>\\n" per asset,
               paths sorted lexicographically
    digest   = "sha256:" + sha256-hex(combined)

A client compares ``openccu_loom_client.wire.SCHEMA_DIGEST`` against the
``schema_digest`` field of ``GET /api/v1/info`` to verify its types
were generated from exactly the daemon build it talks to.

Stdlib-only, like gen_enums.py.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re
import sys

# Closed contract-asset list — keep in sync with the daemon's
# script/generate_schema_digest.go (paths relative to the daemon repo,
# sorted lexicographically).
CONTRACT_ASSETS = (
    "assets/openapi.yaml",
    "assets/schemas/enums.json",
    "assets/schemas/types.json",
    "assets/wsapi.json",
)

INFO_GO = "internal/north/rest/handlers/info.go"
API_VERSION_RE = re.compile(r'^const APIVersion = "([^"]+)"$', re.MULTILINE)


def compute_digest(repo: Path) -> str:
    """Return the canonical contract digest over the daemon's schema assets."""
    combined = hashlib.sha256()
    for rel in CONTRACT_ASSETS:
        path = repo / rel
        file_sum = hashlib.sha256(path.read_bytes()).hexdigest()
        combined.update(f"{rel}\n{file_sum}\n".encode())
    return f"sha256:{combined.hexdigest()}"


def extract_api_version(repo: Path) -> str:
    """Return the APIVersion constant declared by the daemon build."""
    source = (repo / INFO_GO).read_text(encoding="utf-8")
    match = API_VERSION_RE.search(source)
    if not match:
        sys.exit(f"stamp_const: no APIVersion constant found in {repo / INFO_GO}")
    return match.group(1)


def stamp(const_path: Path, digest: str, api_version: str) -> None:
    """Rewrite the two stamped constants in const.py in place."""
    text = const_path.read_text(encoding="utf-8")
    replacements = {
        "SCHEMA_DIGEST": digest,
        "DAEMON_API_VERSION": api_version,
    }
    for name, value in replacements.items():
        pattern = re.compile(rf'^{name}: Final = "[^"]*"$', re.MULTILINE)
        line = f'{name}: Final = "{value}"'
        if pattern.search(text):
            text = pattern.sub(line, text)
        else:
            sys.exit(f"stamp_const: {name} constant not found in {const_path} — restore the stamped block (see README)")
    const_path.write_text(text, encoding="utf-8")


def main() -> None:
    """Stamp const.py from the daemon repository given on the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--openccu-loom-repo",
        type=Path,
        default=Path("../openccu-loom"),
        help="path to the daemon repo checkout (default: ../openccu-loom)",
    )
    parser.add_argument(
        "--const-py",
        type=Path,
        default=Path("openccu_loom_client/wire/const.py"),
        help="path to const.py to stamp",
    )
    args = parser.parse_args()

    digest = compute_digest(args.openccu_loom_repo)
    api_version = extract_api_version(args.openccu_loom_repo)
    stamp(args.const_py, digest, api_version)
    print(f"stamped {args.const_py}: {digest} (api_version {api_version})")


if __name__ == "__main__":
    main()
