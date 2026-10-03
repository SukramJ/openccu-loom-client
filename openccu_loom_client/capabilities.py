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

from collections.abc import Iterable
from enum import StrEnum
from typing import Final


class Capability(StrEnum):
    """Capability tokens this client knows how to act on."""

    # Always emitted.
    REST = "rest.v1"
    WS_BROADCASTS = "ws.broadcasts.v1"
    PROBLEM_DETAILS = "errors.problem_details.v1"

    # Always emitted from openccu-loom v0.79.0 (api 12.0.0) on; an older
    # daemon does not send them, so they are not part of ALWAYS_ON.
    #: Every central reports its ``features`` on ``GET /system/ccu``, and
    #: an operation a central does not offer answers
    #: ``422 feature_unavailable``
    #: (:class:`~openccu_loom_client.exceptions.LoomFeatureUnavailableError`).
    CENTRAL_FEATURES = "central.features.v1"
    #: A central can be an openccu-lite system (``system_type``
    #: ``openccu-lite``).
    SOUTH_OPENCCU_LITE = "south.openccu_lite.v1"
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
    MCP = "mcp.v1"
    #: Implies :attr:`MCP`.
    MCP_WRITE = "mcp.write.v1"
    SUPERVISED_RESTART = "system.restart.supervised.v1"
    #: Predates the ``<area>.<feature>.v<n>`` convention and keeps its
    #: spelling: renaming a token a client already matches on is a
    #: breaking change.
    ADDON_SELF_UPDATE = "addon_self_update"

    # Login paths (the daemon's ADR 0081): one ``auth.<name>.v1`` token per
    # way of signing in the daemon has wired, so a client offers a person
    # only the paths listed. :func:`login_paths` turns them into the short
    # names the daemon's mDNS record carries.
    AUTH_BASIC = "auth.basic.v1"
    AUTH_BEARER = "auth.bearer.v1"
    #: The unauthenticated client-pairing routes are open
    #: (:func:`~openccu_loom_client.pairing.start_pairing`).
    AUTH_PAIRING = "auth.pairing.v1"
    AUTH_OIDC = "auth.oidc.v1"
    AUTH_CCU = "auth.ccu.v1"
    #: An openccu-lite box API token the box's gate accepted is a daemon
    #: identity.
    AUTH_OCCULITE_TOKEN = "auth.occulite_token.v1"  # noqa: S105 # nosec B105 — capability token, not a secret
    #: The same for a session of the openccu-lite box's shell.
    AUTH_OCCULITE_SSO = "auth.occulite_sso.v1"
    #: Home Assistant Ingress requests from the Supervisor count as
    #: authenticated.
    AUTH_HA_INGRESS = "auth.ha_ingress.v1"


#: The tokens every daemon emits, whatever it is configured for.
ALWAYS_ON: Final = frozenset(
    {
        Capability.REST,
        Capability.WS_BROADCASTS,
        Capability.PROBLEM_DETAILS,
    }
)

_LOGIN_PATH_PREFIX: Final = "auth."
_LOGIN_PATH_SUFFIX: Final = ".v1"


def login_paths(*, capabilities: Iterable[str]) -> frozenset[str]:
    """
    Return the short name of every login path among ``capabilities``.

    The daemon's own rule (``LoginPaths`` in its ``/info`` handler): a token
    ``auth.<name>.v1`` names the login path ``<name>`` —
    ``auth.occulite_token.v1`` is ``occulite_token``. It applies to every such
    token, known to :class:`Capability` or not, so a login path a newer daemon
    adds is reported rather than dropped. A token of another version
    (``auth.x.v2``) is a different contract and is not a login path here; an
    empty name (``auth..v1``) names nothing.
    """
    names: set[str] = set()
    for token in capabilities:
        if not token.startswith(_LOGIN_PATH_PREFIX):
            continue
        rest = token.removeprefix(_LOGIN_PATH_PREFIX)
        if rest.endswith(_LOGIN_PATH_SUFFIX) and (name := rest.removesuffix(_LOGIN_PATH_SUFFIX)):
            names.add(name)
    return frozenset(names)
