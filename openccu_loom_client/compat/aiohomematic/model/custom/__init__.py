# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
``aiohomematic.model.custom`` — categorised Custom-DP classes.

The daemon collapses a device's multi-parameter wiring into one Custom
Data Point per function (light, cover, climate, lock, siren, switch,
valve, text-display) and pushes its state as a flat ``state`` dict.
:func:`make_custom_data_point` maps the daemon's ``category`` / ``kind``
classifier to the concrete ``CustomDp*`` class the HA platform files
``isinstance``-dispatch on, and the classes derive typed HA properties
(``is_on``, ``brightness``, ``current_position``, ``hvac_mode``,
``is_locked`` …) from the ``state`` keys the daemon documents, driving
actions through ``invoke(operation, params=…)``.

State-key reference (daemon ``internal/payload/state.go`` +
``internal/model/custom/*/payload.go``):

* light    — ``state`` ("ON"/"OFF"), ``brightness`` (0-255)
* cover    — ``state``, ``current_position`` (0-100), ``current_tilt_position``
             (0-100, blinds), ``direction``
* climate  — ``hvac_mode``, ``preset_mode``, ``action``, profiles…
* lock     — ``lock_state``, ``is_locked``, ``is_locking``,
             ``is_unlocking``, ``is_jammed``
* siren    — ``state`` ("on"/"off")
* switch   — ``is_on`` (bool)
* valve    — ``is_open`` (bool) / ``current_level_pct`` (0-100)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Final

from aiohomematic.model.custom import ClimateMode, ClimateProfile
from openccu_loom_types.enums import DataPointCategory

from openccu_loom_client.canonical import canonical_unique_id
from openccu_loom_client.compat.aiohomematic.model._protocol_surface import _CustomProtocolSurface
from openccu_loom_client.model import CustomDataPoint

if TYPE_CHECKING:
    from openccu_loom_client.store import LoomStore

# Valid wire tokens for the aiohomematic climate enums; unknown daemon
# tokens are skipped instead of raising on enum construction.
_CLIMATE_MODE_VALUES: Final = {m.value for m in ClimateMode}
_CLIMATE_PROFILE_VALUES: Final = {p.value for p in ClimateProfile}


def custom_unique_id(*, serial_suffix: str, device_address: str, channel_no: int) -> str:
    """
    Canonical HA unique id for a Custom-DP, matched by the refresh bridge.

    aiohomematic builds a custom data point's ``unique_id`` from its
    *primary channel address* (no parameter); the daemon's ``channel_no``
    is that primary channel. Built via the shared contract as
    ``loom_<address>`` — a normal device carries no serial prefix, so the
    canonical key is e.g. ``loom_vcu1234567_1``.
    """
    return canonical_unique_id(
        serial_suffix=serial_suffix, address=f"{device_address}:{channel_no}"
    )


# HA-side capability names whose daemon flag is named differently —
# HA checks ``capabilities.profiles`` (daemon: ``profile``, drives
# ClimateEntityFeature.PRESET_MODE), ``capabilities.brightness``
# (daemon: ``dimmable``, drives ColorMode.BRIGHTNESS) and
# ``capabilities.tones`` (daemon: ``acoustic``, drives the siren
# TONES feature).
_CAPABILITY_ALIASES: dict[str, str] = {
    "brightness": "dimmable",
    "profiles": "profile",
    "tones": "acoustic",
}


class _Capabilities:
    """
    Attribute-access view over the daemon's ``capabilities`` flag map.

    HA reads capability flags as attributes (``dp.capabilities.brightness``,
    ``.color``, ``.profiles``, ``.open``, ``.tones`` …). Unknown flags
    read as ``False`` so a missing capability never raises.
    """

    __slots__ = ("_flags",)

    def __init__(self, flags: dict[str, bool] | None) -> None:
        self._flags = flags or {}

    def __getattr__(self, name: str) -> bool:
        if name in self._flags:
            return bool(self._flags[name])
        if (alias := _CAPABILITY_ALIASES.get(name)) is not None:
            return bool(self._flags.get(alias, False))
        return False

    def __contains__(self, name: str) -> bool:
        return name in self._flags


def _as_float(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _as_int(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None


_CATEGORY_BY_STRING: dict[str, DataPointCategory] = {
    "light": DataPointCategory.Light,
    "cover": DataPointCategory.Cover,
    "climate": DataPointCategory.Climate,
    "lock": DataPointCategory.Lock,
    "siren": DataPointCategory.Siren,
    "switch": DataPointCategory.Switch,
    "valve": DataPointCategory.Valve,
    "text_display": DataPointCategory.TextDisplay,
}


class _CustomEntitySurface(_CustomProtocolSurface, CustomDataPoint):
    """Entity-facing surface shared by every ``CustomDp*`` class."""

    _category: ClassVar[DataPointCategory] = DataPointCategory.Switch

    @property
    def category(self) -> DataPointCategory:
        """Return the HA data-point category from the daemon string, else the class default."""
        # Prefer the daemon's category string; fall back to the class default.
        return _CATEGORY_BY_STRING.get(self._summary.category or "", self._category)

    @property
    def unique_id(self) -> str:
        """Return the canonical HA unique id derived from the primary channel address."""
        return custom_unique_id(
            serial_suffix=self._store.serial_suffix,
            device_address=self._device_address,
            channel_no=self._summary.channel_no,
        )

    @property
    def full_name(self) -> str:
        """Return the display name as ``<device name> <data-point name>``."""
        device = self.device
        device_name = device.name if device is not None else self._device_address
        return f"{device_name} {self.name}"

    @property
    def central(self) -> Any:
        """Return the owning central unit; always ``None`` under the daemon model."""
        return None

    @property
    def is_valid(self) -> bool:
        """Return whether a non-empty ``state`` has been received."""
        return bool(self._state)

    @property
    def available(self) -> bool:
        """Return the owning device's availability, defaulting to ``True`` when unknown."""
        device = self.device
        return bool(device.available) if device is not None else True

    @property
    def state_uncertain(self) -> bool:
        """Return the ``state_uncertain`` flag from the state dict."""
        return bool(self._state.get("state_uncertain", False))

    @property
    def enabled_default(self) -> bool:
        """Return whether the entity is enabled by default; always ``True``."""
        return True

    @property
    def capabilities(self) -> _Capabilities:
        """Return an attribute-access view over the summary's capability flags."""
        return _Capabilities(self._summary.capabilities)

    def _config_value(self, key: str) -> Any:
        """Return one entry from the CDP's static ``config`` block, or ``None``."""
        config = getattr(self._summary, "config", None) or {}
        return config.get(key)

    @property
    def translated_name(self) -> str | None:
        """
        Return the channel-derived display name, aiohomematic-style.

        aiohomematic names a custom DP after its CCU channel: the channel
        name minus the device-name prefix ("Küchenstrahler:vch5" →
        "vch5", a user-renamed channel keeps its full name). The primary
        channel usually carries the bare device name, which strips to
        nothing → ``None`` and HA falls back to the device name alone —
        exactly the reference behaviour (primary ``None``, secondaries
        "vch5"/"vch6").
        """
        channel = self._store.get_channel(
            address=self._device_address, number=self._summary.channel_no
        )
        raw = (channel.summary.name if channel is not None else None) or ""
        device = self.device
        device_name = (device.name if device is not None else "") or ""
        if device_name and raw.startswith(device_name):
            raw = raw[len(device_name) :].lstrip(":").strip()
        return raw or None

    @property
    def is_registered(self) -> bool:
        """Return whether the entity has been registered with HA."""
        return getattr(self, "_registered", False)

    def register(self) -> None:
        """Mark the entity as registered with HA."""
        self._registered = True

    def unregister(self) -> None:
        """Mark the entity as no longer registered with HA."""
        self._registered = False

    async def load_data_point_value(self, *, call_source: Any = None) -> None:
        """Refresh this custom data point's state from the daemon."""
        await self._store.refresh_custom_data_point(address=self._device_address, name=self.name)


# ---- switch ----


class CustomDpSwitch(_CustomEntitySurface):
    """Switch-style CDP (on/off)."""

    _category: ClassVar[DataPointCategory] = DataPointCategory.Switch

    @property
    def value(self) -> bool | None:
        """Return the raw ``is_on`` state value, or ``None`` if unknown."""
        return self._state.get("is_on")

    @property
    def is_on(self) -> bool:
        """Return whether the switch is on, from the ``is_on`` state key."""
        return bool(self._state.get("is_on"))

    @property
    def group_value(self) -> Any:
        """Return the group-channel aggregate; always ``None`` under the daemon model."""
        # aiohomematic exposes a group-channel aggregate here; the daemon
        # collapses that server-side, so there is no separate group value.
        return None

    async def turn_on(self, **_kwargs: Any) -> None:
        """Turn the switch on."""
        await self.invoke("turn_on")

    async def turn_off(self, **_kwargs: Any) -> None:
        """Turn the switch off."""
        await self.invoke("turn_off")

    async def set_timer_on_time(self, *, on_time: float) -> None:
        """Turn on for a fixed duration (daemon ``turn_on_for``)."""
        await self.invoke("turn_on_for", params={"seconds": float(on_time)})


# ---- light ----


class CustomDpDimmer(_CustomEntitySurface):
    """Dimmable light CDP (and the base for colour/colour-temp variants)."""

    _category: ClassVar[DataPointCategory] = DataPointCategory.Light

    @property
    def is_on(self) -> bool:
        """Return whether the light is on, from the ``state`` key ("ON"/"OFF")."""
        return str(self._state.get("state", "OFF")).upper() == "ON"

    @property
    def brightness(self) -> int | None:
        """Return the brightness (0-255) from the ``brightness`` state key, or ``None``."""
        return _as_int(self._state.get("brightness"))

    @property
    def group_brightness(self) -> int | None:
        """Return the group-channel brightness (not modelled for loom)."""
        return None

    @property
    def has_color_temperature(self) -> bool:
        """Return whether the light supports colour temperature."""
        return self.capabilities.color_temp

    @property
    def has_hs_color(self) -> bool:
        """Return whether the light supports hue/saturation colour."""
        return self.capabilities.color

    @property
    def has_effects(self) -> bool:
        """Return whether the light supports effects."""
        return self.capabilities.effects

    # Colour / effect state is not carried in the daemon's light CDP
    # state (only on/off + brightness); these read as "unknown" until the
    # daemon surfaces them. Writes still drive the daemon set_* ops.
    @property
    def color_temp_kelvin(self) -> int | None:
        """Return the colour temperature in kelvin, or ``None`` if not surfaced."""
        return _as_int(self._state.get("color_temp_kelvin"))

    @property
    def hs_color(self) -> tuple[float, float] | None:
        """Return the ``(hue, saturation)`` colour, or ``None`` if either is unknown."""
        hue = _as_float(self._state.get("hue"))
        sat = _as_float(self._state.get("saturation"))
        return (hue, sat) if hue is not None and sat is not None else None

    @property
    def effect(self) -> str | None:
        """Return the active effect name, or ``None`` if not set."""
        val = self._state.get("effect")
        return str(val) if val is not None else None

    @property
    def effects(self) -> tuple[str, ...]:
        """Return the available effect names from the ``effects`` state key."""
        raw = self._state.get("effects") or ()
        return tuple(str(e) for e in raw)

    @property
    def last_level(self) -> int:
        """Return the last known brightness (0-255), defaulting to 0."""
        return _as_int(self._state.get("brightness")) or 0

    @staticmethod
    def level_to_brightness(level: float) -> int:
        """Convert a level (0.0-1.0) to a brightness value (0-255)."""
        return round(level * 255)

    @staticmethod
    def brightness_to_level(brightness: int) -> float:
        """Convert a brightness value (0-255) to a level (0.0-1.0)."""
        return brightness / 255.0

    def set_last_level(self, value: int) -> None:
        """Store the last brightness for HA-side restore; performs no daemon write."""
        # Last-brightness restore is HA-side bookkeeping; no daemon write.
        self._state["brightness"] = value

    async def set_timer_on_time(self, *, on_time: float) -> None:
        """Turn on for a fixed duration in seconds."""
        await self.invoke("set_timer_on_time", params={"seconds": float(on_time)})

    async def turn_on(
        self,
        *,
        brightness: int | None = None,
        hs_color: tuple[float, float] | None = None,
        color_temp_kelvin: int | None = None,
        effect: str | None = None,
        **_kwargs: Any,
    ) -> None:
        """Turn the light on, optionally setting brightness, colour, kelvin, or effect."""
        if hs_color is not None:
            await self.invoke("set_color", params={"hue": hs_color[0], "saturation": hs_color[1]})
        if color_temp_kelvin is not None:
            await self.invoke("set_kelvin", params={"kelvin": int(color_temp_kelvin)})
        if effect is not None:
            await self.invoke("set_effect", params={"effect": effect})
        if brightness is not None:
            await self.invoke("set_level", params={"brightness": int(brightness)})
        elif hs_color is None and color_temp_kelvin is None and effect is None:
            await self.invoke("turn_on")

    async def turn_off(self, **_kwargs: Any) -> None:
        """Turn the light off."""
        await self.invoke("turn_off")

    async def set_brightness(self, brightness: int) -> None:
        """Set the light brightness (0-255)."""
        await self.invoke("set_level", params={"brightness": int(brightness)})


class CustomDpIpFixedColorLight(CustomDpDimmer):
    """HmIP fixed-colour light."""

    # Fixed-colour name state is not carried in the daemon's light CDP
    # state yet; these read as ``None`` until the daemon surfaces them.
    @property
    def color_name(self) -> str | None:
        """Return the active fixed-colour name, or ``None`` if not surfaced."""
        val = self._state.get("color_name")
        return str(val) if val is not None else None

    @property
    def channel_color_name(self) -> str | None:
        """Return the channel fixed-colour name, or ``None`` if not surfaced."""
        val = self._state.get("channel_color_name")
        return str(val) if val is not None else None


class CustomDpSoundPlayerLed(CustomDpDimmer):
    """Sound-player LED light variant."""

    # Colour state is not carried in the daemon's light CDP state yet;
    # these read as ``None`` until the daemon surfaces them.
    @property
    def available_colors(self) -> tuple[str, ...] | None:
        """Return the selectable LED colours, or ``None`` if not surfaced."""
        raw = self._state.get("available_colors")
        return tuple(str(c) for c in raw) if raw else None

    @property
    def color_name(self) -> str | None:
        """Return the active LED colour name, or ``None`` if not surfaced."""
        val = self._state.get("color_name")
        return str(val) if val is not None else None


# ---- cover / blind / garage ----


class CustomDpCover(_CustomEntitySurface):
    """Cover (shutter) CDP."""

    _category: ClassVar[DataPointCategory] = DataPointCategory.Cover

    @property
    def current_position(self) -> int | None:
        """Return the current position (0-100) from ``current_position``, or ``None``."""
        return _as_int(self._state.get("current_position"))

    @property
    def current_channel_position(self) -> int | None:
        """Return the current position; alias of :attr:`current_position`."""
        return self.current_position

    @property
    def _state_token(self) -> str:
        """Return the lower-cased ``state`` token (e.g. "closed"/"opening")."""
        return str(self._state.get("state", "")).lower()

    @property
    def is_closed(self) -> bool:
        """Return whether the cover is closed, from position 0 or the ``state`` token."""
        pos = self.current_position
        if pos is not None:
            return pos == 0
        return self._state_token == "closed"  # noqa: S105 # nosec B105 — cover state token, not a secret

    @property
    def is_opening(self) -> bool:
        """Return whether the cover is opening, from ``direction`` or the ``state`` token."""
        if self._state.get("direction") == "opening":
            return True
        return self._state_token == "opening"  # noqa: S105 # nosec B105 — cover state token, not a secret

    @property
    def is_closing(self) -> bool:
        """Return whether the cover is closing, from ``direction`` or the ``state`` token."""
        if self._state.get("direction") == "closing":
            return True
        return self._state_token == "closing"  # noqa: S105 # nosec B105 — cover state token, not a secret

    async def open(self) -> None:
        """Open the cover fully."""
        await self.invoke("open")

    async def close(self) -> None:
        """Close the cover fully."""
        await self.invoke("close")

    async def stop(self) -> None:
        """Stop cover movement."""
        await self.invoke("stop")

    async def set_position(
        self,
        position: int,
        tilt_position: int | None = None,
        collector: Any = None,
        **_kwargs: Any,
    ) -> None:
        """Move the cover to the given position (0-100)."""
        await self.invoke("set_position", params={"position": position / 100.0})


class CustomDpBlind(CustomDpCover):
    """Cover with a tilt axis."""

    @property
    def current_tilt_position(self) -> int | None:
        """Return the current tilt position (0-100) from ``current_tilt_position``, or ``None``."""
        return _as_int(self._state.get("current_tilt_position"))

    @property
    def current_channel_tilt_position(self) -> int | None:
        """Return the current tilt position; alias of :attr:`current_tilt_position`."""
        return self.current_tilt_position

    async def set_position(
        self,
        position: int,
        tilt_position: int | None = None,
        collector: Any = None,
        **_kwargs: Any,
    ) -> None:
        """Move the cover to the given position (0-100) and optional tilt position (0-100)."""
        await self.invoke("set_position", params={"position": position / 100.0})
        if tilt_position is not None:
            await self.invoke("set_tilt", params={"tilt": tilt_position / 100.0})

    async def set_tilt_position(self, tilt_position: int) -> None:
        """Set the tilt position (0-100)."""
        await self.invoke("set_tilt", params={"tilt": tilt_position / 100.0})

    async def open_tilt(self) -> None:
        """Open the tilt fully."""
        await self.invoke("open_tilt")

    async def close_tilt(self) -> None:
        """Close the tilt fully."""
        await self.invoke("close_tilt")

    async def stop_tilt(self) -> None:
        """Stop tilt movement."""
        await self.invoke("stop_tilt")


class CustomDpIpBlind(CustomDpBlind):
    """HmIP blind."""

    @property
    def operation_mode(self) -> str | None:
        """Return the operation mode from the ``operation_mode`` state key, or ``None``."""
        val = self._state.get("operation_mode")
        return str(val) if val is not None else None


class CustomDpGarage(CustomDpCover):
    """Garage-door CDP."""

    async def ventilate(self) -> None:
        """Move the garage door to its ventilation position."""
        await self.invoke("ventilate")


# ---- climate ----


class BaseCustomDpClimate(_CustomEntitySurface):
    """
    Thermostat CDP.

    ``hvac_mode``/``preset_mode``/``action`` come straight from the
    daemon's climate state. Current/target temperature and humidity fall
    back to the channel's generic data points (``ACTUAL_TEMPERATURE``,
    ``SET_POINT_TEMPERATURE``, ``HUMIDITY``) when the daemon's CDP state
    does not carry them — mirroring aiohomematic's field data points;
    the setters drive the documented ``set_*`` operations.
    """

    _category: ClassVar[DataPointCategory] = DataPointCategory.Climate

    def _generic_channel_value(self, parameter: str) -> Any:
        """Return the CDP channel's generic DP value (``None`` when absent/unobserved)."""
        dp = self._store.get_data_point(
            address=self._device_address,
            channel=self._summary.channel_no,
            parameter=parameter,
        )
        return dp.value if dp is not None else None

    @property
    def hvac_mode(self) -> str:
        """Return the HVAC mode from the ``hvac_mode`` state key, defaulting to "off"."""
        return str(self._state.get("hvac_mode", "off"))

    @property
    def preset_mode(self) -> str:
        """Return the preset mode from the ``preset_mode`` state key, defaulting to "none"."""
        return str(self._state.get("preset_mode", "none"))

    @property
    def action(self) -> str | None:
        """Return the current HVAC action from the ``action`` state key, or ``None``."""
        val = self._state.get("action")
        return str(val) if val is not None else None

    @property
    def current_temperature(self) -> float | None:
        """Return the measured temperature (state key, else ``ACTUAL_TEMPERATURE`` DP)."""
        val = self._state.get("current_temperature")
        if isinstance(val, (int, float)):
            return float(val)
        return _as_float(self._generic_channel_value("ACTUAL_TEMPERATURE"))

    @property
    def target_temperature(self) -> float | None:
        """Return the target temperature (state key, else ``SET_POINT_TEMPERATURE`` DP)."""
        val = self._state.get("set_temperature", self._state.get("target_temperature"))
        if isinstance(val, (int, float)):
            return float(val)
        return _as_float(self._generic_channel_value("SET_POINT_TEMPERATURE"))

    @property
    def current_humidity(self) -> int | None:
        """Return the measured humidity (state key, else ``HUMIDITY`` DP)."""
        val = _as_int(self._state.get("current_humidity"))
        if val is not None:
            return val
        return _as_int(self._generic_channel_value("HUMIDITY"))

    @property
    def temperature_offset(self) -> str | None:
        """Return the temperature offset from the ``temperature_offset`` state key, or ``None``."""
        val = self._state.get("temperature_offset")
        return str(val) if val is not None else None

    @property
    def min_temp(self) -> float:
        """Return the device's minimum settable temperature (config), defaulting to 4.5."""
        return (
            _as_float(self._config_value("min_temp"))
            or _as_float(self._state.get("min_temp"))
            or 4.5
        )

    @property
    def max_temp(self) -> float:
        """Return the device's maximum settable temperature (config), defaulting to 30.5."""
        return (
            _as_float(self._config_value("max_temp"))
            or _as_float(self._state.get("max_temp"))
            or 30.5
        )

    @property
    def target_temperature_step(self) -> float:
        """Return the temperature step (config), defaulting to 0.5."""
        return (
            _as_float(self._config_value("temp_step"))
            or _as_float(self._state.get("target_temperature_step"))
            or 0.5
        )

    # ``mode``/``activity``/``profile`` are returned as the daemon's
    # lower-case string tokens (StrEnum hash-equality keeps dict lookups
    # against aiohomematic's Climate* enums working); ``modes``/``profiles``
    # return the real aiohomematic enums because HA reads ``.value`` off
    # their members.
    @property
    def mode(self) -> str:
        """Return the HVAC mode; alias of :attr:`hvac_mode`."""
        return self.hvac_mode

    @property
    def modes(self) -> tuple[ClimateMode, ...]:
        """Return the available HVAC modes (config ``hvac_modes``) as enums."""
        raw = self._config_value("hvac_modes") or self._state.get("hvac_modes") or ()
        modes = tuple(ClimateMode(str(m)) for m in raw if str(m) in _CLIMATE_MODE_VALUES)
        # aiohomematic guarantees at least HEAT so HA renders a usable
        # climate card even when the device reports nothing.
        return modes or (ClimateMode.HEAT,)

    @property
    def activity(self) -> str | None:
        """Return the current HVAC action; alias of :attr:`action`."""
        return self.action

    # Link-peer activity sources are a CCU-only mechanism (a thermostat
    # inferring "idle" from a linked actuator). The daemon reports the
    # action directly via ``activity``, so loom has no link peers.
    @property
    def _peer_level_dp(self) -> None:
        """Return the link-peer level data point (CCU-only; ``None`` on loom)."""
        return None

    @property
    def _peer_state_dp(self) -> None:
        """Return the link-peer state data point (CCU-only; ``None`` on loom)."""
        return None

    @property
    def profile(self) -> str:
        """Return the active profile; alias of :attr:`preset_mode`."""
        return self.preset_mode

    @property
    def profiles(self) -> tuple[ClimateProfile, ...]:
        """
        Return the available profiles (config ``preset_modes``) as enums.

        aiohomematic always lists :attr:`ClimateProfile.NONE` (it is the
        "no profile active" preset every thermostat supports), placed
        after the control-mode block (boost/comfort/eco/away) and before
        the week-program names. The daemon's list omits it, so it is
        inserted here.
        """
        raw = self._config_value("preset_modes") or self._state.get("available_profiles") or ()
        profiles = [ClimateProfile(str(p)) for p in raw if str(p) in _CLIMATE_PROFILE_VALUES]
        if ClimateProfile.NONE not in profiles:
            control_block = {
                ClimateProfile.AWAY,
                ClimateProfile.BOOST,
                ClimateProfile.COMFORT,
                ClimateProfile.ECO,
            }
            insert_at = 0
            while insert_at < len(profiles) and profiles[insert_at] in control_block:
                insert_at += 1
            profiles.insert(insert_at, ClimateProfile.NONE)
        return tuple(profiles)

    async def set_temperature(self, temperature: float) -> None:
        """Set the target temperature."""
        await self.invoke("set_temperature", params={"temperature": float(temperature)})

    async def set_mode(self, mode: str) -> None:
        """Set the HVAC mode."""
        await self.invoke("set_mode", params={"mode": str(mode)})

    async def set_profile(self, profile: str) -> None:
        """Set the active profile."""
        await self.invoke("set_profile", params={"profile": str(profile)})

    async def enable_away_mode_by_duration(self, hours: int, away_temperature: float) -> None:
        """Enable away mode for a number of hours at the given temperature."""
        await self.invoke("enable_away", params={"hours": hours, "temperature": away_temperature})

    async def enable_away_mode_by_calendar(
        self, start: Any, end: Any, away_temperature: float
    ) -> None:
        """Enable away mode between the given start and end at the given temperature."""
        await self.invoke(
            "enable_away",
            params={"start": start, "end": end, "temperature": away_temperature},
        )

    async def disable_away_mode(self) -> None:
        """Disable away mode."""
        await self.invoke("disable_away")


class CustomDpIpThermostat(BaseCustomDpClimate):
    """HmIP thermostat."""


# ---- lock ----


class LockState:
    """Wire-side lock-state token constants."""

    LOCKED = "LOCKED"
    UNLOCKED = "UNLOCKED"
    OPEN = "OPEN"


class BaseCustomDpLock(_CustomEntitySurface):
    """Lock CDP."""

    _category: ClassVar[DataPointCategory] = DataPointCategory.Lock

    @property
    def data_point_name_postfix(self) -> str:
        """
        Return the data-point name postfix.

        Button locks carry their parameter name ("BUTTON_LOCK") so HA's
        entity-description registry matches the button-lock rule
        (entity_category=config, translation_key=button_lock) — exactly
        like aiohomematic's ``CustomDpButtonLock``.
        """
        name = self._summary.name.split("@", 1)[0]
        return name if name in ("BUTTON_LOCK", "GLOBAL_BUTTON_LOCK") else ""

    @property
    def is_locked(self) -> bool:
        """Return whether the lock is locked, from the ``is_locked`` state key."""
        return bool(self._state.get("is_locked"))

    @property
    def is_locking(self) -> bool:
        """Return whether the lock is currently locking, from the ``is_locking`` state key."""
        return bool(self._state.get("is_locking"))

    @property
    def is_unlocking(self) -> bool:
        """Return whether the lock is currently unlocking, from the ``is_unlocking`` state key."""
        return bool(self._state.get("is_unlocking"))

    @property
    def is_jammed(self) -> bool:
        """Return whether the lock is jammed, from the ``is_jammed`` state key."""
        return bool(self._state.get("is_jammed"))

    async def lock(self) -> None:
        """Lock the device."""
        await self.invoke("lock")

    async def unlock(self) -> None:
        """Unlock the device."""
        await self.invoke("unlock")

    async def open(self) -> None:
        """Open (release) the lock latch."""
        await self.invoke("open")


# ---- siren ----


class PlaySoundArgs:
    """Container for play-sound parameters; mirrors aiohomematic shape."""

    def __init__(self, *, sound: str, duration: int | None = None) -> None:
        """Store the sound name and optional duration."""
        self.sound = sound
        self.duration = duration


class SirenOnArgs:
    """Container for siren-on parameters."""

    def __init__(self, *, sound: str | None = None, duration: int | None = None) -> None:
        """Store the optional sound name and optional duration."""
        self.sound = sound
        self.duration = duration


class BaseCustomDpSiren(_CustomEntitySurface):
    """Siren CDP."""

    _category: ClassVar[DataPointCategory] = DataPointCategory.Siren

    @property
    def is_on(self) -> bool:
        """Return whether the siren is on, from the ``state`` key ("on"/"off")."""
        return str(self._state.get("state", "off")).lower() == "on"

    @property
    def available_tones(self) -> Any:
        """Return the available tones (config ``available_tones``)."""
        return self._config_value("available_tones") or self._state.get("available_tones") or ()

    @property
    def available_lights(self) -> Any:
        """Return the available light patterns (config ``available_lights``)."""
        return self._config_value("available_lights") or self._state.get("available_lights") or ()

    async def turn_on(self, **params: Any) -> None:
        """Turn the siren on, passing through any tone/light/duration params."""
        await self.invoke("turn_on", params=params or None)

    async def turn_off(self) -> None:
        """Turn the siren off."""
        await self.invoke("turn_off")


class CustomDpSoundPlayer(BaseCustomDpSiren):
    """Sound-player variant of a siren."""

    @property
    def available_soundfiles(self) -> Any:
        """Return the available sound files from the ``available_soundfiles`` state key."""
        return self._state.get("available_soundfiles") or {}

    @property
    def current_soundfile(self) -> str | None:
        """Return the current sound file from the ``current_soundfile`` state key, or ``None``."""
        val = self._state.get("current_soundfile")
        return str(val) if val is not None else None

    async def play_sound(self, **params: Any) -> None:
        """Play a sound, passing through any sound/duration params."""
        await self.invoke("turn_on", params=params or None)

    async def stop_sound(self) -> None:
        """Stop sound playback."""
        await self.invoke("turn_off")


# ---- valve ----


class CustomDpIpIrrigationValve(_CustomEntitySurface):
    """Irrigation valve CDP."""

    _category: ClassVar[DataPointCategory] = DataPointCategory.Valve

    @property
    def value(self) -> bool:
        """Return whether the valve is open, from the ``is_open`` state key."""
        return bool(self._state.get("is_open"))

    @property
    def is_open(self) -> bool:
        """Return whether the valve is open, from the ``is_open`` state key."""
        return bool(self._state.get("is_open"))

    @property
    def group_value(self) -> Any:
        """Return the group-channel aggregate; always ``None`` under the daemon model."""
        return None

    async def open(self) -> None:
        """Open the valve."""
        await self.invoke("open")

    async def close(self) -> None:
        """Close the valve."""
        await self.invoke("close")

    async def set_timer_on_time(self, *, on_time: float) -> None:
        """Open the valve for a fixed duration in seconds."""
        await self.invoke("open", params={"duration": float(on_time)})


# ---- text display ----


class CustomDpTextDisplay(_CustomEntitySurface):
    """Two-line text-display CDP."""

    _category: ClassVar[DataPointCategory] = DataPointCategory.TextDisplay

    # The daemon's text-display CDP does not yet surface the selectable
    # option lists; expose empty sets so the HA notify entity's state
    # attributes render without the per-option ActionSelects.
    @property
    def available_icons(self) -> tuple[str, ...]:
        """Return the selectable icons (none surfaced by the daemon yet)."""
        return ()

    @property
    def available_sounds(self) -> tuple[str, ...]:
        """Return the selectable sounds (none surfaced by the daemon yet)."""
        return ()

    @property
    def available_background_colors(self) -> tuple[str, ...]:
        """Return the selectable background colours (none surfaced by the daemon yet)."""
        return ()

    @property
    def available_text_colors(self) -> tuple[str, ...]:
        """Return the selectable text colours (none surfaced by the daemon yet)."""
        return ()

    @property
    def available_alignments(self) -> tuple[str, ...]:
        """Return the selectable alignments (none surfaced by the daemon yet)."""
        return ()

    @property
    def has_icons(self) -> bool:
        """Return whether the display supports icons (not surfaced yet)."""
        return False

    @property
    def has_sounds(self) -> bool:
        """Return whether the display supports sounds (not surfaced yet)."""
        return False

    @property
    def burst_limit_warning(self) -> bool:
        """Return whether the display is in burst-limit warning (not surfaced yet)."""
        return False

    async def write(self, **params: Any) -> None:
        """Write text to the display, passing through line/content params."""
        await self.invoke("write", params=params or None)

    async def clear(self) -> None:
        """Clear the display."""
        await self.invoke("clear")


# ---- factory ----

_KIND_TO_CLASS: dict[str, type[_CustomEntitySurface]] = {
    "light": CustomDpDimmer,
    "light_color": CustomDpDimmer,
    "light_color_temp": CustomDpDimmer,
    "light_rgbw": CustomDpDimmer,
    "light_dali": CustomDpDimmer,
    "light_effect": CustomDpDimmer,
    "light_fixed_color": CustomDpIpFixedColorLight,
    "light_sound_led": CustomDpSoundPlayerLed,
    "cover": CustomDpCover,
    "cover_blind": CustomDpIpBlind,
    "cover_garage": CustomDpGarage,
    "climate_simple": BaseCustomDpClimate,
    "climate_rf": BaseCustomDpClimate,
    "climate_hmip": CustomDpIpThermostat,
    "lock": BaseCustomDpLock,
    "siren": BaseCustomDpSiren,
    "siren_smoke": BaseCustomDpSiren,
    "siren_sound": CustomDpSoundPlayer,
    "switch": CustomDpSwitch,
    "valve_irrigation": CustomDpIpIrrigationValve,
    "valve_modulating": CustomDpIpIrrigationValve,
    "text_display": CustomDpTextDisplay,
}

_CATEGORY_FALLBACK: dict[str, type[_CustomEntitySurface]] = {
    "light": CustomDpDimmer,
    "cover": CustomDpCover,
    "climate": BaseCustomDpClimate,
    "lock": BaseCustomDpLock,
    "siren": BaseCustomDpSiren,
    "switch": CustomDpSwitch,
    "valve": CustomDpIpIrrigationValve,
    "text_display": CustomDpTextDisplay,
}


def resolve_custom_class(*, kind: str | None, category: str | None) -> type[_CustomEntitySurface]:
    """Pick the ``CustomDp*`` class from the daemon's kind / category."""
    if kind and kind in _KIND_TO_CLASS:
        return _KIND_TO_CLASS[kind]
    return _CATEGORY_FALLBACK.get(category or "", CustomDpSwitch)


def make_custom_data_point(
    *,
    summary: Any,
    device_address: str,
    store: LoomStore,
    initial_state: dict[str, Any] | None = None,
) -> CustomDataPoint:
    """Store CDP factory: build the categorised ``CustomDp*`` instance."""
    cls = resolve_custom_class(kind=summary.kind, category=summary.category)
    return cls(
        summary=summary,
        device_address=device_address,
        store=store,
        initial_state=initial_state,
    )


__all__ = [
    "BaseCustomDpClimate",
    "BaseCustomDpLock",
    "BaseCustomDpSiren",
    "CustomDpBlind",
    "CustomDpCover",
    "CustomDpDimmer",
    "CustomDpGarage",
    "CustomDpIpBlind",
    "CustomDpIpFixedColorLight",
    "CustomDpIpIrrigationValve",
    "CustomDpIpThermostat",
    "CustomDpSoundPlayer",
    "CustomDpSoundPlayerLed",
    "CustomDpSwitch",
    "CustomDpTextDisplay",
    "LockState",
    "PlaySoundArgs",
    "SirenOnArgs",
    "custom_unique_id",
    "make_custom_data_point",
    "resolve_custom_class",
]
