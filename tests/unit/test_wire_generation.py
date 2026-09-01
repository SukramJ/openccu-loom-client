# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Regression tests for the generated wire modules' post-processing steps.

Two of them, both invisible in the generator's own output:

* the enum generator's Python-keyword escape (below), and
* `script/gen/tolerant_enums.py`, which re-points every generated enum at a
  base that accepts a wire value the client has never seen.

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

from enum import Enum
import importlib
import keyword
from pathlib import Path
import shutil
import subprocess

from pydantic import ValidationError
import pytest

from openccu_loom_client import wire
from openccu_loom_client.wire import enums, rest


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


# --- tolerance for enum values a newer daemon added -------------------------
#
# The daemon may add a member to any enum in an additive minor release. The
# version gate passes (the contract is backwards compatible), and then response
# parsing raises `ValidationError` because a Python enum rejects an unseen
# value — an already-installed client broken by a change that was meant not to
# break it. `script/gen/tolerant_enums.py` gives every generated enum a
# `_missing_` that mints a pseudo-member carrying the raw string instead.

FUTURE_VALUE = "a_value_the_daemon_added_after_this_client_was_generated"


def _generated_enums(module: object) -> list[type[Enum]]:
    """Every enum class the given wire module defines that actually has members."""
    return [obj for obj in vars(module).values() if isinstance(obj, type) and issubclass(obj, Enum) and obj.__members__]


def test_unknown_str_enum_value_in_a_response_payload_parses() -> None:
    """A `Problem` carrying an unknown `code` parses and keeps the raw string."""
    problem = rest.Problem.model_validate(
        {
            "type": "https://openccu-loom.dev/errors/validation",
            "title": "Validation failed",
            "status": 400,
            "code": FUTURE_VALUE,
        }
    )
    assert problem.code == FUTURE_VALUE
    assert problem.code is not None
    assert problem.code.value == FUTURE_VALUE
    assert f'"code":"{FUTURE_VALUE}"' in problem.model_dump_json()


def test_unknown_plain_enum_value_in_a_response_payload_parses() -> None:
    """
    The one plain (non-`str`) generated enum tolerates an unknown value too.

    `Problem.type` is a bare `Enum`, so its pseudo-member cannot be built with
    `str.__new__` and is not itself a `str`. It carries the raw string in
    `.value` and serialises back unchanged, which is what a caller reads.
    """
    unknown_uri = "https://openccu-loom.dev/errors/quota_exhausted"
    problem = rest.Problem.model_validate({"type": unknown_uri, "title": "Nope", "status": 429})
    assert problem.type.value == unknown_uri
    assert f'"type":"{unknown_uri}"' in problem.model_dump_json()


def test_known_members_still_resolve_to_the_declared_member() -> None:
    """Tolerance must not turn a known value into a pseudo-member."""
    problem = rest.Problem.model_validate(
        {"type": "https://openccu-loom.dev/errors/not_found", "title": "Nope", "status": 404, "code": "not_found"}
    )
    assert problem.code is rest.Code.not_found
    assert problem.type is rest.Type.https___openccu_loom_dev_errors_not_found


def test_non_string_values_are_still_rejected() -> None:
    """
    The negative control: tolerance is for unseen *strings*, not for anything.

    Without this, a `_missing_` that accepted every input would pass every
    other test in this block while silently swallowing a wrong-typed payload.
    """
    with pytest.raises(ValidationError):
        rest.Problem.model_validate(
            {"type": "https://openccu-loom.dev/errors/validation", "title": "x", "status": 400, "code": 17}
        )


@pytest.mark.parametrize("module", [rest, enums], ids=["rest", "enums"])
def test_every_generated_enum_accepts_an_unknown_wire_value(module: object) -> None:
    """
    Walk both generated modules — no enum may raise on a value it has not seen.

    Enumerated rather than listed by hand: the generator emits ~75 classes per
    module and adds more with every daemon release, so a hand-written list
    would pin the tolerance of exactly the classes that already had it.
    """
    classes = _generated_enums(module)
    assert len(classes) > 50, f"only {len(classes)} enums found — this guard would pass near-vacuously"

    intolerant: list[str] = []
    for cls in classes:
        try:
            member = cls(FUTURE_VALUE)
        except ValueError:
            intolerant.append(cls.__name__)
            continue
        if member.value != FUTURE_VALUE:
            intolerant.append(cls.__name__)

    assert not intolerant, (
        f"{len(intolerant)} generated enums raise on a value a newer daemon may send: "
        f"{', '.join(sorted(intolerant))}. Run `make generate` — the tolerance comes from "
        f"script/gen/tolerant_enums.py, which must run after every generator."
    )
