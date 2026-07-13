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

import logging
from typing import TYPE_CHECKING, Any, Final

from openccu_loom_client.exceptions import LoomUnsupportedOperationError
from openccu_loom_client.operations.datapoints import DataPointsOperations
from openccu_loom_client.operations.links import LinksOperations

if TYPE_CHECKING:
    from openccu_loom_client.transport.http import HttpTransport

_LOGGER: Final = logging.getLogger(__name__)

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
        value = result.get((address, channel, parameter))
        # batch_read carries a per-item failure as ``{"error": ...}``. Don't
        # hand that back as if it were the value — log and report unavailable.
        if isinstance(value, dict) and "error" in value:
            _LOGGER.warning("get_value(%s:%s/%s) failed: %s", address, channel, parameter, value["error"])
            return None
        return value

    async def get_paramset(self, *, channel_address: str, paramset_key: Any, **_kwargs: Any) -> dict[str, Any]:
        """Read a whole paramset; a peer-address ``paramset_key`` reads the link paramset."""
        token = _paramset_token(paramset_key=paramset_key)
        if token in _PARAMSET_KEYS:
            return await self._datapoints.get_paramset(address=channel_address, paramset_key=token)
        return await self._links.get_link_paramset(address=channel_address, peer=token)

    async def put_paramset(
        self,
        *,
        channel_address: str,
        values: dict[str, Any],
        paramset_key: Any = None,
        paramset_key_or_link_address: Any = None,
        **_kwargs: Any,
    ) -> None:
        """
        Write a whole paramset; a peer-address key writes the link paramset.

        ``homematicip_local`` spells the selector ``paramset_key_or_link_address``
        on the *write* path (``ws_put_link_paramset``) and ``paramset_key`` on the
        read path — mirroring aiohomematic. Both are accepted; a plain
        ``MASTER``/``VALUES`` token goes to the data-point paramset, anything else
        is a peer channel address and routes to the LINK paramset.
        """
        selector = paramset_key_or_link_address if paramset_key_or_link_address is not None else paramset_key
        if selector is None:
            raise ValueError("put_paramset requires paramset_key (or paramset_key_or_link_address)")
        token = _paramset_token(paramset_key=selector)
        if token in _PARAMSET_KEYS:
            await self._datapoints.put_paramset(address=channel_address, paramset_key=token, values=values)
            return
        await self._links.put_link_paramset(address=channel_address, peer=token, values=values)

    async def determine_parameter(self, *, channel_address: str, parameter: str, **_kwargs: Any) -> Any:
        """
        Auto-detect a parameter value from the device.

        Not supported on the loom backend: the daemon exposes no
        determine-parameter endpoint (aiohomematic drives it over raw XML-RPC).
        Raised as a loom exception rather than an ``AttributeError`` so
        ``ws_determine_parameter``'s ``except BaseHomematicException`` catches it
        and reports ``determine_failed`` with this message, instead of leaking a
        bare ``unknown_error`` to the config panel.
        """
        raise LoomUnsupportedOperationError(
            f"determine_parameter({channel_address}/{parameter}) is not supported by the openccu-loom daemon"
        )

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
