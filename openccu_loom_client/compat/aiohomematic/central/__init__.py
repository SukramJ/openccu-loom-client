# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""
``aiohomematic.central``-compatible surface backed by :class:`LoomClient`.

Three symbols are exported to satisfy the ``homematicip_local`` imports:

- :class:`CentralUnit` — alias for :class:`LoomCentralAdapter`, which
  presents the ``aiohomematic`` central + coordinator surface on top of
  a :class:`LoomClient`.
- :class:`CentralConfig` — accepts the ``aiohomematic``-style keyword
  arguments the component already passes (host, name, credentials, …),
  ignores the ones the daemon makes obsolete (callback host/port,
  per-interface ports, storage dir), and ``create_central()`` returns a
  ready-but-unstarted :class:`LoomCentralAdapter`.
- :func:`check_config` — async pre-flight that the HA config-flow runs
  to validate the user's input before committing the entry.

The ``events`` sub-module ships its own aliases — see
``openccu_loom_client.compat.aiohomematic.central.events``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from openccu_loom_client.auth import BasicAuth, BearerAuth
from openccu_loom_client.client import LoomClient
from openccu_loom_client.compat.aiohomematic.central.adapter import LoomCentralAdapter
from openccu_loom_client.config import LoomConfig
from openccu_loom_client.transport import HttpTransport

if TYPE_CHECKING:
    from openccu_loom_client.auth import AuthMethod

# ``CentralUnit`` is the adapter — the component holds it and reaches
# into its coordinator surface (device_coordinator, hub_coordinator, …).
CentralUnit = LoomCentralAdapter


class CentralConfig:
    """
    ``aiohomematic.CentralConfig``-shaped factory for the loom backend.

    The component constructs this with the full aiohomematic keyword
    set; only the daemon-relevant subset is honoured. Authentication
    resolves in priority order: an explicit ``auth`` method, then a
    bearer ``token``, then ``username``/``password`` (HTTP Basic against
    the daemon's user store). Everything CCU-/callback-specific
    (``callback_host``, ``callback_port_xml_rpc``, ``interface_configs``,
    ``storage_directory``, …) is accepted and ignored — the daemon owns it.
    """

    def __init__(
        self,
        *,
        name: str | None = None,
        host: str,
        auth: AuthMethod | None = None,
        token: str | None = None,
        username: str | None = None,
        password: str | None = None,
        port: int | None = None,
        tls: bool = True,
        verify_tls: bool = True,
        serial: str | None = None,
        client_session: Any | None = None,
        locale: str = "en",
        **_ignored: Any,
    ) -> None:
        """Capture the daemon-relevant config and resolve the auth method."""
        self._name = name or host
        self._host = host
        self._port = port
        self._tls = tls
        self._verify_tls = verify_tls
        # HA's UI language — drives the locale-aware schedule names the
        # HA entities read off ``device.config_provider.config.locale``.
        self._locale = locale
        # Optional CCU serial injected by the integration (HA's
        # ``entry.unique_id``). It fills the central-id slot of canonical
        # HA routing keys; when given it wins over the serial the daemon
        # reports on ``/system/ccu``, guaranteeing the live keys match the
        # one-time HA registry migration. Falls back to the daemon's
        # serial when omitted.
        self._serial = serial
        self._client_session = client_session
        # Sysvar/program visibility (marker filter + enabled-by-default) is
        # resolved daemon-side (api ≥ 1.9.0); any markers the integration
        # still passes are absorbed by **_ignored and intentionally unused.
        self._auth = self._resolve_auth(
            auth=auth, token=token, username=username, password=password
        )

    @staticmethod
    def _resolve_auth(
        *,
        auth: AuthMethod | None,
        token: str | None,
        username: str | None,
        password: str | None,
    ) -> AuthMethod:
        if auth is not None:
            return auth
        if token:
            return BearerAuth(token=token)
        if username and password is not None:
            return BasicAuth(username=username, password=password)
        msg = "CentralConfig needs an auth method, a token, or username+password"
        raise ValueError(msg)

    async def create_central(self) -> LoomCentralAdapter:
        """Build the unstarted adapter. Call ``adapter.start()`` to connect."""
        config = LoomConfig(
            host=self._host,
            port=self._port,
            tls=self._tls,
            verify_tls=self._verify_tls,
            auth=self._auth,
        )
        transport = HttpTransport(config, session=self._client_session)
        client = LoomClient(config, http_transport=transport)
        return LoomCentralAdapter(
            client=client,
            name=self._name,
            serial=self._serial,
            locale=self._locale,
        )


async def check_config(
    *,
    central_name: str,
    host: str,
    username: str | None = None,
    password: str | None = None,
    callback_host: str | None = None,
    callback_port_xml_rpc: int | None = None,
    json_port: int | None = None,
    storage_directory: str | None = None,
    **_kwargs: object,
) -> list[str]:
    """
    HA config-flow pre-flight — returns the list of failure messages.

    Aiohomematic's original implementation tried CCU connectivity here
    and returned every problem it could detect. In the daemon-mediated
    setup the connectivity check moves to :meth:`LoomClient.connect`,
    which raises ``LoomAuthError`` / ``LoomTransportError`` on the
    real failures. This shim does cheap static validation only — the
    callback-host / XML-RPC port / JSON-RPC port arguments are no
    longer meaningful but are accepted for signature parity.
    """
    failures: list[str] = []
    if not central_name:
        failures.append("central_name is required")
    if not host:
        failures.append("host is required")
    if username is not None and not username:
        failures.append("username, if given, must not be empty")
    return failures


async def list_ccus(
    *,
    host: str,
    token: str | None = None,
    port: int | None = None,
    tls: bool = False,
    verify_tls: bool = True,
    base_path: str | None = None,
    client_session: Any = None,
) -> list[dict[str, Any]]:
    """
    Return the daemon's CCUs for the HA config flow's CCU-selection step.

    Connects to the daemon (raising ``LoomAuthError`` / ``LoomTransportError``
    on bad token / unreachable host), reads ``GET /system/ccu`` and returns a
    plain-dict projection (``name``, ``serial``, ``host``, ``model``,
    ``available``) so the caller stays decoupled from the wire types.
    """
    config_kwargs: dict[str, Any] = {
        "host": host,
        "port": port,
        "tls": tls,
        "verify_tls": verify_tls,
        # A blank token yields an empty bearer; the daemon then rejects with
        # LoomAuthError, which the config flow maps to invalid_auth.
        "auth": BearerAuth(token=token or ""),
    }
    if base_path is not None:
        config_kwargs["base_path"] = base_path
    config = LoomConfig(**config_kwargs)
    transport = HttpTransport(config, session=client_session)
    client = LoomClient(config, http_transport=transport)
    try:
        await client.connect()
        ccus = await client.system.list_system_ccus()
    finally:
        await client.close()
    return [
        {
            "name": ccu.name,
            "serial": ccu.serial,
            "host": ccu.host,
            "model": ccu.model,
            "available": ccu.available,
        }
        for ccu in ccus
    ]


__all__: Final = [
    "CentralConfig",
    "CentralUnit",
    "LoomCentralAdapter",
    "check_config",
    "list_ccus",
]
