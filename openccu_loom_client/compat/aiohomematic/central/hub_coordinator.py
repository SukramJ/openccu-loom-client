# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
``central.hub_coordinator`` — the hub-singleton + sysvar/program coordinator.

Extracted from ``adapter.py`` (it was ~450 lines / 30% of that file): builds
the categorised sysvar/program data points and the per-central hub singletons
(alarm/service messages, inbox, metrics, connectivity, system-update, install
mode), seeds them from the aggregate ``GET /hub/data-points`` call, and routes
the daemon's ``hub.*`` push broadcasts straight onto them
(:meth:`_HubCoordinator.install_push_routing`). ``LoomCentralAdapter`` composes
it as ``central.hub_coordinator``.
"""

from __future__ import annotations

from datetime import UTC, datetime
import logging
from typing import TYPE_CHECKING, Any, Final

from openccu_loom_client.compat.aiohomematic._upstream import (
    DataPointStateChangedEvent as AioDataPointStateChangedEvent,
    EventBus as AioEventBus,
)
from openccu_loom_client.compat.aiohomematic.central.state_paths import parse_sysvar_state_path
from openccu_loom_client.compat.aiohomematic.model.hub import make_program_data_points, make_sysvar_data_point
from openccu_loom_client.compat.aiohomematic.model.hub.singletons import (
    INSTALL_MODE_TOKEN_BY_INTERFACE,
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
    ServiceMessagesSensor,
    SystemHealthSensor,
    SystemUpdateDp,
)
from openccu_loom_client.events import (
    HubAlarmMessageCountChangedEvent,
    HubConnectivityChangedEvent,
    HubInboxChangedEvent,
    HubMetricsChangedEvent,
    HubServiceMessageCountChangedEvent,
    HubSystemUpdateChangedEvent,
    InstallModeChangedEvent,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from openccu_loom_types.rest import ProgramSummary, SysvarSummary

    from openccu_loom_client.client import LoomClient
    from openccu_loom_client.events import SubscriptionGroup

_LOGGER: Final = logging.getLogger(__name__)


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
        # Cache hub data points by unique_id so register()/unregister()
        # bookkeeping survives repeated get_hub_data_points() scans.
        self._cache: dict[str, Any] = {}
        # Per-central hub singletons, built once on the first
        # fetch_hub_singleton_data() (the interface list is needed for
        # the connectivity / install-mode pairs).
        self._singletons_built = False
        self._alarm_messages_dp: AlarmMessagesSensor | None = None
        self._service_messages_dp: ServiceMessagesSensor | None = None
        self._inbox_dp: InboxSensor | None = None
        self._update_dp: SystemUpdateDp | None = None
        self._metrics_dps: MetricsDpType | None = None
        self._connectivity_dps: dict[str, ConnectivityDpType] = {}
        self._install_mode_dps: dict[str, InstallModeDpType] = {}
        # Maps an interface_id (state.id, as the aggregate / connectivity push
        # use it) to the interface token that keys _install_mode_dps.
        self._install_token_by_id: dict[str, str] = {}

    async def set_system_variable(self, *, legacy_name: str, value: Any) -> None:
        await self._client.hub.set_sysvar(name=legacy_name, value=value)

    def get_system_variable(self, *, legacy_name: str) -> Any:
        sysvar = self._client.store.get_sysvar(name=legacy_name)
        return sysvar.value if sysvar is not None else None

    async def fetch_sysvar_data(self, *, scheduled: bool = False) -> None:
        for summary in await self._client.hub.list_sysvars():
            self._client.store._upsert_sysvar(summary=summary)

    async def fetch_program_data(self, *, scheduled: bool = False) -> None:
        for summary in await self._client.hub.list_programs():
            self._client.store._upsert_program(summary=summary)

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
        """Build (and cache) categorised hub data points from the store."""
        live: dict[str, Any] = {}
        for sysvar in self._client.store.sysvars:
            if not self._is_local(summary=sysvar.summary):
                continue
            if self._is_excluded_sysvar(sysvar.summary):
                continue
            # The daemon already applied the marker + internal inclusion
            # filter and resolved enabled-by-default (api ≥ 1.9.0); render
            # every sysvar it sent and read the flag from the wire (absent →
            # disabled by default on older daemons).
            sv_dp: Any = make_sysvar_data_point(
                summary=sysvar.summary,
                store=self._client.store,
                enabled_default=bool(sysvar.summary.enabled_default),
            )
            live[sv_dp.unique_id] = sv_dp
        for program in self._client.store.programs:
            if not self._is_local(summary=program.summary):
                continue
            for pr_dp in make_program_data_points(
                summary=program.summary,
                store=self._client.store,
                enabled_default=bool(program.summary.enabled_default),
            ):
                # Button and switch share the canonical key; HA scopes
                # unique_ids per platform, the cache needs both.
                live[f"{pr_dp.category}:{pr_dp.unique_id}"] = pr_dp
        # Reuse cached instances (preserving their registered flag) and
        # drop entries whose sysvar/program disappeared.
        for uid, dp in live.items():
            if uid not in self._cache:
                self._cache[uid] = dp
        for uid in list(self._cache):
            if uid not in live:
                del self._cache[uid]
        # The hub singletons are stable instances; they simply ride along.
        return [*self._cache.values(), *self._hub_singletons()]

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
        sysvar = self._client.store.get_sysvar(name=name)
        if sysvar is None:
            return None
        return make_sysvar_data_point(summary=sysvar.summary, store=self._client.store)

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
        singletons: list[Any] = [
            dp
            for dp in (
                self._alarm_messages_dp,
                self._service_messages_dp,
                self._inbox_dp,
                self._update_dp,
            )
            if dp is not None
        ]
        if self._metrics_dps is not None:
            singletons.extend(self._metrics_dps)
        singletons.extend(entry.sensor for entry in self._connectivity_dps.values())
        for pair in self._install_mode_dps.values():
            singletons.extend((pair.sensor, pair.button))
        return singletons

    async def _ensure_singletons(self) -> None:
        """Build the hub singletons once (needs the daemon's interface list)."""
        if self._singletons_built:
            return
        store = self._client.store
        self._alarm_messages_dp = AlarmMessagesSensor(store=store)
        self._service_messages_dp = ServiceMessagesSensor(store=store)
        self._inbox_dp = InboxSensor(store=store)
        self._update_dp = SystemUpdateDp(store=store, system_ops=self._client.system)
        self._metrics_dps = MetricsDpType(
            system_health=SystemHealthSensor(store=store),
            connection_latency=ConnectionLatencySensor(store=store),
            last_event_age=LastEventAgeSensor(store=store),
        )
        try:
            interfaces = await self._client.system.list_interfaces()
        except Exception:  # noqa: BLE001 — interfaces endpoint is optional
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
        self._singletons_built = True

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
        except Exception:  # noqa: BLE001 — endpoint optional, keep last values
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
        await self._publish_each(dps=changed)

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
        try:
            messages = await fetch()
        except Exception:  # noqa: BLE001 — endpoint optional, keep last value
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
        except Exception:  # noqa: BLE001 — endpoint optional, keep last value
            _LOGGER.debug("system-update fetch failed", exc_info=True)
            return []
        entry = next((e for e in entries if self._matches_central(central=e.central)), None)
        if entry is None:
            return []
        return [update_dp] if update_dp.update_data(entry=entry) else []

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
        group.subscribe(event_type=InstallModeChangedEvent, handler=self._on_install_mode_push)

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
