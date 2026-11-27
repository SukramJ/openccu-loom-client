# SPDX-License-Identifier: MIT
# Copyright (C) 2026 SukramJ.

"""
Relay the daemon's device editor component (openccu-loom ADR 0089).

An embedding integration serves the editor's web component from its own
origin and forwards the component's REST calls to the daemon. Everything
HTTP-shaped about that relay lives here, so the integration only maps its
own concepts (who is asking, which CCU the device belongs to) onto it:

- :meth:`EditorOperations.manifest` reads where the module is and which
  calls it makes;
- :func:`route_allowed` is the daemon's rule for whether a call is one of
  them;
- :meth:`EditorOperations.relay` forwards a covered call and hands back the
  daemon's status and body, whatever the status;
- :meth:`EditorOperations.fetch_module` reads the module from the daemon's
  origin;
- :meth:`EditorOperations.resolve_central` names the daemon central of a CCU
  serial.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import unquote

from openccu_loom_client.exceptions import (
    LoomEditorCentralUnresolvedError,
    LoomEditorModuleUrlError,
    LoomEditorRouteRefusedError,
    LoomNotFoundError,
)
from openccu_loom_client.on_behalf_of import ON_BEHALF_OF_HEADER
from openccu_loom_client.operations._base import _OperationsBase
from openccu_loom_client.operations.system import SystemOperations
from openccu_loom_client.transport.http import DEVICE_CENTRAL_QUERY
from openccu_loom_client.wire.rest import EditorComponentManifest, EditorRoute

if TYPE_CHECKING:
    from openccu_loom_client.transport.http import HttpTransport

# Where the daemon serves the editor module (``EditorComponentManifest.module_url``).
_MODULE_PATH_PREFIX: Final = "/app/component/"
_MODULE_SUFFIX: Final = ".js"

# Ceiling on the module download. A built web component is a few hundred
# kilobytes; the cap only bounds what a misbehaving daemon can make the host
# buffer.
_MODULE_MAX_BYTES: Final = 16 * 1024 * 1024

# Headers a relayed call never carries from the component, compared
# case-insensitively:
# - the credential headers the transport sets itself — ``Authorization``
#   (the daemon credential, and behind an openccu-lite box the box token,
#   ``BoxIngressConfig.apply_to_headers``) and ``Cookie`` — so the component
#   can neither displace nor duplicate them;
# - ``Host`` and the framing headers, which describe the component's request
#   to the relay, not this one; aiohttp sets them for the request it sends;
# - ``X-On-Behalf-Of``, which names the person only the relay knows.
_STRIPPED_HEADERS: Final = frozenset(
    {
        "authorization",
        "cookie",
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        ON_BEHALF_OF_HEADER.lower(),
    }
)

# Path segments that would move a call out of the route that covered it.
_DOT_SEGMENTS: Final = frozenset({".", ".."})


@dataclass(frozen=True, slots=True, kw_only=True)
class EditorRelayResponse:
    """
    The daemon's answer to a relayed call, as it gave it.

    ``status`` is the HTTP status for every outcome, a 409 or a 423 as much as
    a 200. ``body`` is the decoded JSON body (a ``problem+json`` document on
    an error), the text of a body that is not JSON, or ``None`` when the body
    is empty.
    """

    status: int
    # Wire-decoded JSON of an arbitrary editor route, handed on undecided.
    body: Any | None


def route_allowed(*, routes: Iterable[EditorRoute], method: str, path: str) -> bool:
    """
    Report whether one of ``routes`` covers ``method path``.

    The daemon's rule, ported from openccu-loom ``pkg/hmapi/editor_component.go``
    (``EditorRoute``: "Prefix covers itself and every path below it, segment
    by segment; a ``*`` segment matches exactly one non-empty segment") and
    from the SPA's implementation of it, ``assets/ui/src/component/routes.ts``
    ``routeAllowed``: the method is compared upper-case; path and prefix are
    split on ``/``; the path has at least as many segments as the prefix; and
    each prefix segment equals the path segment, or is ``*`` and the path
    segment is non-empty.

    ``path`` is matched as given — percent-encoded segments stay encoded.
    """
    verb = method.upper()
    segments = path.split("/")
    for route in routes:
        if str(route.method) != verb:
            continue
        want = route.prefix.split("/")
        if len(segments) < len(want):
            continue
        if all(segments[i] != "" if seg == "*" else seg == segments[i] for i, seg in enumerate(want)):
            return True
    return False


class EditorOperations(_OperationsBase):
    """The device editor relay: manifest, route check, relay, module, central."""

    __slots__ = ("_system",)

    def __init__(self, *, transport: HttpTransport) -> None:
        """Hold the transport, and the system operations ``resolve_central`` reads."""
        super().__init__(transport=transport)
        self._system: Final = SystemOperations(transport=transport)

    async def manifest(self) -> EditorComponentManifest | None:
        """
        Read where the editor module is and which calls it makes.

        Wire: ``GET /ui/editor-component``. ``None`` when the daemon answers
        404 — a build without the component bundle, or a daemon older than
        the editor.
        """
        try:
            payload = await self._transport.request(method="GET", path="/ui/editor-component")
        except LoomNotFoundError:
            return None
        return EditorComponentManifest.model_validate(payload)

    async def relay(
        self,
        *,
        routes: Iterable[EditorRoute],
        method: str,
        path: str,
        query: Mapping[str, str] | None,
        headers: Mapping[str, str] | None,
        body: Any | None,
        central: str,
        on_behalf_of: str | None,
    ) -> EditorRelayResponse:
        """
        Forward one call of the editor component and return the daemon's answer.

        ``path`` is the call's path below ``/api/v1`` as the component sent it,
        percent-encoded segments included; it reaches the daemon unchanged.
        ``body`` is the call's JSON body, ``None`` for none.

        The call is refused with :class:`LoomEditorRouteRefusedError`, and
        nothing is sent, when no entry of ``routes`` covers it
        (:func:`route_allowed`) or the path holds a ``.`` / ``..`` segment.
        ``central`` names the daemon central the device belongs to
        (:meth:`resolve_central`) and is required: it replaces any
        ``central`` the component put in ``query``.

        The component's headers are forwarded except for the credential,
        host and framing headers the transport sets itself and any
        ``X-On-Behalf-Of``; ``on_behalf_of`` (a value from
        :func:`openccu_loom_client.on_behalf_of_label`) is sent in its place.

        Every HTTP status is returned as :class:`EditorRelayResponse`; only a
        call that did not reach the daemon raises (:class:`LoomTransportError`).
        """
        if not central:
            msg = "an editor call needs the daemon central of its device"
            raise LoomEditorCentralUnresolvedError(msg)
        if not route_allowed(routes=routes, method=method, path=path):
            msg = f"the editor manifest covers no route for {method.upper()} {path}"
            raise LoomEditorRouteRefusedError(msg)
        if any(unquote(segment) in _DOT_SEGMENTS for segment in path.split("/")):
            msg = f"the editor call {method.upper()} {path} holds a dot segment"
            raise LoomEditorRouteRefusedError(msg)

        params: dict[str, Any] = {**(query or {}), DEVICE_CENTRAL_QUERY: central}
        forwarded = {k: v for k, v in (headers or {}).items() if k.lower() not in _STRIPPED_HEADERS}
        if on_behalf_of:
            forwarded[ON_BEHALF_OF_HEADER] = on_behalf_of
        status, payload = await self._transport.request_with_status(
            method=method.upper(), path=path, params=params, json_body=body, headers=forwarded
        )
        return EditorRelayResponse(status=status, body=payload)

    async def fetch_module(self, *, module_url: str) -> bytes:
        """
        Read the editor module from the daemon's origin.

        ``module_url`` is :attr:`EditorComponentManifest.module_url`; it must be
        a ``.js`` file below ``/app/component/`` without a ``.`` / ``..``
        segment, else :class:`LoomEditorModuleUrlError` is raised before any
        request. The module is read from the daemon's origin, not below
        ``/api/v1``: behind an openccu-lite box through the ingress prefix and
        with the box token. A non-2xx answer raises like any other call.
        """
        if (
            not module_url.startswith(_MODULE_PATH_PREFIX)
            or not module_url.endswith(_MODULE_SUFFIX)
            or any(ch in module_url for ch in "?#\\")
            or any(unquote(segment) in _DOT_SEGMENTS for segment in module_url.split("/"))
        ):
            msg = f"refusing to fetch editor module {module_url!r}: not a .js file below {_MODULE_PATH_PREFIX}"
            raise LoomEditorModuleUrlError(msg)
        return await self._transport.request_origin_bytes(path=module_url, max_bytes=_MODULE_MAX_BYTES)

    async def resolve_central(self, *, serial: str) -> str:
        """
        Name the daemon central of the CCU with hardware serial ``serial``.

        Wire: ``GET /system/ccu``; the result is the ``name`` of the entry
        whose ``serial`` equals ``serial``. Raises
        :class:`LoomEditorCentralUnresolvedError` for an empty ``serial`` and
        when no entry reports it. Not cached: a caller that keeps the name
        resolves it again after a reconnect.
        """
        if not serial:
            msg = "cannot resolve a daemon central for an empty CCU serial"
            raise LoomEditorCentralUnresolvedError(msg)
        for entry in await self._system.list_system_ccus():
            if entry.serial and entry.serial == serial:
                return entry.name
        msg = f"no central of the daemon reports CCU serial {serial!r}"
        raise LoomEditorCentralUnresolvedError(msg)
