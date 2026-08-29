# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Guard the dependency pins that are maintained in two files at once.

`aiohomematic` is declared as a floor in `pyproject.toml` (what an installer
reads) and pinned exactly in `requirements.txt` (what CI installs). Those two
roles must not be swapped: the application consuming this library depends on
aiohomematic directly and pins it, so a `==` here would make the pair
unsatisfiable together the moment the versions differed.

The file used to guard a second distribution the same way. `openccu-loom-types`
was pinned exactly in both files, and release 2026.8.29 exists because they
drifted — requirements.txt moved to 0.5.9, pyproject.toml stayed on 0.5.8, so
every test ran against the right version and passed while a clean
`pip install` resolved the wrong one and failed on import. That whole class of
failure is gone rather than guarded: the wire bindings ship inside this
distribution now (`openccu_loom_client/wire/`), so there is no second version
number for the two files to disagree about.
"""

from __future__ import annotations

from pathlib import Path
import re
import tomllib

_ROOT = Path(__file__).resolve().parents[2]


def _pyproject_requirements() -> dict[str, str]:
    """Map distribution name → full requirement string from pyproject dependencies."""
    data = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for raw in data["project"]["dependencies"]:
        name = re.split(r"[<>=!~\[ ]", raw, maxsplit=1)[0].strip()
        out[name] = raw.strip()
    return out


def _requirements_txt() -> dict[str, str]:
    """Map distribution name → full requirement string from requirements.txt."""
    out: dict[str, str] = {}
    for line in (_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name = re.split(r"[<>=!~\[ ]", line, maxsplit=1)[0].strip()
        out[name] = line
    return out


class TestAiohomematicIsFloored:
    """A library must not pin what its own consumer pins."""

    def test_declared_as_a_floor_not_a_pin(self) -> None:
        """
        `homematicip_local` depends on aiohomematic directly and pins it exactly.

        An `==` here would be unsatisfiable together with that pin the moment
        the two named different versions — this library would block the only
        application that installs it. The coupling is guarded by the drift test
        against the CI pin instead, which is where a new series is validated.
        """
        spec = _pyproject_requirements()["aiohomematic"]
        assert "==" not in spec, f"aiohomematic must not be pinned exactly here: {spec!r}"
        assert ">=" in spec, f"aiohomematic needs a floor naming what it requires: {spec!r}"

    def test_no_calendar_version_cap(self) -> None:
        """
        There is no honest upper bound to write.

        `<2026.9` claimed 2026.9.0 breaks something merely because a month
        turned. CalVer carries no compatibility semantics, so a boundary drawn
        in it is arbitrary — and one that looks principled is the kind a reader
        trusts.
        """
        spec = _pyproject_requirements()["aiohomematic"]
        assert "<" not in spec, f"aiohomematic carries a calendar-version cap: {spec!r}"

    def test_ci_pin_satisfies_the_floor(self) -> None:
        """The version CI validates has to be one the published metadata allows."""
        from packaging.requirements import Requirement
        from packaging.version import Version

        declared = Requirement(_pyproject_requirements()["aiohomematic"])
        ci_version = _requirements_txt()["aiohomematic"].split("==", 1)[1].strip()
        assert declared.specifier.contains(Version(ci_version)), (
            f"requirements.txt pins aiohomematic {ci_version}, which the declared "
            f"specifier {declared.specifier} does not allow"
        )
