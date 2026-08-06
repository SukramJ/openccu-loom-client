# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Entity-name catalogue operations (daemon ≥ 0.54.0, api 5.2.0).

The daemon is the single naming authority: it names every device,
channel and custom data point on the wire, and it names its own hub and
Security & Safety entities in its i18n catalogue. Until api 5.2.0 that
second half only reached the MQTT discovery plane, so a REST/WebSocket
consumer had to keep a second copy of the same words — which drifts the
moment either side is edited alone.

This façade reads the daemon's copy. A daemon older than api 5.2.0
answers 404; callers treat that as "no catalogue" and fall back to their
own tokens, which is exactly what happens for an unknown key anyway.
"""

from __future__ import annotations

from openccu_loom_types.rest import EntityNameCatalogue

from openccu_loom_client.operations._base import _OperationsBase


class I18nOperations(_OperationsBase):
    """The daemon's entity-naming vocabulary."""

    async def get_entity_names(self, *, locale: str | None = None) -> EntityNameCatalogue:
        """
        Return the entity-name catalogue for ``locale``.

        Wire: ``GET /i18n/entities``. ``locale`` defaults to the daemon's
        configured one; an unknown tag falls back to it as well, and the
        response echoes which locale actually answered so a caller can
        tell a translation from a fallback.

        Values are templates as authored: ``Connectivity {iface}`` keeps
        its placeholder, because only the caller knows which interface it
        is naming.
        """
        params = {"locale": locale} if locale else None
        payload = await self._transport.request(method="GET", path="/i18n/entities", params=params)
        return EntityNameCatalogue.model_validate(payload)
