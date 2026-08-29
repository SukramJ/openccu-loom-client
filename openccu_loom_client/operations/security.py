# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Security & Safety REST operations (daemon ≥ 0.53.1, api 5.0.0).

Thin façade over the daemon's ``/security`` namespace: the folded
domain snapshot, the per-class view, the classified source inventory
with its operator override, and the standing fault ledger.

The domain runs **independently of the alarm engine** — an installation
with smoke and water detectors but no burglar alarm still gets the
hazard classes, the fault plane and the reports; only ``zones`` stays
empty. It therefore has no capability token of its own: the daemon
mounts the routes whenever its persistence tier is present and answers
``503`` (:class:`~openccu_loom_client.exceptions.LoomServiceUnavailableError`)
when it is not, which callers treat as "no Security & Safety domain"
rather than as an error.

Live changes arrive as ``security.*`` broadcasts
(:class:`~openccu_loom_client.events.SecurityStateChangedEvent` and
friends, daemon ≥ 0.54.0 / api 5.1.0). These reads are the bootstrap
and the reconcile path, not a poll loop: the deltas carry what changed,
the snapshot carries the fold, the escalation order and the per-class
``known`` counts that no single delta does.
"""

from __future__ import annotations

from urllib.parse import quote

from openccu_loom_client.operations._base import _OperationsBase
from openccu_loom_client.wire.rest import (
    SecurityClassState,
    SecurityFault,
    SecuritySnapshot,
    SecuritySourceOverride,
    SecuritySourceView,
)


class SecurityOperations(_OperationsBase):
    """The Security & Safety snapshot, source inventory and fault ledger."""

    # ---- aggregate state ----

    async def get_snapshot(self) -> SecuritySnapshot:
        """
        Return the folded Security & Safety state.

        Wire: ``GET /security`` — what is active per hazard class and per
        zone, what is broken and since when, and what was last reported.
        A class the installation has no source for is **omitted** rather
        than reported inactive, so a home without gas detectors does not
        advertise a permanently-off gas alarm.
        """
        payload = await self._transport.request(method="GET", path="/security")
        return SecuritySnapshot.model_validate(payload)

    async def get_class(self, *, security_class: str) -> SecurityClassState:
        """
        Return one hazard or fault class with its full source list.

        Wire: ``GET /security/classes/{class}``. Unlike the snapshot's
        per-class entry this is never truncated, so it answers "which
        detectors exactly" for one class.
        """
        payload = await self._transport.request(
            method="GET", path=f"/security/classes/{quote(security_class, safe='')}"
        )
        return SecurityClassState.model_validate(payload)

    # ---- classified sources ----

    async def list_sources(
        self,
        *,
        security_class: str | None = None,
        central: str | None = None,
        zone_id: str | None = None,
        relevant_only: bool = False,
        active_only: bool = False,
    ) -> list[SecuritySourceView]:
        """
        Return the classified data-point inventory.

        Wire: ``GET /security/sources``. The **unfiltered** list is
        deliberately reachable: a source the classifier got wrong is
        invisible in every aggregate, so listing everything is the only
        way to find it. ``relevant_only`` narrows to the sources that
        actually contribute to an aggregate, ``active_only`` to those
        currently reporting.
        """
        params: dict[str, str] = {}
        if security_class is not None:
            params["class"] = security_class
        if central is not None:
            params["central"] = central
        if zone_id is not None:
            params["zone_id"] = zone_id
        # The daemon models both flags as a one-value enum: the filter is
        # requested by sending "true", never by sending "false".
        if relevant_only:
            params["relevant"] = "true"
        if active_only:
            params["active"] = "true"
        return await self._request_list(
            method="GET", path="/security/sources", model=SecuritySourceView, params=params or None
        )

    async def set_source_override(self, *, ref: str, override: SecuritySourceOverride) -> None:
        """
        Override the classification of one data point (operator role).

        Wire: ``PUT /security/sources/{ref}`` with the routing key
        ``<central>|<interface_id>|<channel_address>|<parameter>``.
        Idempotent, so retried.

        An override carrying no class, ``included=True`` and no note
        **removes** the override and returns the data point to the
        classifier's verdict — the undo a wrong override needs. Omitting
        ``included`` leaves inclusion unchanged, so a request that only
        names a class reclassifies and never excludes.
        """
        await self._transport.request(
            method="PUT",
            path=f"/security/sources/{quote(ref, safe='')}",
            json_body=self._to_json_body(override),
            allow_retry=True,
        )

    # ---- fault ledger ----

    async def list_faults(self) -> list[SecurityFault]:
        """Return the standing fault ledger. Wire: ``GET /security/faults``."""
        return await self._request_list(method="GET", path="/security/faults", model=SecurityFault)

    async def acknowledge_fault(self, *, fault_id: str) -> None:
        """
        Mark a standing fault as seen (operator role).

        Wire: ``POST /security/faults/{id}/acknowledge``. Acknowledgement
        never clears the fault: the condition is still there, the
        operator has merely stopped needing to be told. Not retried —
        the acknowledgement records an actor and a time.
        """
        await self._transport.request(
            method="POST",
            path=f"/security/faults/{quote(fault_id, safe='')}/acknowledge",
            allow_retry=False,
        )
