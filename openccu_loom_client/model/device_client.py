# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
aiohomematic-compatible ``Device.client`` shim.

The HA integration's service handlers reach the raw interface client via
``hm_device.client.<method>`` (``set_value``, ``get_paramset``,
``put_paramset``, link operations). aiohomematic exposes those on the
XML-RPC client; here we present the same call surface on top of the
daemon's REST operations so the existing handlers dispatch unchanged on
the loom backend.

aiohomematic-only knobs (``wait_for_callback``, ``rx_mode``,
``check_against_pd``, ``retry``, ``convert_from_pd``) are accepted and
ignored — the daemon owns write serialization and value typing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from openccu_loom_client.operations.datapoints import DataPointsOperations
from openccu_loom_client.operations.links import LinksOperations

if TYPE_CHECKING:
    from openccu_loom_client.transport.http import HttpTransport

# aiohomematic ParamsetKey members; anything else passed as ``paramset_key``
# is a peer channel address and routes to the link-paramset surface.
_PARAMSET_KEYS = frozenset({"MASTER", "VALUES", "LINK", "SERVICE"})


def _split_channel_address(*, channel_address: str) -> tuple[str, int]:
    """Split ``ABC1234567:3`` into ``("ABC1234567", 3)`` (channel 0 if absent)."""
    device, _, channel = channel_address.partition(":")
    return device, int(channel) if channel else 0


def _paramset_token(*, paramset_key: Any) -> str:
    """Normalise a ParamsetKey enum / string to the daemon's wire token."""
    return str(getattr(paramset_key, "value", paramset_key))


class DeviceClient:
    """
    Per-device façade mirroring aiohomematic's ``Device.client`` surface.

    Bound to one device address; routes value/paramset reads and writes
    to the daemon's data-point REST surface and link operations to the
    link surface.
    """

    __slots__ = ("_datapoints", "_device_address", "_links")

    def __init__(self, *, transport: HttpTransport, device_address: str) -> None:
        """Build the shim against the store's transport for one device."""
        self._device_address = device_address
        self._datapoints = DataPointsOperations(transport=transport)
        self._links = LinksOperations(transport=transport)

    async def set_value(self, *, channel_address: str, parameter: str, value: Any, **_kwargs: Any) -> None:
        """Write a single data-point value (``ParamsetKey.VALUES``)."""
        address, channel = _split_channel_address(channel_address=channel_address)
        await self._datapoints.set_value(address=address, channel=channel, parameter=parameter, value=value)

    async def get_value(self, *, channel_address: str, parameter: str, **_kwargs: Any) -> Any:
        """Read a single data-point value (``ParamsetKey.VALUES``)."""
        address, channel = _split_channel_address(channel_address=channel_address)
        result = await self._datapoints.batch_read(queries=[(address, channel, parameter)])
        return result.get((address, channel, parameter))

    async def get_paramset(self, *, channel_address: str, paramset_key: Any, **_kwargs: Any) -> dict[str, Any]:
        """Read a whole paramset; a peer-address ``paramset_key`` reads the link paramset."""
        token = _paramset_token(paramset_key=paramset_key)
        if token in _PARAMSET_KEYS:
            return await self._datapoints.get_paramset(address=channel_address, paramset_key=token)
        return await self._links.get_link_paramset(address=channel_address, peer=token)

    async def put_paramset(
        self, *, channel_address: str, paramset_key: Any, values: dict[str, Any], **_kwargs: Any
    ) -> None:
        """Write a whole paramset; a peer-address ``paramset_key`` writes the link paramset."""
        token = _paramset_token(paramset_key=paramset_key)
        if token in _PARAMSET_KEYS:
            await self._datapoints.put_paramset(address=channel_address, paramset_key=token, values=values)
            return
        await self._links.put_link_paramset(address=channel_address, peer=token, values=values)

    async def get_link_peers(self, *, channel_address: str, **_kwargs: Any) -> tuple[str, ...]:
        """Return the channel addresses directly linked to ``channel_address``."""
        links = await self._links.list_links(address=self._device_address)
        peers: list[str] = []
        for link in links:
            if link.sender_address == channel_address:
                peers.append(link.receiver_address)
            elif link.receiver_address == channel_address:
                peers.append(link.sender_address)
        return tuple(peers)

    async def add_link(
        self,
        *,
        sender_address: str,
        receiver_address: str,
        name: str | None = None,
        description: str | None = None,
        **_kwargs: Any,
    ) -> None:
        """Create a direct sender → receiver link on this device."""
        await self._links.add_link(
            address=self._device_address,
            sender_address=sender_address,
            receiver_address=receiver_address,
            name=name,
            description=description,
        )

    async def remove_link(self, *, sender_address: str, receiver_address: str, **_kwargs: Any) -> None:
        """Remove the direct sender → receiver link on this device."""
        await self._links.remove_link(address=self._device_address, sender=sender_address, receiver=receiver_address)
