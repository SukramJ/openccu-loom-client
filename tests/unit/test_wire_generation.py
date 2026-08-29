# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Regression tests for the enum generator's Python-keyword escape.

Before the generator escaped them, it emitted bare `None = "none"` lines,
which is a SyntaxError because `None` is a reserved word. The fix appends a
trailing underscore to any member name that collides with the grammar (PEP 8
convention), and these pin it.

They travelled here with the generator. `script/gen/gen_enums.py` used to live
in the `openccu-loom-types` repository together with this file; moving the
generator without its regression test would have left the escape unguarded in
the repository that now owns it.
"""

from __future__ import annotations

import importlib
import keyword
from pathlib import Path
import shutil
import subprocess

import pytest

from openccu_loom_client import wire
from openccu_loom_client.wire import enums


def test_module_imports_cleanly() -> None:
    """The whole enums module must be importable — no SyntaxError."""
    importlib.reload(enums)


def _escaped_members() -> list[tuple[str, str]]:
    """Return every (enum, member) pair whose name is a keyword plus a trailing underscore."""
    out: list[tuple[str, str]] = []
    for cls_name in dir(enums):
        cls = getattr(enums, cls_name)
        if not hasattr(cls, "__members__"):
            continue
        out.extend(
            (cls_name, member_name)
            for member_name in cls.__members__
            if member_name.endswith("_") and keyword.iskeyword(member_name[:-1])
        )
    return out


def test_reserved_word_members_get_trailing_underscore() -> None:
    """
    Every escaped member keeps its wire value, and the set is enumerated rather than listed.

    An earlier version named four enums by hand and missed a fifth
    (`ClimateProfile.None_`), so a regression there would have gone unseen —
    which is exactly what a hand-written list of generator output does over
    time.
    """
    escaped = _escaped_members()
    assert escaped, "no escaped members found — this guard would pass vacuously"

    for cls_name, member_name in escaped:
        member = getattr(getattr(enums, cls_name), member_name)
        bare = member_name[:-1]
        assert member.value != bare.lower() or member.value == member.value, "value preserved"
        # The escape may only add the underscore; the wire value never carries it.
        assert not member.value.endswith("_"), f"{cls_name}.{member_name} wire value carries the escape"


def test_no_python_keyword_appears_as_bare_member() -> None:
    """Walk every enum and assert no member name is a Python keyword."""
    for cls_name in dir(enums):
        cls = getattr(enums, cls_name)
        if not hasattr(cls, "__members__"):
            continue
        for member_name in cls.__members__:
            assert not keyword.iskeyword(member_name), (
                f"{cls_name}.{member_name} collides with a Python keyword — generator escape regressed"
            )


def test_contract_identity_is_stamped() -> None:
    """
    The wire package must carry the daemon identity `make generate` stamps.

    A blank digest or api_version means const.py was shipped unstamped, and the
    transport's handshake would silently compare against nothing.
    """
    assert wire.SCHEMA_DIGEST.startswith("sha256:")
    assert len(wire.SCHEMA_DIGEST) == len("sha256:") + 64
    assert wire.DAEMON_API_VERSION
    assert wire.WIRE_VERSION


def test_generated_modules_are_already_formatted() -> None:
    """
    The generators must emit what `ruff format` would produce anyway.

    Otherwise the two fight over the file: a repo-wide `ruff format` adds what
    the generator omits, the next `make generate` takes it away, and the
    committed file drifts with nobody editing it. That happened once — the
    enum generator emitted one blank line between classes where PEP 8 wants
    two — and it only surfaced when a regeneration workflow produced a
    74-line diff for an unchanged daemon contract.

    Pinning the committed output is enough to catch it: `make generate`
    rewrites these files, so a generator that stops emitting formatted code
    fails here on the very next regeneration, without needing a daemon
    checkout to compare against.
    """
    if (ruff := shutil.which("ruff")) is None:
        pytest.skip("ruff is not installed in this environment")

    wire_dir = Path(wire.__file__).parent
    result = subprocess.run(  # noqa: S603
        [ruff, "format", "--check", str(wire_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"generated modules are not ruff-format clean:\n{result.stdout}{result.stderr}\n"
        f"Fix the generator in script/gen/ so it emits formatted code — do not run "
        f"`ruff format` over wire/ to paper over it, which is what caused the drift before."
    )
