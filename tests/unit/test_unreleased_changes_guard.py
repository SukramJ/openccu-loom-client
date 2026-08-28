# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Tests for `script/check_unreleased_changes.py`.

The guard itself exists because two releases in a row shipped with something
"done" everywhere except where users get it. A guard nobody tests is the same
class of thing, so this drives the real script against throwaway repositories
rather than mocking git out — what it actually has to get right is which paths
count and when the grace window applies, and both are only visible against real
history.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "script" / "check_unreleased_changes.py"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)  # noqa: S603, S607


def _commit(repo: Path, path: str, *, message: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(f"# {message}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, str(_SCRIPT), *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,  # the exit code is the assertion
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Build a git repository with one tagged commit."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.invalid")
    _git(tmp_path, "config", "user.name", "test")
    _commit(tmp_path, "openccu_loom_client/__init__.py", message="initial")
    _git(tmp_path, "tag", "2026.1.1")
    return tmp_path


class TestUnreleasedChangesGuard:
    def test_clean_at_the_tag(self, repo: Path) -> None:
        result = _run(repo)
        assert result.returncode == 0
        assert "nothing owed" in result.stdout

    def test_package_change_is_reported(self, repo: Path) -> None:
        _commit(repo, "openccu_loom_client/client.py", message="a user-visible change")
        result = _run(repo, "--grace-hours", "0")
        assert result.returncode == 1
        assert "Release owed" in result.stdout
        assert "a user-visible change" in result.stdout

    def test_pyproject_counts_too(self, repo: Path) -> None:
        """The pin that broke 2026.8.28 lived there and nowhere else that ships."""
        _commit(repo, "pyproject.toml", message="bump a dependency pin")
        result = _run(repo, "--grace-hours", "0")
        assert result.returncode == 1

    def test_docs_and_tests_do_not_count(self, repo: Path) -> None:
        """
        The check has to stay quiet for what never reaches a user.

        Counting notes or tests would fire on nearly every merge, and a check
        that cries wolf is worse than none — people learn to skip it, including
        on the day it is right.
        """
        _commit(repo, "notes/open-work.md", message="a note")
        _commit(repo, "tests/unit/test_x.py", message="a test")
        _commit(repo, ".github/workflows/ci.yml", message="ci")
        result = _run(repo, "--grace-hours", "0")
        assert result.returncode == 0, result.stdout
        assert "nothing owed" in result.stdout

    def test_grace_window_holds_a_fresh_merge(self, repo: Path) -> None:
        """A merge and its release rarely land in the same minute."""
        _commit(repo, "openccu_loom_client/client.py", message="just merged")
        result = _run(repo, "--grace-hours", "24")
        assert result.returncode == 0
        assert "grace window" in result.stdout

    def test_untagged_repository_is_not_an_error(self, tmp_path: Path) -> None:
        """Before the first release there is nothing to be behind."""
        _git(tmp_path, "init", "-q", "-b", "main")
        _git(tmp_path, "config", "user.email", "t@example.invalid")
        _git(tmp_path, "config", "user.name", "test")
        _commit(tmp_path, "openccu_loom_client/__init__.py", message="initial")
        result = _run(tmp_path)
        assert result.returncode == 0
        assert "no tags" in result.stdout

    def test_tag_on_another_branch_is_ignored(self, repo: Path) -> None:
        """
        Only a tag reachable from HEAD describes a release of this history.

        Comparing against an unreachable one reports the difference between two
        lines of development as unreleased work.
        """
        _git(repo, "checkout", "-q", "-b", "side")
        _commit(repo, "openccu_loom_client/side.py", message="side work")
        _git(repo, "tag", "2026.9.9")
        _git(repo, "checkout", "-q", "main")
        result = _run(repo, "--grace-hours", "0")
        # main is still exactly at its own tag despite the newer tag elsewhere.
        assert result.returncode == 0, result.stdout
        assert "2026.1.1" in result.stdout
