#!/usr/bin/env python3
"""
Generate openccu_loom_client/wire/enums.py from assets/schemas/enums.json.

Stdlib-only so the generation step works in any Python 3.11+ env
without pulling in datamodel-code-generator just for the enum
catalogue.
"""

from __future__ import annotations

import argparse
import json
import keyword
from pathlib import Path
import sys

HEADER = '''# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Generated str-enums mirroring pkg/hmenum from the openccu-loom daemon.

DO NOT EDIT BY HAND — run `make generate-enums` after the daemon repo's
`make export-schemas` has refreshed assets/schemas/enums.json.

Each enum value matches the exact wire string the CCU emits. Compare
with `==` against the wire token; the enum members carry the same
Pythonic CamelCase names as the Go constants for cross-language
grep-ability.
"""

from __future__ import annotations

from enum import Enum

'''


def to_class(name: str, values: list[dict]) -> str:
    """
    Render one enum class.

    Strips the leading type-name prefix from each go_name member to
    produce idiomatic Python (e.g. `BackendCCU` → `CCU` on the
    `Backend` enum).
    """
    lines = [f"class {name}(str, Enum):"]
    if not values:
        lines.append("    pass")
        return "\n".join(lines) + "\n"

    seen: dict[str, str] = {}
    for entry in values:
        go_name = entry["go_name"]
        wire = entry["wire_value"]
        member = go_name
        if go_name.startswith(name) and len(go_name) > len(name):
            member = go_name[len(name) :]
        # Members starting with a digit get an underscore prefix.
        if member and member[0].isdigit():
            member = "_" + member
        # Reserved words (None, True, False, class, def, …) get a
        # trailing underscore — PEP 8 convention for unavoidable
        # collisions with Python's grammar.
        if keyword.iskeyword(member):
            member = member + "_"
        if member in seen:
            # Pick a deterministic alternative — append the wire
            # value as a disambiguator. This is rare (Go side
            # already enforces uniqueness within a type).
            member = f"{member}_{wire}"
        seen[member] = wire
        lines.append(f"    {member} = {json.dumps(wire)}")
    return "\n".join(lines) + "\n"


def main() -> int:
    """Generate the enum module and report the class count."""
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--enums-json", required=True, type=Path)
    p.add_argument("--out-py", required=True, type=Path)
    args = p.parse_args()

    if not args.enums_json.is_file():
        print(f"enums.json not found: {args.enums_json}", file=sys.stderr)
        return 1

    doc = json.loads(args.enums_json.read_text())
    enums = doc.get("enums", [])

    parts = [HEADER]
    for entry in enums:
        parts.append(to_class(entry["name"], entry["values"]))
        parts.append("\n")

    args.out_py.parent.mkdir(parents=True, exist_ok=True)
    args.out_py.write_text("".join(parts))
    print(f"wrote {len(enums)} enums to {args.out_py}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
