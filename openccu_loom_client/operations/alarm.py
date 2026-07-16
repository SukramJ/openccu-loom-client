# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Alarm-system REST operations (daemon ≥ 0.42.0, api 2.22.0).

Thin façade over the daemon's ``/alarm`` namespace: area config +
verbs (arm/disarm/silence/acknowledge), panel entities, readiness,
journal, walk test, output test fire and PIN-code administration.

The daemon leaves every ``/alarm`` route unmounted when the alarm
subsystem is disabled (there is no ``/info`` capability token for it
yet), so callers feature-detect by treating a
:class:`~openccu_loom_client.exceptions.LoomNotFoundError` on the
first read as "alarm not available" rather than an error.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from openccu_loom_types.rest import (
    AlarmArea,
    AlarmAreaStatus,
    AlarmArmAccepted,
    AlarmCode,
    AlarmCodeRequest,
    AlarmJournalEntry,
    AlarmModeReadiness,
    AlarmOutput,
    AlarmPanelEntity,
    AlarmSensor,
    AlarmWalkTestStatus,
)

from openccu_loom_client.operations._base import _OperationsBase


class AlarmOperations(_OperationsBase):
    """Alarm areas, panels, verbs, journal, walk test and codes."""

    # ---- state / panels ----

    async def get_area_statuses(self) -> list[AlarmAreaStatus]:
        """
        Return the live status of every alarm area.

        Wire: ``GET /alarm/state`` — the response envelope is
        ``{"areas": [AlarmAreaStatus]}``; this returns the unwrapped
        list.
        """
        payload = await self._transport.request(method="GET", path="/alarm/state")
        areas = payload.get("areas") if isinstance(payload, dict) else None
        return [AlarmAreaStatus.model_validate(item) for item in areas or []]

    async def list_panels(self) -> list[AlarmPanelEntity]:
        """
        Return the panel model entities (one per area + the master).

        Wire: ``GET /alarm/panels``. The daemon computes each panel's
        canonical ``unique_id`` — consumers use it as-is and never
        derive their own.
        """
        return await self._request_list(method="GET", path="/alarm/panels", model=AlarmPanelEntity)

    async def get_area_readiness(self, *, area_id: str) -> dict[str, AlarmModeReadiness]:
        """
        Return one area's per-mode readiness (blockers/warnings).

        Wire: ``GET /alarm/areas/{id}/readiness`` — a map keyed by
        alarm mode.
        """
        payload = await self._transport.request(method="GET", path=f"/alarm/areas/{quote(area_id, safe='')}/readiness")
        items = payload if isinstance(payload, dict) else {}
        return {mode: AlarmModeReadiness.model_validate(entry) for mode, entry in items.items()}

    # ---- area configuration ----

    async def list_areas(self) -> list[AlarmArea]:
        """List the configured alarm areas. Wire: ``GET /alarm/areas``."""
        return await self._request_list(method="GET", path="/alarm/areas", model=AlarmArea)

    async def get_area(self, *, area_id: str) -> AlarmArea:
        """Return one alarm area's config. Wire: ``GET /alarm/areas/{id}``."""
        payload = await self._transport.request(method="GET", path=f"/alarm/areas/{quote(area_id, safe='')}")
        return AlarmArea.model_validate(payload)

    async def create_area(self, *, area: AlarmArea) -> AlarmArea:
        """
        Create an alarm area.

        Wire: ``POST /alarm/areas``. Not retried — creation is not
        idempotent (a retry surfaces as a duplicate-id error).
        """
        payload = await self._transport.request(
            method="POST",
            path="/alarm/areas",
            json_body=self._to_json_body(area),
            allow_retry=False,
        )
        return AlarmArea.model_validate(payload)

    async def update_area(self, *, area_id: str, area: AlarmArea) -> None:
        """Replace an area's config. Wire: ``PUT /alarm/areas/{id}``. Idempotent."""
        await self._transport.request(
            method="PUT",
            path=f"/alarm/areas/{quote(area_id, safe='')}",
            json_body=self._to_json_body(area),
            allow_retry=True,
        )

    async def delete_area(self, *, area_id: str) -> None:
        """Delete an alarm area. Wire: ``DELETE /alarm/areas/{id}``."""
        await self._transport.request(method="DELETE", path=f"/alarm/areas/{quote(area_id, safe='')}")

    async def list_area_sensors(self, *, area_id: str) -> list[AlarmSensor]:
        """List one area's enrolled sensors. Wire: ``GET /alarm/areas/{id}/sensors``."""
        return await self._request_list(
            method="GET", path=f"/alarm/areas/{quote(area_id, safe='')}/sensors", model=AlarmSensor
        )

    async def replace_area_sensors(self, *, area_id: str, sensors: list[AlarmSensor]) -> None:
        """
        Replace one area's sensor enrolment wholesale.

        Wire: ``PUT /alarm/areas/{id}/sensors``. Idempotent — the PUT
        carries the full desired set.
        """
        await self._transport.request(
            method="PUT",
            path=f"/alarm/areas/{quote(area_id, safe='')}/sensors",
            json_body=[self._to_json_body(sensor) for sensor in sensors],
            allow_retry=True,
        )

    async def list_area_outputs(self, *, area_id: str) -> list[AlarmOutput]:
        """List one area's enrolled outputs. Wire: ``GET /alarm/areas/{id}/outputs``."""
        return await self._request_list(
            method="GET", path=f"/alarm/areas/{quote(area_id, safe='')}/outputs", model=AlarmOutput
        )

    async def replace_area_outputs(self, *, area_id: str, outputs: list[AlarmOutput]) -> None:
        """
        Replace one area's output enrolment wholesale.

        Wire: ``PUT /alarm/areas/{id}/outputs``. Idempotent.
        """
        await self._transport.request(
            method="PUT",
            path=f"/alarm/areas/{quote(area_id, safe='')}/outputs",
            json_body=[self._to_json_body(output) for output in outputs],
            allow_retry=True,
        )

    # ---- verbs ----

    async def arm_area(
        self,
        *,
        area_id: str,
        mode: str,
        force: bool | None = None,
        skip_delay: bool | None = None,
        bypass: list[str] | None = None,
        code: str | None = None,
    ) -> AlarmArmAccepted:
        """
        Arm one area in the given mode.

        Wire: ``POST /alarm/areas/{id}/arm``. ``mode`` is an
        :class:`~openccu_loom_types.enums.AlarmMode` value other than
        ``disarmed`` (``perimeter``/``full``/``night``/``vacation``/
        ``custom``). ``code`` is passed through to the daemon's code
        check and never stored client-side. Not retried — arming has
        side effects (exit delay start, chirps) and readiness may
        change between attempts.
        """
        body: dict[str, Any] = {"mode": mode}
        if force is not None:
            body["force"] = force
        if skip_delay is not None:
            body["skip_delay"] = skip_delay
        if bypass is not None:
            body["bypass"] = bypass
        if code is not None:
            body["code"] = code
        payload = await self._transport.request(
            method="POST",
            path=f"/alarm/areas/{quote(area_id, safe='')}/arm",
            json_body=body,
            allow_retry=False,
        )
        return AlarmArmAccepted.model_validate(payload)

    async def disarm_area(self, *, area_id: str, code: str | None = None) -> None:
        """
        Disarm one area (also ends an active incident).

        Wire: ``POST /alarm/areas/{id}/disarm``. Not retried.
        """
        await self._transport.request(
            method="POST",
            path=f"/alarm/areas/{quote(area_id, safe='')}/disarm",
            json_body={"code": code} if code is not None else {},
            allow_retry=False,
        )

    async def silence_area(self, *, area_id: str, code: str | None = None) -> None:
        """
        Silence one area's sounding outputs (incident stays open).

        Wire: ``POST /alarm/areas/{id}/silence``. Not retried.
        """
        await self._transport.request(
            method="POST",
            path=f"/alarm/areas/{quote(area_id, safe='')}/silence",
            json_body={"code": code} if code is not None else {},
            allow_retry=False,
        )

    async def acknowledge_area(self, *, area_id: str, code: str | None = None) -> None:
        """
        Acknowledge one area's ended incident (clears the triggered latch).

        Wire: ``POST /alarm/areas/{id}/acknowledge``. Not retried.
        """
        await self._transport.request(
            method="POST",
            path=f"/alarm/areas/{quote(area_id, safe='')}/acknowledge",
            json_body={"code": code} if code is not None else {},
            allow_retry=False,
        )

    async def silence_all(self) -> None:
        """
        Silence every sounding output across all areas (break-glass).

        Wire: ``POST /alarm/silence-all``. Not retried.
        """
        await self._transport.request(method="POST", path="/alarm/silence-all", allow_retry=False)

    # ---- journal ----

    async def list_journal(
        self,
        *,
        area_id: str | None = None,
        journal_class: str | None = None,
        from_: Any | None = None,
        to: Any | None = None,
        limit: int | None = None,
    ) -> list[AlarmJournalEntry]:
        """
        Return alarm-journal entries, newest first.

        Wire: ``GET /alarm/journal``. Filters: ``area_id``, the journal
        ``class`` (``arm``/``disarm``/``trigger``/…), an inclusive
        ``from_`` / exclusive ``to`` RFC3339 window and ``limit``
        (daemon default 500, cap 5000).
        """
        params: dict[str, Any] = {}
        if area_id is not None:
            params["area"] = area_id
        if journal_class is not None:
            params["class"] = journal_class
        if from_ is not None:
            params["from"] = from_ if isinstance(from_, str) else from_.isoformat()
        if to is not None:
            params["to"] = to if isinstance(to, str) else to.isoformat()
        if limit is not None:
            params["limit"] = limit
        return await self._request_list(
            method="GET", path="/alarm/journal", params=params or None, model=AlarmJournalEntry
        )

    # ---- walk test ----

    async def start_walk_test(self, *, area_id: str) -> None:
        """
        Start a walk test on one (disarmed) area.

        Wire: ``POST /alarm/areas/{id}/walktest/start``. Not retried —
        a retry after success answers 409 (already active).
        """
        await self._transport.request(
            method="POST",
            path=f"/alarm/areas/{quote(area_id, safe='')}/walktest/start",
            allow_retry=False,
        )

    async def stop_walk_test(self, *, area_id: str) -> None:
        """
        Stop a running walk test.

        Wire: ``POST /alarm/areas/{id}/walktest/stop``. Idempotent —
        stopping an inactive test is a no-op.
        """
        await self._transport.request(
            method="POST",
            path=f"/alarm/areas/{quote(area_id, safe='')}/walktest/stop",
            allow_retry=True,
        )

    async def get_walk_test_status(self, *, area_id: str) -> AlarmWalkTestStatus:
        """Return the walk-test progress. Wire: ``GET /alarm/areas/{id}/walktest``."""
        payload = await self._transport.request(method="GET", path=f"/alarm/areas/{quote(area_id, safe='')}/walktest")
        return AlarmWalkTestStatus.model_validate(payload)

    # ---- output test ----

    async def test_output(self, *, output_id: str, optical_only: bool | None = None) -> None:
        """
        Briefly test-fire one enrolled output (audible unless optical_only).

        Wire: ``POST /alarm/outputs/{id}/test``. Not retried — it fires
        a real siren.
        """
        body: dict[str, Any] = {}
        if optical_only is not None:
            body["optical_only"] = optical_only
        await self._transport.request(
            method="POST",
            path=f"/alarm/outputs/{quote(output_id, safe='')}/test",
            json_body=body,
            allow_retry=False,
        )

    # ---- codes ----

    async def list_codes(self) -> list[AlarmCode]:
        """List the alarm codes (never the PINs). Wire: ``GET /alarm/codes``."""
        return await self._request_list(method="GET", path="/alarm/codes", model=AlarmCode)

    async def get_code(self, *, code_id: str) -> AlarmCode:
        """Return one alarm code's metadata. Wire: ``GET /alarm/codes/{id}``."""
        payload = await self._transport.request(method="GET", path=f"/alarm/codes/{quote(code_id, safe='')}")
        return AlarmCode.model_validate(payload)

    async def create_code(self, *, request: AlarmCodeRequest) -> AlarmCode:
        """
        Create an alarm code (PIN is argon2id-hashed daemon-side).

        Wire: ``POST /alarm/codes``. Not retried.
        """
        payload = await self._transport.request(
            method="POST",
            path="/alarm/codes",
            json_body=self._to_json_body(request),
            allow_retry=False,
        )
        return AlarmCode.model_validate(payload)

    async def update_code(self, *, code_id: str, request: AlarmCodeRequest) -> None:
        """Replace an alarm code. Wire: ``PUT /alarm/codes/{id}``. Idempotent."""
        await self._transport.request(
            method="PUT",
            path=f"/alarm/codes/{quote(code_id, safe='')}",
            json_body=self._to_json_body(request),
            allow_retry=True,
        )

    async def delete_code(self, *, code_id: str) -> None:
        """Delete an alarm code. Wire: ``DELETE /alarm/codes/{id}``."""
        await self._transport.request(method="DELETE", path=f"/alarm/codes/{quote(code_id, safe='')}")
