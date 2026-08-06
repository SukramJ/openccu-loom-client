# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
``central.hub_coordinator`` — the hub-singleton + sysvar/program coordinator.

Extracted from ``adapter.py`` (it was ~450 lines / 30% of that file): builds
the categorised sysvar/program data points and the per-central hub singletons
(alarm/service messages, inbox, metrics, connectivity, system-update, add-on
update, install mode), seeds them from the aggregate ``GET /hub/data-points``
call (add-on update from ``GET /system/addon-update``), and routes
the daemon's ``hub.*`` push broadcasts straight onto them
(:meth:`_HubCoordinator.install_push_routing`). ``LoomCentralAdapter`` composes
it as ``central.hub_coordinator``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import logging
from typing import TYPE_CHECKING, Any, Final

from openccu_loom_client.compat.aiohomematic._upstream import (
    DataPointStateChangedEvent as AioDataPointStateChangedEvent,
    EventBus as AioEventBus,
)
from openccu_loom_client.compat.aiohomematic.central.state_paths import parse_sysvar_state_path
from openccu_loom_client.compat.aiohomematic.model.hub.singletons import (
    INSTALL_MODE_TOKEN_BY_INTERFACE,
    AddonUpdateDp,
    AlarmMessagesSensor,
    ConnectionLatencySensor,
    ConnectivityDpType,
    InboxSensor,
    InstallModeDpButton,
    InstallModeDpSensor,
    InstallModeDpType,
    InterfaceConnectivityDp,
    LastEventAgeSensor,
    MetricsDpType,
    SecurityClassDp,
    SecurityFaultsSensor,
    SecurityReportSensor,
    SecuritySeveritySensor,
    ServiceMessagesSensor,
    SystemHealthSensor,
    SystemUpdateDp,
)
from openccu_loom_client.events import (
    AddonUpdateStateChangedEvent,
    HubAlarmMessageCountChangedEvent,
    HubConnectivityChangedEvent,
    HubInboxChangedEvent,
    HubMetricsChangedEvent,
    HubServiceMessageCountChangedEvent,
    HubSystemUpdateChangedEvent,
    InstallModeChangedEvent,
    SecurityClassChangedEvent,
    SecurityFaultChangedEvent,
    SecurityNotificationEvent,
    SecurityStateChangedEvent,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from openccu_loom_types.rest import ProgramSummary, SysvarSummary

    from openccu_loom_client.client import LoomClient
    from openccu_loom_client.events import SubscriptionGroup

_LOGGER: Final = logging.getLogger(__name__)

# Sentinel for "the store did not hold this entry before the fetch". Distinct
# from ``None``, which is a legitimate sysvar value and a legitimate
# not-yet-reported activity flag — both would otherwise read as unchanged.
_UNSET: Final = object()


def _active_hazard_sources(*, snapshot: Any) -> list[Any]:
    """
    Collect the sources of every currently-active class.

    The folded severity says something is wrong; this says what. Without
    it the severity entity is the one surface of the domain that reports
    a verdict with no way back to the detector behind it.
    """
    out: list[Any] = []
    for state in snapshot.classes or ():
        if state.active:
            out.extend(state.sources or ())
    return out


class _HubCoordinator:
    """``central.hub_coordinator`` surface (sysvars, programs, messages, singletons)."""

    def __init__(
        self,
        *,
        client: LoomClient,
        ha_bus: AioEventBus,
    ) -> None:
        self._client = client
        self._ha_bus = ha_bus
        # Per-central hub singletons, built once on the first
        # fetch_hub_singleton_data() (the interface list is needed for
        # the connectivity / install-mode pairs).
        self._singletons_built = False
        self._alarm_messages_dp: AlarmMessagesSensor | None = None
        self._service_messages_dp: ServiceMessagesSensor | None = None
        self._inbox_dp: InboxSensor | None = None
        self._update_dp: SystemUpdateDp | None = None
        self._addon_update_dp: AddonUpdateDp | None = None
        self._metrics_dps: MetricsDpType | None = None
        self._connectivity_dps: dict[str, ConnectivityDpType] = {}
        self._install_mode_dps: dict[str, InstallModeDpType] = {}
        # Maps an interface_id (state.id, as the aggregate / connectivity push
        # use it) to the interface token that keys _install_mode_dps.
        self._install_token_by_id: dict[str, str] = {}
        # Security & Safety singletons. The severity, the fault ledger and
        # the two report sensors are fixed; the per-class binary sensors are
        # built from what the installation actually has sources for — the
        # daemon omits a class it has none of rather than reporting it
        # inactive, so a home without gas detectors gets no gas entity.
        self._security_severity_dp: SecuritySeveritySensor | None = None
        self._security_faults_dp: SecurityFaultsSensor | None = None
        self._security_alarm_report_dp: SecurityReportSensor | None = None
        self._security_fault_report_dp: SecurityReportSensor | None = None
        self._security_class_dps: dict[str, SecurityClassDp] = {}
        # The daemon's entity-name catalogue, read once at singleton build.
        # Kept so a class sensor created later — a newly-paired detector
        # introduces its class mid-session — is named like its siblings
        # instead of falling back to its raw token.
        self._entity_names: dict[str, str] = {}
        # Per-message-list locks so the 300 s reconcile loop and the count
        # push handlers can't interleave a fetch/apply on the same singleton
        # (a slower in-flight fetch would otherwise clobber a newer list).
        self._message_list_locks: dict[int, asyncio.Lock] = {}

    async def set_system_variable(self, *, legacy_name: str, value: Any) -> None:
        await self._client.hub.set_sysvar(name=legacy_name, value=value)

    def get_system_variable(self, *, legacy_name: str) -> Any:
        sysvar = self._client.store.get_sysvar(name=legacy_name)
        return sysvar.value if sysvar is not None else None

    async def fetch_sysvar_data(self, *, scheduled: bool = False) -> None:
        """
        Re-read the sysvar catalogue and re-render what changed.

        Backs Home Assistant's ``fetch_system_variables`` service. Live
        changes arrive as ``hub.sysvar_changed`` pushes, so this is the
        manual path — and it has to announce its results, or the one action
        an operator takes when a value looks stale appears to do nothing.
        """
        store = self._client.store
        changed: list[Any] = []
        for summary in await self._client.hub.list_sysvars():
            before = store.get_sysvar(name=summary.name)
            previous = before.value if before is not None else _UNSET
            store._upsert_sysvar(summary=summary)
            if (dp := store.get_sysvar(name=summary.name)) is not None and dp.value != previous:
                changed.append(dp)
        await self._publish_each(dps=changed)

    async def fetch_program_data(self, *, scheduled: bool = False) -> None:
        """
        Re-read the program catalogue and re-render what changed.

        One event per program, not per twin: the execute button and the
        activity switch share the canonical key, and the button carries no
        ``value`` of its own — the activity flag is what moved, and both
        entities re-read their own view off the refreshed summary.
        """
        store = self._client.store
        now = datetime.now(tz=UTC)
        for summary in await self._client.hub.list_programs():
            before = store.get_program(program_id=summary.id)
            previous = before.summary.active if before is not None else _UNSET
            store._upsert_program(summary=summary)
            if summary.active == previous or not summary.unique_id:
                continue
            await self._ha_bus.publish(
                event=AioDataPointStateChangedEvent(
                    timestamp=now, unique_id=summary.unique_id, new_value=summary.active
                )
            )

    # ---- entity-spawn surface ----

    def _matches_central(self, *, central: str | None) -> bool:
        """
        Return whether a payload's central tag refers to this central.

        Accepts the HA-facing central name (the adapter name) as well as
        the daemon's own central id, since multi-central deployments may
        differ between the two.
        """
        store = self._client.store
        return not central or central in (store.central_name, store.central_id)

    def _is_local(self, *, summary: SysvarSummary | ProgramSummary) -> bool:
        """
        Return whether a sysvar/program belongs to this central.

        The daemon's catalogue spans every configured central; spawning a
        foreign central's variables here would leak entities (with the
        wrong serial in their unique_id) into this HA entry.
        """
        return self._matches_central(central=summary.central)

    @staticmethod
    def _is_excluded_sysvar(summary: SysvarSummary) -> bool:
        """
        Return whether a sysvar never spawns a generic entity.

        Mirrors the reference stack's three hard exclusions: ``${…}``
        template variables and the fixed CCU IDs 40/41 (alarm/service
        messages) back dedicated hub singletons; names carrying the
        ``OldVal``/``pcCCUID`` tokens (hub.py ``_EXCLUDED``) are CCU
        calculation scratch values.
        """
        name = str(summary.name)
        if name.startswith("${"):
            return True
        if any(token in name for token in ("OldVal", "pcCCUID")):
            return True
        return summary.vid in (40, 41)

    def _all_hub_data_points(self) -> list[Any]:
        """
        Return the categorised hub data points for this central.

        The store owns the instances (it builds them through the factories
        installed at start-up), so this only decides which of them become HA
        entities. It used to build and cache them here instead — which meant
        the object Home Assistant held kept its bootstrap summary forever,
        while the store's copy was the one every refresh and push updated.
        Reading through is what keeps a program's availability and a sysvar's
        value alive.
        """
        out: list[Any] = []
        for sysvar in self._client.store.sysvars:
            if not self._is_local(summary=sysvar.summary):
                continue
            if self._is_excluded_sysvar(sysvar.summary):
                continue
            # The daemon already applied the marker + internal inclusion
            # filter and resolved enabled-by-default (api ≥ 1.9.0); render
            # every sysvar it sent.
            out.append(sysvar)
        for program in self._client.store.programs:
            if not self._is_local(summary=program.summary):
                continue
            # Two entities per program: the execute button and the activity
            # switch. They share the canonical key — HA scopes unique_ids per
            # platform — and both read the one summary the store keeps fresh.
            out.extend(self._client.store.program_data_points(program_id=program.id))
        # The hub singletons are stable instances; they simply ride along.
        out.extend(self._hub_singletons())
        return out

    def get_hub_data_points(
        self, *, category: Any = None, registered: bool | None = None, **_kwargs: Any
    ) -> tuple[Any, ...]:
        """Categorised sysvar/program data points, filtered like aiohomematic."""
        out: list[Any] = []
        for dp in self._all_hub_data_points():
            if category is not None and getattr(dp, "category", None) != category:
                continue
            if registered is not None and getattr(dp, "is_registered", False) != registered:
                continue
            out.append(dp)
        return tuple(out)

    def get_sysvar_data_point(self, *, state_path: str) -> Any:
        """Resolve a categorised sysvar data point from its MQTT state path."""
        name = parse_sysvar_state_path(state_path=state_path)
        if name is None:
            return None
        # The store holds the categorised instance; building a fresh one here
        # would hand out a twin that no refresh or push ever reaches.
        return self._client.store.get_sysvar(name=name)

    # ---- hub singletons ----

    @property
    def alarm_messages_dp(self) -> AlarmMessagesSensor | None:
        """Return the alarm-messages singleton (``None`` before bootstrap)."""
        return self._alarm_messages_dp

    @property
    def service_messages_dp(self) -> ServiceMessagesSensor | None:
        """Return the service-messages singleton (``None`` before bootstrap)."""
        return self._service_messages_dp

    @property
    def inbox_dp(self) -> InboxSensor | None:
        """Return the inbox singleton (``None`` before bootstrap)."""
        return self._inbox_dp

    @property
    def update_dp(self) -> SystemUpdateDp | None:
        """Return the system-update singleton (``None`` before bootstrap)."""
        return self._update_dp

    @property
    def addon_update_dp(self) -> AddonUpdateDp | None:
        """Return the add-on-update singleton (``None`` before bootstrap or when unsupported)."""
        return self._addon_update_dp

    @property
    def metrics_dps(self) -> MetricsDpType | None:
        """Return the metrics singleton triple (``None`` before bootstrap)."""
        return self._metrics_dps

    @property
    def connectivity_dps(self) -> dict[str, ConnectivityDpType]:
        """Return the per-interface connectivity sensors, keyed by interface id."""
        return dict(self._connectivity_dps)

    @property
    def install_mode_dps(self) -> dict[str, InstallModeDpType]:
        """Return the per-interface install-mode button/sensor pairs."""
        return dict(self._install_mode_dps)

    def _hub_singletons(self) -> list[Any]:
        """Return the built hub singletons (empty before the first hub fetch)."""
        if not self._singletons_built:
            return []
        return self._hub_singletons_unfiltered()

    async def _ensure_singletons(self) -> None:
        """Build the hub singletons once (needs the daemon's interface list)."""
        if self._singletons_built:
            return
        store = self._client.store
        self._alarm_messages_dp = AlarmMessagesSensor(store=store)
        self._service_messages_dp = ServiceMessagesSensor(store=store)
        self._inbox_dp = InboxSensor(store=store)
        self._update_dp = SystemUpdateDp(store=store, system_ops=self._client.system)
        # The add-on self-updater is capability-gated daemon-side: only
        # platforms with the firmware installer report supported=True, and
        # daemons older than api 3.3.0 answer 404 — both mean "no entity".
        try:
            addon_status = await self._client.system.get_addon_update_status()
        except Exception:
            _LOGGER.debug("addon-update status unavailable while building hub singletons", exc_info=True)
            addon_status = None
        if addon_status is not None and addon_status.supported:
            self._addon_update_dp = AddonUpdateDp(store=store, system_ops=self._client.system)
            self._addon_update_dp.update_status(status=addon_status)
        self._metrics_dps = MetricsDpType(
            system_health=SystemHealthSensor(store=store),
            connection_latency=ConnectionLatencySensor(store=store),
            last_event_age=LastEventAgeSensor(store=store),
        )
        await self._build_security_singletons(store=store)
        try:
            interfaces = await self._client.system.list_interfaces()
        except Exception:
            _LOGGER.debug("interfaces unavailable while building hub singletons", exc_info=True)
            interfaces = []
        for state in interfaces:
            if not self._matches_central(central=state.central_id):
                continue
            self._connectivity_dps[state.id] = ConnectivityDpType(
                interface_id=state.id,
                interface=state.interface,
                sensor=InterfaceConnectivityDp(store=store, interface_id=state.id),
            )
            if state.interface in INSTALL_MODE_TOKEN_BY_INTERFACE:
                sensor = InstallModeDpSensor(store=store, interface=state.interface)
                self._install_mode_dps[state.interface] = InstallModeDpType(
                    button=InstallModeDpButton(
                        store=store,
                        hub_ops=self._client.hub,
                        interface=state.interface,
                        sensor=sensor,
                    ),
                    sensor=sensor,
                )
                self._install_token_by_id[state.id] = state.interface
        await self._apply_entity_names()
        self._singletons_built = True

    async def _build_security_singletons(self, *, store: Any) -> None:
        """
        Build the Security & Safety singletons from the domain snapshot.

        The snapshot decides which class sensors exist: the daemon omits a
        class the installation has no source for rather than reporting it
        inactive, so this never spawns a permanently-off gas alarm for a
        home without gas detectors.

        The domain has no capability token — it runs with or without the
        alarm engine — so "not available" shows up as the daemon serving
        503 for a missing persistence tier, or 404 on a daemon older than
        api 5.0.0. Either way the entities are simply not built.
        """
        try:
            snapshot = await self._client.security.get_snapshot()
        except Exception:
            _LOGGER.debug("security snapshot unavailable while building hub singletons", exc_info=True)
            return
        self._security_severity_dp = SecuritySeveritySensor(store=store)
        self._security_severity_dp.update_severity(
            severity=str(snapshot.severity), sources=_active_hazard_sources(snapshot=snapshot)
        )
        self._security_faults_dp = SecurityFaultsSensor(store=store)
        self._security_faults_dp.update_faults(faults=snapshot.faults or ())
        self._security_alarm_report_dp = SecurityReportSensor(store=store, fault=False)
        self._security_alarm_report_dp.update_report(report=snapshot.last_alarm)
        self._security_fault_report_dp = SecurityReportSensor(store=store, fault=True)
        self._security_fault_report_dp.update_report(report=snapshot.last_fault)
        for state in snapshot.classes or ():
            dp = SecurityClassDp(store=store, security_class=str(state.class_))
            dp.update_class(active=bool(state.active), sources=state.sources)
            self._security_class_dps[str(state.class_)] = dp

    async def fetch_hub_singleton_data(self, *, scheduled: bool = False) -> None:
        """
        Seed/refresh every hub singleton from the aggregate ``GET /hub/data-points``.

        One aggregate call replaces the old per-endpoint fan-out (inbox, metrics,
        connectivity, install-mode); alarm/service carry the count only, so their
        bodies are refetched only when the count moved, and the firmware strings
        come from ``get_system_update``. Live updates between calls ride the push
        routing (:meth:`install_push_routing`); this runs at cold-start and as the
        slow reconcile backstop. Changed singletons get their keyed HA
        state-changed event so the entities re-render.
        """
        del scheduled
        await self._ensure_singletons()
        try:
            aggregate = await self._client.system.get_hub_data_points()
        except Exception:
            _LOGGER.debug("hub data-points aggregate fetch failed", exc_info=True)
            return
        data = next((d for d in aggregate if self._matches_central(central=d.central)), None)
        if data is None:
            return
        changed: list[Any] = []
        changed += self._apply_inbox_count(count=data.inbox.value)
        changed += self._apply_metrics(metrics=data.metrics)
        changed += self._apply_connectivity(entries=data.connectivity)
        changed += self._apply_install_mode(entries=data.install_mode)
        changed += await self._refresh_message_list(
            dp=self._alarm_messages_dp, count=data.alarm_messages.value, fetch=self._client.hub.list_alarm_messages
        )
        changed += await self._refresh_message_list(
            dp=self._service_messages_dp,
            count=data.service_messages.value,
            fetch=self._client.hub.list_service_messages,
        )
        changed += await self._fetch_system_update()
        changed += await self._fetch_addon_update()
        changed += await self._refresh_security()
        await self._publish_each(dps=changed)

    async def _apply_entity_names(self) -> None:
        """
        Adopt the daemon's own names for every hub singleton.

        The daemon is the single naming authority and has named these
        entities in its i18n catalogue since long before this call — but
        the names only ever reached the MQTT discovery plane, so this
        layer kept rendering Home Assistant's copy of the same words.
        Reading them here removes the second copy; each singleton keeps
        its English token as ``name`` because Home Assistant matches its
        entity descriptions against it.

        Best-effort: a daemon older than api 5.2.0 answers 404, and an
        entity whose key the catalogue does not carry keeps its own
        rendering. Neither is an error — both land on the same fallback.
        """
        try:
            # The store carries Home Assistant's own UI language (set from
            # `hass.config.language` at adapter construction). Asking for it
            # rather than letting the daemon answer in its configured locale
            # is the difference between a German dashboard and a German
            # daemon: they are separate choices and often disagree.
            catalogue = await self._client.i18n.get_entity_names(locale=self._client.store.locale)
        except Exception:
            _LOGGER.debug("entity-name catalogue unavailable; keeping local entity names", exc_info=True)
            return
        entries = catalogue.entries or {}
        if not entries:
            return
        self._entity_names = dict(entries)
        for dp in self._hub_singletons_unfiltered():
            dp.apply_entity_names(entries=entries)

    def _hub_singletons_unfiltered(self) -> list[Any]:
        """
        Return the built singletons regardless of the built flag.

        :meth:`_hub_singletons` gates on ``_singletons_built``, which is
        still false while the build runs — naming has to reach the
        objects before that flag flips, or the first announce would carry
        the untranslated tokens.
        """
        singletons: list[Any] = [
            dp
            for dp in (
                self._alarm_messages_dp,
                self._service_messages_dp,
                self._inbox_dp,
                self._update_dp,
                self._addon_update_dp,
                self._security_severity_dp,
                self._security_faults_dp,
                self._security_alarm_report_dp,
                self._security_fault_report_dp,
            )
            if dp is not None
        ]
        if self._metrics_dps is not None:
            singletons.extend(self._metrics_dps)
        singletons.extend(self._security_class_dps.values())
        singletons.extend(entry.sensor for entry in self._connectivity_dps.values())
        for pair in self._install_mode_dps.values():
            singletons.extend((pair.sensor, pair.button))
        return singletons

    async def _refresh_security(self) -> list[Any]:
        """
        Re-read the Security & Safety snapshot; return the changed singletons.

        The push handlers below carry every change as it happens; this is
        the reconcile backstop for a missed frame, and the path that lets
        a class sensor appear when a newly-paired detector gives the
        installation its first source of that class.
        """
        if self._security_severity_dp is None:
            return []
        try:
            snapshot = await self._client.security.get_snapshot()
        except Exception:
            _LOGGER.debug("security snapshot refresh failed", exc_info=True)
            return []
        changed: list[Any] = []
        if self._security_severity_dp.update_severity(
            severity=str(snapshot.severity), sources=_active_hazard_sources(snapshot=snapshot)
        ):
            changed.append(self._security_severity_dp)
        if self._security_faults_dp is not None and self._security_faults_dp.update_faults(
            faults=snapshot.faults or ()
        ):
            changed.append(self._security_faults_dp)
        if self._security_alarm_report_dp is not None and self._security_alarm_report_dp.update_report(
            report=snapshot.last_alarm
        ):
            changed.append(self._security_alarm_report_dp)
        if self._security_fault_report_dp is not None and self._security_fault_report_dp.update_report(
            report=snapshot.last_fault
        ):
            changed.append(self._security_fault_report_dp)
        for state in snapshot.classes or ():
            dp = self._security_class_dp(security_class=str(state.class_))
            if dp is not None and dp.update_class(active=bool(state.active), sources=state.sources):
                changed.append(dp)
        return changed

    def _security_class_dp(self, *, security_class: str) -> SecurityClassDp | None:
        """
        Return the class sensor, building it the first time the class appears.

        A class only enters the snapshot once the installation has a source
        for it, so a newly-paired smoke detector introduces the smoke class
        mid-session. Building it lazily here keeps that first activation
        from landing on nothing; the entity reaches Home Assistant on the
        next announce pass.
        """
        if (dp := self._security_class_dps.get(security_class)) is not None:
            return dp
        if self._security_severity_dp is None:
            # No Security & Safety domain on this daemon at all.
            return None
        dp = SecurityClassDp(store=self._client.store, security_class=security_class)
        if self._entity_names:
            dp.apply_entity_names(entries=self._entity_names)
        self._security_class_dps[security_class] = dp
        return dp

    async def _on_security_state_push(self, event: SecurityStateChangedEvent, /) -> None:
        """Apply a ``security.state_changed`` fold push."""
        dp = self._security_severity_dp
        if dp is not None and dp.update_severity(severity=str(event.payload.severity)):
            await self._publish_changed(dp=dp)

    async def _on_security_class_push(self, event: SecurityClassChangedEvent, /) -> None:
        """Apply a ``security.class_changed`` push onto the class binary sensor."""
        dp = self._security_class_dp(security_class=str(event.payload.class_))
        if dp is not None and dp.update_class(active=bool(event.payload.active), sources=event.payload.sources):
            await self._publish_changed(dp=dp)

    async def _on_security_fault_push(self, event: SecurityFaultChangedEvent, /) -> None:
        """
        Apply a ``security.fault_changed`` push.

        The broadcast carries the standing count after the change, so the
        count entity needs no second read. The per-fault attributes do
        come from a read: a fault line carries an attribution the delta
        does not.
        """
        dp = self._security_faults_dp
        if dp is None:
            return
        try:
            faults = await self._client.security.list_faults()
        except Exception:
            _LOGGER.debug("fault ledger refetch failed after a fault push", exc_info=True)
            if dp.update_value(value=event.payload.open_count, attributes=dp.attributes):
                await self._publish_changed(dp=dp)
            return
        if dp.update_faults(faults=faults):
            await self._publish_changed(dp=dp)

    async def _on_security_notification_push(self, event: SecurityNotificationEvent, /) -> None:
        """
        Apply a ``security.notification`` push onto the matching report sensor.

        A covert report never arrives here: the daemon gates it off the
        WebSocket unless the operator chose ``duress_visibility: full``,
        exactly as it gates its own retained state.
        """
        dp = self._security_fault_report_dp if event.payload.fault else self._security_alarm_report_dp
        if dp is not None and dp.update_report(report=event.payload):
            await self._publish_changed(dp=dp)

    async def _publish_each(self, *, dps: list[Any]) -> None:
        """Emit a keyed HA state-changed event for each changed singleton."""
        now = datetime.now(tz=UTC)
        for dp in dps:
            await self._ha_bus.publish(
                event=AioDataPointStateChangedEvent(timestamp=now, unique_id=dp.unique_id, new_value=dp.value)
            )

    def _apply_inbox_count(self, *, count: int) -> list[Any]:
        """Apply the inbox count to its singleton; return it if it changed."""
        dp = self._inbox_dp
        return [dp] if dp is not None and dp.update_value(value=count) else []

    def _apply_metrics(self, *, metrics: list[Any] | None) -> list[Any]:
        """Apply metric values to the matching sensors, keyed by ``legacy_name``."""
        changed: list[Any] = []
        for metric in metrics or ():
            dp = self._metric_sensor_for(metric=metric.legacy_name)
            if dp is not None and dp.update_value(value=metric.value):
                changed.append(dp)
        return changed

    def _apply_connectivity(self, *, entries: list[Any] | None) -> list[Any]:
        """Apply per-interface reachability to the connectivity sensors."""
        changed: list[Any] = []
        for entry in entries or ():
            pair = self._connectivity_dps.get(entry.interface_id)
            if pair is not None and pair.sensor.update_value(value=bool(entry.reachable)):
                changed.append(pair.sensor)
        return changed

    def _apply_install_mode(self, *, entries: list[Any] | None) -> list[Any]:
        """Apply per-interface install-mode countdown (remaining seconds, 0 when off)."""
        changed: list[Any] = []
        for entry in entries or ():
            pair = self._install_pair_for(interface_id=entry.interface_id)
            if pair is not None and pair.sensor.update_value(value=entry.remaining_s if entry.enabled else 0):
                changed.append(pair.sensor)
        return changed

    def _install_pair_for(self, *, interface_id: str) -> InstallModeDpType | None:
        """Resolve an install-mode pair from an interface_id (aggregate / push keying)."""
        token = self._install_token_by_id.get(interface_id)
        return self._install_mode_dps.get(token) if token is not None else None

    async def _refresh_message_list(
        self, *, dp: Any, count: int, fetch: Callable[[], Awaitable[list[Any]]]
    ) -> list[Any]:
        """Refetch a message list only when the count moved; return it if it changed."""
        if dp is None or count == dp.value:
            return []
        lock = self._message_list_locks.setdefault(id(dp), asyncio.Lock())
        async with lock:
            # Re-check under the lock: while we waited, a concurrent refresh
            # (reconcile loop vs. count push) may already have applied a list
            # for this count. Comparing against the count this fetch was
            # issued for — not the fetched length — collapses the redundant
            # refetch and prevents an older in-flight list from clobbering it.
            if count == dp.value:
                return []
            try:
                messages = await fetch()
            except Exception:
                _LOGGER.debug("message-list refetch failed", exc_info=True)
                return []
            local = [m for m in messages if self._matches_central(central=getattr(m, "central", None))]
            return [dp] if dp.update_messages(messages=local) else []

    async def _fetch_system_update(self) -> list[Any]:
        """Refresh the system-update singleton (full entry incl. firmware strings)."""
        if (update_dp := self._update_dp) is None:
            return []
        try:
            entries = await self._client.system.get_system_update()
        except Exception:
            _LOGGER.debug("system-update fetch failed", exc_info=True)
            return []
        entry = next((e for e in entries if self._matches_central(central=e.central)), None)
        if entry is None:
            return []
        return [update_dp] if update_dp.update_data(entry=entry) else []

    async def _fetch_addon_update(self) -> list[Any]:
        """Refresh the add-on-update singleton (reconcile backstop for missed pushes)."""
        if (addon_dp := self._addon_update_dp) is None:
            return []
        try:
            status = await self._client.system.get_addon_update_status()
        except Exception:
            _LOGGER.debug("addon-update fetch failed", exc_info=True)
            return []
        return [addon_dp] if addon_dp.update_status(status=status) else []

    # ---- live push routing (G6) ----

    def install_push_routing(self, *, group: SubscriptionGroup) -> None:
        """
        Subscribe the hub-singleton push handlers on the loom event bus.

        Routes the daemon's ``hub.*`` / ``connectivity.changed`` broadcasts
        straight onto the singleton ``update_*`` setters so the entities stay
        live without polling. Count topics carry only a count; the message
        lists are refetched lazily, and only when the count actually moved. The
        install-mode push is central-wide (no interface_id), so it applies to
        every interface sensor of this central. Subscribed on the supplied group
        so a single ``group.cancel()`` tears them down. ``system_update`` gained
        its own ``hub.system_update_changed`` broadcast in daemon api 1.19.0
        (openccu-loom v0.9.1) and now routes live like the rest; the slow
        reconcile loop is kept purely as a general missed-push backstop.
        """
        group.subscribe(event_type=HubInboxChangedEvent, handler=self._on_inbox_push)
        group.subscribe(event_type=HubMetricsChangedEvent, handler=self._on_metrics_push)
        group.subscribe(event_type=HubConnectivityChangedEvent, handler=self._on_connectivity_push)
        group.subscribe(event_type=HubAlarmMessageCountChangedEvent, handler=self._on_alarm_push)
        group.subscribe(event_type=HubServiceMessageCountChangedEvent, handler=self._on_service_push)
        group.subscribe(event_type=HubSystemUpdateChangedEvent, handler=self._on_system_update_push)
        group.subscribe(event_type=AddonUpdateStateChangedEvent, handler=self._on_addon_update_push)
        group.subscribe(event_type=InstallModeChangedEvent, handler=self._on_install_mode_push)
        # The Security & Safety plane (daemon ≥ 0.54.0 / api 5.1.0). Before it
        # existed the domain had no push at all, so a smoke alarm reached a
        # consumer whenever it next happened to read GET /security.
        group.subscribe(event_type=SecurityStateChangedEvent, handler=self._on_security_state_push)
        group.subscribe(event_type=SecurityClassChangedEvent, handler=self._on_security_class_push)
        group.subscribe(event_type=SecurityFaultChangedEvent, handler=self._on_security_fault_push)
        group.subscribe(event_type=SecurityNotificationEvent, handler=self._on_security_notification_push)

    async def _publish_changed(self, *, dp: Any) -> None:
        """Emit the keyed HA state-changed event for a single mutated singleton."""
        await self._publish_each(dps=[dp])

    def _metric_sensor_for(self, *, metric: str) -> Any | None:
        """Map a metric legacy-name (either spelling) to its sensor."""
        if (metrics := self._metrics_dps) is None:
            return None
        return {
            "system_health": metrics.system_health,
            "connection_latency": metrics.connection_latency,
            "connection_latency_ms": metrics.connection_latency,
            "last_event_age": metrics.last_event_age,
            "last_event_age_seconds": metrics.last_event_age,
        }.get(metric)

    async def _on_inbox_push(self, event: HubInboxChangedEvent, /) -> None:
        """Apply an ``hub.inbox_changed`` count push."""
        if self._matches_central(central=event.payload.central):
            await self._publish_each(dps=self._apply_inbox_count(count=event.payload.count))

    async def _on_metrics_push(self, event: HubMetricsChangedEvent, /) -> None:
        """Apply an ``hub.metrics_changed`` push to the matching metric sensor."""
        if not self._matches_central(central=event.payload.central):
            return
        dp = self._metric_sensor_for(metric=event.payload.metric)
        if dp is not None and dp.update_value(value=event.payload.value):
            await self._publish_changed(dp=dp)

    async def _on_connectivity_push(self, event: HubConnectivityChangedEvent, /) -> None:
        """Apply a ``connectivity.changed`` push to the per-interface sensor."""
        if self._matches_central(central=event.payload.central):
            await self._publish_each(dps=self._apply_connectivity(entries=[event.payload]))

    async def _on_system_update_push(self, event: HubSystemUpdateChangedEvent, /) -> None:
        """Apply a ``hub.system_update_changed`` push to the system-update singleton."""
        if not self._matches_central(central=event.payload.central):
            return
        if (update_dp := self._update_dp) is not None and update_dp.update_from_push(payload=event.payload):
            await self._publish_changed(dp=update_dp)

    async def _on_addon_update_push(self, event: AddonUpdateStateChangedEvent, /) -> None:
        """Apply an ``addon_update.state_changed`` push (daemon-global, no central tag)."""
        if (addon_dp := self._addon_update_dp) is not None and addon_dp.update_status(status=event.payload):
            await self._publish_changed(dp=addon_dp)

    async def _on_alarm_push(self, event: HubAlarmMessageCountChangedEvent, /) -> None:
        """Apply an ``hub.alarm_message`` count push (refetch the list on a count delta)."""
        if self._matches_central(central=event.payload.central):
            await self._publish_each(
                dps=await self._refresh_message_list(
                    dp=self._alarm_messages_dp, count=event.payload.count, fetch=self._client.hub.list_alarm_messages
                )
            )

    async def _on_service_push(self, event: HubServiceMessageCountChangedEvent, /) -> None:
        """Apply an ``hub.service_message`` count push (refetch the list on a count delta)."""
        if self._matches_central(central=event.payload.central):
            await self._publish_each(
                dps=await self._refresh_message_list(
                    dp=self._service_messages_dp,
                    count=event.payload.count,
                    fetch=self._client.hub.list_service_messages,
                )
            )

    async def _on_install_mode_push(self, event: InstallModeChangedEvent, /) -> None:
        """Apply a central-wide ``hub.install_mode_changed`` push to every interface sensor."""
        if not self._matches_central(central=event.payload.central):
            return
        value = event.payload.remaining_s if event.payload.enabled else 0
        changed = [pair.sensor for pair in self._install_mode_dps.values() if pair.sensor.update_value(value=value)]
        await self._publish_each(dps=changed)
