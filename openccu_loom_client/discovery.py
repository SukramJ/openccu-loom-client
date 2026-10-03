# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Reading the daemon's mDNS TXT record.

The daemon announces itself as ``_openccu-loom._tcp`` with a TXT record that
is the short form of its ``/info`` self-description (its ADR 0081). Keys:
``path``, ``api_version``, ``tls`` (``0``/``1``), ``instance``, ``centrals``
(a decimal count) and ``ccus`` (comma-separated serial suffixes) on every
daemon; from api 13.5.0 on also ``txtvers``, ``deploy`` (the vocabulary of
``deployment.kind``), ``ingress`` (only when there is an ingress path) and
``auth`` (comma-separated login-path names, as
:func:`~openccu_loom_client.capabilities.login_paths` derives them; absent
when there are none).

The TXT rules are RFC 6763's: keys are case-insensitive (section 6.4), a key
that occurs more than once counts by its first occurrence (section 6.4), and
the record is versioned by ``txtvers`` (section 6.7). Here a mapping stands in
for the record, so "first" is the mapping's iteration order — whether that is
the order on the wire depends on whoever built the mapping.

The record is a hint, not a contract: it is what some announcer said, cached
by the network. Where the daemon's ``/info`` is readable
(:func:`~openccu_loom_client.probe.probe_daemon`), ``/info`` is the
authority. So the parser never raises: a value it cannot read is absent, and
a key it does not know is ignored.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

__all__ = [
    "DiscoveredDaemon",
    "parse_txt_record",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class DiscoveredDaemon:
    """What a daemon's TXT record says; every field is optional."""

    txtvers: int | None = None
    """The record's key-set version; ``None`` on a daemon older than api 13.5.0."""

    path: str | None = None
    """The REST base path (``/api/v1``)."""

    api_version: str | None = None
    """The daemon's REST API version."""

    tls: bool | None = None
    """Whether the daemon serves TLS; ``None`` when the record does not say so readably."""

    instance: str | None = None
    """The daemon's instance name."""

    centrals: int | None = None
    """How many centrals the daemon mediates."""

    ccus: tuple[str, ...] = ()
    """The serial suffixes of the CCUs, in record order."""

    deploy: str | None = None
    """Where the daemon runs (``lite-addon``, …); unknown values kept as sent."""

    ingress: str | None = None
    """The ingress path the daemon is served under, when there is one."""

    login_paths: frozenset[str] = field(default_factory=frozenset)
    """The short names of the login paths the daemon accepts, unknown names kept."""


def parse_txt_record(*, properties: Mapping[str, str | bytes | None]) -> DiscoveredDaemon:
    """
    Parse the TXT properties of an ``_openccu-loom._tcp`` service.

    Bytes values are decoded as UTF-8; an undecodable, ``None`` or empty value
    is absent, as is a number that is not plain decimal. Never raises.
    """
    values = _normalise(properties=properties)
    return DiscoveredDaemon(
        txtvers=_decimal(value=values.get("txtvers")),
        path=values.get("path"),
        api_version=values.get("api_version"),
        tls=_flag(value=values.get("tls")),
        instance=values.get("instance"),
        centrals=_decimal(value=values.get("centrals")),
        ccus=tuple(_items(value=values.get("ccus"))),
        deploy=values.get("deploy"),
        ingress=values.get("ingress"),
        login_paths=frozenset(_items(value=values.get("auth"))),
    )


def _normalise(*, properties: Mapping[str, str | bytes | None]) -> dict[str, str]:
    """
    Fold keys to lower case and values to non-empty text.

    The first occurrence of a key decides (RFC 6763 section 6.4), even when
    its value is unreadable — a later spelling of the same key does not
    replace it.
    """
    seen: set[str] = set()
    out: dict[str, str] = {}
    for key, raw in properties.items():
        folded = key.lower()
        if folded in seen:
            continue
        seen.add(folded)
        if (text := _text(value=raw)) is not None:
            out[folded] = text
    return out


def _text(*, value: object) -> str | None:
    """Return the value as non-empty text, or ``None``."""
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(value, str) and value:
        return value
    return None


def _decimal(*, value: str | None) -> int | None:
    """Return a plain ASCII decimal as an int, anything else as ``None``."""
    if value is None or not value.isascii() or not value.isdigit():
        return None
    return int(value)


def _flag(*, value: str | None) -> bool | None:
    """Return ``"1"`` as True, ``"0"`` as False, anything else as ``None``."""
    if value == "1":
        return True
    if value == "0":
        return False
    return None


def _items(*, value: str | None) -> list[str]:
    """Split a comma-separated value, dropping blanks around and between items."""
    if value is None:
        return []
    return [item for part in value.split(",") if (item := part.strip())]
