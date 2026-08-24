# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
The daemon's capability tokens, as names instead of bare strings.

A token in ``Info.capabilities`` means the daemon is **configured** for
that capability — not that the subsystem is working at this instant. It
answers "may I use this path at all", which is what a client needs to
build its feature set. A broker that is briefly unreachable is not a
missing capability, and a token that came and went with connectivity
would force every client to re-derive its surface on each poll. For what
is running right now, read the daemon's ``/health`` components instead.

Why names rather than the strings they wrap: a token is only ever
compared, never parsed, so a typo cannot fail loudly. Passing
``"alram.v1"`` to :meth:`LoomClient.connect`'s ``required_capabilities``
raises "daemon is missing required capabilities" on every daemon that
will ever exist, and reads like the daemon's fault. :data:`Capability`
turns that into an ``AttributeError`` at the call site.

The set is open on purpose. The daemon may advertise tokens this
package does not know, and a client must ignore what it does not
recognise rather than reject the payload — so this is a convenience for
the tokens we act on, not an allowlist to validate against.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class Capability(StrEnum):
    """Capability tokens this client knows how to act on."""

    # Always emitted.
    REST = "rest.v1"
    WS_BROADCASTS = "ws.broadcasts.v1"
    PROBLEM_DETAILS = "errors.problem_details.v1"

    # Emitted when the matching subsystem is configured.
    ALARM = "alarm.v1"
    HISTORY = "history.v1"
    MATTER_BRIDGE = "matter.bridge.v1"
    MQTT_DISCOVERY = "mqtt.discovery.v1"
    MQTT_RAW = "mqtt.raw.v1"
    WEBHOOK_INBOUND = "webhook.inbound.v1"
    DIAGRAMS = "diagrams.v1"
    #: The database behind stored users, tokens, centrals, config
    #: sections, preferences and areas. Without it those routes are
    #: mounted and every write is refused, which a caller cannot tell
    #: apart from a permission problem.
    ADMIN_PERSISTENCE = "admin.persistence.v1"
    AUTH_OIDC = "auth.oidc.v1"
    AUTH_CCU = "auth.ccu.v1"
    MCP = "mcp.v1"
    #: Implies :attr:`MCP`.
    MCP_WRITE = "mcp.write.v1"
    SUPERVISED_RESTART = "system.restart.supervised.v1"
    #: Predates the ``<area>.<feature>.v<n>`` convention and keeps its
    #: spelling: renaming a token a client already matches on is a
    #: breaking change.
    ADDON_SELF_UPDATE = "addon_self_update"


#: The tokens every daemon emits, whatever it is configured for.
ALWAYS_ON: Final = frozenset(
    {
        Capability.REST,
        Capability.WS_BROADCASTS,
        Capability.PROBLEM_DETAILS,
    }
)
