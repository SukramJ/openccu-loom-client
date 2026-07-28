# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Alarm-panel domain model — wraps AlarmPanelEntity (+ live zone detail)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from openccu_loom_types.rest import AlarmModeReadiness, AlarmPanelEntity, AlarmZoneStatus

    from openccu_loom_client.store import LoomStore

# The daemon's pseudo-zone id of the aggregate master panel
# (``alarmpanel.MasterZoneID``, wire-stable).
MASTER_ZONE_ID = "master"


def _token(*, value: Any) -> str | None:
    """Return the string token of an enum-or-string wire value (``None`` passes)."""
    if value is None:
        return None
    return str(getattr(value, "value", value))


class AlarmPanel:
    """
    Store-aware wrapper around one alarm-control-panel entity.

    The daemon models the panel as a first-class entity (one per alarm
    zone, plus — with ≥ 2 zones — an aggregate master panel). The
    summary carries the daemon-computed ``unique_id`` and the *HA state
    token* (``disarmed``/``arming``/``pending``/``triggered``/
    ``armed_home``/…), so consumers never re-derive either.

    Real-zone panels additionally hold live zone detail — mode,
    countdown, readiness, incident — seeded from ``GET /alarm/state``
    at bootstrap and updated by the ``alarm.*`` pushes. The auto-named
    payload enums differ per wire schema (``Mode`` vs ``Mode2``, …), so
    the live fields are kept as plain Python values instead of inside
    the wire status model. The master panel aggregates and carries no
    zone detail.
    """

    __slots__ = (
        "_bypassed",
        "_countdown_kind",
        "_countdown_remaining_s",
        "_countdown_total_s",
        "_last_incident_cause",
        "_last_incident_id",
        "_last_incident_sensor",
        "_mode",
        "_readiness",
        "_store",
        "_summary",
        "_walktest_active",
    )

    def __init__(self, *, summary: AlarmPanelEntity, store: LoomStore) -> None:
        """Bind this wrapper to its wire summary and owning store."""
        self._summary = summary
        self._store = store
        self._mode: str | None = None
        self._bypassed: tuple[str, ...] = ()
        self._countdown_kind: str | None = None
        self._countdown_remaining_s: int | None = None
        self._countdown_total_s: int | None = None
        self._readiness: dict[str, AlarmModeReadiness] = {}
        self._walktest_active = False
        self._last_incident_id: int | None = None
        self._last_incident_cause: str | None = None
        self._last_incident_sensor: str | None = None

    @property
    def summary(self) -> AlarmPanelEntity:
        """Return the backing wire summary."""
        return self._summary

    @property
    def unique_id(self) -> str:
        """The daemon-computed stable entity id (``openccu-loom_alarm_<zone>``)."""
        return self._summary.unique_id

    @property
    def zone_id(self) -> str:
        """The alarm zone id, or ``"master"`` for the aggregate panel."""
        return self._summary.zone_id

    @property
    def name(self) -> str:
        """The display name (the zone name; master name is daemon-localised)."""
        return self._summary.name

    @property
    def state(self) -> str:
        """
        The HA alarm state token, daemon-computed.

        One of ``disarmed``/``arming``/``pending``/``triggered``/
        ``armed_home``/``armed_away``/``armed_night``/``armed_vacation``/
        ``armed_custom_bypass``.
        """
        return _token(value=self._summary.state) or ""

    @property
    def available(self) -> bool:
        """The alarm-health verdict for this entity."""
        return self._summary.available

    @property
    def is_master(self) -> bool:
        """Whether this is the aggregate master panel."""
        return bool(self._summary.master)

    @property
    def supported_modes(self) -> tuple[str, ...]:
        """The armable protection modes (``perimeter``/``full``/``night``/…)."""
        return tuple(self._summary.supported_modes or ())

    @property
    def code_arm_required(self) -> bool:
        """
        Effective code requirement for arming, daemon-computed.

        True exactly when the daemon will demand a code: the zone's
        code policy AND an applicable enabled PIN code exists (master
        aggregates any-zone). Live policy edits arrive via
        ``alarm.panel_changed``.
        """
        return bool(self._summary.code_arm_required)

    @property
    def code_disarm_required(self) -> bool:
        """Effective code requirement for disarming (same derivation)."""
        return bool(self._summary.code_disarm_required)

    # ---- live zone detail (real zones only) ----

    @property
    def mode(self) -> str | None:
        """The active protection mode while armed/arming, else ``None``."""
        return self._mode

    @property
    def bypassed(self) -> tuple[str, ...]:
        """Sensor ids bypassed for the current arming cycle."""
        return self._bypassed

    @property
    def countdown_kind(self) -> str | None:
        """``exit``/``entry`` while a countdown runs, else ``None``."""
        return self._countdown_kind

    @property
    def countdown_remaining_s(self) -> int | None:
        """Remaining countdown seconds, ``None`` outside a countdown."""
        return self._countdown_remaining_s

    @property
    def countdown_total_s(self) -> int | None:
        """Total countdown length in seconds, ``None`` outside a countdown."""
        return self._countdown_total_s

    @property
    def readiness(self) -> dict[str, AlarmModeReadiness]:
        """Per-mode readiness (blockers/warnings), empty until seeded."""
        return dict(self._readiness)

    @property
    def walktest_active(self) -> bool:
        """Whether a walk test is running on this zone."""
        return self._walktest_active

    @property
    def last_incident_id(self) -> int | None:
        """The most recent incident id seen via ``alarm.triggered``."""
        return self._last_incident_id

    @property
    def last_incident_cause(self) -> str | None:
        """The most recent trigger cause (machine string)."""
        return self._last_incident_cause

    @property
    def last_incident_sensor(self) -> str | None:
        """The sensor name behind the most recent trigger, if any."""
        return self._last_incident_sensor

    # ---- write-back ----

    async def arm(
        self,
        *,
        mode: str,
        code: str | None = None,
        force: bool | None = None,
        skip_delay: bool | None = None,
        bypass: list[str] | None = None,
    ) -> None:
        """
        Arm this panel's zone (master: every zone configuring the mode).

        The master fan-out mirrors the daemon's MQTT ``MasterArm``
        semantics: zones whose config does not offer ``mode`` are
        skipped silently, the rest are armed best-effort.
        """
        if not self.is_master:
            await self._store.arm_alarm_zone(
                zone_id=self.zone_id, mode=mode, code=code, force=force, skip_delay=skip_delay, bypass=bypass
            )
            return
        for panel in list(self._store.alarm_panels):
            if panel.is_master or mode not in panel.supported_modes:
                continue
            await self._store.arm_alarm_zone(
                zone_id=panel.zone_id, mode=mode, code=code, force=force, skip_delay=skip_delay, bypass=bypass
            )

    async def disarm(self, *, code: str | None = None) -> None:
        """Disarm this panel's zone (master: every zone, best-effort)."""
        if not self.is_master:
            await self._store.disarm_alarm_zone(zone_id=self.zone_id, code=code)
            return
        for panel in list(self._store.alarm_panels):
            if not panel.is_master:
                await self._store.disarm_alarm_zone(zone_id=panel.zone_id, code=code)

    async def silence(self, *, code: str | None = None) -> None:
        """Silence sounding outputs (master: daemon-side silence-all)."""
        if self.is_master:
            await self._store.silence_all_alarm_zones()
            return
        await self._store.silence_alarm_zone(zone_id=self.zone_id, code=code)

    async def acknowledge(self, *, code: str | None = None) -> None:
        """Acknowledge this zone's ended incident (clears the latch)."""
        await self._store.acknowledge_alarm_zone(zone_id=self.zone_id, code=code)

    # ---- store-facing mutation (never rebuild) ----

    def _replace_summary(self, *, summary: AlarmPanelEntity) -> None:
        self._summary = summary

    def _replace_status(self, *, status: AlarmZoneStatus) -> None:
        """Seed the live detail from a full ``AlarmZoneStatus`` snapshot."""
        self._mode = _token(value=status.mode)
        self._bypassed = tuple(status.bypassed or ())
        countdown = status.countdown
        if countdown is not None:
            self._countdown_kind = _token(value=countdown.kind)
            self._countdown_remaining_s = countdown.remaining_s
            self._countdown_total_s = countdown.total_s
        else:
            self._clear_countdown()
        self._readiness = dict(status.readiness or {})
        self._walktest_active = bool(status.walktest_active)

    def _set_mode(self, *, mode: str | None) -> None:
        self._mode = mode

    def _set_countdown(self, *, kind: str | None, remaining_s: int, total_s: int) -> None:
        self._countdown_kind = kind
        self._countdown_remaining_s = remaining_s
        self._countdown_total_s = total_s

    def _clear_countdown(self) -> None:
        self._countdown_kind = None
        self._countdown_remaining_s = None
        self._countdown_total_s = None

    def _set_readiness(self, *, readiness: dict[str, AlarmModeReadiness]) -> None:
        self._readiness = dict(readiness)

    def _set_walktest_active(self, *, active: bool) -> None:
        self._walktest_active = active

    def _record_incident(self, *, incident_id: int, cause: str, sensor_name: str | None) -> None:
        self._last_incident_id = incident_id
        self._last_incident_cause = cause
        self._last_incident_sensor = sensor_name

    def __repr__(self) -> str:
        """Return a debug representation with id, state and availability."""
        return f"AlarmPanel(unique_id={self.unique_id!r}, state={self.state!r}, available={self.available!r})"
