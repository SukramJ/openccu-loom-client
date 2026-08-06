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

from openccu_loom_types.enums import DataPointCategory
from slugify import slugify

from openccu_loom_client.canonical import canonical_unique_id
from openccu_loom_client.compat.aiohomematic._upstream import HUB_ADDRESS, INSTALL_MODE_ADDRESS
from openccu_loom_client.compat.aiohomematic.model._protocol_surface import _SysvarProtocolSurface
from openccu_loom_client.compat.aiohomematic.model.hub._surface import _HubEntitySurface

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from openccu_loom_types.rest import AddonUpdateStatus, SystemUpdateEntry
    from openccu_loom_types.ws import HubSystemUpdateChangedPayload

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
        name_key: str | None = None,
        enabled_default: bool = True,
    ) -> None:
        """Bind the singleton to its store, registry slug and translation key."""
        self._store = store
        self._name = name
        self._parameter_slug = parameter_slug
        self._translation_key = translation_key
        self._name_key = name_key
        self._resolved_name: str | None = None
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
    def name_key(self) -> str | None:
        """Return the daemon catalogue key naming this entity, if any."""
        return self._name_key

    @property
    def name_args(self) -> dict[str, str]:
        """
        Return the placeholder substitutions for :attr:`name_key`.

        The daemon hands out templates — `Connectivity {iface}` — because
        it does not know which interface a consumer is naming. Subclasses
        that carry such a template override this.
        """
        return {}

    @property
    def resolved_name(self) -> str | None:
        """
        Return the daemon's name for this entity, or ``None``.

        The daemon is the single naming authority, but :attr:`name` stays
        the stable English token: Home Assistant matches its entity
        descriptions against it (`var_name_contains`), and renaming that
        token would cost the entity its icon, device class and category.
        A consumer renders this and matches on :attr:`name`.
        """
        return self._resolved_name

    def apply_entity_names(self, *, entries: Mapping[str, str]) -> bool:
        """
        Adopt the daemon's name from a catalogue; return whether it moved.

        A key the catalogue does not carry leaves the name unset, which is
        the same state as a daemon too old to serve the catalogue at all —
        the consumer falls back to its own rendering either way.
        """
        if self._name_key is None:
            return False
        template = entries.get(self._name_key)
        if not template:
            return False
        rendered = template
        for placeholder, value in self.name_args.items():
            rendered = rendered.replace("{" + placeholder + "}", value)
        if rendered == self._resolved_name:
            return False
        self._resolved_name = rendered
        return True

    # Singletons reuse ``_SysvarProtocolSurface`` structurally but are not real
    # sysvars: they hold no wire-side ``SysvarSummary`` (state comes from the
    # hub-data-points aggregate, not a per-variable record). This deliberate
    # divergence from the typed host contract is the one place it doesn't hold.
    @property
    def summary(self) -> None:  # type: ignore[override]
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


def _alarm_attribute(*, message: Any) -> str:
    """Render one alarm message as a single attribute line."""
    label = str(message.display_name or message.name)
    raised = getattr(message, "timestamp", None)
    if raised is None:
        return label
    return f"{label} ({raised.isoformat()})" if hasattr(raised, "isoformat") else f"{label} ({raised})"


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
            name_key="discovery.alarm_messages",
        )

    def update_messages(self, *, messages: Sequence[Any]) -> bool:
        """
        Apply the fetched alarm list: count + ``alarm_<n>`` attributes.

        An alarm entry names no device — it is backed by an alarm system
        variable a program raises, and the CCU reports its trigger data
        point as the "unknown" sentinel — so the attribute carries the
        translated message and, where the daemon knows it, when the alarm
        was raised. Prefixing a device name here would mean inventing one.
        """
        attributes = {
            f"alarm_{idx}": _alarm_attribute(message=message) for idx, message in enumerate(messages, start=1)
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
            name_key="discovery.service_messages",
        )

    def update_messages(self, *, messages: Sequence[Any]) -> bool:
        """Apply the fetched service list: count + ``message_<n>`` attributes."""
        attributes = {
            f"message_{idx}": (
                f"{message.device_name}: {message.name}" if getattr(message, "device_name", None) else str(message.name)
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
            name_key="discovery.inbox",
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
            name_key="discovery.system_health",
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
            name_key="discovery.connection_latency",
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
            name_key="discovery.last_event_age",
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
            name_key="discovery.connectivity",
        )
        self._interface_id: Final = interface_id

    @property
    def interface_id(self) -> str:
        """Return the interface id this sensor tracks."""
        return self._interface_id

    @property
    def name_args(self) -> dict[str, str]:
        """Fill the catalogue template's `{iface}` with the interface id."""
        return {"iface": self._interface_id}

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
            name_key="discovery.system_update",
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
        """Apply a fetched ``GET /system/update`` entry; return whether anything changed."""
        return self._apply(
            current_firmware=entry.current_firmware or "",
            available_firmware=entry.available_firmware or "",
            update_available=bool(entry.update_available),
            in_progress=bool(entry.in_progress),
        )

    def update_from_push(self, *, payload: HubSystemUpdateChangedPayload) -> bool:
        """Apply a ``hub.system_update_changed`` push (same fields as the REST entry)."""
        return self._apply(
            current_firmware=payload.current_firmware or "",
            available_firmware=payload.available_firmware or "",
            update_available=bool(payload.update_available),
            in_progress=bool(payload.in_progress),
        )

    def _apply(
        self,
        *,
        current_firmware: str,
        available_firmware: str,
        update_available: bool,
        in_progress: bool,
    ) -> bool:
        """Set the firmware-update fields; return whether anything changed."""
        new = (current_firmware, available_firmware, update_available, in_progress)
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


class AddonUpdateDp(HubSingletonDp):
    """
    Daemon add-on self-update data point (second ``HmUpdate`` twin).

    Mirrors :class:`SystemUpdateDp` for the daemon's own CCU add-on
    package instead of the CCU firmware: same ``HubUpdate`` category, so
    the HA update platform spawns it through the identical hub-update
    path (install button, progress, backup toggle). The coordinator only
    builds it when the daemon reports the platform installer as present
    (``supported`` on ``GET /system/addon-update``); REST poll and the
    ``addon_update.state_changed`` push share one payload model, applied
    via :meth:`update_status`. The status is daemon-global — on a daemon
    serving several centrals each adapter renders its own twin (distinct
    unique_ids via the serial suffix).
    """

    _category: ClassVar[DataPointCategory] = DataPointCategory.HubUpdate

    # Updater lifecycle states that HA renders as "install in progress".
    _IN_PROGRESS_STATES: ClassVar[frozenset[str]] = frozenset({"downloading", "installing"})

    def __init__(self, *, store: LoomStore, system_ops: SystemOperations) -> None:
        """Bind the add-on-update singleton to the store and system operations."""
        super().__init__(
            store=store,
            name="Add-on Update",
            parameter_slug="addon-update",
            translation_key="addon_update",
            name_key="discovery.addon_update",
        )
        self._system_ops: Final = system_ops
        self._current_firmware: str = ""
        self._available_firmware: str = ""
        self._update_available: bool = False
        self._state: str = "idle"
        self._release_url: str | None = None
        self._error: str | None = None

    @property
    def current_firmware(self) -> str:
        """Return the installed add-on version."""
        return self._current_firmware

    @property
    def firmware(self) -> str:
        """Return the installed add-on version (alias)."""
        return self._current_firmware

    @property
    def available_firmware(self) -> str:
        """Return the add-on version available for install."""
        return self._available_firmware

    @property
    def latest_firmware(self) -> str:
        """Return the latest installable version, falling back to the installed one."""
        return self._available_firmware or self._current_firmware

    @property
    def update_available(self) -> bool:
        """Return whether an add-on update is available."""
        return self._update_available

    @property
    def in_progress(self) -> bool:
        """Return whether an add-on update is currently downloading/installing."""
        return self._state in self._IN_PROGRESS_STATES

    @property
    def state(self) -> str:
        """Return the raw updater lifecycle state (``idle`` … ``failed``)."""
        return self._state

    @property
    def release_url(self) -> str | None:
        """Return the release-notes page of the latest version (if known)."""
        return self._release_url

    @property
    def error(self) -> str | None:
        """Return the failure detail while the updater state is ``failed``."""
        return self._error

    def update_status(self, *, status: AddonUpdateStatus) -> bool:
        """Apply a REST/WS ``AddonUpdateStatus``; return whether anything changed."""
        new = (
            status.current_version or "",
            status.latest_version or "",
            bool(status.update_available),
            status.state.value,
            status.release_url,
            status.error,
        )
        old = (
            self._current_firmware,
            self._available_firmware,
            self._update_available,
            self._state,
            self._release_url,
            self._error,
        )
        now = datetime.now(tz=UTC)
        if new != old:
            (
                self._current_firmware,
                self._available_firmware,
                self._update_available,
                self._state,
                self._release_url,
                self._error,
            ) = new
            self._modified = now
            self._refreshed = now
            return True
        self._refreshed = now
        return False

    async def install(self) -> bool:
        """Trigger the add-on self-update install via the daemon."""
        await self._system_ops.install_addon_update()
        # Optimistic: `installing` is terminal from the caller's view —
        # the daemon restarts on success and the post-reconnect fetch
        # shows the new version. Flip immediately so HA shows progress.
        self._state = "installing"
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
            name_key="discovery.install_mode_duration",
        )
        self._interface: Final = interface

    @property
    def interface(self) -> str:
        """Return the interface this sensor belongs to."""
        return self._interface

    @property
    def name_args(self) -> dict[str, str]:
        """Fill the catalogue template's `{iface}` with the interface name."""
        return {"iface": self._interface}

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
            name_key="discovery.install_mode_activate",
        )
        self._hub_ops: Final = hub_ops
        self._interface: Final = interface
        self._sensor: Final = sensor

    @property
    def interface(self) -> str:
        """Return the interface this button belongs to."""
        return self._interface

    @property
    def name_args(self) -> dict[str, str]:
        """Fill the catalogue template's `{iface}` with the interface name."""
        return {"iface": self._interface}

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
        await self._hub_ops.set_install_mode_interface(interface=self._interface, active=True, seconds=time)
        # Optimistic countdown start; the periodic hub poll resyncs it.
        self._sensor.update_value(value=time)
        return True

    async def deactivate(self) -> bool:
        """Deactivate install mode on this interface."""
        await self._hub_ops.set_install_mode_interface(interface=self._interface, active=False, seconds=0)
        self._sensor.update_value(value=0)
        return True

    async def press(self) -> None:
        """Activate install mode with default settings (HA button press)."""
        await self.activate()


# ---- Security & Safety ----

# Class → HA binary-sensor device class. The mapping is what turns a
# generic on/off into a smoke alarm on the dashboard, so it is stated
# once here rather than derived per surface. `technical`, `intrusion`
# and `panic` have no HA device class that fits — HA's `safety` is the
# generic hazard bucket and would flatten them into the others.
SECURITY_CLASS_DEVICE_CLASS: Final[dict[str, str]] = {
    "smoke": "smoke",
    "water": "moisture",
    "gas": "gas",
    "co": "carbon_monoxide",
    "tamper": "tamper",
    "battery": "battery",
}


class SecuritySeveritySensor(HubSingletonDp):
    """
    The folded severity of the Security & Safety domain.

    One value (`ok`/`info`/`warning`/`alarm`/`critical`) that answers
    "is anything wrong here" without reading nine class entities.
    """

    _data_type: ClassVar[str | None] = "STRING"

    def __init__(self, *, store: LoomStore) -> None:
        """Bind the severity singleton to the store."""
        super().__init__(
            store=store,
            name="security_severity",
            parameter_slug="security-severity",
            translation_key="security_severity",
            name_key="security.entity.state",
        )


class SecurityFaultsSensor(HubSingletonDp):
    """
    Count of standing Security & Safety faults, one attribute per fault.

    A fault is a *self-diagnosis*: an unreachable detector, a flat
    battery, a blocked radio. It never clears through acknowledgement —
    the condition is still there, the operator has merely seen it — so
    the count keeps standing until the installation is repaired.
    """

    _data_type: ClassVar[str | None] = "INTEGER"

    def __init__(self, *, store: LoomStore) -> None:
        """Bind the fault-count singleton to the store."""
        super().__init__(
            store=store,
            name="security_faults",
            parameter_slug="security-faults",
            translation_key="security_faults",
            name_key="security.entity.problem",
        )

    def update_faults(self, *, faults: Sequence[Any]) -> bool:
        """Apply the fetched fault ledger: count + ``fault_<n>`` attributes."""
        attributes = {f"fault_{idx}": _fault_attribute(fault=fault) for idx, fault in enumerate(faults, start=1)}
        return self.update_value(value=len(faults), attributes=attributes)


class SecurityClassDp(HubSingletonDp):
    """
    Binary sensor for one hazard or fault class (smoke, water, gas, …).

    A class the installation has no source for is never built: the
    daemon omits it from the snapshot rather than reporting it inactive,
    so a home without gas detectors gets no permanently-off gas alarm.
    """

    _category: ClassVar[DataPointCategory] = DataPointCategory.HubBinarySensor
    _data_type: ClassVar[str | None] = "LOGIC"

    def __init__(self, *, store: LoomStore, security_class: str) -> None:
        """Bind the class singleton to its hazard/fault class."""
        super().__init__(
            store=store,
            name=f"security_{security_class}",
            parameter_slug=f"security-{slugify(security_class)}",
            translation_key="security_class",
            name_key=f"security.entity.class.{security_class}",
        )
        self._security_class: Final = security_class

    @property
    def security_class(self) -> str:
        """Return the hazard/fault class this sensor tracks."""
        return self._security_class

    @property
    def device_class(self) -> str | None:
        """Return the HA binary-sensor device class, if the class maps onto one."""
        return SECURITY_CLASS_DEVICE_CLASS.get(self._security_class)

    def update_class(self, *, active: bool, sources: Sequence[Any] | None = None) -> bool:
        """Apply an active flag plus the names of the contributing sources."""
        names = [str(name) for source in sources or () if (name := getattr(source, "name", None))]
        attributes: dict[str, Any] = {"security_class": self._security_class}
        if names:
            attributes["sources"] = names
        return self.update_value(value=active, attributes=attributes)


class SecurityReportSensor(HubSingletonDp):
    """
    The last rendered Security & Safety report — hazard or fault.

    The value is the report's one-line subject; the attributes carry the
    full sentence plus the i18n key and args, so a consumer that would
    rather render in its own locale can.

    A covert report (duress code, silent panic) never arrives here
    unless the daemon runs ``alarm.duress_visibility: full``: it gates
    the WebSocket exactly as it gates its own retained state, because a
    wall tablet showing "duress code entered" defeats the covert trigger
    it reports.
    """

    _data_type: ClassVar[str | None] = "STRING"

    def __init__(self, *, store: LoomStore, fault: bool) -> None:
        """Bind the report singleton to the hazard or the fault plane."""
        slug = "security-last-fault" if fault else "security-last-alarm"
        super().__init__(
            store=store,
            name=slug.replace("-", "_"),
            parameter_slug=slug,
            translation_key=slug.replace("-", "_"),
            name_key="security.entity.last_fault" if fault else "security.entity.last_alarm",
        )
        self._fault: Final = fault

    @property
    def fault(self) -> bool:
        """Return whether this sensor tracks fault reports rather than hazards."""
        return self._fault

    def update_report(self, *, report: Any) -> bool:
        """Apply one rendered report (or clear the sensor when there is none)."""
        if report is None:
            return self.update_value(value=None, attributes={})
        attributes = {
            key: value
            for key, value in (
                ("message", getattr(report, "message", None)),
                ("security_class", getattr(report, "class_", None)),
                ("severity", getattr(report, "severity", None)),
                ("verb", _enum_value(value=getattr(report, "verb", None))),
                ("i18n_key", getattr(report, "i18n_key", None)),
                ("args", getattr(report, "args", None)),
                ("zone_name", getattr(report, "zone_name", None)),
                ("at", _isoformat(value=getattr(report, "at", None))),
            )
            if value
        }
        return self.update_value(value=getattr(report, "subject", None), attributes=attributes)


def _fault_attribute(*, fault: Any) -> str:
    """Render one standing fault as a single attribute line."""
    reason = _enum_value(value=getattr(fault, "reason", None)) or "fault"
    source = getattr(fault, "source", None)
    name = getattr(source, "name", None) or getattr(source, "channel_address", None) or ""
    return f"{name}: {reason}" if name else str(reason)


def _enum_value(*, value: Any) -> Any:
    """Unwrap a generated StrEnum to its wire string; pass anything else through."""
    return getattr(value, "value", value)


def _isoformat(*, value: Any) -> Any:
    """Render a datetime as ISO-8601; pass anything else through unchanged."""
    return value.isoformat() if hasattr(value, "isoformat") else value


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
    "SECURITY_CLASS_DEVICE_CLASS",
    "AddonUpdateDp",
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
    "SecurityClassDp",
    "SecurityFaultsSensor",
    "SecurityReportSensor",
    "SecuritySeveritySensor",
    "ServiceMessagesSensor",
    "SystemHealthSensor",
    "SystemUpdateDp",
]
