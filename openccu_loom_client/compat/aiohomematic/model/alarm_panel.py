# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Categorised alarm-control-panel data point (loom-native, no aio twin).

The alarm panel is the one HA surface that exists **only** on the loom
backend: aiohomematic has no alarm engine, so there is no aiohomematic
class to twin — ``homematicip_local``'s ``alarm_control_panel`` platform
dispatches on this class alone. It therefore inherits the domain
:class:`~openccu_loom_client.model.alarm_panel.AlarmPanel` (the live
store object) and adds the hub-entity surface (category, registration,
``enabled_default``) the HA generic-hub-entity base expects.

Identity: the ``unique_id`` is the daemon-computed
``openccu-loom_alarm_<area>`` (``alarmpanel.PanelUniqueID``) — consumed
as-is, never re-derived, so the REST/WS/MQTT surfaces and the compat
layer can never drift. The panel attaches to the central hub device in
HA (``channel`` is ``None``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from openccu_loom_types.enums import DataPointCategory

from openccu_loom_client.compat.aiohomematic.model.hub._surface import _HubEntitySurface
from openccu_loom_client.model.alarm_panel import AlarmPanel

if TYPE_CHECKING:
    from openccu_loom_types.rest import AlarmPanelEntity

    from openccu_loom_client.store import LoomStore


class LoomDpAlarmControlPanel(_HubEntitySurface, AlarmPanel):
    """
    Alarm panel with the aiohomematic-shaped entity surface on top.

    ``state`` is the daemon-computed HA state token (``disarmed``/
    ``arming``/``pending``/``triggered``/``armed_home``/…), commands
    route through the domain wrapper's :meth:`arm`/:meth:`disarm`/
    :meth:`silence`/:meth:`acknowledge` (master panels fan out).
    """

    _category: ClassVar[DataPointCategory] = DataPointCategory.AlarmControlPanel

    def __init__(self, *, summary: AlarmPanelEntity, store: LoomStore) -> None:
        """Bind the panel and enable it by default (unlike generic sysvars)."""
        super().__init__(summary=summary, store=store)
        self._enabled_default = True

    @property
    def channel(self) -> None:
        """Return ``None`` — panels attach to the central hub device."""
        return None

    @property
    def value(self) -> str:
        """Return the HA alarm state token (alias of :attr:`state`)."""
        return self.state

    @property
    def is_valid(self) -> bool:
        """Return whether a state has been observed (always daemon-seeded)."""
        return bool(self.state)

    @property
    def code_arm_required(self) -> bool:
        """
        Whether arming requires a code.

        The daemon enforces its per-area code policy server-side either
        way (an arm without a required code answers 403); the flag only
        drives HA's code-prompt UX. Conservative default: no prompt —
        the flag flips once the daemon exposes the policy on the panel
        entity.
        """
        return False

    @property
    def code_disarm_required(self) -> bool:
        """
        Whether disarming requires a code.

        Same server-side enforcement note as :attr:`code_arm_required`;
        HA still offers the code field on disarm when the panel reports
        ``REMOTE_CODE`` semantics integration-side.
        """
        return False

    @property
    def additional_information(self) -> dict[str, Any]:
        """Return the extra state attributes (mirrors HA's attribute card)."""
        return self.attributes

    @property
    def attributes(self) -> dict[str, Any]:
        """Return the extra state attributes of the panel."""
        attrs: dict[str, Any] = {
            "area_id": self.area_id,
            "master": self.is_master,
            "supported_modes": list(self.supported_modes),
        }
        if self.mode is not None:
            attrs["mode"] = self.mode
        if self.bypassed:
            attrs["bypassed"] = list(self.bypassed)
        if self.countdown_kind is not None:
            attrs["countdown_kind"] = self.countdown_kind
            attrs["countdown_remaining_s"] = self.countdown_remaining_s
            attrs["countdown_total_s"] = self.countdown_total_s
        if self.readiness:
            attrs["readiness"] = {
                mode: {
                    "ready": entry.ready,
                    "blockers": list(entry.blockers or ()),
                    "warnings": list(entry.warnings or ()),
                }
                for mode, entry in self.readiness.items()
            }
        if self.walktest_active:
            attrs["walktest_active"] = True
        if self.last_incident_id is not None:
            attrs["last_incident_id"] = self.last_incident_id
            attrs["last_incident_cause"] = self.last_incident_cause
            attrs["last_incident_sensor"] = self.last_incident_sensor
        return attrs


def make_alarm_panel_data_point(*, summary: AlarmPanelEntity, store: LoomStore) -> AlarmPanel:
    """Store alarm-panel factory: build the categorised panel instance."""
    return LoomDpAlarmControlPanel(summary=summary, store=store)


__all__ = ["LoomDpAlarmControlPanel", "make_alarm_panel_data_point"]
