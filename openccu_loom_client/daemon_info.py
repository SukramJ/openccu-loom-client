# SPDX-License-Identifier: MIT
# Copyright (C) 2026 SukramJ.

"""
The daemon's ``/info`` payload, readable from daemons older than the generated types.

The generated :class:`openccu_loom_client.wire.rest.Info` mirrors one daemon
API version, and from api 13.5.0 on that version requires ``deployment``. A
daemon older than 13.5.0 does not send it, so validating its ``/info`` against
the generated model fails and the client could not connect to it at all. This
module holds the one hand-written relaxation of that model; everything that
validates ``/info`` uses it.
"""

from __future__ import annotations

from openccu_loom_client.wire import rest as _wire_rest


class Info(_wire_rest.Info):
    """
    ``/info`` with ``deployment`` optional; every other field stays as generated.

    A subclass of the generated model, so ``isinstance(info, wire.rest.Info)``
    holds and code typed against the generated model keeps working.

    ``deployment is None`` means the daemon is older than api 13.5.0 and did
    not say where it runs. It does not mean ``standalone``, and it does not mean
    the daemon has no deployment — it means unknown. There is no truthful value
    to put in its place, so none is invented. :func:`probe_daemon` reports the
    same case as ``DaemonProbe.deployment_kind is None``.
    """

    # Widening a required field to optional is the whole point of this class,
    # and it is Liskov-incompatible by construction: mypy reports it as an
    # incompatible assignment against the base. A subclass is still the right
    # form — it keeps isinstance with the generated model and leaves every
    # other field untouched — so the one error is silenced here, by code.
    deployment: _wire_rest.Deployment | None = None  # type: ignore[assignment]
