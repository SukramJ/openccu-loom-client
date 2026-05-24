# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""``aiohomematic.CentralUnit`` adapter backed by :class:`LoomClient`.

``homematicip_local`` does not just hold a ``CentralUnit`` reference —
it reaches into a coordinator surface (``central.device_coordinator``,
``central.hub_coordinator``, ``central.query_facade``,
``central.client_coordinator``, ``central.cache_coordinator``,
``central.json_rpc_client``, ``central.link``, …). This module presents
that surface on top of the daemon-mediated :class:`LoomClient`, so the
component can run against an openccu-loom daemon with the same call
sites it uses for the direct-CCU aiohomematic backend.

Scope of this adapter:

* **Implemented** — lifecycle (``start``/``stop``), identity
  (``name``/``model``/``version``/``url``/``state``/``available``/
  ``system_information``/``health``), the event bus, and the *action*
  coordinators (device lookup/removal, sysvar/program fetch + write,
  links, service/alarm messages + ack, inbox accept, rename, paramset
  read, backup, values-cache clear, un-ignore candidates).

* **Stubbed (``NotImplementedError`` with a TODO)** — the
  entity-spawn surface that depends on aiohomematic's *categorized
  data-point model* (``query_facade.get_data_points`` filtered by
  ``data_point_type``/``category`` with ``unique_id`` + ``registered``
  bookkeeping, ``hub_coordinator.get_hub_data_points`` and the
  ``*_dp`` collections, ``get_event_groups``, ``get_state_paths``).
  Porting that model onto the :class:`LoomStore` is a separate, larger
  workstream — these raise loudly rather than return wrong shapes.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Final

from openccu_loom_types.enums import CentralState, DataPointCategory

from openccu_loom_client.compat.aiohomematic.central.refresh import install_refresh_bridge
from openccu_loom_client.compat.aiohomematic.const import SystemInformation
from openccu_loom_client.compat.aiohomematic.model.custom import make_custom_data_point
from openccu_loom_client.compat.aiohomematic.model.generic import make_generic_data_point
from openccu_loom_client.compat.aiohomematic.model.hub import (
    make_program_data_point,
    make_sysvar_data_point,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from openccu_loom_client.client import LoomClient
    from openccu_loom_client.events import EventBus
    from openccu_loom_client.model import Device

_LOGGER: Final = logging.getLogger(__name__)

# Marker used in every stub so the punch-list is greppable and the HA
# log explains *why* a call failed rather than dying on an AttributeError.
_MODEL_PORT_TODO: Final = (
    "requires the categorized data-point model port onto LoomStore "
    "(unique_id / category / data_point_type / registered bookkeeping) — "
    "tracked as the data-point-model workstream"
)


def _not_implemented(what: str, reason: str) -> NotImplementedError:
    return NotImplementedError(f"LoomCentralAdapter.{what}: {reason}")


def _category_for_type(data_point_type: Any) -> DataPointCategory | None:
    """Map a coarse ``DataPointType`` (platform) to its custom-DP category.

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

    def add_new_devices_manually(self, *_args: Any, **_kwargs: Any) -> None:
        raise _not_implemented(
            "device_coordinator.add_new_devices_manually",
            "device creation is daemon-driven; accept pairing candidates "
            "via json_rpc_client.accept_device_in_inbox instead",
        )


class _HubCoordinator:
    """``central.hub_coordinator`` surface (sysvars, programs, messages)."""

    def __init__(self, client: LoomClient) -> None:
        self._client = client
        # Cache hub data points by unique_id so register()/unregister()
        # bookkeeping survives repeated get_hub_data_points() scans.
        self._cache: dict[str, Any] = {}

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

    def _all_hub_data_points(self) -> list[Any]:
        """Build (and cache) categorised hub data points from the store."""
        live: dict[str, Any] = {}
        for sysvar in self._client.store.sysvars:
            sv_dp: Any = make_sysvar_data_point(
                summary=sysvar.summary, store=self._client.store
            )
            live[sv_dp.unique_id] = sv_dp
        for program in self._client.store.programs:
            pr_dp: Any = make_program_data_point(
                summary=program.summary, store=self._client.store
            )
            live[pr_dp.unique_id] = pr_dp
        # Reuse cached instances (preserving their registered flag) and
        # drop entries whose sysvar/program disappeared.
        for uid, dp in live.items():
            if uid not in self._cache:
                self._cache[uid] = dp
        for uid in list(self._cache):
            if uid not in live:
                del self._cache[uid]
        return list(self._cache.values())

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
        raise _not_implemented(
            "hub_coordinator.get_sysvar_data_point",
            "state-path addressing has no daemon equivalent; look up by sysvar name",
        )

    @property
    def install_mode_dps(self) -> dict[str, Any]:
        raise _not_implemented("hub_coordinator.install_mode_dps", _MODEL_PORT_TODO)


class _QueryFacade:
    """``central.query_facade`` surface."""

    def __init__(self, client: LoomClient) -> None:
        self._client = client

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
        """Device data points (generic + custom), filtered like aiohomematic.

        The store holds categorised ``Dp*`` and ``CustomDp*`` instances
        (built by the injected factories). Generic platforms pass an
        explicit ``category``; custom platforms (light/cover/climate/…)
        pass only ``data_point_type``, whose name matches the custom
        ``category`` — so an unset ``category`` is derived from
        ``data_point_type``.
        """
        target = category if category is not None else _category_for_type(data_point_type)
        out: list[Any] = []
        for dp in (*self._client.store.data_points, *self._client.store.custom_data_points):
            dp_category = getattr(dp, "category", None)
            if target is not None and dp_category != target:
                continue
            if registered is not None and getattr(dp, "is_registered", False) != registered:
                continue
            out.append(dp)
        return tuple(out)

    def get_generic_data_point(self, *, state_path: str) -> Any:
        raise _not_implemented("query_facade.get_generic_data_point", _MODEL_PORT_TODO)

    def get_event_groups(self, **_kwargs: Any) -> tuple[Any, ...]:
        raise _not_implemented(
            "query_facade.get_event_groups",
            "device trigger (keypress) events are not yet broadcast by the daemon",
        )

    def get_state_paths(self, **_kwargs: Any) -> tuple[Any, ...]:
        raise _not_implemented(
            "query_facade.get_state_paths",
            "aiohomematic state-path addressing has no daemon equivalent; "
            "the loom client addresses by (address, channel, parameter)",
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


class _CacheCoordinator:
    """``central.cache_coordinator`` surface."""

    def __init__(self, client: LoomClient) -> None:
        self._client = client

    async def clear_all(self) -> None:
        await self._client.diagnostics.reset_values_cache()

    @property
    def incident_store(self) -> Any:
        raise _not_implemented(
            "cache_coordinator.incident_store",
            "expose incidents via client.diagnostics.list_incidents() instead",
        )

    @property
    def recorder(self) -> Any:
        raise _not_implemented(
            "cache_coordinator.recorder",
            "use client.diagnostics.start_rpc_recording() / list_rpc_recordings()",
        )


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
        raise _not_implemented(
            "json_rpc_client.rename_device",
            "the daemon renames by device address (PATCH /devices/{addr}); "
            "map the CCU ise_id to an address before calling "
            "client.devices.patch_device",
        )


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

    async def remove_link(
        self, *, address: str, sender: str, receiver: str
    ) -> None:
        await self._client.links.remove_link(
            address=address, sender=sender, receiver=receiver
        )

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


class _Configuration:
    """``central.configuration`` surface (paramset descriptors)."""

    def __init__(self, client: LoomClient) -> None:
        self._client = client

    async def get_paramset(
        self, *, address: str, paramset_key: str
    ) -> dict[str, Any]:
        return await self._client.datapoints.get_paramset(
            address=address, paramset_key=paramset_key
        )

    def get_link_paramset_description(self, **_kwargs: Any) -> Any:
        raise _not_implemented(
            "configuration.get_link_paramset_description",
            "available over WS (links.get_form_schema), not yet wrapped in the REST client",
        )

    def get_configurable_devices(self, **_kwargs: Any) -> Any:
        raise _not_implemented(
            "configuration.get_configurable_devices",
            "derive from client.store.devices once the config-panel routing lands",
        )


class LoomCentralAdapter:
    """Presents the ``aiohomematic.CentralUnit`` surface over ``LoomClient``."""

    def __init__(self, *, client: LoomClient, name: str) -> None:
        self._client = client
        self._name = name
        self._state: CentralState = CentralState.Stopped
        self._system_information = SystemInformation()
        # Make the store build categorised Dp* / CustomDp* instances so
        # HA-side isinstance dispatch works on the live objects. Must be
        # set before bootstrap() runs.
        client.store.set_data_point_factory(make_generic_data_point)
        client.store.set_custom_data_point_factory(make_custom_data_point)
        self._refresh_group: Any = None
        self.device_coordinator: Final = _DeviceCoordinator(client)
        self.hub_coordinator: Final = _HubCoordinator(client)
        self.query_facade: Final = _QueryFacade(client)
        self.client_coordinator: Final = _ClientCoordinator(client)
        self.cache_coordinator: Final = _CacheCoordinator(client)
        self.json_rpc_client: Final = _JsonRpcClient(client)
        self.link: Final = _LinkCoordinator(client)
        self.configuration: Final = _Configuration(client)

    # ---- identity ----

    @property
    def name(self) -> str:
        return self._name

    @property
    def model(self) -> str:
        return "openccu-loom"

    @property
    def version(self) -> str | None:
        # Populated by start() / validate_config_and_get_system_information().
        return self._system_information.version

    @property
    def url(self) -> str:
        return self._client.config.http_base_url

    @property
    def state(self) -> CentralState:
        return self._state

    @property
    def available(self) -> bool:
        return self._state in (CentralState.Running, CentralState.Degraded)

    @property
    def system_information(self) -> SystemInformation:
        return self._system_information

    @property
    def config(self) -> Any:
        return self._client.config

    @property
    def events(self) -> EventBus:
        return self._client.events

    @property
    def event_bus(self) -> EventBus:
        return self._client.events

    async def health(self) -> Any:
        return await self._client.system.get_health()

    # ---- lifecycle ----

    async def start(self) -> None:
        await self._client.connect()
        await self._refresh_system_information()
        await self.client_coordinator.refresh()
        self._state = CentralState.Starting
        await self._client.bootstrap()
        await self._bootstrap_custom_data_points()
        await self._client.start_events()
        # Fan the daemon's typed value events into the uniform
        # DataPointStateChangedEvent the HA entities subscribe to.
        self._refresh_group = self._client.events.create_subscription_group(
            name="loom-compat-refresh"
        )
        install_refresh_bridge(bus=self._client.events, group=self._refresh_group)
        self._state = CentralState.Running

    async def _bootstrap_custom_data_points(self) -> None:
        """Fetch each device's Custom DPs into the store (categorised).

        The core bootstrap covers devices/channels/generic data points;
        custom DPs are an HA-backend concern, so the adapter pulls them
        here. State arrives later via ``custom_data_point.state_changed``.
        """
        for device in list(self._client.store.devices):
            cdps = await self._client.custom_data_points.list_for_device(
                address=device.address
            )
            if cdps:
                self._client.store.attach_custom_data_points(
                    device_address=device.address, cdps=cdps
                )

    async def stop(self) -> None:
        if self._refresh_group is not None:
            self._refresh_group.cancel()
            self._refresh_group = None
        await self._client.close()
        self._state = CentralState.Stopped

    async def validate_config_and_get_system_information(self) -> SystemInformation:
        """Pre-flight used by the HA config flow.

        Opens the session (capability handshake), reads daemon + CCU
        metadata, and returns it without starting the event stream.
        """
        await self._client.connect()
        await self._refresh_system_information()
        return self._system_information

    async def create_backup_and_download(self) -> dict[str, Any]:
        """Trigger a CCU backup.

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
        except Exception:
            _LOGGER.debug("system/ccu unavailable during system-information refresh")
            ccus = []
        serial = ccus[0].serial if ccus and getattr(ccus[0], "serial", None) else None
        interfaces: tuple[str, ...] = ()
        try:
            interfaces = tuple(i.id for i in await self._client.system.list_interfaces())
        except Exception:
            _LOGGER.debug("interfaces unavailable during system-information refresh")
        self._system_information = SystemInformation(
            serial=serial,
            version=info.version,
            available_interfaces=interfaces,
        )
