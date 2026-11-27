# SPDX-License-Identifier: MIT
# Copyright (C) 2026 SukramJ.
"""
``on_behalf_of_label`` only ever yields a value the daemon stores.

``_daemon_refuses`` restates openccu-loom's ``invalidOnBehalfOf``
(``internal/north/rest/middleware/audit_actor.go``): empty, more than 128
bytes, not UTF-8, or a ``Cc`` / ``Cf`` character is a 400.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
import unicodedata

import pytest

from openccu_loom_client import ON_BEHALF_OF_HEADER, on_behalf_of_label
from openccu_loom_client.transport import HttpTransport
from openccu_loom_client.wire import DAEMON_API_VERSION
from tests.helpers import MockDaemon


def _daemon_refuses(value: str) -> str:
    if value == "":
        return "empty"
    try:
        encoded = value.encode()
    except UnicodeEncodeError:
        return "not UTF-8"
    if len(encoded) > 128:
        return "longer than 128 bytes"
    if any(unicodedata.category(c) in {"Cc", "Cf"} for c in value):
        return "control or format character"
    return ""


@pytest.mark.parametrize(
    ("name", "user_id", "expected"),
    [
        ("Alex", "ha:1f2e", "Alex (ha:1f2e)"),
        ("Alex", None, "Alex"),
        ("", "ha:1f2e", "ha:1f2e"),
        ("  Alex  ", " ha:1f2e ", "Alex (ha:1f2e)"),
        ("Al\nex\t", "ha:1f2e", "Alex (ha:1f2e)"),
        ("\u202exelA", "ha:1f2e", "xelA (ha:1f2e)"),  # bidi override
        ("A\u200blex", "ha:1f2e", "Alex (ha:1f2e)"),  # zero-width space
        ("Al\ud800ex", "ha:1f2e", "Alex (ha:1f2e)"),  # lone surrogate
        ("Jürgen Groß", "ha:1f2e", "Jürgen Groß (ha:1f2e)"),
        ("", None, None),
        ("\u200b\n", "", None),
    ],
)
def test_label(name: str, user_id: str | None, expected: str | None) -> None:
    assert on_behalf_of_label(name=name, user_id=user_id) == expected


def test_long_name_is_cut_before_the_id() -> None:
    label = on_behalf_of_label(name="A" * 300, user_id="ha:0123456789abcdef")
    assert label is not None
    assert label.endswith(" (ha:0123456789abcdef)")
    assert len(label.encode()) == 128
    assert _daemon_refuses(label) == ""


def test_cut_never_splits_a_character() -> None:
    label = on_behalf_of_label(name="😀" * 60, user_id="ha:42")
    assert label is not None
    assert _daemon_refuses(label) == ""
    assert label.endswith(" (ha:42)")
    assert set(label.removesuffix(" (ha:42)")) == {"😀"}


def test_oversized_id_alone_still_fits() -> None:
    label = on_behalf_of_label(name="Alex", user_id="x" * 200)
    assert label == "x" * 128


@pytest.mark.parametrize(
    "name",
    [
        "\x00\x1f\x7f\x85",
        "\u061c\u200e\u2066\ufeff",
        "é" * 200,
        "名" * 100,
        " ",
    ],
)
def test_daemon_never_refuses_a_produced_label(name: str) -> None:
    for user_id in (None, "", "ha:abc", "ha:" + "z" * 200):
        label = on_behalf_of_label(name=name, user_id=user_id)
        assert label is None or _daemon_refuses(label) == "", (name, user_id, label)


_INFO = {
    "version": "1.2.3",
    "api_version": DAEMON_API_VERSION,
    "commit": "deadbeef",
    "build_date": "2026-05-24T10:00:00Z",
    "addon_build": False,
    "started_at": "2026-05-24T10:01:00Z",
    "uptime": "PT60S",
    "capabilities": ["rest.v1"],
    "schema_digest": "sha256:test",
    "config_ui_url": "",
}


@pytest.fixture
async def http(mock_daemon: MockDaemon) -> AsyncIterator[tuple[HttpTransport, MockDaemon]]:
    t = HttpTransport(config=mock_daemon.config, backoff_sequence=(0.0,))
    mock_daemon.get("/api/v1/info", payload=_INFO)
    await t.connect()
    yield t, mock_daemon
    await t.close()


async def test_label_reaches_the_daemon_as_utf8(http) -> None:
    """A non-ASCII label arrives intact — aiohttp sends header values as UTF-8."""
    t, daemon = http
    label = on_behalf_of_label(name="Jürgen Groß", user_id="ha:1f2e")
    assert label is not None
    daemon.put("/api/v1/devices/A1/paramsets/MASTER", status=204)
    await t.request(
        method="PUT", path="/devices/A1/paramsets/MASTER", json_body={}, headers={ON_BEHALF_OF_HEADER: label}
    )
    assert daemon.requests[-1].headers[ON_BEHALF_OF_HEADER] == label
