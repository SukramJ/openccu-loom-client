# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Hub singleton data points (messages, inbox, metrics, update, install mode).

aiohomematic spawns a fixed set of per-central hub entities next to the
sysvar/program layer (``model/hub/alarm_messages.py`` & friends). This
module is their loom twin: each singleton carries the exact unique_id
the aiohomematic registry would produce (``loom_<serial10>_hub_<slug>``,
install mode on the ``install_mode`` pseudo-address) so HA's one-time
registry migration matches the live keys. Values arrive via the
adapter's :meth:`fetch_hub_singleton_data` poll of the daemon's hub
endpoints (``/alarm-messages``, ``/service-messages``, ``/inbox``,
``/system/metrics``, ``/system/update``, ``/install-mode/interfaces``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar, Final, NamedTuple

from aiohomematic.const import HUB_ADDRESS, INSTALL_MODE_ADDRESS
from openccu_loom_types.enums import DataPointCategory
from slugify import slugify

from openccu_loom_client.canonical import canonical_unique_id
from openccu_loom_client.compat.aiohomematic.model._protocol_surface import _SysvarProtocolSurface
from openccu_loom_client.compat.aiohomematic.model.hub._surface import _HubEntitySurface

if TYPE_CHECKING:
    from collections.abc import Sequence

    from openccu_loom_types.rest import SystemUpdateEntry

    from openccu_loom_client.operations.hub import HubOperations
    from openccu_loom_client.operations.system import SystemOperations
    from openccu_loom_client.store import LoomStore

# Interface → install-mode token, mirroring aiohomematic's
# ``interface_suffix`` (hub.py): only HmIP-RF and BidCos-RF carry an
# install-mode entity pair; other interfaces are skipped.
INSTALL_MODE_TOKEN_BY_INTERFACE: Final[dict[str, str]] = {
    "HmIP-RF": "hmip",
    "BidCos-RF": "bidcos",
}


class HubSingletonDp(_HubEntitySurface, _SysvarProtocolSurface):
    """
    Base class for per-central hub singleton data points.

    Carries the value/attribute slots the adapter's hub poll writes and
    the canonical unique_id (``loom_<serial10>_<address>_<slug>``). The
    protocol-surface mixins fill the aiohomematic long tail so HA's
    structural ``isinstance`` dispatch and entity-description lookup
    (``var_name_contains`` on :attr:`name`) work on the live object.
    """

    _address: ClassVar[str] = HUB_ADDRESS
    _data_type: ClassVar[str | None] = None
    _unit: ClassVar[str | None] = None

    def __init__(
        self,
        *,
        store: LoomStore,
        name: str,
        parameter_slug: str,
        translation_key: str,
        enabled_default: bool = True,
    ) -> None:
        """Bind the singleton to its store, registry slug and translation key."""
        self._store = store
        self._name = name
        self._parameter_slug = parameter_slug
        self._translation_key = translation_key
        # Read back by _HubEntitySurface.enabled_default — singletons are
        # enabled by default in aiohomematic (unlike generic sysvars).
        self._enabled_default = enabled_default
        self._value: Any = None
        self._attributes: dict[str, Any] = {}
        self._modified: datetime | None = None
        self._refreshed: datetime | None = None

    # ---- identity ----

    @property
    def unique_id(self) -> str:
        """Return the canonical key ``loom_<serial10>_<address>_<slug>``."""
        return canonical_unique_id(
            serial_suffix=self._store.serial_suffix,
            address=self._address,
            parameter=self._parameter_slug,
        )

    @property
    def name(self) -> str:
        """Return the registry name (entity-description ``var_name`` match)."""
        return self._name

    @property
    def translation_key(self) -> str:
        """Return the HA translation key."""
        return self._translation_key

    @property
    def summary(self) -> None:
        """Return ``None`` — singletons have no wire-side summary record."""
        return None

    @property
    def description(self) -> str | None:
        """Return the data-point description."""
        return None

    # ---- value / state ----

    @property
    def value(self) -> Any:
        """Return the last fetched value (``None`` until observed)."""
        return self._value

    @property
    def is_valid(self) -> bool:
        """Return whether a value has been observed."""
        return self._value is not None

    @property
    def additional_information(self) -> dict[str, Any]:
        """Return the extra state attributes of the singleton."""
        return dict(self._attributes)

    @property
    def attributes(self) -> dict[str, Any]:
        """Return the extra state attributes of the singleton."""
        return dict(self._attributes)

    @property
    def data_type(self) -> str | None:
        """Return the hub value type token (``INTEGER`` / ``FLOAT`` / ``LOGIC``)."""
        return self._data_type

    @property
    def unit(self) -> str | None:
        """Return the unit of the value."""
        return self._unit

    @property
    def values(self) -> tuple[str, ...]:
        """Return the allowed value list (none for singletons)."""
        return ()

    @property
    def value_list(self) -> tuple[str, ...]:
        """Return the allowed value list (none for singletons)."""
        return ()

    @property
    def modified_at(self) -> datetime | None:
        """Return when the value last changed."""
        return self._modified

    @property
    def refreshed_at(self) -> datetime | None:
        """Return when the value was last fetched."""
        return self._refreshed

    def update_value(self, *, value: Any, attributes: dict[str, Any] | None = None) -> bool:
        """
        Apply a fetched value (plus extra attributes).

        Returns whether anything changed — the adapter pings the keyed
        HA state-changed event only for changed singletons.
        """
        new_attributes = dict(attributes or {})
        now = datetime.now(tz=UTC)
        changed = value != self._value or new_attributes != self._attributes
        if changed:
            self._value = value
            self._attributes = new_attributes
            self._modified = now
        self._refreshed = now
        return changed


# ---- message / inbox sensors ----


class AlarmMessagesSensor(HubSingletonDp):
    """Count of pending CCU alarm messages, one attribute per message."""

    _data_type: ClassVar[str | None] = "INTEGER"

    def __init__(self, *, store: LoomStore) -> None:
        """Bind the alarm-messages singleton to the store."""
        super().__init__(
            store=store,
            name="alarm_messages",
            parameter_slug="alarm-messages",
            translation_key="alarm_messages",
        )

    def update_messages(self, *, messages: Sequence[Any]) -> bool:
        """Apply the fetched alarm list: count + ``alarm_<n>`` attributes."""
        attributes = {
            f"alarm_{idx}": (
                f"{message.device_name}: {message.name}"
                if getattr(message, "device_name", None)
                else str(message.name)
            )
            for idx, message in enumerate(messages, start=1)
        }
        return self.update_value(value=len(messages), attributes=attributes)


class ServiceMessagesSensor(HubSingletonDp):
    """Count of pending CCU service messages, one attribute per message."""

    _data_type: ClassVar[str | None] = "INTEGER"

    def __init__(self, *, store: LoomStore) -> None:
        """Bind the service-messages singleton to the store."""
        super().__init__(
            store=store,
            name="service_messages",
            parameter_slug="service-messages",
            translation_key="service_messages",
        )

    def update_messages(self, *, messages: Sequence[Any]) -> bool:
        """Apply the fetched service list: count + ``message_<n>`` attributes."""
        attributes = {
            f"message_{idx}": (
                f"{message.device_name}: {message.name}"
                if getattr(message, "device_name", None)
                else str(message.name)
            )
            for idx, message in enumerate(messages, start=1)
        }
        return self.update_value(value=len(messages), attributes=attributes)


class InboxSensor(HubSingletonDp):
    """Count of devices waiting in the CCU inbox."""

    _data_type: ClassVar[str | None] = "INTEGER"

    def __init__(self, *, store: LoomStore) -> None:
        """Bind the inbox singleton to the store."""
        super().__init__(
            store=store,
            name="inbox",
            parameter_slug="inbox",
            translation_key="inbox",
        )


# ---- metrics sensors ----


class SystemHealthSensor(HubSingletonDp):
    """Overall system health score (0–100 %)."""

    _data_type: ClassVar[str | None] = "FLOAT"
    _unit: ClassVar[str | None] = "%"

    def __init__(self, *, store: LoomStore) -> None:
        """Bind the system-health singleton to the store."""
        super().__init__(
            store=store,
            name="system_health",
            parameter_slug="system-health",
            translation_key="system_health",
        )


class ConnectionLatencySensor(HubSingletonDp):
    """Average backend connection latency in milliseconds."""

    _data_type: ClassVar[str | None] = "FLOAT"
    _unit: ClassVar[str | None] = "ms"

    def __init__(self, *, store: LoomStore) -> None:
        """Bind the connection-latency singleton to the store."""
        super().__init__(
            store=store,
            name="connection_latency",
            parameter_slug="connection-latency",
            translation_key="connection_latency",
        )


class LastEventAgeSensor(HubSingletonDp):
    """Seconds since the last backend event was received."""

    _data_type: ClassVar[str | None] = "FLOAT"
    _unit: ClassVar[str | None] = "s"

    def __init__(self, *, store: LoomStore) -> None:
        """Bind the last-event-age singleton to the store."""
        super().__init__(
            store=store,
            name="last_event_age",
            parameter_slug="last-event-age",
            translation_key="last_event_age",
        )


# ---- interface connectivity ----


class InterfaceConnectivityDp(HubSingletonDp):
    """Binary sensor showing one interface's connectivity state."""

    _category: ClassVar[DataPointCategory] = DataPointCategory.HubBinarySensor
    _data_type: ClassVar[str | None] = "LOGIC"

    def __init__(self, *, store: LoomStore, interface_id: str) -> None:
        """Bind the connectivity singleton to its interface."""
        super().__init__(
            store=store,
            name=f"Connectivity {interface_id}",
            parameter_slug=f"connectivity-{slugify(interface_id)}",
            translation_key="interface_connectivity",
        )
        self._interface_id: Final = interface_id

    @property
    def interface_id(self) -> str:
        """Return the interface id this sensor tracks."""
        return self._interface_id

    @property
    def available(self) -> bool:
        """Return ``True`` — the sensor itself shows the connection state."""
        return True


# ---- system update ----


class SystemUpdateDp(HubSingletonDp):
    """CCU system-update data point (aiohomematic ``HmUpdate`` twin)."""

    _category: ClassVar[DataPointCategory] = DataPointCategory.HubUpdate

    def __init__(self, *, store: LoomStore, system_ops: SystemOperations) -> None:
        """Bind the system-update singleton to the store and system operations."""
        super().__init__(
            store=store,
            name="System Update",
            parameter_slug="system-update",
            translation_key="system_update",
        )
        self._system_ops: Final = system_ops
        self._current_firmware: str = ""
        self._available_firmware: str = ""
        self._update_available: bool = False
        self._in_progress: bool = False

    @property
    def current_firmware(self) -> str:
        """Return the installed CCU firmware version."""
        return self._current_firmware

    @property
    def firmware(self) -> str:
        """Return the installed CCU firmware version (alias)."""
        return self._current_firmware

    @property
    def available_firmware(self) -> str:
        """Return the firmware version available for install."""
        return self._available_firmware

    @property
    def latest_firmware(self) -> str:
        """Return the latest installable firmware, falling back to the installed one."""
        return self._available_firmware or self._current_firmware

    @property
    def update_available(self) -> bool:
        """Return whether a CCU system update is available."""
        return self._update_available

    @property
    def in_progress(self) -> bool:
        """Return whether a system update is currently installing."""
        return self._in_progress

    def update_data(self, *, entry: SystemUpdateEntry) -> bool:
        """Apply a fetched system-update entry; return whether anything changed."""
        new = (
            entry.current_firmware or "",
            entry.available_firmware or "",
            bool(entry.update_available),
            bool(entry.in_progress),
        )
        old = (
            self._current_firmware,
            self._available_firmware,
            self._update_available,
            self._in_progress,
        )
        now = datetime.now(tz=UTC)
        if new != old:
            (
                self._current_firmware,
                self._available_firmware,
                self._update_available,
                self._in_progress,
            ) = new
            self._modified = now
            self._refreshed = now
            return True
        self._refreshed = now
        return False

    async def install(self) -> bool:
        """Trigger the CCU system-update install via the daemon."""
        await self._system_ops.install_system_update(central=self._store.central_name or None)
        # Optimistic: the daemon broadcasts the real progress on the
        # next /system/update poll; flip immediately so HA shows it.
        self._in_progress = True
        return True


# ---- install mode ----


class InstallModeDpSensor(HubSingletonDp):
    """Sensor showing the remaining install-mode seconds of one interface."""

    _address: ClassVar[str] = INSTALL_MODE_ADDRESS
    _data_type: ClassVar[str | None] = "INTEGER"

    def __init__(self, *, store: LoomStore, interface: str) -> None:
        """Bind the sensor to its interface (``HmIP-RF`` / ``BidCos-RF``)."""
        token = INSTALL_MODE_TOKEN_BY_INTERFACE[interface]
        super().__init__(
            store=store,
            name=f"install_mode_{token}",
            parameter_slug=token,
            translation_key="install_mode",
        )
        self._interface: Final = interface

    @property
    def interface(self) -> str:
        """Return the interface this sensor belongs to."""
        return self._interface

    @property
    def is_active(self) -> bool:
        """Return whether install mode is currently active."""
        return bool(self._value) and int(self._value) > 0


class InstallModeDpButton(HubSingletonDp):
    """Button activating install mode on one interface."""

    _address: ClassVar[str] = INSTALL_MODE_ADDRESS
    _category: ClassVar[DataPointCategory] = DataPointCategory.HubButton

    def __init__(
        self,
        *,
        store: LoomStore,
        hub_ops: HubOperations,
        interface: str,
        sensor: InstallModeDpSensor,
    ) -> None:
        """Bind the button to its interface, hub operations and countdown sensor."""
        token = INSTALL_MODE_TOKEN_BY_INTERFACE[interface]
        super().__init__(
            store=store,
            name=f"install_mode_{token}_button",
            parameter_slug=f"{token}-button",
            translation_key="install_mode",
        )
        self._hub_ops: Final = hub_ops
        self._interface: Final = interface
        self._sensor: Final = sensor

    @property
    def interface(self) -> str:
        """Return the interface this button belongs to."""
        return self._interface

    @property
    def sensor(self) -> InstallModeDpSensor:
        """Return the paired countdown sensor."""
        return self._sensor

    async def activate(self, *, time: int = 60, device_address: str | None = None) -> bool:
        """
        Activate install mode for ``time`` seconds.

        ``device_address`` is accepted for aiohomematic signature parity
        but ignored — the daemon's install mode has no per-device
        narrowing.
        """
        del device_address
        await self._hub_ops.set_install_mode_interface(
            interface=self._interface, active=True, seconds=time
        )
        # Optimistic countdown start; the periodic hub poll resyncs it.
        self._sensor.update_value(value=time)
        return True

    async def deactivate(self) -> bool:
        """Deactivate install mode on this interface."""
        await self._hub_ops.set_install_mode_interface(
            interface=self._interface, active=False, seconds=0
        )
        self._sensor.update_value(value=0)
        return True

    async def press(self) -> None:
        """Activate install mode with default settings (HA button press)."""
        await self.activate()


class InstallModeDpType(NamedTuple):
    """Button + sensor pair for one interface's install mode."""

    button: InstallModeDpButton
    sensor: InstallModeDpSensor


class MetricsDpType(NamedTuple):
    """Container for the metrics hub sensors (aiohomematic parity)."""

    system_health: SystemHealthSensor
    connection_latency: ConnectionLatencySensor
    last_event_age: LastEventAgeSensor


class ConnectivityDpType(NamedTuple):
    """Container for one interface's connectivity sensor (aiohomematic parity)."""

    interface_id: str
    interface: str
    sensor: InterfaceConnectivityDp


__all__ = [
    "INSTALL_MODE_TOKEN_BY_INTERFACE",
    "AlarmMessagesSensor",
    "ConnectionLatencySensor",
    "ConnectivityDpType",
    "HubSingletonDp",
    "InboxSensor",
    "InstallModeDpButton",
    "InstallModeDpSensor",
    "InstallModeDpType",
    "InterfaceConnectivityDp",
    "LastEventAgeSensor",
    "MetricsDpType",
    "ServiceMessagesSensor",
    "SystemHealthSensor",
    "SystemUpdateDp",
]
