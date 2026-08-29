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

Wire types come from ``openccu_loom_client.wire.rest`` (:class:`Link`,
:class:`AddLinkRequest`, :class:`CentralLinksStatus`); the LINK
paramset is a free-form ``map[string]any`` and stays a dict.
"""

from __future__ import annotations

import contextlib
from typing import Any

from openccu_loom_client.exceptions import BaseLoomException
from openccu_loom_client.operations._base import _OperationsBase
from openccu_loom_client.operations.sessions import SessionsOperations
from openccu_loom_client.wire.rest import AddLinkRequest, CentralLinksStatus, Link


class LinksOperations(_OperationsBase):
    """Wraps the daemon's direct-link + central-link REST surface."""

    # ---- direct links ----

    async def list_links(self, *, address: str, locale: str = "en") -> list[Link]:
        """
        List the direct links a device participates in.

        Wire: ``GET /devices/{addr}/links?locale=``.
        """
        return await self._request_list(
            method="GET",
            path=f"/devices/{address}/links",
            params={"locale": locale},
            model=Link,
        )

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
            method="POST",
            path=f"/devices/{address}/links",
            json_body=self._to_json_body(body),
            allow_retry=False,
        )

    async def remove_link(self, *, address: str, sender: str, receiver: str) -> None:
        """
        Remove a direct link.

        Wire: ``DELETE /devices/{addr}/links?sender=&receiver=``.
        Idempotent — removing an absent link is a no-op.
        """
        await self._transport.request(
            method="DELETE",
            path=f"/devices/{address}/links",
            params={"sender": sender, "receiver": receiver},
            allow_retry=True,
        )

    async def get_link_paramset(self, *, address: str, peer: str) -> dict[str, Any]:
        """
        Read the LINK paramset between this channel and a peer.

        Wire: ``GET /devices/{addr}/link-ps/{peer}``.
        """
        payload = await self._transport.request(
            method="GET",
            path=f"/devices/{address}/link-ps/{peer}",
        )
        return dict(payload or {})

    async def put_link_paramset(self, *, address: str, peer: str, values: dict[str, Any]) -> None:
        """
        Write the LINK paramset to a peer.

        Wire: ``PUT /devices/{addr}/link-ps/{peer}``. A LINK paramset is
        per-peer configuration behind the daemon's per-resource **edit lock**:
        without a valid ``X-Edit-Token`` for the key
        ``channel:{addr}:LINK:{peer}`` the daemon rejects the write with
        ``423 Locked``. So the lock is opened, the write performed, and the lock
        released again — even if the write fails.
        """
        sessions = SessionsOperations(transport=self._transport)
        lock_key = f"channel:{address}:LINK:{peer}"
        session = await sessions.acquire(key=lock_key)
        token = session.get("token")
        try:
            await self._transport.request(
                method="PUT",
                path=f"/devices/{address}/link-ps/{peer}",
                json_body=values,
                headers={"X-Edit-Token": token} if token else None,
                allow_retry=True,
            )
        finally:
            # Never let a release failure mask the write's own outcome. The
            # release must name the lock and prove ownership (api 6.0.0);
            # without a token there is nothing to prove, so the lock is left
            # to its 5-minute TTL.
            if token:
                with contextlib.suppress(BaseLoomException):
                    await sessions.release(key=lock_key, token=token)

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
            method="GET",
            path=f"/devices/{address}/channels/{channel}/linkable-channels",
            params={"role": role, "interface": interface, "locale": locale},
        )

    # ---- central links (PRESS-event forwarding) ----

    async def get_central_links_status(self, *, address: str) -> CentralLinksStatus:
        """
        PRESS-event forwarding status for a device.

        Wire: ``GET /devices/{addr}/central-links``.
        """
        payload = await self._transport.request(
            method="GET",
            path=f"/devices/{address}/central-links",
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
            method="POST",
            path=f"/devices/{address}/central-links",
            allow_retry=False,
        )

    async def disable_central_links(self, *, address: str) -> None:
        """
        Disable central click-event forwarding for a device.

        Wire: ``DELETE /devices/{addr}/central-links``. Idempotent.
        """
        await self._transport.request(
            method="DELETE",
            path=f"/devices/{address}/central-links",
            allow_retry=True,
        )

    # ---- central links, named after the daemon's central.* commands ----

    async def central_links_status(self, *, address: str) -> CentralLinksStatus:
        """
        PRESS-event forwarding status for a device.

        Wire: ``GET /devices/{addr}/central-links``. Alias of
        :meth:`get_central_links_status` named after the daemon's
        ``central.links_status`` command.
        """
        return await self.get_central_links_status(address=address)

    async def create_central_links(self, *, address: str) -> None:
        """
        Enable central click-event forwarding for a device.

        Wire: ``POST /devices/{addr}/central-links``. Alias of
        :meth:`enable_central_links` named after the daemon's
        ``central.create_links`` command.
        """
        await self.enable_central_links(address=address)

    async def remove_central_links(self, *, address: str) -> None:
        """
        Disable central click-event forwarding for a device.

        Wire: ``DELETE /devices/{addr}/central-links``. Alias of
        :meth:`disable_central_links` named after the daemon's
        ``central.remove_links`` command. Idempotent.
        """
        await self.disable_central_links(address=address)
