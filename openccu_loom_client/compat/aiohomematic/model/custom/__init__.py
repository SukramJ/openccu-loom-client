# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""``aiohomematic.model.custom`` — categorised Custom-DP classes.

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

from typing import TYPE_CHECKING, Any, ClassVar

from openccu_loom_types.enums import DataPointCategory

from openccu_loom_client.model import CustomDataPoint

if TYPE_CHECKING:
    from openccu_loom_client.store import LoomStore


def custom_unique_id(*, device_address: str, name: str) -> str:
    """Stable HA unique id for a Custom-DP, matched by the refresh bridge."""
    return f"{device_address}_cdp_{name}".replace(":", "_").replace("-", "_").lower()


class _Capabilities:
    """Attribute-access view over the daemon's ``capabilities`` flag map.

    HA reads capability flags as attributes (``dp.capabilities.brightness``,
    ``.color``, ``.profiles``, ``.open``, ``.tones`` …). Unknown flags
    read as ``False`` so a missing capability never raises.
    """

    __slots__ = ("_flags",)

    def __init__(self, flags: dict[str, bool] | None) -> None:
        self._flags = flags or {}

    def __getattr__(self, name: str) -> bool:
        return bool(self._flags.get(name, False))

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


class _CustomEntitySurface(CustomDataPoint):
    """Entity-facing surface shared by every ``CustomDp*`` class."""

    _category: ClassVar[DataPointCategory] = DataPointCategory.Switch

    @property
    def category(self) -> DataPointCategory:
        # Prefer the daemon's category string; fall back to the class default.
        return _CATEGORY_BY_STRING.get(self._summary.category or "", self._category)

    @property
    def unique_id(self) -> str:
        return custom_unique_id(device_address=self._device_address, name=self.name)

    @property
    def full_name(self) -> str:
        device = self.device
        device_name = device.name if device is not None else self._device_address
        return f"{device_name} {self.name}"

    @property
    def central(self) -> Any:
        return None

    @property
    def is_valid(self) -> bool:
        return bool(self._state)

    @property
    def available(self) -> bool:
        device = self.device
        return bool(device.available) if device is not None else True

    @property
    def state_uncertain(self) -> bool:
        return bool(self._state.get("state_uncertain", False))

    @property
    def enabled_default(self) -> bool:
        return True

    @property
    def capabilities(self) -> _Capabilities:
        return _Capabilities(self._summary.capabilities)

    @property
    def is_registered(self) -> bool:
        return getattr(self, "_registered", False)

    def register(self) -> None:
        self._registered = True

    def unregister(self) -> None:
        self._registered = False

    async def load_data_point_value(self, *, call_source: Any = None) -> None:
        await self._store.refresh_custom_data_point(
            address=self._device_address, name=self.name
        )


# ---- switch ----


class CustomDpSwitch(_CustomEntitySurface):
    """Switch-style CDP (on/off)."""

    _category: ClassVar[DataPointCategory] = DataPointCategory.Switch

    @property
    def value(self) -> bool | None:
        return self._state.get("is_on")

    @property
    def is_on(self) -> bool:
        return bool(self._state.get("is_on"))

    @property
    def group_value(self) -> Any:
        # aiohomematic exposes a group-channel aggregate here; the daemon
        # collapses that server-side, so there is no separate group value.
        return None

    async def turn_on(self, **_kwargs: Any) -> None:
        await self.invoke("turn_on")

    async def turn_off(self, **_kwargs: Any) -> None:
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
        return str(self._state.get("state", "OFF")).upper() == "ON"

    @property
    def brightness(self) -> int | None:
        return _as_int(self._state.get("brightness"))

    @property
    def has_color_temperature(self) -> bool:
        return self.capabilities.color_temp

    @property
    def has_hs_color(self) -> bool:
        return self.capabilities.color

    @property
    def has_effects(self) -> bool:
        return self.capabilities.effects

    # Colour / effect state is not carried in the daemon's light CDP
    # state (only on/off + brightness); these read as "unknown" until the
    # daemon surfaces them. Writes still drive the daemon set_* ops.
    @property
    def color_temp_kelvin(self) -> int | None:
        return _as_int(self._state.get("color_temp_kelvin"))

    @property
    def hs_color(self) -> tuple[float, float] | None:
        hue = _as_float(self._state.get("hue"))
        sat = _as_float(self._state.get("saturation"))
        return (hue, sat) if hue is not None and sat is not None else None

    @property
    def effect(self) -> str | None:
        val = self._state.get("effect")
        return str(val) if val is not None else None

    @property
    def effects(self) -> tuple[str, ...]:
        raw = self._state.get("effects") or ()
        return tuple(str(e) for e in raw)

    @property
    def last_level(self) -> int:
        return _as_int(self._state.get("brightness")) or 0

    @staticmethod
    def level_to_brightness(level: float) -> int:
        return round(level * 255)

    @staticmethod
    def brightness_to_level(brightness: int) -> float:
        return brightness / 255.0

    def set_last_level(self, value: int) -> None:
        # Last-brightness restore is HA-side bookkeeping; no daemon write.
        self._state["brightness"] = value

    async def set_timer_on_time(self, *, on_time: float) -> None:
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
        if hs_color is not None:
            await self.invoke(
                "set_color", params={"hue": hs_color[0], "saturation": hs_color[1]}
            )
        if color_temp_kelvin is not None:
            await self.invoke("set_kelvin", params={"kelvin": int(color_temp_kelvin)})
        if effect is not None:
            await self.invoke("set_effect", params={"effect": effect})
        if brightness is not None:
            await self.invoke("set_level", params={"brightness": int(brightness)})
        elif hs_color is None and color_temp_kelvin is None and effect is None:
            await self.invoke("turn_on")

    async def turn_off(self, **_kwargs: Any) -> None:
        await self.invoke("turn_off")

    async def set_brightness(self, brightness: int) -> None:
        await self.invoke("set_level", params={"brightness": int(brightness)})


class CustomDpIpFixedColorLight(CustomDpDimmer):
    """HmIP fixed-colour light."""


class CustomDpSoundPlayerLed(CustomDpDimmer):
    """Sound-player LED light variant."""


# ---- cover / blind / garage ----


class CustomDpCover(_CustomEntitySurface):
    """Cover (shutter) CDP."""

    _category: ClassVar[DataPointCategory] = DataPointCategory.Cover

    @property
    def current_position(self) -> int | None:
        return _as_int(self._state.get("current_position"))

    @property
    def current_channel_position(self) -> int | None:
        return self.current_position

    @property
    def _state_token(self) -> str:
        return str(self._state.get("state", "")).lower()

    @property
    def is_closed(self) -> bool:
        pos = self.current_position
        if pos is not None:
            return pos == 0
        return self._state_token == "closed"

    @property
    def is_opening(self) -> bool:
        return self._state.get("direction") == "opening" or self._state_token == "opening"

    @property
    def is_closing(self) -> bool:
        return self._state.get("direction") == "closing" or self._state_token == "closing"

    async def open(self) -> None:
        await self.invoke("open")

    async def close(self) -> None:
        await self.invoke("close")

    async def stop(self) -> None:
        await self.invoke("stop")

    async def set_position(
        self,
        position: int,
        tilt_position: int | None = None,
        collector: Any = None,
        **_kwargs: Any,
    ) -> None:
        await self.invoke("set_position", params={"position": position / 100.0})


class CustomDpBlind(CustomDpCover):
    """Cover with a tilt axis."""

    @property
    def current_tilt_position(self) -> int | None:
        return _as_int(self._state.get("current_tilt_position"))

    @property
    def current_channel_tilt_position(self) -> int | None:
        return self.current_tilt_position

    async def set_position(
        self,
        position: int,
        tilt_position: int | None = None,
        collector: Any = None,
        **_kwargs: Any,
    ) -> None:
        await self.invoke("set_position", params={"position": position / 100.0})
        if tilt_position is not None:
            await self.invoke("set_tilt", params={"tilt": tilt_position / 100.0})

    async def set_tilt_position(self, tilt_position: int) -> None:
        await self.invoke("set_tilt", params={"tilt": tilt_position / 100.0})

    async def open_tilt(self) -> None:
        await self.invoke("open_tilt")

    async def close_tilt(self) -> None:
        await self.invoke("close_tilt")

    async def stop_tilt(self) -> None:
        await self.invoke("stop_tilt")


class CustomDpIpBlind(CustomDpBlind):
    """HmIP blind."""

    @property
    def operation_mode(self) -> str | None:
        val = self._state.get("operation_mode")
        return str(val) if val is not None else None


class CustomDpGarage(CustomDpCover):
    """Garage-door CDP."""

    async def ventilate(self) -> None:
        await self.invoke("ventilate")


# ---- climate ----


class BaseCustomDpClimate(_CustomEntitySurface):
    """Thermostat CDP.

    ``hvac_mode``/``preset_mode``/``action`` come straight from the
    daemon's climate state. Current/target temperature are read from the
    state dict when present (key names may evolve daemon-side); the
    setters drive the documented ``set_*`` operations.
    """

    _category: ClassVar[DataPointCategory] = DataPointCategory.Climate

    @property
    def hvac_mode(self) -> str:
        return str(self._state.get("hvac_mode", "off"))

    @property
    def preset_mode(self) -> str:
        return str(self._state.get("preset_mode", "none"))

    @property
    def action(self) -> str | None:
        val = self._state.get("action")
        return str(val) if val is not None else None

    @property
    def current_temperature(self) -> float | None:
        val = self._state.get("current_temperature")
        return float(val) if isinstance(val, (int, float)) else None

    @property
    def target_temperature(self) -> float | None:
        val = self._state.get("set_temperature", self._state.get("target_temperature"))
        return float(val) if isinstance(val, (int, float)) else None

    @property
    def current_humidity(self) -> int | None:
        return _as_int(self._state.get("current_humidity"))

    @property
    def temperature_offset(self) -> str | None:
        val = self._state.get("temperature_offset")
        return str(val) if val is not None else None

    @property
    def min_temp(self) -> float:
        return _as_float(self._state.get("min_temp")) or 4.5

    @property
    def max_temp(self) -> float:
        return _as_float(self._state.get("max_temp")) or 30.5

    @property
    def target_temperature_step(self) -> float:
        return _as_float(self._state.get("target_temperature_step")) or 0.5

    # ``mode``/``activity``/``profile`` are returned as the daemon's
    # lower-case string tokens; consumers comparing against
    # aiohomematic's Climate* enums should compare by ``.value``.
    @property
    def mode(self) -> str:
        return self.hvac_mode

    @property
    def modes(self) -> tuple[str, ...]:
        raw = self._state.get("hvac_modes") or ()
        return tuple(str(m) for m in raw)

    @property
    def activity(self) -> str | None:
        return self.action

    @property
    def profile(self) -> str:
        return self.preset_mode

    @property
    def profiles(self) -> tuple[str, ...]:
        raw = self._state.get("available_profiles") or ()
        return tuple(str(p) for p in raw)

    async def set_temperature(self, temperature: float) -> None:
        await self.invoke("set_temperature", params={"temperature": float(temperature)})

    async def set_mode(self, mode: str) -> None:
        await self.invoke("set_mode", params={"mode": str(mode)})

    async def set_profile(self, profile: str) -> None:
        await self.invoke("set_profile", params={"profile": str(profile)})

    async def enable_away_mode_by_duration(
        self, hours: int, away_temperature: float
    ) -> None:
        await self.invoke(
            "enable_away", params={"hours": hours, "temperature": away_temperature}
        )

    async def enable_away_mode_by_calendar(
        self, start: Any, end: Any, away_temperature: float
    ) -> None:
        await self.invoke(
            "enable_away",
            params={"start": start, "end": end, "temperature": away_temperature},
        )

    async def disable_away_mode(self) -> None:
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
    def is_locked(self) -> bool:
        return bool(self._state.get("is_locked"))

    @property
    def is_locking(self) -> bool:
        return bool(self._state.get("is_locking"))

    @property
    def is_unlocking(self) -> bool:
        return bool(self._state.get("is_unlocking"))

    @property
    def is_jammed(self) -> bool:
        return bool(self._state.get("is_jammed"))

    async def lock(self) -> None:
        await self.invoke("lock")

    async def unlock(self) -> None:
        await self.invoke("unlock")

    async def open(self) -> None:
        await self.invoke("open")


# ---- siren ----


class PlaySoundArgs:
    """Container for play-sound parameters; mirrors aiohomematic shape."""

    def __init__(self, *, sound: str, duration: int | None = None) -> None:
        self.sound = sound
        self.duration = duration


class SirenOnArgs:
    """Container for siren-on parameters."""

    def __init__(self, *, sound: str | None = None, duration: int | None = None) -> None:
        self.sound = sound
        self.duration = duration


class BaseCustomDpSiren(_CustomEntitySurface):
    """Siren CDP."""

    _category: ClassVar[DataPointCategory] = DataPointCategory.Siren

    @property
    def is_on(self) -> bool:
        return str(self._state.get("state", "off")).lower() == "on"

    @property
    def available_tones(self) -> Any:
        return self._state.get("available_tones") or {}

    @property
    def available_lights(self) -> Any:
        return self._state.get("available_lights") or {}

    async def turn_on(self, **params: Any) -> None:
        await self.invoke("turn_on", params=params or None)

    async def turn_off(self) -> None:
        await self.invoke("turn_off")


class CustomDpSoundPlayer(BaseCustomDpSiren):
    """Sound-player variant of a siren."""

    @property
    def available_soundfiles(self) -> Any:
        return self._state.get("available_soundfiles") or {}

    @property
    def current_soundfile(self) -> str | None:
        val = self._state.get("current_soundfile")
        return str(val) if val is not None else None

    async def play_sound(self, **params: Any) -> None:
        await self.invoke("turn_on", params=params or None)

    async def stop_sound(self) -> None:
        await self.invoke("turn_off")


# ---- valve ----


class CustomDpIpIrrigationValve(_CustomEntitySurface):
    """Irrigation valve CDP."""

    _category: ClassVar[DataPointCategory] = DataPointCategory.Valve

    @property
    def value(self) -> bool:
        return bool(self._state.get("is_open"))

    @property
    def is_open(self) -> bool:
        return bool(self._state.get("is_open"))

    @property
    def group_value(self) -> Any:
        return None

    async def open(self) -> None:
        await self.invoke("open")

    async def close(self) -> None:
        await self.invoke("close")

    async def set_timer_on_time(self, *, on_time: float) -> None:
        await self.invoke("open", params={"duration": float(on_time)})


# ---- text display ----


class CustomDpTextDisplay(_CustomEntitySurface):
    """Two-line text-display CDP."""

    _category: ClassVar[DataPointCategory] = DataPointCategory.TextDisplay

    async def write(self, **params: Any) -> None:
        await self.invoke("write", params=params or None)

    async def clear(self) -> None:
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


def resolve_custom_class(
    *, kind: str | None, category: str | None
) -> type[_CustomEntitySurface]:
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
