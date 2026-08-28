#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Report package code that is merged but not tagged.

Two releases in a row went out with a variant of the same problem: a change
that was "done" everywhere except where users get it. 2026.8.28 shipped a
dependency pin that had only been updated in one of the two files it lives in;
2026.8.30's contents sat on `main` unreleased because the merge and the tag are
separate acts and only one of them is prompted by a pull request.

The pin half is covered by `tests/unit/test_dependency_pins.py`. This covers
the other half, and it has to run on a schedule rather than per PR: every PR
adds unreleased commits by definition, so a per-PR check would be red on merge
by design and read as noise within a week.

What counts is deliberately narrow — only what actually reaches a user through
the built distribution. Documentation, notes, tests and CI can sit on `main`
indefinitely without anyone being owed a release for them, and counting them
would produce exactly the false alarms that teach people to ignore a check.

Exit codes: 0 nothing owed, 1 a release is owed, 2 the check could not run.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import subprocess
import sys

# Paths whose content ends up in the built distribution, so a change to one of
# them is invisible to users until a release. `pyproject.toml` earns its place
# from experience rather than principle: the pin that broke 2026.8.28 lived
# there and nowhere else that ships.
_SHIPPING_PATHS = ("openccu_loom_client/", "pyproject.toml")

# How long merged package code may sit untagged before this reports it. A merge
# and its release rarely land in the same minute, and a check that fires in
# that gap would be a false alarm every single release.
_GRACE_HOURS_DEFAULT = 24


def _git(*args: str) -> str:
    """Run a git command and return its stdout, or exit 2 when git fails."""
    try:
        done = subprocess.run(
            ["git", *args],  # noqa: S607
            capture_output=True,
            check=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as err:
        print(f"::error::cannot run git {' '.join(args)}: {err}", file=sys.stderr)
        raise SystemExit(2) from err
    return done.stdout.strip()


def _latest_tag() -> str | None:
    """
    Return the most recent tag reachable from HEAD, or None when there is none.

    Reachability is the point, not recency: a tag cut on another branch is not
    a release of *this* history, and comparing against it would report the
    difference between two lines of development as unreleased work.
    """
    try:
        done = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0"],  # noqa: S607
            capture_output=True,
            check=True,
            text=True,
        )
    except OSError, subprocess.CalledProcessError:
        return None  # no reachable tag at all
    return done.stdout.strip() or None


def main() -> int:
    """Report whether shipping paths changed since the latest tag."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--grace-hours",
        type=float,
        default=_GRACE_HOURS_DEFAULT,
        help="how long merged package code may sit untagged before it is reported",
    )
    args = parser.parse_args()

    if (tag := _latest_tag()) is None:
        print("no tags yet — nothing to compare against")
        return 0

    changed = _git("log", f"{tag}..HEAD", "--format=%H", "--", *_SHIPPING_PATHS)
    if not changed:
        print(f"no shipping-path changes since {tag} — nothing owed")
        return 0

    commits = changed.splitlines()
    # Oldest of them decides: that is how long the earliest user-visible change
    # has been waiting, which is the number a reader wants.
    oldest = commits[-1]
    when = datetime.fromisoformat(_git("show", "-s", "--format=%cI", oldest))
    waiting_hours = (datetime.now(UTC) - when).total_seconds() / 3600

    subjects = _git("log", f"{tag}..HEAD", "--format=%h %s", "--", *_SHIPPING_PATHS)
    if waiting_hours < args.grace_hours:
        print(
            f"{len(commits)} shipping-path commit(s) since {tag}, oldest "
            f"{waiting_hours:.1f}h ago — inside the {args.grace_hours:.0f}h grace window:\n{subjects}"
        )
        return 0

    print(
        f"::error title=Release owed::{len(commits)} commit(s) have changed package code "
        f"since {tag}, the oldest {waiting_hours / 24:.1f} days ago. They are on main but not "
        f"in any release, so nobody consuming this package has them.\n{subjects}"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
