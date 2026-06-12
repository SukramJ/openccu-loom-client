# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
``aiohomematic.CentralUnit`` adapter backed by :class:`LoomClient`.

``homematicip_local`` does not just hold a ``CentralUnit`` reference —
it reaches into a coordinator surface (``central.device_coordinator``,
``central.hub_coordinator``, ``central.query_facade``,
``central.client_coordinator``, ``central.cache_coordinator``,
``central.json_rpc_client``, ``central.link``, …). This module presents
that surface on top of the daemon-mediated :class:`LoomClient`, so the
component can run against an openccu-loom daemon with the same call
sites it uses for the direct-CCU aiohomematic backend.

Scope of this adapter:

* lifecycle (``start``/``stop``), identity
  (``name``/``model``/``version``/``url``/``state``/``available``/
  ``system_information``/``health``), the event bus, and the *action*
  coordinators (device lookup/removal, sysvar/program fetch + write,
  links, service/alarm messages + ack, inbox accept, rename, paramset
  read, backup, values-cache clear, un-ignore candidates).

* the entity-spawn surface on aiohomematic's categorized data-point
  model: ``query_facade.get_data_points`` (generic + custom + the
  adapter-built week-profile / schedule-switch / combined-duration
  data points), ``hub_coordinator.get_hub_data_points`` (sysvars,
  programs and the hub singletons: alarm/service messages, inbox,
  metrics, connectivity, system update, install mode),
  ``get_event_groups`` and ``get_state_paths``. The hub singletons are
  polled every 30 s via ``fetch_hub_singleton_data``.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
import logging
from typing import TYPE_CHECKING, Any, Final

from aiohomematic.async_support import Looper
from aiohomematic.central.events import (
    DataPointsCreatedEvent as AioDataPointsCreatedEvent,
    DataPointStateChangedEvent as AioDataPointStateChangedEvent,
    EventBus as AioEventBus,
)
from aiohomematic.const import DataPointCategory as AioDataPointCategory
from openccu_loom_types.enums import CentralState, DataPointCategory

from openccu_loom_client.compat.aiohomematic.central.configurable_devices import (
    ConfigurableDevice,
    build_configurable_devices,
)
from openccu_loom_client.compat.aiohomematic.central.refresh import install_refresh_bridge
from openccu_loom_client.compat.aiohomematic.central.state_paths import (
    device_state_path,
    parse_device_state_path,
    parse_sysvar_state_path,
)
from openccu_loom_client.compat.aiohomematic.const import SystemInformation
from openccu_loom_client.compat.aiohomematic.model.calculated import make_calculated_data_point
from openccu_loom_client.compat.aiohomematic.model.combined import (
    CombinedDurationDp,
    channel_has_duration_pair,
)
from openccu_loom_client.compat.aiohomematic.model.custom import make_custom_data_point
from openccu_loom_client.compat.aiohomematic.model.event_group import build_event_groups
from openccu_loom_client.compat.aiohomematic.model.generic import make_generic_data_point
from openccu_loom_client.compat.aiohomematic.model.hub import (
    make_program_data_points,
    make_sysvar_data_point,
    resolve_hub_inclusion,
)
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
from openccu_loom_client.compat.aiohomematic.model.update import make_update_data_point
from openccu_loom_client.compat.aiohomematic.model.week_profile import (
    ScheduleChannelSwitch,
    WeekProfileDp,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from openccu_loom_client.client import LoomClient
    from openccu_loom_client.events import EventBus
    from openccu_loom_client.model import Device

_LOGGER: Final = logging.getLogger(__name__)

# Cadence of the hub-singleton poll (messages, inbox, metrics, system
# update, install mode, connectivity) — matches aiohomematic's hub
# data-fetch interval.
_HUB_REFRESH_INTERVAL: Final = 30

# Channel types owning a device's weekly program end in this suffix.
_WEEK_PROFILE_CHANNEL_SUFFIX: Final = "WEEK_PROFILE"

# The daemon ships a calculated DURATION sensor; the ccu twin covers the
# DURATION_VALUE/DURATION_UNIT pair with a combined number instead, so
# the calculated flavour is suppressed to avoid a surplus entity.
_SUPPRESSED_CALCULATED_NAMES: Final = frozenset({"DURATION"})


def _category_for_type(data_point_type: Any) -> DataPointCategory | None:
    """
    Map a coarse ``DataPointType`` (platform) to its custom-DP category.

    Custom platforms (light/cover/climate/lock/siren/valve) request data
    points by ``data_point_type`` only; the type's name matches the
    custom ``DataPointCategory`` member 1:1 (``Light`` → ``Light`` …).
    Returns ``None`` for an unset type or one with no category twin.
    """
    if data_point_type is None:
        return None
    name = getattr(data_point_type, "name", None)
    if name is None:
        return None
    return DataPointCategory.__members__.get(name)


# Usage verdicts that never spawn an HA entity. The daemon pipeline
# computes the full aiohomematic visibility model (forced sensors,
# un-ignore, HIDDEN_PARAMETERS, custom-DP absorption, click events) and
# ships the verdict on DataPointSummary.usage — the same gate the MQTT
# discovery plane applies. "event" covers physical devices' PRESS_*
# parameters: they surface through keypress event groups, never as
# generic button entities (virtual remotes report data_point and keep
# their buttons).
_NON_CREATABLE_USAGES: Final = frozenset({"no_create", "ignored", "event"})


def _is_creatable(dp: Any) -> bool:
    """Return whether the DP's usage verdict allows an HA entity."""
    usage = getattr(getattr(dp, "summary", None), "usage", None)
    return usage not in _NON_CREATABLE_USAGES


class _DeviceCoordinator:
    """``central.device_coordinator`` surface."""

    def __init__(self, client: LoomClient) -> None:
        self._client = client

    def get_device(self, *, address: str) -> Device | None:
        return self._client.store.get_device(address=address)

    @property
    def devices(self) -> Iterable[Device]:
        return self._client.store.devices

    async def delete_device(self, *, address: str) -> None:
        await self._client.devices.delete_device(address=address)

    async def refresh_firmware_data(self) -> None:
        # The daemon owns the firmware cache; a global re-pull is the
        # closest equivalent to aiohomematic's per-central refresh.
        await self._client.devices.refresh_all()

    async def create_central_links(self, *, address: str) -> None:
        await self._client.links.enable_central_links(address=address)

    async def remove_central_links(self, *, address: str) -> None:
        await self._client.links.disable_central_links(address=address)

    def get_virtual_remotes(self) -> tuple[Any, ...]:
        # aiohomematic synthesises virtual-remote devices; the daemon
        # exposes them as ordinary devices, so there is no separate list.
        return ()

    async def add_new_devices_manually(
        self,
        *,
        interface_id: str | None = None,
        address_names: dict[str, str] | None = None,
        **_kwargs: Any,
    ) -> None:
        """
        Confirm devices HA discovered — a no-op for loom plus any rename.

        Device creation is daemon-driven: the addresses HA passes are
        already in the store (broadcast as ``device.created``), so there
        is nothing to add. Any non-empty name supplied alongside is
        applied via ``PATCH /devices/{addr}``.
        """
        for address, name in (address_names or {}).items():
            if name:
                await self._client.devices.patch_device(address=address, name=name)


class _HubCoordinator:
    """``central.hub_coordinator`` surface (sysvars, programs, messages, singletons)."""

    def __init__(
        self,
        client: LoomClient,
        *,
        ha_bus: AioEventBus,
        sysvar_markers: tuple[str, ...] = (),
        program_markers: tuple[str, ...] = (),
    ) -> None:
        self._client = client
        self._ha_bus = ha_bus
        self._sysvar_markers = sysvar_markers
        self._program_markers = program_markers
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

    async def set_system_variable(self, *, legacy_name: str, value: Any) -> None:
        await self._client.hub.set_sysvar(name=legacy_name, value=value)

    def get_system_variable(self, *, legacy_name: str) -> Any:
        sysvar = self._client.store.get_sysvar(name=legacy_name)
        return sysvar.value if sysvar is not None else None

    async def fetch_sysvar_data(self, *, scheduled: bool = False) -> None:
        for summary in await self._client.hub.list_sysvars():
            self._client.store._upsert_sysvar(summary)

    async def fetch_program_data(self, *, scheduled: bool = False) -> None:
        for summary in await self._client.hub.list_programs():
            self._client.store._upsert_program(summary)

    # ---- entity-spawn surface ----

    def _matches_central(self, central: str | None) -> bool:
        """
        Return whether a payload's central tag refers to this central.

        Accepts the HA-facing central name (the adapter name) as well as
        the daemon's own central id, since multi-central deployments may
        differ between the two.
        """
        store = self._client.store
        return not central or central in (store.central_name, store.central_id)

    def _is_local(self, summary: Any) -> bool:
        """
        Return whether a sysvar/program belongs to this central.

        The daemon's catalogue spans every configured central; spawning a
        foreign central's variables here would leak entities (with the
        wrong serial in their unique_id) into this HA entry.
        """
        return self._matches_central(getattr(summary, "central", None))

    @staticmethod
    def _is_internal(summary: Any) -> bool:
        """
        Return whether a sysvar is CCU-internal.

        Prefers the wire flag (``is_internal`` from SysVar.getAll);
        falls back to the ``${…}`` name heuristic for daemons that do
        not ship it yet. aiohomematic surfaces internals through
        dedicated hub singletons, never as generic sysvar entities.
        """
        if getattr(summary, "is_internal", None):
            return True
        return str(getattr(summary, "name", "")).startswith("${")

    @staticmethod
    def _is_excluded_sysvar(summary: Any) -> bool:
        """
        Return whether a sysvar never spawns a generic entity.

        Mirrors the reference stack's three hard exclusions: ``${…}``
        template variables and the fixed CCU IDs 40/41 (alarm/service
        messages) back dedicated hub singletons; names carrying the
        ``OldVal``/``pcCCUID`` tokens (hub.py ``_EXCLUDED``) are CCU
        calculation scratch values.
        """
        name = str(getattr(summary, "name", ""))
        if name.startswith("${"):
            return True
        if any(token in name for token in ("OldVal", "pcCCUID")):
            return True
        return getattr(summary, "vid", None) in (40, 41)

    def _all_hub_data_points(self) -> list[Any]:
        """Build (and cache) categorised hub data points from the store."""
        live: dict[str, Any] = {}
        for sysvar in self._client.store.sysvars:
            if not self._is_local(sysvar.summary):
                continue
            if self._is_excluded_sysvar(sysvar.summary):
                continue
            include, enabled = resolve_hub_inclusion(
                name=sysvar.summary.name,
                description=getattr(sysvar.summary, "description", None),
                is_internal=self._is_internal(sysvar.summary),
                markers=self._sysvar_markers,
                # aiohomematic includes internal sysvars by default
                # (DEFAULT_INCLUDE_INTERNAL_SYSVARS=True): CCU bookkeeping
                # variables (svEnergyCounter_…, CCU-Reboot, …) spawn
                # disabled-by-default, exactly like the ccu twin.
                include_internal_default=True,
            )
            if not include:
                continue
            sv_dp: Any = make_sysvar_data_point(
                summary=sysvar.summary, store=self._client.store, enabled_default=enabled
            )
            live[sv_dp.unique_id] = sv_dp
        for program in self._client.store.programs:
            if not self._is_local(program.summary):
                continue
            include, enabled = resolve_hub_inclusion(
                name=program.summary.name,
                description=getattr(program.summary, "description", None),
                is_internal=bool(getattr(program.summary, "is_internal", False)),
                markers=self._program_markers,
                # DEFAULT_INCLUDE_INTERNAL_PROGRAMS is False — CCU-internal
                # helper programs (prgEnergyCounter-…) never spawn.
                include_internal_default=False,
            )
            if not include:
                continue
            for pr_dp in make_program_data_points(
                summary=program.summary, store=self._client.store, enabled_default=enabled
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
        name = parse_sysvar_state_path(state_path)
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
            if not self._matches_central(state.central_id):
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
        self._singletons_built = True

    async def fetch_hub_singleton_data(self, *, scheduled: bool = False) -> None:
        """
        Build (once) and refresh every hub singleton from the daemon.

        Each endpoint is polled independently and failures degrade to the
        previous value; changed singletons get their keyed HA
        state-changed event so the entities re-render.
        """
        del scheduled
        await self._ensure_singletons()
        changed: list[Any] = []
        changed.extend(await self._fetch_messages())
        changed.extend(await self._fetch_inbox())
        changed.extend(await self._fetch_metrics())
        changed.extend(await self._fetch_system_update())
        changed.extend(await self._fetch_install_mode())
        changed.extend(await self._fetch_connectivity())
        now = datetime.now(tz=UTC)
        for dp in changed:
            await self._ha_bus.publish(
                event=AioDataPointStateChangedEvent(
                    timestamp=now, unique_id=dp.unique_id, new_value=dp.value
                )
            )

    async def _fetch_messages(self) -> list[Any]:
        """Refresh the alarm/service message singletons."""
        changed: list[Any] = []
        if (alarm_dp := self._alarm_messages_dp) is not None:
            try:
                alarms = await self._client.hub.list_alarm_messages()
            except Exception:  # noqa: BLE001 — endpoint optional, keep last value
                _LOGGER.debug("alarm-messages fetch failed", exc_info=True)
            else:
                local_alarms = [
                    m for m in alarms if self._matches_central(getattr(m, "central", None))
                ]
                if alarm_dp.update_messages(messages=local_alarms):
                    changed.append(alarm_dp)
        if (service_dp := self._service_messages_dp) is not None:
            try:
                services = await self._client.hub.list_service_messages()
            except Exception:  # noqa: BLE001 — endpoint optional, keep last value
                _LOGGER.debug("service-messages fetch failed", exc_info=True)
            else:
                local_services = [
                    m for m in services if self._matches_central(getattr(m, "central", None))
                ]
                if service_dp.update_messages(messages=local_services):
                    changed.append(service_dp)
        return changed

    async def _fetch_inbox(self) -> list[Any]:
        """Refresh the inbox-count singleton."""
        if (inbox_dp := self._inbox_dp) is None:
            return []
        try:
            entries = await self._client.hub.list_inbox()
        except Exception:  # noqa: BLE001 — endpoint optional, keep last value
            _LOGGER.debug("inbox fetch failed", exc_info=True)
            return []
        count = sum(1 for e in entries if self._matches_central(e.get("central")))
        return [inbox_dp] if inbox_dp.update_value(value=count) else []

    async def _fetch_metrics(self) -> list[Any]:
        """Refresh the metrics singletons (None until the daemon observed them)."""
        if (metrics_dps := self._metrics_dps) is None:
            return []
        try:
            entries = await self._client.system.get_hub_metrics()
        except Exception:  # noqa: BLE001 — endpoint optional, keep last value
            _LOGGER.debug("hub-metrics fetch failed", exc_info=True)
            return []
        entry = next((e for e in entries if self._matches_central(e.central)), None)
        if entry is None:
            return []
        changed: list[Any] = []
        for dp, value in (
            (metrics_dps.system_health, entry.system_health),
            (metrics_dps.connection_latency, entry.connection_latency_ms),
            (metrics_dps.last_event_age, entry.last_event_age_seconds),
        ):
            if dp.update_value(value=value):
                changed.append(dp)
        return changed

    async def _fetch_system_update(self) -> list[Any]:
        """Refresh the system-update singleton."""
        if (update_dp := self._update_dp) is None:
            return []
        try:
            entries = await self._client.system.get_system_update()
        except Exception:  # noqa: BLE001 — endpoint optional, keep last value
            _LOGGER.debug("system-update fetch failed", exc_info=True)
            return []
        entry = next((e for e in entries if self._matches_central(e.central)), None)
        if entry is None:
            return []
        return [update_dp] if update_dp.update_data(entry=entry) else []

    async def _fetch_install_mode(self) -> list[Any]:
        """Refresh the per-interface install-mode countdown sensors."""
        if not self._install_mode_dps:
            return []
        try:
            entries = await self._client.hub.list_install_mode_interfaces()
        except Exception:  # noqa: BLE001 — endpoint optional, keep last value
            _LOGGER.debug("install-mode fetch failed", exc_info=True)
            return []
        changed: list[Any] = []
        for entry in entries:
            if not self._matches_central(entry.central):
                continue
            if (pair := self._install_mode_dps.get(entry.interface)) is None:
                continue
            if pair.sensor.update_value(value=entry.seconds if entry.active else 0):
                changed.append(pair.sensor)
        return changed

    async def _fetch_connectivity(self) -> list[Any]:
        """Refresh the per-interface connectivity binary sensors."""
        if not self._connectivity_dps:
            return []
        try:
            states = await self._client.system.list_interfaces()
        except Exception:  # noqa: BLE001 — endpoint optional, keep last value
            _LOGGER.debug("interface fetch failed", exc_info=True)
            return []
        changed: list[Any] = []
        for state in states:
            if (entry := self._connectivity_dps.get(state.id)) is None:
                continue
            if entry.sensor.update_value(value=bool(state.connected)):
                changed.append(entry.sensor)
        return changed


class _QueryFacade:
    """``central.query_facade`` surface."""

    def __init__(self, client: LoomClient, *, extra_data_points: list[Any]) -> None:
        self._client = client
        # Adapter-built data points without a store summary (week
        # profiles, schedule switches, combined numbers). The adapter
        # owns and fills the list during bootstrap; the facade only
        # reads it, so sharing the reference keeps both in sync.
        self._extra_data_points = extra_data_points
        # Event groups carry per-instance state (registered flag, last
        # trigger); cache them by unique_id so repeated scans and the
        # refresh bridge's trigger recording hit the same instances.
        self._event_groups: dict[str, Any] = {}

    async def get_un_ignore_candidates(self) -> Any:
        return await self._client.visibility.get_unignore_candidates()

    def get_data_points(
        self,
        *,
        data_point_type: Any = None,
        category: Any = None,
        exclude_no_create: bool = True,
        registered: bool | None = None,
        **_kwargs: Any,
    ) -> tuple[Any, ...]:
        """
        Device data points (generic + custom), filtered like aiohomematic.

        The store holds categorised ``Dp*`` and ``CustomDp*`` instances
        (built by the injected factories). Generic platforms pass an
        explicit ``category``; custom platforms (light/cover/climate/…)
        pass only ``data_point_type``, whose name matches the custom
        ``category`` — so an unset ``category`` is derived from
        ``data_point_type``.
        """
        target = category if category is not None else _category_for_type(data_point_type)
        out: list[Any] = []
        for dp in (
            *self._client.store.data_points,
            *self._client.store.custom_data_points,
            *self._extra_data_points,
        ):
            dp_category = getattr(dp, "category", None)
            if target is not None and dp_category != target:
                continue
            if registered is not None and getattr(dp, "is_registered", False) != registered:
                continue
            if exclude_no_create and not _is_creatable(dp):
                continue
            out.append(dp)
        return tuple(out)

    def get_generic_data_point(self, *, state_path: str) -> Any:
        """Resolve a generic data point from its MQTT state path."""
        parsed = parse_device_state_path(state_path)
        if parsed is None:
            return None
        address, channel, parameter = parsed
        return self._client.store.get_data_point(
            address=address, channel=channel, parameter=parameter
        )

    def get_event_groups(
        self,
        *,
        event_type: Any = None,
        registered: bool | None = None,
        **_kwargs: Any,
    ) -> tuple[Any, ...]:
        """Return device-trigger event groups built from the store's trigger DPs."""
        out = []
        for built in build_event_groups(
            store=self._client.store,
            central_id=self._client.store.serial_suffix,
            event_type=event_type,
            registered=None,
        ):
            group = self._event_groups.setdefault(built.unique_id, built)
            if registered is None or group.is_registered == registered:
                out.append(group)
        return tuple(out)

    def find_event_group(
        self, *, device_address: str, channel_no: int | None, event_type: Any
    ) -> Any:
        """Return the cached event group for one channel + trigger type, or ``None``."""
        for group in self._event_groups.values():
            channel = group.channel
            if (
                channel.device_address == device_address
                and channel.number == channel_no
                and group.device_trigger_event_type == event_type
            ):
                return group
        return None

    def get_state_paths(
        self, *, rpc_callback_supported: bool | None = None, **_kwargs: Any
    ) -> tuple[str, ...]:
        """
        Synthesise the MQTT state path of every generic data point.

        The daemon mediates all interfaces, so the ``rpc_callback_supported``
        filter aiohomematic uses to pick MQTT-only devices does not apply —
        every generic DP gets a path. Sysvars are handled by the bridge's
        ``sysvar/status/+`` wildcard, not enumerated here.
        """
        return tuple(
            device_state_path(
                address=dp.device_address,
                channel=dp.channel_number,
                parameter=dp.parameter,
            )
            for dp in self._client.store.data_points
        )


class _ClientCoordinator:
    """``central.client_coordinator`` surface (interface connectivity)."""

    def __init__(self, client: LoomClient) -> None:
        self._client = client
        self._interface_ids: frozenset[str] = frozenset()

    async def refresh(self) -> None:
        states = await self._client.system.list_interfaces()
        self._interface_ids = frozenset(i.id for i in states)

    def has_client(self, *, interface_id: str) -> bool:
        return interface_id in self._interface_ids

    @property
    def has_clients(self) -> bool:
        return bool(self._interface_ids)

    @property
    def clients(self) -> frozenset[str]:
        return self._interface_ids


def _incident_list(payload: Any) -> list[dict[str, Any]]:
    """Pull the incident list out of the daemon's ``GET /incidents`` envelope."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("incidents", "items", "entries"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


class _IncidentStore:
    """``cache_coordinator.incident_store`` surface over ``client.diagnostics``."""

    def __init__(self, client: LoomClient) -> None:
        self._client = client

    async def get_diagnostics(self) -> dict[str, Any]:
        return await self._client.diagnostics.list_incidents()

    async def get_incidents_by_interface(self, *, interface_id: str) -> list[dict[str, Any]]:
        incidents = _incident_list(await self._client.diagnostics.list_incidents())
        return [i for i in incidents if i.get("interface_id") == interface_id]

    async def get_recent_incidents(self, *, limit: int) -> list[dict[str, Any]]:
        incidents = _incident_list(await self._client.diagnostics.list_incidents())
        return incidents[:limit] if limit > 0 else incidents

    def clear_incidents(self) -> None:
        # The daemon owns the incident store; there is no client-side clear.
        _LOGGER.debug("clear_incidents() is a no-op on the loom backend")


class _Recorder:
    """``cache_coordinator.recorder`` surface over the daemon's RPC recording."""

    def __init__(self, client: LoomClient) -> None:
        self._client = client

    async def activate(self, **options: Any) -> Any:
        return await self._client.diagnostics.start_rpc_recording(options=options)

    async def deactivate(self, **options: Any) -> Any:
        return await self._client.diagnostics.stop_rpc_recording(options=options)


class _CacheCoordinator:
    """``central.cache_coordinator`` surface."""

    def __init__(self, client: LoomClient) -> None:
        self._client = client
        self._incident_store = _IncidentStore(client)
        self._recorder = _Recorder(client)

    async def clear_all(self) -> None:
        await self._client.diagnostics.reset_values_cache()

    @property
    def incident_store(self) -> _IncidentStore:
        return self._incident_store

    @property
    def recorder(self) -> _Recorder:
        return self._recorder


class _JsonRpcClient:
    """``central.json_rpc_client`` surface (CCU-side message/inbox ops)."""

    def __init__(self, client: LoomClient) -> None:
        self._client = client

    async def get_service_messages(self) -> Any:
        return await self._client.hub.list_service_messages()

    async def get_alarm_messages(self) -> Any:
        return await self._client.hub.list_alarm_messages()

    async def get_inbox_devices(self) -> Any:
        return await self._client.hub.list_inbox()

    async def accept_device_in_inbox(self, *, device_address: str) -> None:
        await self._client.devices.accept_device(address=device_address)

    async def acknowledge_message(self, *, message_id: str) -> None:
        # aiohomematic exposed a single ack; the daemon splits alarm vs.
        # service. Service messages are the common HA case; callers that
        # need alarm-ack should use client.hub.ack_alarm_message.
        await self._client.hub.ack_service_message(message_id=message_id)

    async def rename_device(self, *, ise_id: int, name: str) -> None:
        """Rename a device by its CCU ise_id (mapped to the address)."""
        address = next(
            (d.address for d in self._client.store.devices if d.ise_id == ise_id),
            None,
        )
        if address is None:
            raise ValueError(f"no device with ise_id {ise_id} in the store")
        await self._client.devices.patch_device(address=address, name=name)


class _LinkCoordinator:
    """``central.link`` surface (direct links)."""

    def __init__(self, client: LoomClient) -> None:
        self._client = client

    async def add_link(
        self, *, address: str, sender_address: str, receiver_address: str, **kwargs: Any
    ) -> None:
        await self._client.links.add_link(
            address=address,
            sender_address=sender_address,
            receiver_address=receiver_address,
            name=kwargs.get("name"),
            description=kwargs.get("description"),
        )

    async def remove_link(self, *, address: str, sender: str, receiver: str) -> None:
        await self._client.links.remove_link(address=address, sender=sender, receiver=receiver)

    async def get_device_links(self, *, address: str, locale: str = "en") -> Any:
        return await self._client.links.list_links(address=address, locale=locale)

    async def get_linkable_channels(
        self, *, address: str, channel: int, role: str, interface: str, locale: str = "en"
    ) -> Any:
        return await self._client.links.linkable_channels(
            address=address,
            channel=channel,
            role=role,
            interface=interface,
            locale=locale,
        )


def _paramset_token(paramset_key: Any) -> str:
    """Normalise a ParamsetKey enum / string to the daemon's wire token."""
    return str(getattr(paramset_key, "value", paramset_key))


def _split_channel_address(channel_address: str) -> tuple[str, int]:
    """Split ``ABC1234567:3`` into ``("ABC1234567", 3)`` (channel 0 if absent)."""
    device, _, channel = channel_address.partition(":")
    return device, int(channel) if channel else 0


class _Configuration:
    """
    ``central.configuration`` surface (paramset values + descriptors).

    The daemon exposes the renderable parameter descriptions as the
    channel *ui-schema* (``GET /devices/{addr}/channels/{no}/ui-schema``,
    ``paramset=VALUES|MASTER|LINK``), so the description getters are async
    here — unlike aiohomematic's cached, synchronous variants. HA's config
    websocket handlers must ``await`` them on the loom backend.
    """

    def __init__(self, client: LoomClient) -> None:
        self._client = client

    async def get_paramset(
        self,
        *,
        paramset_key: Any,
        channel_address: str | None = None,
        address: str | None = None,
        interface_id: str | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        """Read current paramset values. Wire: ``GET /devices/{addr}/paramsets/{key}``."""
        target = channel_address or address
        if target is None:
            raise ValueError("get_paramset requires channel_address (or address)")
        return await self._client.datapoints.get_paramset(
            address=target, paramset_key=_paramset_token(paramset_key)
        )

    async def get_paramset_description(
        self,
        *,
        channel_address: str,
        paramset_key: Any,
        peer: str | None = None,
        locale: str = "en",
        expert: bool | None = None,
        interface_id: str | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        """Return renderable parameter descriptions for a channel paramset (ui-schema)."""
        device, channel = _split_channel_address(channel_address)
        return await self._client.devices.get_ui_schema(
            address=device,
            channel=channel,
            paramset=_paramset_token(paramset_key),
            peer=peer,
            locale=locale,
            expert=expert,
        )

    async def get_link_paramset_description(
        self,
        *,
        channel_address: str,
        peer: str,
        locale: str = "en",
        expert: bool | None = None,
        interface_id: str | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        """Return renderable LINK-paramset descriptions between a channel and a peer."""
        device, channel = _split_channel_address(channel_address)
        return await self._client.devices.get_ui_schema(
            address=device,
            channel=channel,
            paramset="LINK",
            peer=peer,
            locale=locale,
            expert=expert,
        )

    def get_configurable_devices(
        self, *, locale: str = "en", **_kwargs: Any
    ) -> tuple[ConfigurableDevice, ...]:
        """Return configurable-device descriptors for the config UI."""
        return build_configurable_devices(self._client.store)


class LoomCentralAdapter:
    """Presents the ``aiohomematic.CentralUnit`` surface over ``LoomClient``."""

    def __init__(
        self,
        *,
        client: LoomClient,
        name: str,
        serial: str | None = None,
        sysvar_markers: tuple[str, ...] = (),
        program_markers: tuple[str, ...] = (),
    ) -> None:
        """Wire the coordinator surface and data-point factories onto ``client``."""
        self._client = client
        self._name = name
        # CCU serial injected by the integration (HA's ``entry.unique_id``).
        # When set it fills the central-id slot of canonical HA routing
        # keys and wins over the serial reported on ``/system/ccu`` — so
        # the live keys match the one-time HA registry migration.
        self._serial = serial
        self._state: CentralState = CentralState.Stopped
        self._system_information = SystemInformation()
        # Make the store build categorised Dp* / CustomDp* instances so
        # HA-side isinstance dispatch works on the live objects. Must be
        # set before bootstrap() runs.
        client.store.set_data_point_factory(make_generic_data_point)
        client.store.set_calculated_data_point_factory(make_calculated_data_point)
        client.store.set_custom_data_point_factory(make_custom_data_point)
        # HA links every device to this central via Device.central_info.name,
        # which must equal the adapter name (the integration's instance name).
        client.store.set_central_name(name)
        self._refresh_group: Any = None
        # HA entities subscribe on aiohomematic's *own* event bus and match
        # events by ``type(event)``/``.key``. The adapter therefore exposes a
        # real aiohomematic EventBus (not the loom wire bus) as ``event_bus``
        # and the bridges publish real aiohomematic events onto it.
        self._looper: Final = Looper()
        self._ha_bus: Final = AioEventBus(task_scheduler=self._looper)
        # Adapter-built data points without a store summary (week
        # profiles, schedule switches, combined numbers); shared with
        # the query facade so the platforms see them.
        self._extra_data_points: Final[list[Any]] = []
        self._hub_refresh_task: asyncio.Task[None] | None = None
        self.device_coordinator: Final = _DeviceCoordinator(client)
        self.hub_coordinator: Final = _HubCoordinator(
            client,
            ha_bus=self._ha_bus,
            sysvar_markers=sysvar_markers,
            program_markers=program_markers,
        )
        self.query_facade: Final = _QueryFacade(client, extra_data_points=self._extra_data_points)
        self.client_coordinator: Final = _ClientCoordinator(client)
        self.cache_coordinator: Final = _CacheCoordinator(client)
        self.json_rpc_client: Final = _JsonRpcClient(client)
        self.link: Final = _LinkCoordinator(client)
        self.configuration: Final = _Configuration(client)

    # ---- identity ----

    @property
    def name(self) -> str:
        """Return the central's display name."""
        return self._name

    @property
    def model(self) -> str:
        """Return the backend model identifier."""
        return "openccu-loom"

    @property
    def version(self) -> str | None:
        """Return the daemon version, or ``None`` before the first refresh."""
        # Populated by start() / validate_config_and_get_system_information().
        return self._system_information.version

    @property
    def url(self) -> str:
        """Return the daemon's HTTP base URL."""
        return self._client.config.http_base_url

    @property
    def state(self) -> CentralState:
        """Return the current central lifecycle state."""
        return self._state

    @property
    def available(self) -> bool:
        """Return whether the central is running or degraded."""
        return self._state in (CentralState.Running, CentralState.Degraded)

    @property
    def system_information(self) -> SystemInformation:
        """Return the cached daemon + CCU system information."""
        return self._system_information

    @property
    def config(self) -> Any:
        """Return the underlying client's :class:`LoomConfig`."""
        return self._client.config

    @property
    def events(self) -> EventBus:
        """Return the client's event bus."""
        return self._client.events

    @property
    def event_bus(self) -> AioEventBus:
        """Return the real aiohomematic event bus HA entities subscribe on."""
        return self._ha_bus

    async def health(self) -> Any:
        """Return the daemon's health report."""
        return await self._client.system.get_health()

    # ---- lifecycle ----

    async def start(self) -> None:
        """Connect, bootstrap the store, open the event stream, and install the refresh bridge."""
        await self._client.connect()
        await self._refresh_system_information()
        await self.client_coordinator.refresh()
        self._state = CentralState.Starting
        await self._client.bootstrap()
        await self._bootstrap_hub_catalogue()
        await self.hub_coordinator.fetch_hub_singleton_data()
        await self._bootstrap_schedules()
        await self._bootstrap_combined_data_points()
        await self._bootstrap_custom_data_points()
        await self._client.start_events()
        # Fan the daemon's typed value events into the uniform
        # DataPointStateChangedEvent the HA entities subscribe to.
        self._refresh_group = self._client.events.create_subscription_group(
            name="loom-compat-refresh"
        )
        install_refresh_bridge(
            group=self._refresh_group,
            store=self._client.store,
            ha_bus=self._ha_bus,
            central_name=self._name,
            event_group_resolver=self.query_facade.find_event_group,
        )
        # Announce every data point (generic + custom) in one batch *after*
        # the custom DPs are attached, so HA's platforms spawn entities for
        # them too. Published on the real aiohomematic bus as the real
        # DataPointsCreatedEvent HA subscribes to.
        await self._emit_data_points_created()
        # Keep the hub singletons fresh; the daemon has no push channel
        # for messages/metrics/install mode yet, so they are polled.
        self._hub_refresh_task = asyncio.create_task(
            self._hub_singleton_refresh_loop(), name="loom-hub-singleton-refresh"
        )
        self._state = CentralState.Running

    async def _hub_singleton_refresh_loop(self) -> None:
        """Re-poll the hub singleton endpoints every refresh interval."""
        while True:
            await asyncio.sleep(_HUB_REFRESH_INTERVAL)
            try:
                await self.hub_coordinator.fetch_hub_singleton_data(scheduled=True)
            except Exception:  # noqa: BLE001 — keep the refresh loop alive
                _LOGGER.debug("hub singleton refresh failed", exc_info=True)

    async def _emit_data_points_created(self) -> None:
        """Publish a real ``DataPointsCreatedEvent`` grouped by aiohomematic category."""
        grouped: dict[AioDataPointCategory, list[Any]] = {}
        # Hub data points (sysvars, programs) ride along: HA's platforms
        # may finish their initial get_new_hub_data_points() scan before
        # the snapshot is loaded, and no later event would re-announce
        # them — without this the hub layer never spawns.
        hub_dps = self.hub_coordinator.get_hub_data_points(registered=False)
        # One firmware-update data point per updatable device (uid
        # ``loom_<address>_update``), mirroring aiohomematic's DpUpdate.
        update_dps = [
            make_update_data_point(device=device, store=self._client.store)
            for device in self._client.store.devices
            if getattr(device.summary, "updatable", True)
        ]
        event_groups = self.query_facade.get_event_groups(registered=False)
        for dp in (
            *self._client.store.data_points,
            *self._client.store.custom_data_points,
            *self._extra_data_points,
            *hub_dps,
            *update_dps,
            *event_groups,
        ):
            loom_category = getattr(dp, "category", None)
            if loom_category is None:
                continue
            if not _is_creatable(dp):
                continue
            # Loom and aiohomematic share identical category *values*; map by
            # value (the loom StrEnum's ``str()`` yields its repr, not the value).
            category_value = getattr(loom_category, "value", loom_category)
            grouped.setdefault(AioDataPointCategory(category_value), []).append(dp)
        if grouped:
            await self._ha_bus.publish(
                event=AioDataPointsCreatedEvent(
                    timestamp=datetime.now(tz=UTC),
                    new_data_points={category: tuple(dps) for category, dps in grouped.items()},
                )
            )

    async def _bootstrap_hub_catalogue(self) -> None:
        """
        Merge the complete sysvar/program catalogue into the store.

        The snapshot's hub block only covers the daemon's snapshot index
        (first central in multi-central deployments); the dedicated
        endpoints return everything. Failures are non-fatal — the hub
        layer then degrades to whatever the snapshot carried.
        """
        try:
            sysvars = await self._client.hub.list_sysvars()
            programs = await self._client.hub.list_programs()
        except Exception:  # noqa: BLE001 — hub endpoints are optional
            _LOGGER.debug("hub catalogue refresh failed during bootstrap", exc_info=True)
            return
        self._client.store.attach_hub_catalogue(sysvars=sysvars, programs=programs)

    async def _bootstrap_custom_data_points(self) -> None:
        """
        Fetch each device's Custom DPs into the store (categorised).

        The core bootstrap covers devices/channels/generic data points;
        custom DPs are an HA-backend concern, so the adapter pulls them
        here. State arrives later via ``custom_data_point.state_changed``.
        """
        for device in list(self._client.store.devices):
            cdps = await self._client.custom_data_points.list_for_device(address=device.address)
            if cdps:
                self._client.store.attach_custom_data_points(
                    device_address=device.address, cdps=cdps
                )
            for channel in device.channels:
                calculated = await self._client.devices.list_calculated_data_points(
                    address=device.address, channel=channel.number
                )
                # The combined duration number replaces the daemon's
                # calculated DURATION sensor (the ccu twin has none).
                calculated = [
                    calc for calc in calculated if calc.name not in _SUPPRESSED_CALCULATED_NAMES
                ]
                if calculated:
                    self._client.store.attach_channel_calculated_data_points(
                        device_address=device.address,
                        channel_number=channel.number,
                        calculated=calculated,
                    )

    async def _bootstrap_schedules(self) -> None:
        """
        Build the week-profile and schedule-switch data points.

        Channels whose type ends in ``WEEK_PROFILE`` own the device's
        weekly program. Per channel, the daemon's week-profile
        descriptor spawns one :class:`WeekProfileDp` (entry count loaded
        from the channel schedule, fetch errors degrade to "unknown")
        plus one :class:`ScheduleChannelSwitch` per ``schedule_enabled``
        key. Devices without a week-profile channel are skipped.
        """
        store = self._client.store
        for device in list(store.devices):
            for channel in store.channels_of(address=device.address):
                if not (channel.channel_type or "").endswith(_WEEK_PROFILE_CHANNEL_SUFFIX):
                    continue
                try:
                    week_profile = await self._client.schedules.get_channel_week_profile(
                        address=device.address, channel=channel.number
                    )
                except Exception:  # noqa: BLE001 — channel has no attached week profile
                    _LOGGER.debug(
                        "no week profile on %s:%s", device.address, channel.number, exc_info=True
                    )
                    continue
                wp_dp = WeekProfileDp(
                    store=store,
                    device=device,
                    channel_no=channel.number,
                    week_profile=week_profile,
                )
                try:
                    schedule = await self._client.schedules.get_channel_schedule(
                        address=device.address, channel=channel.number
                    )
                except Exception:  # noqa: BLE001 — entry count degrades to unknown
                    _LOGGER.debug(
                        "schedule fetch failed for %s:%s",
                        device.address,
                        channel.number,
                        exc_info=True,
                    )
                else:
                    wp_dp.update_from(schedule=schedule)
                self._extra_data_points.append(wp_dp)
                for channel_key in week_profile.schedule_enabled or {}:
                    self._extra_data_points.append(
                        ScheduleChannelSwitch(
                            store=store,
                            device=device,
                            channel_no=channel.number,
                            channel_key=channel_key,
                            week_profile_dp=wp_dp,
                            schedules_ops=self._client.schedules,
                        )
                    )

    async def _bootstrap_combined_data_points(self) -> None:
        """
        Build one combined duration number per DURATION_VALUE/UNIT pair.

        Mirrors aiohomematic's combined timer: channels carrying both
        parameters get a single seconds-typed number entity.
        """
        store = self._client.store
        for device in list(store.devices):
            for channel in store.channels_of(address=device.address):
                if channel_has_duration_pair(
                    store=store, address=device.address, channel_no=channel.number
                ):
                    self._extra_data_points.append(
                        CombinedDurationDp(store=store, device=device, channel_no=channel.number)
                    )

    async def stop(self) -> None:
        """Cancel the refresh bridge and hub poll, close the client, and stop."""
        if self._hub_refresh_task is not None:
            self._hub_refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._hub_refresh_task
            self._hub_refresh_task = None
        if self._refresh_group is not None:
            self._refresh_group.cancel()
            self._refresh_group = None
        self._looper.cancel_tasks()
        await self._client.close()
        self._state = CentralState.Stopped

    async def validate_config_and_get_system_information(self) -> SystemInformation:
        """
        Pre-flight used by the HA config flow.

        Opens the session (capability handshake), reads daemon + CCU
        metadata, and returns it without starting the event stream.
        """
        await self._client.connect()
        await self._refresh_system_information()
        return self._system_information

    async def create_backup_and_download(self) -> dict[str, Any]:
        """
        Trigger a CCU backup.

        Returns the daemon's trigger response. The downloadable archive
        is fetched separately via ``client.backup.download_backup``
        once the daemon reports the backup id.
        """
        return await self._client.backup.trigger_backup()

    # ---- internals ----

    async def _refresh_system_information(self) -> None:
        info = await self._client.system.get_info()
        try:
            ccus = await self._client.system.list_system_ccus()
        except Exception:  # noqa: BLE001 — system/ccu endpoint is optional
            _LOGGER.debug("system/ccu unavailable during system-information refresh")
            ccus = []
        # The daemon manages multiple centrals; pick the entry matching this
        # central's name (falling back to the first) — taking ccus[0] blindly
        # stamps the WRONG serial in a multi-central deployment and corrupts
        # every hub / internal / virtual-remote routing key.
        ccu_entry = next((c for c in ccus if getattr(c, "name", None) == self._name), None)
        if ccu_entry is None and ccus:
            ccu_entry = ccus[0]
        daemon_serial = getattr(ccu_entry, "serial", None) if ccu_entry is not None else None
        # An injected serial (HA's entry.unique_id) wins over the daemon's,
        # so the live keys match the HA registry migration exactly.
        serial = self._serial or daemon_serial
        # The serial is the central-id slot of every canonical HA routing
        # key (hub / internal / virtual-remote); record it before bootstrap
        # so the categorised data-point layer builds matching unique_ids.
        if not serial:
            _LOGGER.warning(
                "No CCU serial available (neither injected nor reported by the "
                "daemon) — canonical unique_ids for hub / internal / "
                "virtual-remote data points will carry an empty central-id "
                "slot (loom__…) and break the HA registry contract"
            )
        self._client.store.set_serial(serial)
        interfaces: tuple[str, ...] = ()
        try:
            interfaces = tuple(i.id for i in await self._client.system.list_interfaces())
        except Exception:  # noqa: BLE001 — interfaces endpoint is optional
            _LOGGER.debug("interfaces unavailable during system-information refresh")
        self._system_information = SystemInformation(
            serial=serial,
            version=info.version,
            available_interfaces=interfaces,
        )
