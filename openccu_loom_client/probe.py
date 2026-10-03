# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Asking a daemon what it is before any login.

The daemon answers ``GET /info`` without a credential, and from its api
13.5.0 on that answer says where it runs and how a person may sign in (its
ADR 0081): ``deployment.kind`` — ``lite-addon``, ``ccu-addon``, ``ha-addon``,
``standalone``, an open vocabulary for a client — with the ingress path when
there is one, and one ``auth.<name>.v1`` capability token per login path
(:func:`~openccu_loom_client.capabilities.login_paths`). An older daemon sends
no ``deployment``; that reads as unknown, not as an error.

On an openccu-lite box the daemon sits behind the box's gate, which answers a
request without a credential itself — 401 in plain JSON, or a redirect to the
box login for a browser. The daemon never answers ``/info`` with 401, so the
probe reports that gate answer as a result rather than an error: it tells the
caller to ask for a box credential. Gate and daemon are told apart by the one
rule in :mod:`openccu_loom_client.boxgate`.

The daemon's mDNS record (:mod:`openccu_loom_client.discovery`) carries the
short form of the same description. The record is a hint; where ``/info`` is
readable, ``/info`` is the authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from http import HTTPStatus
import json
from typing import Any, Final

import aiohttp

from openccu_loom_client.boxgate import gate_refusal
from openccu_loom_client.capabilities import login_paths
from openccu_loom_client.config import (
    DEFAULT_BASE_PATH,
    DEFAULT_HTTP_PORT,
    DEFAULT_HTTPS_PORT,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
)
from openccu_loom_client.exceptions import LoomTransportError, http_error_from_problem, parse_problem

__all__ = [
    "DaemonProbe",
    "ProbeOutcome",
    "probe_daemon",
]

_INFO_PATH: Final = "/info"


class ProbeOutcome(StrEnum):
    """Who answered the probe."""

    #: The daemon answered ``/info`` itself.
    DAEMON = "daemon"
    #: An openccu-lite box's gate answered instead; the daemon needs a box credential.
    BOX_GATE = "box_gate"


@dataclass(frozen=True, slots=True, kw_only=True)
class DaemonProbe:
    """
    What a daemon says about itself before any login.

    Every field but :attr:`reached` is empty or ``None`` for
    :attr:`ProbeOutcome.BOX_GATE` — the gate says nothing about the daemon.
    """

    reached: ProbeOutcome
    """Whether the daemon or a box gate answered."""

    api_version: str = ""
    """The daemon's REST API version."""

    version: str = ""
    """The daemon's release version, ``""`` when it sent none."""

    capabilities: frozenset[str] = field(default_factory=frozenset)
    """Every capability token the daemon advertised, known to this package or not."""

    login_paths: frozenset[str] = field(default_factory=frozenset)
    """The short names of the daemon's login paths (``pairing``, ``occulite_token``, …)."""

    deployment_kind: str | None = None
    """``deployment.kind`` as sent, unknown values kept; ``None`` when the daemon sent no ``deployment``."""

    ingress_path: str | None = None
    """``deployment.ingress_path``, ``None`` when there is none."""


async def probe_daemon(
    *,
    host: str,
    port: int | None = None,
    tls: bool = True,
    verify_tls: bool = True,
    base_path: str = DEFAULT_BASE_PATH,
) -> DaemonProbe:
    """
    Send one ``GET <base>/info`` without a credential and report who answered.

    ``host``/``port``/``tls``/``verify_tls``/``base_path`` are the transport
    knobs :func:`~openccu_loom_client.pairing.start_pairing` takes; behind a
    box's ingress, ``base_path`` carries the ingress prefix
    (``/addons/loom/api/v1``). No redirect is followed: a redirect is the
    gate's answer.

    Returns a :class:`DaemonProbe` — :attr:`ProbeOutcome.BOX_GATE` is a
    result, not an error. Raises :class:`LoomTransportError` when the daemon
    cannot be reached, or answers 200 with something that is not an ``/info``
    object, and the mapped
    :class:`~openccu_loom_client.exceptions.LoomHttpError` subclass for any
    other non-200 answer.
    """
    resolved_port = port if port is not None else (DEFAULT_HTTPS_PORT if tls else DEFAULT_HTTP_PORT)
    url = f"{'https' if tls else 'http'}://{host}:{resolved_port}{base_path}{_INFO_PATH}"
    timeout = aiohttp.ClientTimeout(total=DEFAULT_REQUEST_TIMEOUT_SECONDS)
    try:
        async with (
            aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=verify_tls),
                timeout=timeout,
                # Nothing set by an earlier answer may ride along: the probe
                # must reach the daemon exactly as an anonymous caller.
                cookie_jar=aiohttp.DummyCookieJar(),
            ) as session,
            session.get(url, headers={"Accept": "application/json"}, allow_redirects=False) as resp,
        ):
            status = resp.status
            content_type = resp.headers.get("Content-Type", "")
            raw = await resp.read()
    except (aiohttp.ClientError, TimeoutError) as exc:
        msg = f"probe at {url} failed: {type(exc).__name__}: {exc}"
        raise LoomTransportError(msg) from exc

    if gate_refusal(status=status, content_type=content_type, host=host) is not None:
        return DaemonProbe(reached=ProbeOutcome.BOX_GATE)
    payload = _decode(raw=raw)
    if status != HTTPStatus.OK:
        problem = parse_problem(payload=payload)
        raise http_error_from_problem(
            status=status,
            problem=problem,
            raw_body=raw if problem is None else None,
            method="GET",
            url=url,
        )
    return _from_info(payload=payload, url=url)


def _decode(*, raw: bytes) -> Any:
    """Decode a JSON body, or return ``None`` for anything else."""
    try:
        return json.loads(raw.decode("utf-8")) if raw else None
    except UnicodeDecodeError, json.JSONDecodeError:
        return None


def _from_info(*, payload: Any, url: str) -> DaemonProbe:
    """Read the fields the probe reports, tolerantly, from a decoded ``/info`` body."""
    if not isinstance(payload, dict):
        msg = f"probe at {url} answered 200 without an /info object"
        raise LoomTransportError(msg)
    api_version = payload.get("api_version")
    if not isinstance(api_version, str) or not api_version:
        msg = f"probe at {url} answered an /info object without api_version"
        raise LoomTransportError(msg)
    version = payload.get("version")
    raw_capabilities = payload.get("capabilities")
    capabilities = (
        frozenset(c for c in raw_capabilities if isinstance(c, str))
        if isinstance(raw_capabilities, list)
        else frozenset[str]()
    )
    deployment = payload.get("deployment")
    kind = deployment.get("kind") if isinstance(deployment, dict) else None
    ingress = deployment.get("ingress_path") if isinstance(deployment, dict) else None
    return DaemonProbe(
        reached=ProbeOutcome.DAEMON,
        api_version=api_version,
        version=version if isinstance(version, str) else "",
        capabilities=capabilities,
        login_paths=login_paths(capabilities=capabilities),
        deployment_kind=kind if isinstance(kind, str) and kind else None,
        ingress_path=ingress if isinstance(ingress, str) and ingress else None,
    )
