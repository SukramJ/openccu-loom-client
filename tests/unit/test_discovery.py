# SPDX-License-Identifier: MIT
# Copyright (C) 2026 SukramJ.

"""
Parsing the daemon's ``_openccu-loom._tcp`` TXT record.

The record is a hint, so the parser never raises: a key it cannot read is
absent, and a key it does not know is ignored.
"""

from __future__ import annotations

from openccu_loom_client import DiscoveredDaemon, parse_txt_record


def test_a_full_new_style_record() -> None:
    parsed = parse_txt_record(
        properties={
            "txtvers": "1",
            "path": "/api/v1",
            "api_version": "13.5.0",
            "tls": "1",
            "instance": "loom-kearney",
            "centrals": "2",
            "ccus": "1234567,7654321",
            "deploy": "lite-addon",
            "ingress": "/addons/loom/",
            "auth": "pairing,occulite_token",
        }
    )

    assert parsed == DiscoveredDaemon(
        txtvers=1,
        path="/api/v1",
        api_version="13.5.0",
        tls=True,
        instance="loom-kearney",
        centrals=2,
        ccus=("1234567", "7654321"),
        deploy="lite-addon",
        ingress="/addons/loom/",
        login_paths=frozenset({"pairing", "occulite_token"}),
    )


def test_an_old_style_record_leaves_the_new_keys_absent() -> None:
    parsed = parse_txt_record(
        properties={
            "path": "/api/v1",
            "api_version": "12.1.0",
            "tls": "0",
            "instance": "loom",
            "centrals": "1",
            "ccus": "1234567",
        }
    )

    assert parsed.tls is False
    assert parsed.centrals == 1
    assert parsed.ccus == ("1234567",)
    assert parsed.txtvers is None
    assert parsed.deploy is None
    assert parsed.ingress is None
    assert parsed.login_paths == frozenset()


def test_bytes_values_are_decoded_as_utf8() -> None:
    parsed = parse_txt_record(
        properties={"instance": "Wohnzimmer-Büro".encode(), "tls": b"1", "auth": b"pairing", "path": b"\xff\xfe"}
    )

    assert parsed.instance == "Wohnzimmer-Büro"
    assert parsed.tls is True
    assert parsed.login_paths == frozenset({"pairing"})
    # Undecodable bytes read as absent, never as an exception.
    assert parsed.path is None


def test_keys_are_case_insensitive() -> None:
    """RFC 6763 section 6.4: DNS-SD keys are case-insensitive."""
    parsed = parse_txt_record(properties={"TxtVers": "1", "API_VERSION": "13.5.0", "Deploy": "ha-addon"})

    assert parsed.txtvers == 1
    assert parsed.api_version == "13.5.0"
    assert parsed.deploy == "ha-addon"


def test_none_and_empty_values_are_absent() -> None:
    parsed = parse_txt_record(
        properties={"path": None, "instance": "", "ccus": "", "auth": b"", "ingress": None, "tls": None}
    )

    assert parsed.path is None
    assert parsed.instance is None
    assert parsed.ccus == ()
    assert parsed.login_paths == frozenset()
    assert parsed.ingress is None
    assert parsed.tls is None


def test_garbage_numbers_and_flags_are_absent() -> None:
    parsed = parse_txt_record(properties={"txtvers": "one", "centrals": "-1", "tls": "yes"})

    assert parsed.txtvers is None
    assert parsed.centrals is None
    assert parsed.tls is None


def test_non_ascii_digits_are_not_numbers() -> None:
    """``int()`` accepts other scripts' digits; a decimal count on the wire is ASCII."""
    parsed = parse_txt_record(properties={"centrals": "٣", "txtvers": " 1"})

    assert parsed.centrals is None
    assert parsed.txtvers is None


def test_unknown_keys_are_ignored() -> None:
    parsed = parse_txt_record(properties={"path": "/api/v1", "future_key": "whatever"})

    assert parsed.path == "/api/v1"


def test_an_unknown_login_path_is_kept() -> None:
    """The login-path names are an open set: a newer daemon's name is not dropped."""
    parsed = parse_txt_record(properties={"auth": "pairing, future_login ,,oidc"})

    assert parsed.login_paths == frozenset({"pairing", "future_login", "oidc"})


def test_an_empty_record() -> None:
    assert parse_txt_record(properties={}) == DiscoveredDaemon()
