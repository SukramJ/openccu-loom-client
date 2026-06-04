# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
Direct-link and central-link REST operations.

Maps to the ``links``-tagged endpoints in the daemon's OpenAPI
surface. Two distinct concepts live here:

- **Direct links** — sender→receiver peerings between two channels
  (``aiohomematic``'s direct-link feature). Listing, adding, removing,
  plus the LINK paramset between a channel and a peer.
- **Central links** — the daemon-side PRESS-event forwarding that
  turns physical button presses into central (HA-visible) click
  events. Enabling these is the prerequisite for HA automations on
  Homematic button/remote presses.

Wire types come from ``openccu_loom_types.rest`` (:class:`Link`,
:class:`AddLinkRequest`, :class:`CentralLinksStatus`); the LINK
paramset is a free-form ``map[string]any`` and stays a dict.
"""

from __future__ import annotations

from typing import Any

from openccu_loom_types.rest import AddLinkRequest, CentralLinksStatus, Link

from openccu_loom_client.operations._base import _OperationsBase


class LinksOperations(_OperationsBase):
    """Wraps the daemon's direct-link + central-link REST surface."""

    # ---- direct links ----

    async def list_links(self, *, address: str, locale: str = "en") -> list[Link]:
        """
        List the direct links a device participates in.

        Wire: ``GET /devices/{addr}/links?locale=``.
        """
        payload = await self._transport.request(
            "GET",
            f"/devices/{address}/links",
            params={"locale": locale},
        )
        return [Link.model_validate(link) for link in (payload or [])]

    async def add_link(
        self,
        *,
        address: str,
        sender_address: str,
        receiver_address: str,
        name: str | None = None,
        description: str | None = None,
    ) -> None:
        """
        Add a direct link (sender → receiver).

        Wire: ``POST /devices/{addr}/links`` with an
        :class:`AddLinkRequest`. Not retried — link creation is a CCU
        mutation that is not safe to repeat blindly.
        """
        body = AddLinkRequest(
            sender_address=sender_address,
            receiver_address=receiver_address,
            name=name,
            description=description,
        )
        await self._transport.request(
            "POST",
            f"/devices/{address}/links",
            json_body=body.model_dump(mode="json", exclude_none=True),
            allow_retry=False,
        )

    async def remove_link(self, *, address: str, sender: str, receiver: str) -> None:
        """
        Remove a direct link.

        Wire: ``DELETE /devices/{addr}/links?sender=&receiver=``.
        Idempotent — removing an absent link is a no-op.
        """
        await self._transport.request(
            "DELETE",
            f"/devices/{address}/links",
            params={"sender": sender, "receiver": receiver},
            allow_retry=True,
        )

    async def get_link_paramset(self, *, address: str, peer: str) -> dict[str, Any]:
        """
        Read the LINK paramset between this channel and a peer.

        Wire: ``GET /devices/{addr}/link-ps/{peer}``.
        """
        payload = await self._transport.request(
            "GET",
            f"/devices/{address}/link-ps/{peer}",
        )
        return dict(payload or {})

    async def put_link_paramset(self, *, address: str, peer: str, values: dict[str, Any]) -> None:
        """
        Write the LINK paramset to a peer.

        Wire: ``PUT /devices/{addr}/link-ps/{peer}``.
        """
        await self._transport.request(
            "PUT",
            f"/devices/{address}/link-ps/{peer}",
            json_body=values,
            allow_retry=True,
        )

    async def linkable_channels(
        self,
        *,
        address: str,
        channel: int,
        role: str,
        interface: str,
        locale: str = "en",
    ) -> Any:
        """
        List channels eligible to link against this channel.

        Wire: ``GET /devices/{addr}/channels/{n}/linkable-channels``.
        ``role`` is ``sender`` or ``receiver``.
        """
        return await self._transport.request(
            "GET",
            f"/devices/{address}/channels/{channel}/linkable-channels",
            params={"role": role, "interface": interface, "locale": locale},
        )

    # ---- central links (PRESS-event forwarding) ----

    async def get_central_links_status(self, *, address: str) -> CentralLinksStatus:
        """
        PRESS-event forwarding status for a device.

        Wire: ``GET /devices/{addr}/central-links``.
        """
        payload = await self._transport.request(
            "GET",
            f"/devices/{address}/central-links",
        )
        return CentralLinksStatus.model_validate(payload)

    async def enable_central_links(self, *, address: str) -> None:
        """
        Enable central click-event forwarding for a device.

        Wire: ``POST /devices/{addr}/central-links``. This is the
        switch that makes physical button presses observable as
        central (HA) trigger events.
        """
        await self._transport.request(
            "POST",
            f"/devices/{address}/central-links",
            allow_retry=False,
        )

    async def disable_central_links(self, *, address: str) -> None:
        """
        Disable central click-event forwarding for a device.

        Wire: ``DELETE /devices/{addr}/central-links``. Idempotent.
        """
        await self._transport.request(
            "DELETE",
            f"/devices/{address}/central-links",
            allow_retry=True,
        )
