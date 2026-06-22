# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Fill the ``aiohomematic`` data-point protocol surface on the compat twins.

``homematicip_local`` and ``aiohomematic-config`` dispatch on
``isinstance(dp, <Protocol>)`` where the protocols come from the real
``aiohomematic.interfaces.model`` and are ``@runtime_checkable`` (so the
check is structural — every declared member must be present). The compat
``Dp*`` / ``CustomDp*`` / ``Sysvar*`` / ``Program*`` twins expose the
members HA reads for *behaviour* (``value``, ``unique_id``, ``category``,
``name``, ``device``, ``channel`` …) but not the long tail of
aiohomematic-internal plumbing (event/payload/translation/timer/service
machinery).

These mixins provide that tail so the twins satisfy the protocols. Values
are derived from loom data where meaningful and safe, neutral defaults
otherwise. Where a value would need data the daemon does not yet ship on
the wire (Strategy B — accurate per-parameter ``data_point_type``,
rooms/functions, translations), the default is intentionally neutral and
HA's own fallbacks apply.

The mixins are placed *after* each twin's existing surface in the MRO, so
any member already implemented there wins and these only fill gaps.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from openccu_loom_types.enums import DataPointType, DataPointUsage, ParamsetKey

from openccu_loom_client.compat.aiohomematic.model.naming import generic_translated_name

if TYPE_CHECKING:
    from openccu_loom_types.rest import CustomDPSummary, DataPointSummary, ProgramSummary, SysvarSummary

    from openccu_loom_client.model import Device
    from openccu_loom_client.store import LoomStore


class _NameData:
    """Minimal stand-in for aiohomematic's ``name_data`` value object."""

    __slots__ = ("channel_name", "full_name", "name", "parameter_name")

    def __init__(self, *, parameter_name: str, name: str, full_name: str, channel_name: str | None = None) -> None:
        """Carry the few name fields HA reads off ``name_data``."""
        self.parameter_name = parameter_name
        self.channel_name = channel_name
        self.name = name
        self.full_name = full_name


class _CommonProtocolSurface:
    """Protocol members shared by every aiohomematic data-point kind."""

    # ---- type / classification ----

    @property
    def data_point_type(self) -> DataPointType | None:
        # Prefer the daemon's value; fall back to a category-derived guess.
        explicit = getattr(self.summary, "data_point_type", None)  # type: ignore[attr-defined]
        if explicit is not None:
            return explicit  # type: ignore[no-any-return]
        category = getattr(self, "category", None)
        name = getattr(category, "name", None)
        if name is None:
            return None
        derived: DataPointType | None = getattr(DataPointType, name, None)
        return derived

    @property
    def usage(self) -> DataPointUsage:
        return DataPointUsage.DataPoint

    # ---- status / freshness ----

    @property
    def is_refreshed(self) -> bool:
        return True

    @property
    def is_status_valid(self) -> bool:
        return True

    # ---- event / payload plumbing (daemon-driven; neutral here) ----

    @property
    def config_payload(self) -> dict[str, Any]:
        return {}

    @property
    def info_payload(self) -> dict[str, Any]:
        return {}

    @property
    def state_payload(self) -> dict[str, Any]:
        return {}

    @property
    def state_path(self) -> str | None:
        return None

    @property
    def set_path(self) -> str | None:
        return None

    @property
    def signature(self) -> str | None:
        return None

    @property
    def published_event_at(self) -> Any:
        return None

    @property
    def published_event_recently(self) -> bool:
        return False

    async def publish_data_point_updated_event(self, *_args: Any, **_kwargs: Any) -> None:
        """No-op: value updates flow through the daemon → store → bus path."""

    async def publish_device_removed_event(self, *_args: Any, **_kwargs: Any) -> None:
        """No-op: device removal is broadcast by the daemon, not the data point."""

    def cleanup_subscriptions(self, *_args: Any, **_kwargs: Any) -> None:
        """No-op: subscriptions are owned by the client's event bus, not the DP."""

    def event(self, *_args: Any, **_kwargs: Any) -> None:
        """No-op: value/event ingestion flows through the daemon → store path."""

    # ---- service-call introspection (no per-DP service registry on loom) ----

    @property
    def service_method_names(self) -> tuple[str, ...]:
        return ()

    @property
    def service_methods(self) -> dict[str, Any]:
        return {}

    # ---- presentation ----

    @property
    def additional_information(self) -> dict[str, Any]:
        return {}

    @property
    def room(self) -> str | None:
        # Default for kinds with no channel (hub sysvars / programs). The
        # generic + custom surfaces override this to resolve the channel's
        # daemon-supplied room.
        return None

    @property
    def rooms(self) -> set[str]:
        room = self.room
        return {room} if room else set()

    @property
    def translation_key(self) -> str | None:
        return None


class _GenericProtocolSurface(_CommonProtocolSurface):
    """Protocol tail specific to generic ``Dp*`` data points."""

    if TYPE_CHECKING:
        # Host attributes the concrete ``Dp*`` twin provides (via ``DataPoint``
        # + ``_GenericEntitySurface``). Declared so mypy --strict type-checks
        # this mixin without ``# type: ignore[attr-defined]`` at each use site.
        summary: DataPointSummary
        parameter: str
        device: Device | None
        device_address: str
        channel_number: int
        unit: str | None
        value: Any
        emits_events: bool
        _store: LoomStore
        # Read-only on the concrete twins (overridden as properties), so declare
        # them as such — a plain-attribute annotation would clash with the override.
        parameter_label: str | None
        type: str | None
        value_list: tuple[str, ...]

        @property
        def name(self) -> str: ...  # kwonly: disable
        @property
        def full_name(self) -> str: ...  # kwonly: disable

        async def send_value(self, *, value: Any, priority: str | None = None) -> None: ...  # kwonly: disable

    @property
    def paramset_key(self) -> ParamsetKey:
        return ParamsetKey.Values

    @property
    def dpk(self) -> Any:
        # aiohomematic's DataPointKey; loom routes by unique_id instead.
        return None

    @property
    def status_dpk(self) -> Any:
        return None

    @property
    def name_data(self) -> _NameData:
        return _NameData(
            parameter_name=self.parameter,
            name=self.name,
            full_name=self.full_name,
        )

    @property
    def description(self) -> str | None:
        return None

    @property
    def room(self) -> str | None:
        """Resolve the channel's daemon-supplied room (``None`` if unknown)."""
        device = self.device
        if device is None:
            return None
        channel = device.get_channel(number=self.channel_number)
        return channel.room if channel is not None else None

    @property
    def function(self) -> str | None:
        """Resolve the channel's single Gewerk (the first daemon-supplied function)."""
        device = self.device
        if device is None:
            return None
        channel = device.get_channel(number=self.channel_number)
        if channel is None:
            return None
        functions = channel.functions
        return functions[0] if functions else None

    @property
    def service(self) -> str | None:
        return None

    @property
    def raw_unit(self) -> str | None:
        return self.unit

    @property
    def is_unit_fixed(self) -> bool:
        return False

    @property
    def visible(self) -> bool:
        return True

    @property
    def requires_polling(self) -> bool:
        return False

    @property
    def ignore_on_initial_load(self) -> bool:
        return False

    @property
    def is_un_ignored(self) -> bool:
        return False

    @property
    def is_forced_sensor(self) -> bool:
        return False

    @property
    def is_in_multiple_channels(self) -> bool:
        return False

    @property
    def has_events(self) -> bool:
        return bool(self.emits_events)

    @property
    def status(self) -> Any:
        return None

    @property
    def last_non_default_value(self) -> Any:
        return self.value

    @property
    def unconfirmed_last_value_send(self) -> Any:
        return None

    @property
    def value_translations(self) -> dict[str, Any]:
        """Localised display labels per raw ENUM value (daemon api ≥ 1.19.0)."""
        return dict(self.summary.value_translations or {})

    @property
    def translation(self) -> str | None:
        return None

    @property
    def translated_name(self) -> str | None:
        """
        Return the aiohomematic-schema display name.

        Combines the (possibly user-renamed) CCU channel name, the
        daemon's locale-resolved parameter label (``translated_name``,
        suppressed when ``label_omitted`` marks the channel's primary
        parameter) and the `` chN`` multi-channel postfix — the exact
        composition of aiohomematic's ``get_data_point_name_data``.
        ``None`` collapses the entity to the device name alone.
        """
        summary = self.summary
        device = self.device
        if device is None:
            # No device in the store (e.g. partial fixtures): fall back to
            # the daemon's plain label without channel-name composition.
            if summary.label_omitted:
                return None
            return summary.translated_name or None
        return generic_translated_name(
            store=self._store,
            device=device,
            channel_no=self.channel_number,
            parameter=self.parameter,
            translation=summary.translated_name or None,
            label_omitted=bool(summary.label_omitted),
        )

    @property
    def translated_full_name(self) -> str | None:
        name = self.translated_name
        if name is None:
            return None
        device = self.device
        device_name = device.name if device is not None else self.device_address
        return f"{device_name} {name}".strip()

    @property
    def timer_on_time(self) -> float | None:
        return None

    @property
    def timer_on_time_running(self) -> bool:
        return False

    def get_event_data(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {}

    def is_state_change(self, *_args: Any, **_kwargs: Any) -> bool:
        return True

    def force_to_sensor(self, *_args: Any, **_kwargs: Any) -> None:
        """No-op: category is fixed at construction by the resolver."""

    def force_usage(self, *_args: Any, **_kwargs: Any) -> None:
        """No-op: loom does not carry per-DP usage overrides."""

    def finalize_init(self, *_args: Any, **_kwargs: Any) -> None:
        """No-op: the store builds fully-initialised twins."""

    def on_config_changed(self, *_args: Any, **_kwargs: Any) -> None:
        """No-op: master-config changes arrive as fresh summaries."""

    def reset_timer_on_time(self, *_args: Any, **_kwargs: Any) -> None:
        """No-op."""

    def set_timer_on_time(self, *_args: Any, **_kwargs: Any) -> None:
        """No-op."""

    def update_parameter_data(self, *_args: Any, **_kwargs: Any) -> None:
        """No-op: parameter metadata updates arrive via summary replacement."""

    def update_status(self, *_args: Any, **_kwargs: Any) -> None:
        """No-op."""

    async def write_value(self, *, value: Any, **_kwargs: Any) -> None:
        """Write a raw value through the store (aiohomematic alias)."""
        await self.send_value(value=value)

    async def write_unconfirmed_value(self, *, value: Any, **_kwargs: Any) -> None:
        """Optimistic write — same path as :meth:`write_value` for loom."""
        await self.send_value(value=value)


class _CustomProtocolSurface(_CommonProtocolSurface):
    """Protocol tail specific to ``CustomDp*`` data points."""

    if TYPE_CHECKING:
        # Host attributes the concrete ``CustomDp*`` twin provides (via
        # ``CustomDataPoint`` + ``_CustomEntitySurface``).
        summary: CustomDPSummary
        device: Device | None

        @property
        def name(self) -> str: ...  # kwonly: disable
        @property
        def full_name(self) -> str: ...  # kwonly: disable

    @property
    def channel(self) -> Any:
        device = self.device
        if device is None:
            return None
        return device.get_channel(number=self.summary.channel_no)

    @property
    def channel_group(self) -> Any:
        return None

    @property
    def group_no(self) -> int | None:
        return None

    @property
    def device_config(self) -> dict[str, Any]:
        return {}

    @property
    def data_point_name_postfix(self) -> str:
        return ""

    @property
    def allow_undefined_generic_data_points(self) -> bool:
        return False

    @property
    def has_data_points(self) -> bool:
        return True

    @property
    def is_in_multiple_channels(self) -> bool:
        return False

    @property
    def is_refreshed(self) -> bool:
        return True

    @property
    def name_data(self) -> _NameData:
        return _NameData(
            parameter_name=self.name,
            name=self.name,
            full_name=self.full_name,
        )

    @property
    def room(self) -> str | None:
        """Resolve the primary channel's daemon-supplied room (``None`` if unknown)."""
        channel = self.channel
        return channel.room if channel is not None else None

    @property
    def function(self) -> str | None:
        """Resolve the primary channel's single Gewerk (first daemon-supplied function)."""
        channel = self.channel
        if channel is None:
            return None
        functions = channel.functions
        return functions[0] if functions else None

    @property
    def modified_at(self) -> Any:
        return getattr(self.summary, "modified_at", None)

    @property
    def refreshed_at(self) -> Any:
        return getattr(self.summary, "last_seen_at", None)

    @property
    def translated_name(self) -> str | None:
        return None

    @property
    def translated_full_name(self) -> str | None:
        return None

    @property
    def timer_on_time(self) -> float | None:
        return None

    @property
    def timer_on_time_running(self) -> bool:
        return False

    @property
    def unconfirmed_last_values_send(self) -> Any:
        return None

    def has_data_point_key(self, *_args: Any, **_kwargs: Any) -> bool:
        return False

    def is_state_change(self, *_args: Any, **_kwargs: Any) -> bool:
        return True

    def force_usage(self, *_args: Any, **_kwargs: Any) -> None:
        """No-op."""

    def reset_timer_on_time(self, *_args: Any, **_kwargs: Any) -> None:
        """No-op."""

    async def set_timer_on_time(self, *, on_time: float) -> None:
        """No-op default: only CDP kinds with a timed-on operation override this."""

    def unsubscribe_from_data_point_updated(self, *_args: Any, **_kwargs: Any) -> None:
        """No-op: the client owns subscriptions."""


class _HubProtocolSurface(_CommonProtocolSurface):
    """Protocol tail shared by hub (sysvar + program) data points."""

    if TYPE_CHECKING:
        # Shared by both hub kinds, whose summary types differ — declared as a
        # union so the cross-kind reads below stay ``getattr`` (a sysvar-only
        # field like ``value`` is absent on a program summary and vice versa).
        summary: SysvarSummary | ProgramSummary

        @property
        def name(self) -> str: ...  # kwonly: disable

    @property
    def channel(self) -> Any:
        return None

    @property
    def available(self) -> bool:
        return True

    @property
    def is_valid(self) -> bool:
        # ``value`` is a sysvar-only field — cross-kind probe over the hub union.
        return getattr(self.summary, "value", None) is not None

    @property
    def full_name(self) -> str:
        return self.name

    @property
    def legacy_name(self) -> str:
        return self.name

    @property
    def modified_at(self) -> Any:
        # modified_at / last_seen_at are not on the hub summaries in the pinned
        # wire schema — deliberate cross-version probes.
        return getattr(self.summary, "modified_at", None)

    @property
    def refreshed_at(self) -> Any:
        return getattr(self.summary, "last_seen_at", None)


class _SysvarProtocolSurface(_HubProtocolSurface):
    """Protocol tail specific to ``Sysvar*`` data points."""

    if TYPE_CHECKING:
        # Host attributes the concrete ``Sysvar*`` twin provides (via ``Sysvar``).
        summary: SysvarSummary  # narrows the hub union

        @property
        def value_list(self) -> tuple[str, ...]: ...  # kwonly: disable

        async def set_value(self, *, value: Any) -> None: ...  # kwonly: disable

    @property
    def vid(self) -> str:
        return self.name

    @property
    def is_extended(self) -> bool:
        return bool(self.value_list)

    @property
    def min(self) -> Any:
        return self.summary.min

    @property
    def max(self) -> Any:
        return self.summary.max

    @property
    def previous_value(self) -> Any:
        # ``previous`` is not on SysvarSummary in the pinned wire schema —
        # deliberate cross-version probe (loud here if it ever lands typed).
        return getattr(self.summary, "previous", None)

    async def write_value(self, *, value: Any, **_kwargs: Any) -> None:
        """Write the sysvar value through the store."""
        await self.set_value(value=value)


class _ProgramProtocolSurface(_HubProtocolSurface):
    """Protocol tail specific to ``Program*`` data points."""

    if TYPE_CHECKING:
        # Host attributes the concrete ``Program*`` twin provides (via ``Program``).
        summary: ProgramSummary  # narrows the hub union
        id: str

    @property
    def pid(self) -> str:
        return self.id

    @property
    def is_active(self) -> bool:
        return bool(self.summary.active)

    @property
    def is_internal(self) -> bool:
        return False

    @property
    def last_execute_time(self) -> Any:
        # aiohomematic exposes this as ``last_execute_time``; the daemon ships
        # it as ``last_executed`` (was a silent None before typing surfaced it).
        return self.summary.last_executed

    def update_data(self, *_args: Any, **_kwargs: Any) -> None:
        """No-op: program metadata updates arrive via summary replacement."""
