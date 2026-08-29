# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Pin ``LoomCentralAdapter``'s public surface here, where it is produced.

`homematicip_local` imports the live class and checks that every central
member its own code uses exists on it. That is the right check and it stays —
a snapshot could disagree with the class, and the live import cannot. What it
cannot do is tell anyone *here* that a member went away: it fails in the other
repository, on the pull request that bumps the pin, days after the removal
landed, and its message asks the reader to go change a repository they are not
in.

So this snapshot exists for the opposite direction. It says nothing about
which members the consumer needs — it says which ones this package published
last time, and it fails in this package's own gate the moment one disappears.
The two together give both halves: what the consumer needs, checked there
against the real class; what this package promised, checked here against its
own history.

Resolution mirrors the consumer's `_member_exists`: attributes on the class
plus `self.x = …` assignments in `__init__`, because a coordinator handed out
as an instance attribute is as much of a published member as a property.

Refresh deliberately, in the same commit as the change:

    pytest tests/compat/test_adapter_surface_snapshot.py --update-adapter-surface

Reviewing that diff is the point. A member appearing is routine; a member
disappearing is a decision about somebody else's integration.
"""

from __future__ import annotations

import ast
import json
import pathlib

import pytest

from openccu_loom_client.compat.aiohomematic.central.adapter import LoomCentralAdapter

_ADAPTER_SOURCE = (
    pathlib.Path(__file__).resolve().parents[2]
    / "openccu_loom_client"
    / "compat"
    / "aiohomematic"
    / "central"
    / "adapter.py"
)
_SNAPSHOT = pathlib.Path(__file__).resolve().parent / "adapter_surface_snapshot.json"


def _init_assignments() -> set[str]:
    """Return the names assigned to ``self`` in ``LoomCentralAdapter.__init__``."""
    tree = ast.parse(_ADAPTER_SOURCE.read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "LoomCentralAdapter")
    init = next(
        node
        for node in cls.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == "__init__"
    )
    names: set[str] = set()
    for node in ast.walk(init):
        if isinstance(node, ast.Assign):
            targets: list[ast.expr] = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self":
                names.add(target.attr)
    return names


def _public_surface() -> list[str]:
    """Return every public member the adapter publishes, class-level or per instance."""
    members = {name for name in dir(LoomCentralAdapter) if not name.startswith("_")}
    members |= {name for name in _init_assignments() if not name.startswith("_")}
    return sorted(members)


def test_adapter_surface_matches_the_snapshot(request: pytest.FixtureRequest) -> None:
    """Fail when a published adapter member appears or disappears without the snapshot moving."""
    current = _public_surface()
    assert current, "resolved no adapter members — the AST walk stopped matching"

    if request.config.getoption("--update-adapter-surface"):
        _SNAPSHOT.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
        pytest.skip(f"rewrote {_SNAPSHOT.name} with {len(current)} members")

    recorded = json.loads(_SNAPSHOT.read_text(encoding="utf-8"))
    removed = sorted(set(recorded) - set(current))
    added = sorted(set(current) - set(recorded))
    assert not removed, (
        f"LoomCentralAdapter no longer publishes: {removed}. `homematicip_local` resolves this class "
        "live, so this breaks there rather than here — decide it deliberately and refresh the snapshot "
        "in the same commit."
    )
    assert not added, (
        f"LoomCentralAdapter publishes new members: {added}. Refresh the snapshot in the same commit "
        "(pytest --update-adapter-surface) so the addition is reviewed rather than assumed."
    )
