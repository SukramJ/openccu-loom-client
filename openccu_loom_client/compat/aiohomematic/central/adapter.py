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
  seeded at bootstrap via ``fetch_hub_singleton_data`` (one aggregate
  ``GET /hub/data-points`` call) and kept live by the daemon's ``hub.*``
  push broadcasts (see ``_HubCoordinator.install_push_routing`` —
  ``system_update`` included since daemon api 1.19.0); a slow reconcile
  loop backstops missed pushes.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
from dataclasses import dataclass
from datetime import UTC, datetime
import logging
from typing import TYPE_CHECKING, Any, Final, cast

from openccu_loom_types.enums import CentralState, DataPointCategory

from openccu_loom_client.compat.aiohomematic._upstream import (
    AlarmMessageData,
    CentralHealth,
    CentralState as AioCentralState,
    ClientState as AioClientState,
    DataPointCategory as AioDataPointCategory,
    DataPointsCreatedEvent as AioDataPointsCreatedEvent,
    DeviceLink,
    EventBus as AioEventBus,
    InboxDeviceData,
    Interface as AioInterface,
    LinkableChannel,
    Looper,
    ServiceMessageData,
    validate_paramset,
)
from openccu_loom_client.compat.aiohomematic.central.configurable_devices import (
    ConfigurableDevice,
    build_configurable_devices,
)
from openccu_loom_client.compat.aiohomematic.central.hub_coordinator import _HubCoordinator
from openccu_loom_client.compat.aiohomematic.central.refresh import install_refresh_bridge
from openccu_loom_client.compat.aiohomematic.central.state_paths import device_state_path, parse_device_state_path
from openccu_loom_client.compat.aiohomematic.const import SystemInformation, make_system_information
from openccu_loom_client.compat.aiohomematic.model.alarm_panel import make_alarm_panel_data_point
from openccu_loom_client.compat.aiohomematic.model.calculated import make_calculated_data_point
from openccu_loom_client.compat.aiohomematic.model.combined import CombinedDurationDp, channel_has_duration_pair
from openccu_loom_client.compat.aiohomematic.model.custom import (
    BaseCustomDpSiren,
    CustomDpSoundPlayer,
    make_custom_data_point,
)
from openccu_loom_client.compat.aiohomematic.model.event_group import build_event_groups
from openccu_loom_client.compat.aiohomematic.model.generic import make_generic_data_point
from openccu_loom_client.compat.aiohomematic.model.hub import make_program_data_points, make_sysvar_data_point
from openccu_loom_client.compat.aiohomematic.model.update import make_update_data_point
from openccu_loom_client.compat.aiohomematic.model.week_profile import (
    ClimateWeekProfileDp,
    ScheduleChannelSwitch,
    WeekProfileDp,
)
from openccu_loom_client.events import AlarmPanelChangedEvent as LoomAlarmPanelChangedEvent
from openccu_loom_client.exceptions import BaseLoomException, LoomHttpError, LoomNotFoundError

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from openccu_loom_client.client import LoomClient
    from openccu_loom_client.events import EventBus
    from openccu_loom_client.model import Device

_LOGGER: Final = logging.getLogger(__name__)

# Slow reconcile cadence. Every hub singleton is now push-driven (see
# _HubCoordinator.install_push_routing — system_update included since daemon
# api 1.19.0); this loop is a general missed-push backstop only. Deliberately
# coarse — ~70x slower than the retired 30 s poll.
_HUB_RECONCILE_INTERVAL: Final = 300

# Channel types owning a device's weekly program end in this suffix.
_WEEK_PROFILE_CHANNEL_SUFFIX: Final = "WEEK_PROFILE"

# The daemon ships a calculated DURATION sensor; the ccu twin covers the
# DURATION_VALUE/DURATION_UNIT pair with a combined number instead, so
# the calculated flavour is suppressed to avoid a surplus entity.
_SUPPRESSED_CALCULATED_NAMES: Final = frozenset({"DURATION"})


_CATEGORY_BY_VALUE: Final[dict[str, DataPointCategory]] = {member.value: member for member in DataPointCategory}


def _category_for_type(*, data_point_type: Any) -> DataPointCategory | None:
    """
    Map a coarse ``DataPointType`` (platform) to its custom-DP category.

    Custom platforms (light/cover/climate/lock/siren/valve, and the
    loom-only alarm panel) request data points by ``data_point_type``
    only. Matching happens on the enum *value* (``"siren"``), which is
    identical between the loom enums (PascalCase members) and the
    aiohomematic enums (SCREAMING_CASE members) — a member-*name* lookup
    would miss the aiohomematic spelling ``homematicip_local`` passes.
    Returns ``None`` for an unset type or one with no category twin.
    """
    if data_point_type is None:
        return None
    token = getattr(data_point_type, "value", data_point_type)
    return _CATEGORY_BY_VALUE.get(str(token))


# Usage verdicts that never spawn an HA entity. The daemon pipeline
# computes the full aiohomematic visibility model (forced sensors,
# un-ignore, HIDDEN_PARAMETERS, custom-DP absorption, click events) and
# ships the verdict on DataPointSummary.usage — the same gate the MQTT
# discovery plane applies. "event" covers physical devices' PRESS_*
# parameters: they surface through keypress event groups, never as
# generic button entities (virtual remotes report data_point and keep
# their buttons).
_NON_CREATABLE_USAGES: Final = frozenset({"no_create", "ignored", "event"})


def _is_creatable(*, dp: Any) -> bool:
    """Return whether the DP's usage verdict allows an HA entity."""
    usage = getattr(getattr(dp, "summary", None), "usage", None)
    return usage not in _NON_CREATABLE_USAGES


class _DeviceCoordinator:
    """``central.device_coordinator`` surface."""

    def __init__(self, *, client: LoomClient) -> None:
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


class _QueryFacade:
    """``central.query_facade`` surface."""

    def __init__(self, *, client: LoomClient, extra_data_points: list[Any]) -> None:
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
        # un-ignore candidates, prefetched once during start(). HA's
        # options flow calls get_un_ignore_candidates *synchronously*
        # (aiohomematic computes the list from local caches), so the
        # loom facade must serve it without a round-trip.
        self._un_ignore_candidates: list[str] = []

    def get_un_ignore_candidates(self, *, include_master: bool = False) -> list[str]:
        """
        Return the cached un-ignore candidates (aiohomematic-shaped, sync).

        aiohomematic's signature is synchronous with an
        ``include_master`` switch; the loom daemon's candidate endpoint
        already includes MASTER parameters, so the flag only exists for
        signature parity. The cache is filled by
        :meth:`prefetch_un_ignore_candidates` during central start and
        degrades to an empty list before that (HA then hides the
        un-ignore selector).
        """
        del include_master  # daemon candidates already span both paramsets
        return list(self._un_ignore_candidates)

    async def prefetch_un_ignore_candidates(self) -> None:
        """Fetch and cache the un-ignore candidate list (best-effort)."""
        try:
            result = await self._client.visibility.get_unignore_candidates()
        except Exception:
            _LOGGER.debug("un-ignore candidate prefetch failed", exc_info=True)
            return
        self._un_ignore_candidates = list(getattr(result, "candidates", None) or [])

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
        target = category if category is not None else _category_for_type(data_point_type=data_point_type)
        out: list[Any] = []
        for dp in (
            *self._client.store.data_points,
            *self._client.store.custom_data_points,
            *self._client.store.alarm_panels,
            *self._extra_data_points,
        ):
            dp_category = getattr(dp, "category", None)
            if target is not None and dp_category != target:
                continue
            if registered is not None and getattr(dp, "is_registered", False) != registered:
                continue
            if exclude_no_create and not _is_creatable(dp=dp):
                continue
            out.append(dp)
        return tuple(out)

    def get_generic_data_point(self, *, state_path: str) -> Any:
        """Resolve a generic data point from its MQTT state path."""
        parsed = parse_device_state_path(state_path=state_path)
        if parsed is None:
            return None
        address, channel, parameter = parsed
        return self._client.store.get_data_point(address=address, channel=channel, parameter=parameter)

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

    def find_event_group(self, *, device_address: str, channel_no: int | None, event_type: Any) -> Any:
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

    def get_state_paths(self, *, rpc_callback_supported: bool | None = None, **_kwargs: Any) -> tuple[str, ...]:
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

    def __init__(self, *, client: LoomClient) -> None:
        self._client = client
        self._interface_ids: frozenset[str] = frozenset()
        self._states: tuple[Any, ...] = ()

    async def refresh(self) -> None:
        states = await self._client.system.list_interfaces()
        self._states = tuple(states)
        self._interface_ids = frozenset(i.id for i in states)

    @property
    def states(self) -> tuple[Any, ...]:
        """Return the daemon's per-interface state records (id / interface / connected)."""
        return self._states

    def has_client(self, *, interface_id: str) -> bool:
        return interface_id in self._interface_ids

    @property
    def has_clients(self) -> bool:
        return bool(self._interface_ids)

    @property
    def clients(self) -> tuple[_InterfaceClient, ...]:
        """
        Return one record per wired interface.

        The integration dashboard's throttle view iterates these and reads
        ``client.interface_id`` plus ``client.command_throttle.*`` — a bare
        interface-id string raised ``AttributeError`` and, because the panel
        fetches the four integration sections in one ``Promise.all``, took the
        whole tab down with it.
        """
        return tuple(_InterfaceClient(interface_id=interface_id) for interface_id in sorted(self._interface_ids))

    @property
    def interface_ids(self) -> frozenset[str]:
        """Return the bare interface ids (loom-internal callers)."""
        return self._interface_ids


@dataclass(frozen=True, slots=True)
class _CommandThrottle:
    """
    Per-interface command-throttle stats.

    The daemon serialises CCU commands itself, so there is no client-side
    throttle: the panel is told, honestly, that throttling is off and the
    counters are empty — rather than being handed an ``AttributeError``.
    """

    interval: float = 0.0
    is_enabled: bool = False
    queue_size: int = 0
    throttled_count: int = 0
    critical_count: int = 0
    burst_count: int = 0
    burst_threshold: int = 0
    burst_window: float = 0.0


@dataclass(frozen=True, slots=True)
class _InterfaceClient:
    """One wired interface, shaped like the aiohomematic client the throttle view iterates."""

    interface_id: str
    command_throttle: _CommandThrottle = dataclasses.field(default_factory=_CommandThrottle)


def _to_aio_central_state(*, state: Any) -> AioCentralState:
    """Map the loom lifecycle state onto aiohomematic's ``CentralState`` (same value vocabulary)."""
    token = str(getattr(state, "value", state))
    try:
        return AioCentralState(token)
    except ValueError:
        return AioCentralState.STARTING


def _to_aio_interface(*, value: Any) -> AioInterface | None:
    """Map a daemon interface token (``HmIP-RF``) onto aiohomematic's ``Interface``, or ``None``."""
    if value is None:
        return None
    try:
        return AioInterface(str(getattr(value, "value", value)))
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class _Incident:
    """One recorded incident. The HA handler calls ``to_dict()`` on each."""

    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Return the incident as a plain dict."""
        return dict(self.payload)


def _incident_list(*, payload: Any) -> list[dict[str, Any]]:
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

    def __init__(self, *, client: LoomClient, looper: Looper) -> None:
        self._client = client
        self._looper = looper

    async def get_diagnostics(self) -> dict[str, Any]:
        return await self._client.diagnostics.list_incidents()

    async def get_incidents_by_interface(self, *, interface_id: str) -> list[_Incident]:
        """Return one interface's incidents as records — the handler calls ``to_dict()`` on each."""
        incidents = _incident_list(payload=await self._client.diagnostics.list_incidents())
        return [_Incident(payload=i) for i in incidents if i.get("interface_id") == interface_id]

    async def get_recent_incidents(self, *, limit: int) -> list[dict[str, Any]]:
        """Return the most recent incidents as plain dicts (the handler forwards these verbatim)."""
        incidents = _incident_list(payload=await self._client.diagnostics.list_incidents())
        return incidents[:limit] if limit > 0 else incidents

    def clear_incidents(self) -> None:
        """
        Drop the daemon's incident store.

        The HA handler calls this synchronously, but the daemon owns the store
        and only a ``DELETE /incidents`` actually clears it — so the request is
        scheduled on the adapter's looper. A client-side no-op left the list
        unchanged and the panel's "clear" button dead.
        """
        self._looper.create_task(
            target=self._client.diagnostics.clear_incidents(),
            name="loom-clear-incidents",
        )


class _Recorder:
    """``cache_coordinator.recorder`` surface over the daemon's RPC recording."""

    def __init__(self, *, client: LoomClient) -> None:
        self._client = client

    async def activate(self, **options: Any) -> Any:
        return await self._client.diagnostics.start_rpc_recording(options=options)

    async def deactivate(self, **options: Any) -> Any:
        return await self._client.diagnostics.stop_rpc_recording(options=options)


class _CacheCoordinator:
    """``central.cache_coordinator`` surface."""

    def __init__(self, *, client: LoomClient, looper: Looper) -> None:
        self._client = client
        self._incident_store = _IncidentStore(client=client, looper=looper)
        self._recorder = _Recorder(client=client)

    async def clear_all(self) -> None:
        """
        Clear the CCU-derivable caches.

        Mirrors aiohomematic's ``clear_all`` scope (device + paramset
        descriptions, device details, data cache) via the daemon's
        ``POST /admin/cache/clear``. The narrower values-cache reset stays
        available on ``client.diagnostics.reset_values_cache``.
        """
        await self._client.diagnostics.clear_cache()

    @property
    def incident_store(self) -> _IncidentStore:
        return self._incident_store

    @property
    def recorder(self) -> _Recorder:
        return self._recorder


def _iso(*, value: Any) -> str:
    """Render a wire timestamp as an ISO string (the aiohomematic records type it as ``str``)."""
    if value is None:
        return ""
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _to_service_message(*, message: Any) -> ServiceMessageData:
    """
    Convert a wire ``ServiceMessage`` into aiohomematic's ``ServiceMessageData``.

    ``rooms`` / ``functions`` / ``last_timestamp`` have been on the
    aiohomematic record since it was written but stayed empty here until
    daemon api 5.0.0 started populating them: the CCU script emitted them
    all along, the loader never read them. ``description`` and
    ``priority`` went the other way — the script never emitted either, so
    both are gone from the wire and the record carries no slot for them.
    """
    return ServiceMessageData(
        msg_id=message.id,
        name=message.name,
        timestamp=_iso(value=message.timestamp),
        last_timestamp=_iso(value=message.last_timestamp),
        # The CCU's numeric message type has no daemon equivalent; the textual
        # `type` carries the same information and is what the card renders.
        msg_type=0,
        msg_type_name=message.type or "",
        display_name=message.display_name or "",
        address=message.address or "",
        device_name=message.device_name or "",
        counter=message.counter or 0,
        rooms=tuple(message.rooms or ()),
        functions=tuple(message.functions or ()),
        quittable=bool(message.quittable),
    )


def _to_alarm_message(*, message: Any) -> AlarmMessageData:
    """
    Convert a wire ``AlarmMessage`` into aiohomematic's ``AlarmMessageData``.

    An alarm entry is backed by an alarm system variable a program raises,
    not by a device — the CCU reports its trigger data point as the 65535
    "unknown" sentinel. ``device_name``, ``last_trigger`` and ``rooms``
    therefore never carried real data and left the wire in daemon api
    4.0.0; the record's defaults stand in for them rather than a value
    invented here. In their place ``last_timestamp`` carries the CCU's
    actual occurrence data.
    """
    return AlarmMessageData(
        alarm_id=message.id,
        name=message.name,
        display_name=message.display_name or "",
        description=message.description or "",
        timestamp=_iso(value=message.timestamp),
        last_timestamp=_iso(value=message.last_timestamp),
        counter=message.counter or 0,
    )


def _to_inbox_device(*, entry: Any) -> InboxDeviceData:
    """Convert a daemon inbox entry into aiohomematic's ``InboxDeviceData``."""
    data = _as_dict(entry=entry)
    address = data.get("address") or ""
    return InboxDeviceData(
        # The daemon's inbox DTO carries no ise_id; the serial is the stable
        # identity it does ship (the address is the fallback).
        device_id=data.get("serial") or address,
        address=address,
        name=address,
        device_type=data.get("model") or "",
        interface="",
    )


class _JsonRpcClient:
    """
    ``central.json_rpc_client`` surface (CCU-side message/inbox ops).

    Returns aiohomematic's record *dataclasses* — the HA handlers call
    ``dataclasses.asdict()`` on the lists — and ``bool`` from the mutations,
    which the handlers test (``if not success: send_error(…, "…_failed")``); a
    ``None`` return made every acknowledge/accept report failure even when the
    CCU-side write had succeeded.
    """

    def __init__(self, *, client: LoomClient) -> None:
        self._client = client

    async def get_service_messages(self, *, message_type: Any = None) -> tuple[ServiceMessageData, ...]:
        """Return the pending service messages as aiohomematic records."""
        messages = await self._client.hub.list_service_messages()
        return tuple(_to_service_message(message=message) for message in messages)

    async def get_alarm_messages(self) -> tuple[AlarmMessageData, ...]:
        """Return the pending alarm messages as aiohomematic records."""
        messages = await self._client.hub.list_alarm_messages()
        return tuple(_to_alarm_message(message=message) for message in messages)

    async def get_inbox_devices(self) -> tuple[InboxDeviceData, ...]:
        """Return the devices waiting in the CCU inbox as aiohomematic records."""
        entries = await self._client.hub.list_inbox()
        return tuple(_to_inbox_device(entry=entry) for entry in entries)

    async def accept_device_in_inbox(self, *, device_address: str) -> bool:
        """Accept a device out of the inbox. The transport raises on refusal, so reaching the return means success."""
        await self._client.devices.accept_device(address=device_address)
        return True

    async def acknowledge_message(self, *, message_id: str) -> bool:
        """
        Acknowledge a service *or* alarm message.

        aiohomematic exposes a single ack primitive and both HA handlers
        (``ws_acknowledge_service_message`` / ``ws_acknowledge_alarm_message``)
        route through it, but the daemon splits the two endpoints. Ids share the
        CCU id space, so try the service store first (the common case) and fall
        back to the alarm store; a failure of *both* propagates.
        """
        try:
            await self._client.hub.ack_service_message(message_id=message_id)
        except LoomHttpError:
            await self._client.hub.ack_alarm_message(message_id=message_id)
        return True

    async def rename_device(self, *, ise_id: int, new_name: str) -> bool:
        """Rename a device by its CCU ise_id (mapped to the address)."""
        address = next(
            (d.address for d in self._client.store.devices if d.ise_id == ise_id),
            None,
        )
        if address is None:
            # Must stay inside the aiohomematic hierarchy — the handler catches
            # BaseHomematicException and would otherwise leak an unknown_error.
            raise LoomNotFoundError(
                status=404, problem=None, raw_body=None, method="PATCH", url=f"/devices?ise_id={ise_id}"
            )
        await self._client.devices.patch_device(address=address, name=new_name)
        return True


# The daemon's wire `Link` / `LinkableChannel` models are field-for-field
# identical to aiohomematic's `DeviceLink` / `LinkableChannel` dataclasses, so
# the conversion is a straight copy. It has to happen, though:
# `homematicip_local`'s link handlers call `dataclasses.asdict()` on whatever
# the coordinator returns, which throws on a pydantic model.
_DEVICE_LINK_INT_FIELDS: Final = frozenset({"flags"})


def _as_dict(*, entry: Any) -> dict[str, Any]:
    """Return a wire model (or plain mapping) as a dict."""
    return dict(entry.model_dump(mode="json")) if hasattr(entry, "model_dump") else dict(entry)


def _to_device_link(*, link: Any) -> DeviceLink:
    """Convert a wire ``Link`` into aiohomematic's ``DeviceLink`` dataclass."""
    data = _as_dict(entry=link)
    values: dict[str, Any] = {}
    for field in dataclasses.fields(DeviceLink):
        value = data.get(field.name)
        if value is None:
            # The dataclass types the optional wire fields as plain str / int.
            value = 0 if field.name in _DEVICE_LINK_INT_FIELDS else ""
        values[field.name] = value
    return DeviceLink(**values)


def _to_linkable_channel(*, entry: Any) -> LinkableChannel:
    """Convert a wire ``LinkableChannel`` into aiohomematic's dataclass."""
    data = _as_dict(entry=entry)
    return LinkableChannel(
        **{field.name: (data.get(field.name) or "") for field in dataclasses.fields(LinkableChannel)}
    )


class _LinkCoordinator:
    """
    ``central.link`` surface (direct links).

    Signatures mirror aiohomematic's ``LinkCoordinator`` exactly — the HA
    handlers address channels by full ``"<device>:<channel>"`` address, expect a
    ``bool`` back from the mutations (they render ``add_link_failed`` /
    ``remove_link_failed`` on a falsy result) and call ``dataclasses.asdict()``
    on the read results.
    """

    def __init__(self, *, client: LoomClient) -> None:
        self._client = client

    async def add_link(
        self,
        *,
        sender_channel_address: str,
        receiver_channel_address: str,
        name: str = "",
        description: str = "created by HA",
    ) -> bool:
        """Create a direct link (sender → receiver). Returns ``False`` on a daemon refusal."""
        device, _ = _split_channel_address(channel_address=sender_channel_address)
        try:
            await self._client.links.add_link(
                address=device,
                sender_address=sender_channel_address,
                receiver_address=receiver_channel_address,
                name=name or f"{sender_channel_address} -> {receiver_channel_address}",
                description=description,
            )
        except BaseLoomException:
            _LOGGER.debug("add_link %s -> %s failed", sender_channel_address, receiver_channel_address, exc_info=True)
            return False
        return True

    async def remove_link(self, *, sender_channel_address: str, receiver_channel_address: str) -> bool:
        """Remove a direct link. Returns ``False`` on a daemon refusal."""
        device, _ = _split_channel_address(channel_address=sender_channel_address)
        try:
            await self._client.links.remove_link(
                address=device, sender=sender_channel_address, receiver=receiver_channel_address
            )
        except BaseLoomException:
            _LOGGER.debug(
                "remove_link %s -> %s failed", sender_channel_address, receiver_channel_address, exc_info=True
            )
            return False
        return True

    async def get_device_links(self, *, device_address: str, locale: str = "en") -> tuple[DeviceLink, ...]:
        """Return the device's direct links as aiohomematic ``DeviceLink`` dataclasses."""
        links = await self._client.links.list_links(address=device_address, locale=locale)
        return tuple(_to_device_link(link=link) for link in links)

    async def get_linkable_channels(
        self, *, interface_id: str, source_channel_address: str, role: str, locale: str = "en"
    ) -> tuple[LinkableChannel, ...]:
        """
        Return the channels eligible to link against ``source_channel_address``.

        Async on the loom backend (the daemon computes the candidates from the
        link-peer role metadata, which the client store does not hold), whereas
        aiohomematic answers it synchronously from its cached model. The HA
        handler dual-awaits via ``isawaitable``, the same accommodation it
        already makes for ``get_paramset_description``.
        """
        device, channel = _split_channel_address(channel_address=source_channel_address)
        payload = await self._client.links.linkable_channels(
            address=device,
            channel=channel,
            role=role,
            interface=interface_id,
            locale=locale,
        )
        entries = payload.get("channels", []) if isinstance(payload, dict) else (payload or [])
        return tuple(_to_linkable_channel(entry=entry) for entry in entries)


def _paramset_token(*, paramset_key: Any) -> str:
    """Normalise a ParamsetKey enum / string to the daemon's wire token."""
    return str(getattr(paramset_key, "value", paramset_key))


def _split_channel_address(*, channel_address: str) -> tuple[str, int]:
    """Split ``ABC1234567:3`` into ``("ABC1234567", 3)`` (channel 0 if absent)."""
    device, _, channel = channel_address.partition(":")
    return device, int(channel) if channel else 0


@dataclass(frozen=True, slots=True)
class _PutParamsetResult:
    """
    Structural twin of aiohomematic's ``PutParamsetResult``.

    ``homematicip_local``'s ``ws_put_paramset`` / ``ws_session_save`` read only
    ``success`` / ``validated`` / ``validation_errors`` off the result, so the
    loom facade returns this duck-typed record instead of importing the deep
    coordinator internal.
    """

    success: bool
    validated: bool
    validation_errors: Mapping[str, str]


# aiohomematic OPERATIONS / FLAGS bitmasks (aiohomematic.const.Operations / Flag).
_OP_READ: Final = 1
_OP_WRITE: Final = 2
_OP_EVENT: Final = 4
_FLAG_VISIBLE: Final = 1
_FLAG_INTERNAL: Final = 2
_FLAG_SERVICE: Final = 8


def _ui_schema_to_parameter_data(*, ui_schema: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """
    Translate the daemon's channel *ui-schema* into aiohomematic's ``ParameterData`` map.

    The daemon serves a rich, list-shaped renderable descriptor
    (``{parameters: [{name, type, min, max, operations: {read,write,event},
    flags: {visible,…}, value_list: [{value,key,label}]}]}``), but
    ``homematicip_local``'s ``FormSchemaGenerator`` and the config-session
    validator (both from ``aiohomematic_config``) expect aiohomematic's
    ``Mapping[str, ParameterData]`` — parameter *name* → a dict with the
    upper-cased ``TYPE`` / ``MIN`` / ``MAX`` / ``DEFAULT`` / ``UNIT`` /
    ``VALUE_LIST`` / ``OPERATIONS`` / ``FLAGS`` keys and integer operation/flag
    bitmasks. Without this the panel's form generation and change validation
    see an empty descriptor and render/validate nothing.
    """
    result: dict[str, dict[str, Any]] = {}
    for param in ui_schema.get("parameters") or ():
        name = param.get("name")
        if not name:
            continue
        ops = param.get("operations") or {}
        flags = param.get("flags") or {}
        data: dict[str, Any] = {
            "ID": name,
            "TYPE": param.get("type"),
            "OPERATIONS": (
                (_OP_READ if ops.get("read") else 0)
                | (_OP_WRITE if ops.get("write") else 0)
                | (_OP_EVENT if ops.get("event") else 0)
            ),
            "FLAGS": (
                (_FLAG_VISIBLE if flags.get("visible") else 0)
                | (_FLAG_INTERNAL if flags.get("internal") else 0)
                | (_FLAG_SERVICE if flags.get("service") else 0)
            ),
        }
        for wire_key, pd_key in (("min", "MIN"), ("max", "MAX"), ("default", "DEFAULT"), ("unit", "UNIT")):
            if (value := param.get(wire_key)) is not None:
                data[pd_key] = value
        if value_list := param.get("value_list"):
            # aiohomematic's VALUE_LIST is an index-ordered tuple of the enum keys.
            data["VALUE_LIST"] = tuple(entry["key"] for entry in sorted(value_list, key=lambda e: e.get("value", 0)))
        result[name] = data
    return result


class _Configuration:
    """
    ``central.configuration`` surface (paramset values + descriptors).

    The daemon exposes the renderable parameter descriptions as the
    channel *ui-schema* (``GET /devices/{addr}/channels/{no}/ui-schema``,
    ``paramset=VALUES|MASTER|LINK``), so the description getters are async
    here — unlike aiohomematic's cached, synchronous variants. HA's config
    websocket handlers must ``await`` them on the loom backend.
    """

    def __init__(self, *, client: LoomClient) -> None:
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
            address=target, paramset_key=_paramset_token(paramset_key=paramset_key)
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
        """Return the channel paramset descriptions as aiohomematic ``ParameterData`` (from the daemon ui-schema)."""
        device, channel = _split_channel_address(channel_address=channel_address)
        ui_schema = await self._client.devices.get_ui_schema(
            address=device,
            channel=channel,
            paramset=_paramset_token(paramset_key=paramset_key),
            peer=peer,
            locale=locale,
            expert=expert,
        )
        return _ui_schema_to_parameter_data(ui_schema=ui_schema)

    async def put_paramset(
        self,
        *,
        channel_address: str,
        paramset_key: Any,
        values: dict[str, Any],
        validate: bool = True,
        interface_id: str | None = None,
        **_kwargs: Any,
    ) -> _PutParamsetResult:
        """
        Validate (against the ui-schema descriptions) and write a channel paramset.

        Mirrors aiohomematic's ``ConfigurationCoordinator.put_paramset``: when
        ``validate`` is set, the values are checked against the parameter
        descriptions first and a failing result is returned *without* writing;
        otherwise the whole map is written transactionally
        (``PUT /devices/{addr}/paramsets/{key}``). The daemon raises a typed
        ``BaseHomematicException`` on a rejected write, which the handler maps
        to ``write_failed``.
        """
        token = _paramset_token(paramset_key=paramset_key)
        if validate:
            descriptions = await self.get_paramset_description(
                channel_address=channel_address, paramset_key=paramset_key
            )
            if failures := validate_paramset(descriptions=descriptions, values=values):
                return _PutParamsetResult(
                    success=False,
                    validated=True,
                    validation_errors={param: result.reason for param, result in failures.items()},
                )
        await self._client.datapoints.put_paramset(address=channel_address, paramset_key=token, values=values)
        return _PutParamsetResult(success=True, validated=validate, validation_errors={})

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
        """Return LINK-paramset descriptions (as aiohomematic ``ParameterData``) between a channel and a peer."""
        device, channel = _split_channel_address(channel_address=channel_address)
        ui_schema = await self._client.devices.get_ui_schema(
            address=device,
            channel=channel,
            paramset="LINK",
            peer=peer,
            locale=locale,
            expert=expert,
        )
        return _ui_schema_to_parameter_data(ui_schema=ui_schema)

    def get_configurable_devices(self, *, locale: str = "en", **_kwargs: Any) -> tuple[ConfigurableDevice, ...]:
        """Return configurable-device descriptors for the config UI."""
        return build_configurable_devices(store=self._client.store)


class LoomCentralAdapter:
    """Presents the ``aiohomematic.CentralUnit`` surface over ``LoomClient``."""

    def __init__(
        self,
        *,
        client: LoomClient,
        name: str,
        serial: str | None = None,
        locale: str = "en",
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
        self._system_information = make_system_information()
        # Make the store build categorised Dp* / CustomDp* instances so
        # HA-side isinstance dispatch works on the live objects. Must be
        # set before bootstrap() runs.
        client.store.set_data_point_factory(factory=make_generic_data_point)
        client.store.set_calculated_data_point_factory(factory=make_calculated_data_point)
        client.store.set_custom_data_point_factory(factory=make_custom_data_point)
        client.store.set_alarm_panel_factory(factory=make_alarm_panel_data_point)
        # Hub entities follow the same rule: one live object per entity, owned
        # by the store, so a catalogue refresh or a push reaches the instance
        # HA holds instead of a copy beside it.
        client.store.set_hub_data_point_factories(
            program_factory=make_program_data_points,
            sysvar_factory=make_sysvar_data_point,
        )
        # HA links every device to this central via Device.central_info.name,
        # which must equal the adapter name (the integration's instance name).
        client.store.set_central_name(central_name=name)
        # The HA UI language; entities read it back through
        # ``device.config_provider.config.locale`` (schedule names).
        client.store.set_locale(locale=locale)
        self._refresh_group: Any = None
        self._hub_reconcile_task: asyncio.Task[None] | None = None
        # Alarm-panel unique_ids already announced to HA via
        # DataPointsCreatedEvent — a live ``alarm.panel_changed`` push for a
        # panel outside this set means a new zone appeared at runtime and its
        # entity must be spawned (mirrors the device.created reconcile).
        self._announced_alarm_panel_ids: set[str] = set()
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
        # locale-aware labels of suppressed calculated DPs, keyed by
        # (device_address, channel_no) — consumed by the combined-DP
        # bootstrap to name the replacement number like the reference.
        self._suppressed_calc_labels: Final[dict[tuple[str, int], str]] = {}
        self.device_coordinator: Final = _DeviceCoordinator(client=client)
        self.hub_coordinator: Final = _HubCoordinator(
            client=client,
            ha_bus=self._ha_bus,
        )
        self.query_facade: Final = _QueryFacade(client=client, extra_data_points=self._extra_data_points)
        self.client_coordinator: Final = _ClientCoordinator(client=client)
        self.cache_coordinator: Final = _CacheCoordinator(client=client, looper=self._looper)
        self.json_rpc_client: Final = _JsonRpcClient(client=client)
        self.link: Final = _LinkCoordinator(client=client)
        self.configuration: Final = _Configuration(client=client)

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
    def config_ui_url(self) -> str:
        """
        Return the browser-reachable Config-UI address, or ``""``.

        Distinct from :attr:`url`, and the distinction is the whole point.
        :attr:`url` is how THIS process reaches the daemon — a container
        address, a LAN host behind a reverse proxy — which a browser on
        someone's desk may not be able to follow. This is the address the
        operator declared for people, via ``north.rest.public_url``.

        Empty when unconfigured, or against a daemon older than API
        5.14.0. Callers that need a link either fall back to deriving one
        from :attr:`url` or offer none; guessing here would override a
        caller that knows its own network better.
        """
        info = self._client.info
        return getattr(info, "config_ui_url", "") or ""

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

    @property
    def health(self) -> CentralHealth:
        """
        Return the central's health as aiohomematic's ``CentralHealth``.

        A *property* (``ws_get_system_health`` does ``central.health.to_dict()``
        with no await) returning the **real upstream record**, not the daemon's
        ``/health`` probe: the integration dashboard's health card is typed
        against ``SystemHealthData`` — ``central_state`` + ``overall_health_score``
        + ``client_health`` — which is exactly what ``CentralHealth.to_dict()``
        emits. Handing it the daemon's ``{status, components}`` probe instead gave
        the card none of the fields it renders.

        Built fresh from the live state (lifecycle state + one connection record
        per wired interface), so it needs no refresh cadence of its own.
        """
        health = CentralHealth()
        health.update_central_state(state=_to_aio_central_state(state=self._state))
        for state in self.client_coordinator.states:
            interface = _to_aio_interface(value=getattr(state, "interface", None))
            if interface is None:
                continue
            connection = health.register_client(interface_id=state.id, interface=interface)
            connection.client_state = (
                AioClientState.CONNECTED if getattr(state, "connected", False) else AioClientState.DISCONNECTED
            )
            if health.primary_interface is None:
                health.primary_interface = interface
        return health

    # ---- lifecycle ----

    async def start(self) -> None:
        """Connect, bootstrap the store, open the event stream, and install the refresh bridge."""
        try:
            await self._client.connect()
            await self._refresh_system_information()
            await self.client_coordinator.refresh()
            self._state = CentralState.Starting
            await self._client.bootstrap()
            await self._bootstrap_hub_catalogue()
            await self.hub_coordinator.fetch_hub_singleton_data()
            # Custom DPs first: schedule discovery and the combined duration
            # number key off the devices' CDP catalogue (aiohomematic builds
            # both through the custom data points).
            await self._bootstrap_custom_data_points()
            await self._bootstrap_schedules()
            await self._bootstrap_combined_data_points()
            await self.query_facade.prefetch_un_ignore_candidates()
            await self._client.start_events()
            # Fan the daemon's typed value events into the uniform
            # DataPointStateChangedEvent the HA entities subscribe to.
            self._refresh_group = self._client.events.create_subscription_group(name="loom-compat-refresh")
            install_refresh_bridge(
                group=self._refresh_group,
                store=self._client.store,
                ha_bus=self._ha_bus,
                central_name=self._name,
                event_group_resolver=self.query_facade.find_event_group,
            )
            # Route the daemon's hub-singleton push broadcasts (alarm/service/inbox
            # counts, metrics, connectivity, system-update, install-mode) straight
            # onto the singletons — this replaces the old 30 s poll loop. The
            # cold-start fetch above seeded the values once; the pushes keep them live.
            self.hub_coordinator.install_push_routing(group=self._refresh_group)
            # Spawn the HA entity for an alarm panel that appears at runtime
            # (zone created / master materialising at the second zone). The
            # wire bridge seeded the store stub before this handler runs —
            # subscription groups fan out in registration order.
            self._refresh_group.subscribe(event_type=LoomAlarmPanelChangedEvent, handler=self._on_alarm_panel_changed)
            # Announce every data point (generic + custom) in one batch *after*
            # the custom DPs are attached, so HA's platforms spawn entities for
            # them too. Published on the real aiohomematic bus as the real
            # DataPointsCreatedEvent HA subscribes to.
            await self._emit_data_points_created()
            # Slow reconcile backstop: re-seed the singletons from the aggregate so
            # a missed push can't drift. Every singleton (system_update included) is
            # push-driven now; this loop is pure resilience.
            self._hub_reconcile_task = asyncio.create_task(self._hub_reconcile_loop(), name="loom-hub-reconcile")
            self._state = CentralState.Running
        except Exception:
            # A failure partway through start() would otherwise orphan the
            # HTTP session, the WS reader + dispatch tasks, and the reconcile
            # loop — they accumulate across HA setup retries. stop() is
            # idempotent and None-safe, so it cleans up whatever was created.
            await self.stop()
            raise

    async def _on_alarm_panel_changed(self, event: LoomAlarmPanelChangedEvent, /) -> None:
        """Announce a net-new alarm panel to HA (entity spawn at runtime)."""
        payload = event.payload
        if payload.removed:
            self._announced_alarm_panel_ids.discard(payload.unique_id)
            return
        if payload.unique_id in self._announced_alarm_panel_ids:
            return
        panel = self._client.store.get_alarm_panel(unique_id=payload.unique_id)
        category = getattr(panel, "category", None)
        if panel is None or category is None:
            return
        try:
            aio_category = AioDataPointCategory(str(getattr(category, "value", category)))
        except ValueError:
            return  # installed aiohomematic lacks the category — same gate as the batch announce
        self._announced_alarm_panel_ids.add(payload.unique_id)
        await self._ha_bus.publish(
            event=AioDataPointsCreatedEvent(
                timestamp=datetime.now(tz=UTC),
                # The compat panel satisfies the callback protocol structurally
                # (register/unregister via the hub-entity surface); the domain
                # base type the store getter returns doesn't carry that in its
                # static type, hence the cast.
                new_data_points={aio_category: (cast("Any", panel),)},
            )
        )

    async def _hub_reconcile_loop(self) -> None:
        """Re-seed the hub singletons from the aggregate every reconcile interval."""
        while True:
            await asyncio.sleep(_HUB_RECONCILE_INTERVAL)
            try:
                await self.hub_coordinator.fetch_hub_singleton_data(scheduled=True)
            except Exception:
                _LOGGER.debug("hub singleton reconcile failed", exc_info=True)

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
            if device.summary.updatable
        ]
        event_groups = self.query_facade.get_event_groups(registered=False)
        alarm_panels = list(self._client.store.alarm_panels)
        # Identity map so only panels that actually survive the category gate
        # below count as announced — a gated panel must stay un-announced so
        # the runtime announce can pick it up once aiohomematic is capable.
        alarm_panel_uid_by_id = {id(panel): panel.unique_id for panel in alarm_panels}
        for dp in (
            *self._client.store.data_points,
            *self._client.store.custom_data_points,
            *self._extra_data_points,
            *hub_dps,
            *update_dps,
            *event_groups,
            *alarm_panels,
        ):
            loom_category = getattr(dp, "category", None)
            if loom_category is None:
                continue
            if not _is_creatable(dp=dp):
                continue
            # Loom and aiohomematic share identical category *values*; map by
            # value (the loom StrEnum's ``str()`` yields its repr, not the value).
            category_value = getattr(loom_category, "value", loom_category)
            try:
                aio_category = AioDataPointCategory(category_value)
            except ValueError:
                # A loom-only category the installed aiohomematic does not know
                # yet (e.g. alarm_control_panel before its enum lands upstream)
                # — skip rather than crash; the entities appear once
                # aiohomematic ships the member.
                _LOGGER.debug("skipping %s data points — aiohomematic lacks the category", category_value)
                continue
            grouped.setdefault(aio_category, []).append(dp)
            if (panel_uid := alarm_panel_uid_by_id.get(id(dp))) is not None:
                self._announced_alarm_panel_ids.add(panel_uid)
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
        except Exception:
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
                self._client.store.attach_custom_data_points(device_address=device.address, cdps=cdps)
            for channel in device.channels:
                calculated = await self._client.devices.list_calculated_data_points(
                    address=device.address, channel=channel.number
                )
                # The combined duration number replaces the daemon's
                # calculated DURATION sensor (the ccu twin has none) —
                # but its locale-aware label names the combined number.
                for calc in calculated:
                    if calc.name in _SUPPRESSED_CALCULATED_NAMES and calc.translated_name:
                        self._suppressed_calc_labels[(device.address, channel.number)] = str(calc.translated_name)
                calculated = [calc for calc in calculated if calc.name not in _SUPPRESSED_CALCULATED_NAMES]
                if calculated:
                    self._client.store.attach_channel_calculated_data_points(
                        device_address=device.address,
                        channel_number=channel.number,
                        calculated=calculated,
                    )

    async def _bootstrap_schedules(self) -> None:
        """
        Build the week-profile and schedule-switch data points.

        aiohomematic initialises a device's week profile only through a
        custom data point, so devices without a CDP never spawn schedule
        entities. The schedule channel is the channel whose type ends in
        ``WEEK_PROFILE``; climate devices carry no such channel — their
        schedule lives on the climate CDP's channel, probed directly
        (a 404 means the device has no schedule). Per schedule channel,
        the daemon's week-profile descriptor spawns one
        :class:`WeekProfileDp` (entry count loaded from the channel
        schedule, fetch errors degrade to "unknown") plus — for
        non-climate devices only, like aiohomematic — one
        :class:`ScheduleChannelSwitch` per ``schedule_enabled`` key.
        """
        store = self._client.store
        for device in list(store.devices):
            cdps = store.custom_data_points_of(address=device.address)
            if not cdps:
                continue
            climate_cdp = next((cdp for cdp in cdps if (cdp.summary.category or "") == "climate"), None)
            week_profile_channel_no = next(
                (
                    channel.number
                    for channel in store.channels_of(address=device.address)
                    if (channel.channel_type or "").endswith(_WEEK_PROFILE_CHANNEL_SUFFIX)
                ),
                None,
            )
            # The schedule lives on the WEEK_PROFILE channel when one exists,
            # otherwise on the climate CDP's own channel (probed directly).
            schedule_channel_no = week_profile_channel_no
            if schedule_channel_no is None:
                if climate_cdp is None:
                    continue
                schedule_channel_no = climate_cdp.summary.channel_no
            # Per-channel schedule switches are bound only to a non-climate
            # (DefaultWeekProfile) schedule — i.e. one that lives on a
            # WEEK_PROFILE channel, mirroring the reference, which gates the
            # switches on the week profile NOT being a ClimateWeekProfile.
            # A device may carry a climate CDP and still expose its schedule on
            # a WEEK_PROFILE channel (HmIP-WGTC): the switches follow the
            # channel, not the mere presence of a climate CDP.
            await self._spawn_schedule_data_points(
                device=device,
                channel_no=schedule_channel_no,
                with_switches=week_profile_channel_no is not None,
            )

    async def _spawn_schedule_data_points(self, *, device: Device, channel_no: int, with_switches: bool) -> None:
        """Probe one schedule channel and spawn its week-profile (+switch) DPs."""
        store = self._client.store
        try:
            week_profile = await self._client.schedules.get_channel_week_profile(
                address=device.address, channel=channel_no
            )
        except Exception:
            _LOGGER.debug("no week profile on %s:%s", device.address, channel_no, exc_info=True)
            return
        # Climate schedules get the ClimateWeekProfileDp (satisfies
        # ClimateWeekProfileDataPointProtocol); simple schedules get the base
        # WeekProfileDp — the isinstance split the schedule facade and the HA
        # climate/sensor entities branch on, mirroring the reference's
        # ClimateWeekProfile / DefaultWeekProfile classes.
        wp_cls = (
            ClimateWeekProfileDp
            if str(getattr(week_profile.schedule_type, "value", "")) == "climate"
            else WeekProfileDp
        )
        wp_dp = wp_cls(
            store=store,
            device=device,
            channel_no=channel_no,
            week_profile=week_profile,
            schedules_ops=self._client.schedules,
        )
        try:
            schedule = await self._client.schedules.get_channel_schedule(address=device.address, channel=channel_no)
        except Exception:
            _LOGGER.debug("schedule fetch failed for %s:%s", device.address, channel_no, exc_info=True)
        else:
            wp_dp.update_from(schedule=schedule)
        self._extra_data_points.append(wp_dp)
        # Register on the store so the base Device can expose it as
        # ``week_profile_data_point`` (the HA schedule services reach it there).
        store.set_week_profile_data_point(address=device.address, data_point=wp_dp)
        if not with_switches:
            return
        for channel_key in week_profile.schedule_enabled or {}:
            self._extra_data_points.append(
                ScheduleChannelSwitch(
                    store=store,
                    device=device,
                    channel_no=channel_no,
                    channel_key=channel_key,
                    week_profile_dp=wp_dp,
                    schedules_ops=self._client.schedules,
                )
            )

    async def _bootstrap_combined_data_points(self) -> None:
        """
        Build the combined duration number for siren channels.

        aiohomematic's only *visible* combined timer is
        ``CustomDpIpSiren._dp_duration`` (sound players declare the
        DURATION pair too, but invisible), so the seconds-typed number
        spawns solely on channels hosting a plain siren CDP that carries
        both ``DURATION_VALUE`` and ``DURATION_UNIT``.
        """
        store = self._client.store
        for device in list(store.devices):
            for cdp in store.custom_data_points_of(address=device.address):
                if not isinstance(cdp, BaseCustomDpSiren) or isinstance(cdp, CustomDpSoundPlayer):
                    continue
                channel_no = cdp.summary.channel_no
                if channel_has_duration_pair(store=store, address=device.address, channel_no=channel_no):
                    self._extra_data_points.append(
                        CombinedDurationDp(
                            store=store,
                            device=device,
                            channel_no=channel_no,
                            translated_name=self._suppressed_calc_labels.get((device.address, channel_no)),
                        )
                    )

    async def stop(self) -> None:
        """Cancel the reconcile loop + refresh bridge, close the client, and stop."""
        if self._hub_reconcile_task is not None:
            self._hub_reconcile_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._hub_reconcile_task
            self._hub_reconcile_task = None
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
        try:
            await self._client.connect()
            await self._refresh_system_information()
        except Exception:
            # Pre-flight failure must not leave the just-opened HTTP session
            # dangling (config-flow retries would leak one per attempt).
            await self._client.close()
            raise
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
        self._client.store.set_serial(serial=serial)
        interfaces: tuple[str, ...] = ()
        try:
            interfaces = tuple(i.id for i in await self._client.system.list_interfaces())
        except Exception:  # noqa: BLE001 — interfaces endpoint is optional
            _LOGGER.debug("interfaces unavailable during system-information refresh")
        self._system_information = make_system_information(
            serial=serial,
            version=info.version,
            available_interfaces=interfaces,
            # The CCU dashboard renders these; the daemon reports them on the
            # /system/ccu entry. The two security flags describe the *CCU's
            # own* posture (api ≥ 3.5.0) — not this client's auth, which is
            # always on. They stay ``None`` on an older daemon or before the
            # first successful CCU connect, which the dashboard renders as
            # "unknown" rather than as a claim either way.
            hostname=getattr(ccu_entry, "hostname", None) if ccu_entry is not None else None,
            is_ha_app=bool(getattr(ccu_entry, "is_ha_app", False)) if ccu_entry is not None else False,
            auth_enabled=getattr(ccu_entry, "auth_enabled", None) if ccu_entry is not None else None,
            https_redirect_enabled=(
                getattr(ccu_entry, "https_redirect_enabled", None) if ccu_entry is not None else None
            ),
        )
