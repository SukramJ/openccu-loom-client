# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Guard the dependency pins that are maintained in two files at once.

`openccu-loom-types` is pinned exactly, in `pyproject.toml` (what an
installer reads) and in `requirements.txt` (what CI installs). Release
2026.8.29 exists because those two drifted: `requirements.txt` moved to
0.5.9 and `pyproject.toml` stayed on 0.5.8, so every test ran against the
right version and passed while a clean `pip install` of the published
package resolved the wrong one and failed on import.

That is the failure mode this file exists for — one nothing else can catch,
because the half that is wrong is the half the test suite never uses.
"""

from __future__ import annotations

from pathlib import Path
import re
import tomllib

_ROOT = Path(__file__).resolve().parents[2]
# Pins kept exactly in both files, by distribution name. Both couple this
# package to one build of something it does not control: the wire models are
# generated per daemon release, and the compat shim reaches into aiohomematic
# internals that only the drift-guard test validates — and it validates one
# version, not a range.
_EXACT_IN_BOTH = ("openccu-loom-types", "aiohomematic")


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


class TestExactPinsAgree:
    """The exactly-pinned dependencies must name the same version in both files."""

    def test_openccu_loom_types_pin_matches(self) -> None:
        pyproject = _pyproject_requirements()
        requirements = _requirements_txt()
        for dist in _EXACT_IN_BOTH:
            assert dist in pyproject, f"{dist} vanished from pyproject dependencies"
            assert dist in requirements, f"{dist} vanished from requirements.txt"
            assert pyproject[dist] == requirements[dist], (
                f"{dist} pin drifted: pyproject.toml says {pyproject[dist]!r}, "
                f"requirements.txt says {requirements[dist]!r}. The installer reads "
                f"pyproject; CI reads requirements — so a mismatch ships broken and "
                f"tests green."
            )

    def test_types_pin_is_exact(self) -> None:
        """
        A range would let an installer resolve a types version this build never saw.

        The wire models are generated per daemon build, so anything other than
        `==` reintroduces exactly the drift the schema-digest handshake reports.
        """
        assert _pyproject_requirements()["openccu-loom-types"].startswith("openccu-loom-types==")

    def test_installed_types_version_matches_the_pin(self) -> None:
        """The environment running these tests must be the one the pins describe."""
        from openccu_loom_types import VERSION

        pinned = _pyproject_requirements()["openccu-loom-types"].split("==", 1)[1].strip()
        assert pinned == VERSION, (
            f"installed openccu-loom-types is {VERSION}, pins say {pinned} — "
            f"a green run here would say nothing about the published package"
        )


class TestAiohomematicIsPinnedNotRanged:
    """The shim couples to internals the drift guard validates one version of."""

    def test_pin_is_exact(self) -> None:
        """
        A range would declare versions compatible that nothing has checked.

        It was a calendar-versioned cap (`<2026.9`) before, which was worse than
        no bound at all: it read as though 2026.9.0 were a breaking change when
        all it means is that a month turned. CalVer carries no compatibility
        semantics, so any boundary drawn in it is arbitrary — and one that looks
        principled is the kind a reader trusts.
        """
        spec = _pyproject_requirements()["aiohomematic"]
        assert spec.startswith("aiohomematic=="), f"aiohomematic is not pinned exactly: {spec!r}"
