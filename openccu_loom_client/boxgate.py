# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Telling an openccu-lite box gate's refusal from the daemon's own answer.

An openccu-lite box serves the daemon at ``/addons/loom/`` behind a gate. In
box-ingress mode every request carries a box API token as
``Authorization: Bearer`` (openccu-lite 1.0.0-dev.36 or newer), and the gate
answers a request it does not accept itself — it never reaches the daemon.
From occulited's ``docs/system-api.md`` (*The gate and API tokens*): **401**
for a token the box does not know or an expired one, **403** ``forbidden``
for a token without the add-on's scope or sent from outside its address
ranges. Neither clears on its own, so neither is retried: a new token needs a
new pairing.

The daemon's own 401 and 403 are told apart by contract: the daemon answers
every error as ``application/problem+json`` (its
``errors.problem_details.v1`` capability), so a 401 or 403 with any other
content type cannot be its answer. A redirect is the gate's too — the daemon
contract has none — which is what a box older than the token gate answers a
caller it cannot authenticate.
"""

from __future__ import annotations

from http import HTTPStatus

from openccu_loom_client.exceptions import LoomBoxGateError, LoomBoxTokenError

_PROBLEM_JSON = "application/problem+json"


def gate_refusal(*, status: int, content_type: str, host: str) -> LoomBoxGateError | None:
    """
    Return the error for an answer the box gate gave instead of the daemon.

    ``None`` when the answer is the daemon's own (any status the gate does not
    produce, or a 401/403 in the daemon's ``problem+json`` form).
    """
    if HTTPStatus.MULTIPLE_CHOICES <= status < HTTPStatus.BAD_REQUEST:
        return LoomBoxGateError(
            f"the box at {host} redirected the request ({status}) instead of passing it to the daemon — "
            "the box may predate API tokens at the gate (openccu-lite 1.0.0-dev.36), or the ingress "
            "path prefix is wrong"
        )
    if content_type.startswith(_PROBLEM_JSON):
        return None
    if status == HTTPStatus.UNAUTHORIZED:
        return LoomBoxTokenError(
            f"the box at {host} does not accept the box token (401) — it is unknown, expired or revoked "
            "(pair with the box again), or the box predates API tokens at the gate "
            "(openccu-lite 1.0.0-dev.36)"
        )
    if status == HTTPStatus.FORBIDDEN:
        return LoomBoxGateError(
            f"the box at {host} refused the box token for this add-on (403) — the token lacks the scope "
            "addon:openccu-loom, or this address is outside the token's ranges"
        )
    return None
