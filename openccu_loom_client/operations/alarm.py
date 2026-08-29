# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Alarm-system REST operations (daemon ≥ 0.49.2, api 3.0.0).

Thin façade over the daemon's ``/alarm`` namespace: zone config +
verbs (arm/disarm/silence/acknowledge/reset-motion), panel entities,
readiness, journal, walk test, output test fire and PIN-code
administration.

The daemon leaves every ``/alarm`` route unmounted when the alarm
subsystem is disabled. Since daemon 0.43.1 the ``/info`` capability
list carries ``alarm.v1`` exactly when the surface is mounted —
callers gate on that token (as :meth:`LoomClient._bootstrap_alarm_panels`
does) and keep treating a
:class:`~openccu_loom_client.exceptions.LoomNotFoundError` on the
first read as "alarm not available" rather than an error.

The armable unit is a **zone** (``/alarm/zones/{id}``, ``zone_id``);
daemon < 0.49.2 called it an *area* — the API 3.0.0 rename is
deliberate and has no compatibility shim on either side.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from openccu_loom_client.operations._base import _OperationsBase
from openccu_loom_client.wire.rest import (
    AlarmArmAccepted,
    AlarmCode,
    AlarmCodeRequest,
    AlarmIncident,
    AlarmJournalEntry,
    AlarmModeReadiness,
    AlarmMotionResetResult,
    AlarmOutput,
    AlarmOutputCandidate,
    AlarmPanelEntity,
    AlarmRemoteKeyCandidate,
    AlarmSensor,
    AlarmSensorCandidate,
    AlarmTriggeredMotionSensor,
    AlarmWalkTestStatus,
    AlarmZone,
    AlarmZoneCreate,
    AlarmZoneStatus,
)


class AlarmOperations(_OperationsBase):
    """Alarm zones, panels, verbs, journal, walk test and codes."""

    # ---- state / panels ----

    async def get_zone_statuses(self) -> list[AlarmZoneStatus]:
        """
        Return the live status of every alarm zone.

        Wire: ``GET /alarm/state`` — the response envelope is
        ``{"zones": [AlarmZoneStatus]}``; this returns the unwrapped
        list.
        """
        payload = await self._transport.request(method="GET", path="/alarm/state")
        zones = payload.get("zones") if isinstance(payload, dict) else None
        return [AlarmZoneStatus.model_validate(item) for item in zones or []]

    async def list_panels(self) -> list[AlarmPanelEntity]:
        """
        Return the panel model entities (one per zone + the master).

        Wire: ``GET /alarm/panels``. The daemon computes each panel's
        canonical ``unique_id`` — consumers use it as-is and never
        derive their own.
        """
        return await self._request_list(method="GET", path="/alarm/panels", model=AlarmPanelEntity)

    async def get_zone_readiness(self, *, zone_id: str) -> dict[str, AlarmModeReadiness]:
        """
        Return one zone's per-mode readiness (blockers/warnings).

        Wire: ``GET /alarm/zones/{id}/readiness`` — a map keyed by
        alarm mode.
        """
        payload = await self._transport.request(method="GET", path=f"/alarm/zones/{quote(zone_id, safe='')}/readiness")
        items = payload if isinstance(payload, dict) else {}
        return {mode: AlarmModeReadiness.model_validate(entry) for mode, entry in items.items()}

    # ---- zone configuration ----

    async def list_zones(self) -> list[AlarmZone]:
        """List the configured alarm zones. Wire: ``GET /alarm/zones``."""
        return await self._request_list(method="GET", path="/alarm/zones", model=AlarmZone)

    async def get_zone(self, *, zone_id: str) -> AlarmZone:
        """Return one alarm zone's config. Wire: ``GET /alarm/zones/{id}``."""
        payload = await self._transport.request(method="GET", path=f"/alarm/zones/{quote(zone_id, safe='')}")
        return AlarmZone.model_validate(payload)

    async def create_zone(self, *, zone: AlarmZone | AlarmZoneCreate) -> AlarmZone:
        """
        Create an alarm zone.

        Wire: ``POST /alarm/zones``. Not retried — creation is not
        idempotent (a retry surfaces as a duplicate-id error).

        Since daemon api 7.1.0 the request body is ``AlarmZoneCreate``, which
        omits ``id``: the server mints its own and ignores one sent in the
        body. Prefer it — building an ``AlarmZone`` here means inventing an id
        that is discarded. The full model stays accepted so existing callers
        keep working.
        """
        payload = await self._transport.request(
            method="POST",
            path="/alarm/zones",
            json_body=self._to_json_body(zone),
            allow_retry=False,
        )
        return AlarmZone.model_validate(payload)

    async def update_zone(self, *, zone_id: str, zone: AlarmZone) -> None:
        """Replace a zone's config. Wire: ``PUT /alarm/zones/{id}``. Idempotent."""
        await self._transport.request(
            method="PUT",
            path=f"/alarm/zones/{quote(zone_id, safe='')}",
            json_body=self._to_json_body(zone),
            allow_retry=True,
        )

    async def delete_zone(self, *, zone_id: str) -> None:
        """Delete an alarm zone. Wire: ``DELETE /alarm/zones/{id}``."""
        await self._transport.request(method="DELETE", path=f"/alarm/zones/{quote(zone_id, safe='')}")

    async def list_zone_sensors(self, *, zone_id: str) -> list[AlarmSensor]:
        """List one zone's enrolled sensors. Wire: ``GET /alarm/zones/{id}/sensors``."""
        return await self._request_list(
            method="GET", path=f"/alarm/zones/{quote(zone_id, safe='')}/sensors", model=AlarmSensor
        )

    async def replace_zone_sensors(self, *, zone_id: str, sensors: list[AlarmSensor]) -> None:
        """
        Replace one zone's sensor enrolment wholesale.

        Wire: ``PUT /alarm/zones/{id}/sensors``. Idempotent — the PUT
        carries the full desired set.
        """
        await self._transport.request(
            method="PUT",
            path=f"/alarm/zones/{quote(zone_id, safe='')}/sensors",
            json_body=[self._to_json_body(sensor) for sensor in sensors],
            allow_retry=True,
        )

    async def list_zone_outputs(self, *, zone_id: str) -> list[AlarmOutput]:
        """List one zone's enrolled outputs. Wire: ``GET /alarm/zones/{id}/outputs``."""
        return await self._request_list(
            method="GET", path=f"/alarm/zones/{quote(zone_id, safe='')}/outputs", model=AlarmOutput
        )

    async def replace_zone_outputs(self, *, zone_id: str, outputs: list[AlarmOutput]) -> None:
        """
        Replace one zone's output enrolment wholesale.

        Wire: ``PUT /alarm/zones/{id}/outputs``. Idempotent.
        """
        await self._transport.request(
            method="PUT",
            path=f"/alarm/zones/{quote(zone_id, safe='')}/outputs",
            json_body=[self._to_json_body(output) for output in outputs],
            allow_retry=True,
        )

    # ---- enrollment candidates (setup wizard; daemon ≥ 0.43.0) ----

    async def list_output_candidates(self) -> list[AlarmOutputCandidate]:
        """
        Return the channels that can back each device-backed output class.

        Wire: ``GET /alarm/output-candidates`` — derived from the live
        domain model (incl. ON_TIME-gated switched-siren eligibility)
        with the device's real ENUM value lists + localised labels
        (tones, optical patterns, soundfiles). Since api 3.1.0 each
        candidate also carries the channel's ``rooms`` and ``functions``
        (both optional) so a picker can filter and label without a
        second lookup.
        """
        return await self._request_list(method="GET", path="/alarm/output-candidates", model=AlarmOutputCandidate)

    async def list_remote_key_candidates(self) -> list[AlarmRemoteKeyCandidate]:
        """
        Return the physical remote/wall-button key channels for bindings.

        Wire: ``GET /alarm/remote-key-candidates`` — PRESS_SHORT /
        PRESS_LONG key channels from the live model (virtual remotes
        excluded), for guided keyfob-arming code bindings.
        """
        return await self._request_list(
            method="GET", path="/alarm/remote-key-candidates", model=AlarmRemoteKeyCandidate
        )

    async def list_sensor_candidates(self, *, unenrolled_only: bool = False) -> list[AlarmSensorCandidate]:
        """
        Return the data points a zone can enrol as alarm sensors.

        Wire: ``GET /alarm/sensor-candidates`` (daemon ≥ 0.53.1, api
        5.0.0). Sensor enrollment was the one alarm surface without a
        candidate list — outputs and remote keys had one, sensors were
        unvalidated free text over (central, interface, channel address,
        parameter), so a typo produced a sensor that silently never
        fired.

        Each candidate carries the pre-fill an enrollment needs: the
        suggested role, the hazard class, the parameter's value list and
        ``active_values`` wherever the default "anything but index 0 is
        active" rule would be wrong. ``unenrolled_only`` drops the ones
        already enrolled.
        """
        params = {"enrolled": "false"} if unenrolled_only else None
        return await self._request_list(
            method="GET", path="/alarm/sensor-candidates", params=params, model=AlarmSensorCandidate
        )

    # ---- incidents ----

    async def list_incidents(self, *, zone_id: str, limit: int | None = None) -> list[AlarmIncident]:
        """
        Return one zone's incident history, newest first.

        Wire: ``GET /alarm/incidents`` (daemon ≥ 0.53.1, api 5.0.0).
        ``zone_id`` is required — an incident belongs to exactly one
        zone. ``limit`` defaults to 50 daemon-side and caps at 500.

        An incident is the unit the journal's individual rows belong to:
        it carries every contributing source, the silence attribution
        and why it closed.
        """
        params: dict[str, Any] = {"zone_id": zone_id}
        if limit is not None:
            params["limit"] = limit
        return await self._request_list(method="GET", path="/alarm/incidents", params=params, model=AlarmIncident)

    async def get_incident(self, *, incident_id: int | str) -> AlarmIncident:
        """
        Return one alarm incident with its full source ledger.

        Wire: ``GET /alarm/incidents/{id}``. ``sources`` lists every
        contributing data point oldest first, so "what else went off
        while the alarm ran" is answerable after the fact.
        """
        payload = await self._transport.request(
            method="GET", path=f"/alarm/incidents/{quote(str(incident_id), safe='')}"
        )
        return AlarmIncident.model_validate(payload)

    # ---- verbs ----

    async def arm_zone(
        self,
        *,
        zone_id: str,
        mode: str,
        force: bool | None = None,
        skip_delay: bool | None = None,
        bypass: list[str] | None = None,
        code: str | None = None,
    ) -> AlarmArmAccepted:
        """
        Arm one zone in the given mode.

        Wire: ``POST /alarm/zones/{id}/arm``. ``mode`` is an
        :class:`~openccu_loom_client.wire.enums.AlarmMode` value other than
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
            path=f"/alarm/zones/{quote(zone_id, safe='')}/arm",
            json_body=body,
            allow_retry=False,
        )
        return AlarmArmAccepted.model_validate(payload)

    async def disarm_zone(self, *, zone_id: str, code: str | None = None) -> None:
        """
        Disarm one zone (also ends an active incident).

        Wire: ``POST /alarm/zones/{id}/disarm``. Not retried.
        """
        await self._transport.request(
            method="POST",
            path=f"/alarm/zones/{quote(zone_id, safe='')}/disarm",
            json_body={"code": code} if code is not None else {},
            allow_retry=False,
        )

    async def silence_zone(self, *, zone_id: str, code: str | None = None) -> None:
        """
        Silence one zone's sounding outputs (incident stays open).

        Wire: ``POST /alarm/zones/{id}/silence``. Not retried.
        """
        await self._transport.request(
            method="POST",
            path=f"/alarm/zones/{quote(zone_id, safe='')}/silence",
            json_body={"code": code} if code is not None else {},
            allow_retry=False,
        )

    async def acknowledge_zone(self, *, zone_id: str, code: str | None = None) -> None:
        """
        Acknowledge one zone's ended incident (clears the triggered latch).

        Wire: ``POST /alarm/zones/{id}/acknowledge``. Not retried.
        """
        await self._transport.request(
            method="POST",
            path=f"/alarm/zones/{quote(zone_id, safe='')}/acknowledge",
            json_body={"code": code} if code is not None else {},
            allow_retry=False,
        )

    async def silence_all(self) -> None:
        """
        Silence every sounding output across all zones (break-glass).

        Wire: ``POST /alarm/silence-all``. Not retried.
        """
        await self._transport.request(method="POST", path="/alarm/silence-all", allow_retry=False)

    # ---- motion reset ----

    async def list_triggered_motion(self, *, zone_id: str | None = None) -> list[AlarmTriggeredMotionSensor]:
        """
        Return the latched detectors a motion reset would clear.

        Wire: ``GET /alarm/triggered-motion`` (daemon ≥ 0.58.1, api
        5.17.0). ``zone_id`` restricts the answer to one zone; omit it
        for every zone.

        A motion detector holds its ``MOTION`` flag until the device's
        own blocking time expires or the reset parameter is written.
        While it does, the sensor reads as open and blocks an arm or
        forces an auto-bypass.

        The daemon derives this list from the same predicate the reset
        verbs use — currently active *and* the channel exposes a
        writable reset parameter — so a count shown to an operator can
        never name a detector the reset would skip. Motion detectors
        (``MOTION`` → ``RESET_MOTION``) and, since daemon 0.58.1,
        presence detectors (``PRESENCE_DETECTION_STATE`` →
        ``RESET_PRESENCE``) are covered; door contacts fall out by
        construction. ``parameter`` names the sensor's own state
        parameter, not the reset one.

        Daemon 0.58.0 shipped the route but a type assertion made it
        inert on real hardware, so a client tested against exactly that
        release sees the call succeed and report nothing.
        """
        params = {"zone_id": zone_id} if zone_id is not None else None
        return await self._request_list(
            method="GET", path="/alarm/triggered-motion", params=params, model=AlarmTriggeredMotionSensor
        )

    async def reset_zone_motion(self, *, zone_id: str) -> AlarmMotionResetResult:
        """
        Clear one zone's latched motion/presence detectors.

        Wire: ``POST /alarm/zones/{id}/reset-motion`` (daemon ≥ 0.58.1,
        api 5.17.0). Not retried.

        The counters are why this returns a result rather than ``None``:
        ``reset == 0 and failed == 0`` ("nothing was latched") is a
        different outcome from ``failed > 0`` ("individual detectors did
        not answer"), and only the caller can decide what to tell the
        user. The daemon reports a failing write in the body rather than
        as an HTTP error — the verb ran, and a partial result is
        actionable. Detectors that are not triggered are never written
        to, so a routine call adds no radio traffic.
        """
        payload = await self._transport.request(
            method="POST", path=f"/alarm/zones/{quote(zone_id, safe='')}/reset-motion", allow_retry=False
        )
        return AlarmMotionResetResult.model_validate(payload)

    async def reset_all_motion(self) -> AlarmMotionResetResult:
        """
        Clear every latched motion/presence detector across all zones.

        Wire: ``POST /alarm/reset-motion`` (daemon ≥ 0.58.1, api
        5.17.0). Not retried. Same counter semantics as
        :meth:`reset_zone_motion`.

        Arming already runs this pass for the zone it arms, so the
        explicit verb is for the operator who wants the blockers cleared
        *before* deciding to arm.
        """
        payload = await self._transport.request(method="POST", path="/alarm/reset-motion", allow_retry=False)
        return AlarmMotionResetResult.model_validate(payload)

    # ---- journal ----

    async def list_journal(
        self,
        *,
        zone_id: str | None = None,
        journal_class: str | None = None,
        from_: Any | None = None,
        to: Any | None = None,
        limit: int | None = None,
    ) -> list[AlarmJournalEntry]:
        """
        Return alarm-journal entries, newest first.

        Wire: ``GET /alarm/journal``. Filters: ``zone_id``, the journal
        ``class`` (``arm``/``disarm``/``trigger``/…), an inclusive
        ``from_`` / exclusive ``to`` RFC3339 window and ``limit``
        (daemon default 500, cap 5000).
        """
        params: dict[str, Any] = {}
        if zone_id is not None:
            params["zone"] = zone_id
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

    async def start_walk_test(self, *, zone_id: str) -> None:
        """
        Start a walk test on one (disarmed) zone.

        Wire: ``POST /alarm/zones/{id}/walktest/start``. Not retried —
        a retry after success answers 409 (already active).
        """
        await self._transport.request(
            method="POST",
            path=f"/alarm/zones/{quote(zone_id, safe='')}/walktest/start",
            allow_retry=False,
        )

    async def stop_walk_test(self, *, zone_id: str) -> None:
        """
        Stop a running walk test.

        Wire: ``POST /alarm/zones/{id}/walktest/stop``. Idempotent —
        stopping an inactive test is a no-op.
        """
        await self._transport.request(
            method="POST",
            path=f"/alarm/zones/{quote(zone_id, safe='')}/walktest/stop",
            allow_retry=True,
        )

    async def get_walk_test_status(self, *, zone_id: str) -> AlarmWalkTestStatus:
        """Return the walk-test progress. Wire: ``GET /alarm/zones/{id}/walktest``."""
        payload = await self._transport.request(method="GET", path=f"/alarm/zones/{quote(zone_id, safe='')}/walktest")
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
