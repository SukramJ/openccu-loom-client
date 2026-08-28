# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Contract check: the URLs ``operations/`` builds against the daemon's OpenAPI.

Why a test and not a generator. Generating the façades from
``assets/openapi.yaml`` was assessed and rejected: only 82 of 224 public
method names coincide with ``snake_case(operationId)``, because the ids were
minted for the TypeScript side (``deleteDevice``, ``patchDevice``) while this
package reads as Python (``execute_program``, ``batch_read``, ``arm_zone``).
Generation would rename 142 public methods and break 125 in-package call
sites, or carry a hand-kept 142-line name table. The daemon's own SPA — same
maintainer, same document — writes its façade by hand (2 660 LOC, 249
methods) and pins it with a contract test instead. This is that test.

What it is worth depends on the spec being pinned to the code, and it is:
``tests/contract/rest_router_openapi_walk_test.go`` in the daemon asserts
both directions — every mounted route documented, every documented operation
mounted. So a path this test accepts is a path the daemon really serves.

Two directions, deliberately asymmetric:

* **Every URL this package builds must exist in the document.** No
  maintenance, and it catches the failure the daemon's SPA wrote its own test
  for after shipping a call to ``/link-paramsets/`` when the contract serves
  ``/link-ps/`` — every save failed, silently, because a 404 on a write looks
  like a write that did not take.
* **Tags this package serves completely must stay complete.** Pinning all 127
  unserved operations would fail on every daemon addition regardless of
  whether it concerns this client, which is how a guard gets ignored. Pinning
  the *tags* means a new ``alarm`` route is a decision someone makes, and a
  new ``matter`` route is not this package's business.

Skipped when the daemon repo is not checked out beside this one; the CI job
checks it out and passes ``OPENCCU_LOOM_REPO``.
"""

from __future__ import annotations

import ast
import os
import pathlib
import re

import pytest
import yaml

_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})

_OPERATIONS_DIR = pathlib.Path(__file__).resolve().parents[2] / "openccu_loom_client" / "operations"

# The request helpers every façade routes through. `_base.py` is excluded from
# the walk: its `_request_list` forwards a `path` parameter it was handed, so
# it is plumbing rather than a call site.
_REQUEST_HELPERS = frozenset({"request", "_request_list"})

# Tags whose every documented operation this package serves today. Pinned so
# that completeness is kept on purpose rather than by accident — a daemon
# addition under one of these is a decision, not a surprise.
#
# Absent on purpose: `matter`, `auth`, `central-admin`, `config-admin`,
# `user-admin` and `token-admin` are the admin façades removed in #118 — the
# daemon serves them and node-red drives five of them, but nothing that goes
# through this package does. `core`, `history`, `webhook` and `setup` were
# never served here.
_FULLY_SERVED_TAGS: frozenset[str] = frozenset(
    {
        "System",
        "alarm",
        "audit",
        "config",
        "messages",
        "schedules",
        "security",
        "sessions",
        "snapshot",
        "visibility",
    }
)


def _find_openapi() -> pathlib.Path | None:
    """
    Locate the daemon's ``openapi.yaml``.

    Same resolution as the broadcast drift guard: the daemon repo is normally
    checked out beside this one, and ``OPENCCU_LOOM_REPO`` overrides for CI
    and non-sibling layouts.
    """
    candidates = []
    if env := os.environ.get("OPENCCU_LOOM_REPO"):
        candidates.append(pathlib.Path(env))
    candidates.append(pathlib.Path(__file__).resolve().parents[3] / "openccu-loom")
    for repo in candidates:
        spec = repo / "assets/openapi.yaml"
        if spec.is_file():
            return spec
    return None


_OPENAPI = _find_openapi()


def _normalise(path: str) -> str:
    """
    Reduce a path to its shape, with every parameter as ``{}``.

    Parameter *names* are not part of the wire contract — the daemon calls one
    ``{channel_no}`` where this package calls it ``{channel}`` — so comparing
    them would report a difference that does not exist.
    """
    return re.sub(r"\{[^{}]*\}", "{}", path)


def _literal_path(node: ast.AST) -> str | None:
    """Return the path a ``path=`` argument builds, or ``None`` if it is not static."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(str(v.value) if isinstance(v, ast.Constant) else "{}" for v in node.values)
    return None


def _client_calls() -> dict[tuple[str, str], list[str]]:
    """Return ``{(method, path shape): [source locations]}`` for every façade call."""
    calls: dict[tuple[str, str], list[str]] = {}
    for source in sorted(_OPERATIONS_DIR.glob("*.py")):
        if source.name in ("_base.py", "__init__.py"):
            continue
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr not in _REQUEST_HELPERS:
                continue
            kwargs = {kw.arg: kw.value for kw in node.keywords}
            method_node, path_node = kwargs.get("method"), kwargs.get("path")
            if not (isinstance(method_node, ast.Constant) and isinstance(method_node.value, str)):
                continue
            if path_node is None or (path := _literal_path(path_node)) is None:
                continue
            calls.setdefault((method_node.value.upper(), _normalise(path)), []).append(f"{source.name}:{node.lineno}")
    return calls


def _spec_operations() -> dict[tuple[str, str], set[str]]:
    """Return ``{(method, path shape): {tags}}`` for every documented operation."""
    assert _OPENAPI is not None
    document = yaml.safe_load(_OPENAPI.read_text(encoding="utf-8"))
    operations: dict[tuple[str, str], set[str]] = {}
    for path, item in document["paths"].items():
        for method, operation in item.items():
            if method not in _HTTP_METHODS:
                continue
            key = (method.upper(), _normalise(path))
            operations.setdefault(key, set()).update(operation.get("tags") or ())
    return operations


pytestmark = pytest.mark.skipif(
    _OPENAPI is None,
    reason="openccu-loom repo not found beside this one (set OPENCCU_LOOM_REPO)",
)


class TestOperationsMatchTheContract:
    def test_every_url_this_package_builds_is_documented(self) -> None:
        """A façade path with no operation behind it is a 404 nobody notices."""
        calls = _client_calls()
        assert calls, "extracted no façade calls — the AST walk stopped matching"
        documented = _spec_operations()
        undocumented = {key: where for key, where in calls.items() if key not in documented}
        assert undocumented == {}, "these operations/ calls address no documented daemon operation:\n" + "\n".join(
            f"  {m} {p}  ({', '.join(where)})" for (m, p), where in sorted(undocumented.items())
        )

    def test_tags_served_completely_stay_complete(self) -> None:
        """A new operation under a tag this package covers is a decision, not a surprise."""
        calls = set(_client_calls())
        documented = _spec_operations()
        gaps: dict[str, list[str]] = {}
        for key, tags in documented.items():
            if key in calls:
                continue
            for tag in tags & _FULLY_SERVED_TAGS:
                gaps.setdefault(tag, []).append(f"{key[0]} {key[1]}")
        assert gaps == {}, (
            "the daemon documents operations under tags this package serves completely:\n"
            + "\n".join(f"  {tag}: {', '.join(sorted(paths))}" for tag, paths in sorted(gaps.items()))
            + "\n\nEither serve them, or drop the tag from _FULLY_SERVED_TAGS with a reason."
        )

    def test_pinned_tags_are_still_tags(self) -> None:
        """A renamed or removed tag would silently empty its own guard."""
        documented_tags = {tag for tags in _spec_operations().values() for tag in tags}
        vanished = sorted(_FULLY_SERVED_TAGS - documented_tags)
        assert vanished == [], (
            f"_FULLY_SERVED_TAGS names tags the document no longer has: {vanished}. "
            "Their entries in the guard above now assert nothing."
        )
