# SPDX-License-Identifier: MIT
# Copyright (C) 2026 SukramJ.

"""
The device editor relay: manifest, route rule, status-preserving relay, module, central.

``route_allowed`` restates openccu-loom's rule (``pkg/hmapi/editor_component.go``
``EditorRoute``, SPA ``assets/ui/src/component/routes.ts`` ``routeAllowed``);
the relay cases run against the in-process mock daemon, so what they assert is
what reached the server.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from openccu_loom_client import (
    ON_BEHALF_OF_HEADER,
    BoxIngressConfig,
    LoomClient,
    LoomConfig,
    LoomEditorCentralUnresolvedError,
    LoomEditorModuleUrlError,
    LoomEditorRouteRefusedError,
    LoomForbiddenError,
    LoomNotFoundError,
    LoomTransportError,
    NoAuth,
)
from openccu_loom_client.operations import EditorOperations, route_allowed
from openccu_loom_client.transport import HttpTransport
from openccu_loom_client.wire import DAEMON_API_VERSION
from openccu_loom_client.wire.rest import EditorRoute
from tests.helpers import MockDaemon

_INFO = {
    "version": "1.2.3",
    "api_version": DAEMON_API_VERSION,
    "commit": "deadbeef",
    "build_date": "2026-05-24T10:00:00Z",
    "addon_build": False,
    "started_at": "2026-05-24T10:01:00Z",
    "uptime": "PT60S",
    "capabilities": ["rest.v1"],
    "schema_digest": "sha256:test",
    "config_ui_url": "",
}

_MANIFEST = {
    "module_url": "/app/component/editor-3f9a.js",
    "hash": "3f9a",
    "routes": [
        {"method": "GET", "prefix": "/devices/*"},
        {"method": "PUT", "prefix": "/devices/*/paramsets"},
        {"method": "POST", "prefix": "/devices/*/paramsets"},
        {"method": "DELETE", "prefix": "/devices/*/paramsets"},
    ],
}


def _routes(*pairs: tuple[str, str]) -> list[EditorRoute]:
    return [EditorRoute.model_validate({"method": m, "prefix": p}) for m, p in pairs]


_ROUTES = _routes(*((r["method"], r["prefix"]) for r in _MANIFEST["routes"]))  # type: ignore[index]


# ---- route_allowed ----


@pytest.mark.parametrize(
    ("routes", "method", "path", "expected"),
    [
        # the prefix covers itself and every path below it
        (_routes(("GET", "/devices")), "GET", "/devices", True),
        (_routes(("GET", "/devices")), "GET", "/devices/A:1", True),
        # segment by segment, not by string prefix
        (_routes(("GET", "/devices")), "GET", "/devicesx/1", False),
        # method compared upper-case, and it must match
        (_routes(("GET", "/devices")), "get", "/devices/A:1", True),
        (_routes(("GET", "/devices")), "PUT", "/devices/A:1", False),
        (_routes(("PUT", "/devices/*/paramsets")), "POST", "/devices/A:1/paramsets/MASTER", False),
        # `*` matches exactly one non-empty segment
        (_routes(("GET", "/devices/*")), "GET", "/devices/A%3A1/channels/1/flags", True),
        (_routes(("GET", "/devices/*")), "GET", "/devices", False),
        (_routes(("GET", "/devices/*")), "GET", "/devices/", False),
        (_routes(("GET", "/devices/*/paramsets")), "GET", "/devices/paramsets", False),
        (_routes(("GET", "/devices/*/paramsets")), "GET", "/devices/A/B/paramsets", False),
        (_routes(("PUT", "/devices/*/paramsets")), "PUT", "/devices/A:1/paramsets/MASTER", True),
        (_routes(("PUT", "/devices/*/paramsets")), "PUT", "/devices/A:1/paramsets", True),
        (_routes(("POST", "/devices/*/paramsets")), "POST", "/devices/A:1/release", False),
        # several routes: any one covering is enough; none covers nothing
        (_routes(("GET", "/system"), ("POST", "/devices/*/paramsets")), "POST", "/devices/A/paramsets/X", True),
        ([], "GET", "/devices", False),
    ],
)
def test_route_allowed(routes: list[EditorRoute], method: str, path: str, expected: bool) -> None:
    assert route_allowed(routes=routes, method=method, path=path) is expected


# ---- fixtures ----


@pytest.fixture
async def editor(mock_daemon: MockDaemon) -> AsyncIterator[tuple[EditorOperations, HttpTransport, MockDaemon]]:
    t = HttpTransport(config=mock_daemon.config, backoff_sequence=(0.0,))
    mock_daemon.get("/api/v1/info", payload=_INFO)
    await t.connect()
    yield EditorOperations(transport=t), t, mock_daemon
    await t.close()


def _relay_kwargs(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "routes": _ROUTES,
        "method": "PUT",
        "path": "/devices/A%3A1/paramsets/MASTER",
        "query": None,
        "headers": None,
        "body": {"LEVEL": 1},
        "central": "ccu-main",
        "on_behalf_of": None,
    }
    kwargs.update(overrides)
    return kwargs


# ---- manifest ----


async def test_manifest_parses(editor) -> None:
    ops, _, daemon = editor
    daemon.get("/api/v1/ui/editor-component", payload=_MANIFEST)
    manifest = await ops.manifest()
    assert manifest is not None
    assert manifest.module_url == "/app/component/editor-3f9a.js"
    assert [(str(r.method), r.prefix) for r in manifest.routes][1] == ("PUT", "/devices/*/paramsets")


async def test_manifest_is_none_on_404(editor) -> None:
    ops, _, _ = editor
    # No stub: the mock daemon answers 404 problem+json.
    assert await ops.manifest() is None


async def test_manifest_other_errors_raise(editor) -> None:
    ops, _, daemon = editor
    daemon.get(
        "/api/v1/ui/editor-component",
        status=403,
        payload={"type": "https://openccu-loom.dev/errors/forbidden", "title": "no", "status": 403},
    )
    with pytest.raises(LoomForbiddenError):
        await ops.manifest()


# ---- relay ----


@pytest.mark.parametrize(
    ("status", "payload"),
    [
        (200, {"ok": True}),
        (201, {"id": "x"}),
        (204, None),
        (409, {"type": "https://openccu-loom.dev/errors/conflict", "title": "edited meanwhile", "status": 409}),
        (422, {"type": "https://openccu-loom.dev/errors/validation", "title": "bad value", "status": 422}),
        (423, {"type": "about:blank", "title": "locked", "status": 423}),
        (500, {"type": "https://openccu-loom.dev/errors/internal", "title": "boom", "status": 500}),
    ],
)
async def test_relay_returns_every_status_with_its_body(editor, status: int, payload: object) -> None:
    ops, _, daemon = editor
    daemon.put("/api/v1/devices/A:1/paramsets/MASTER", status=status, payload=payload)
    answer = await ops.relay(**_relay_kwargs())
    assert (answer.status, answer.body) == (status, payload)


async def test_relay_returns_text_of_a_non_json_error(editor) -> None:
    ops, _, daemon = editor
    daemon.put("/api/v1/devices/A:1/paramsets/MASTER", status=502, body=b"bad gateway", content_type="text/plain")
    answer = await ops.relay(**_relay_kwargs())
    assert (answer.status, answer.body) == (502, "bad gateway")


async def test_relay_does_not_retry_a_post(editor) -> None:
    ops, _, daemon = editor
    daemon.post("/api/v1/devices/A:1/paramsets/MASTER", status=503, payload={"status": 503})
    answer = await ops.relay(**_relay_kwargs(method="POST"))
    assert answer.status == 503
    assert len([r for r in daemon.requests if r.method == "POST"]) == 1


async def test_relay_retries_an_idempotent_503(editor) -> None:
    ops, _, daemon = editor
    daemon.put("/api/v1/devices/A:1/paramsets/MASTER", status=503, payload={"status": 503})
    daemon.put("/api/v1/devices/A:1/paramsets/MASTER", status=204)
    answer = await ops.relay(**_relay_kwargs())
    assert answer.status == 204
    assert len([r for r in daemon.requests if r.method == "PUT"]) == 2


async def test_refused_route_sends_nothing(editor) -> None:
    ops, _, daemon = editor
    before = len(daemon.requests)
    with pytest.raises(LoomEditorRouteRefusedError):
        await ops.relay(**_relay_kwargs(method="POST", path="/devices/A%3A1/release"))
    with pytest.raises(LoomEditorRouteRefusedError):
        await ops.relay(**_relay_kwargs(path="/devices/A/paramsets/../../../auth/tokens"))
    with pytest.raises(LoomEditorRouteRefusedError):
        await ops.relay(**_relay_kwargs(path="/devices/A/paramsets/%2E%2E/x"))
    assert len(daemon.requests) == before


async def test_relay_without_central_sends_nothing(editor) -> None:
    ops, _, daemon = editor
    before = len(daemon.requests)
    with pytest.raises(LoomEditorCentralUnresolvedError):
        await ops.relay(**_relay_kwargs(central=""))
    assert len(daemon.requests) == before


async def test_relay_keeps_the_percent_encoded_path(editor) -> None:
    ops, _, daemon = editor
    daemon.put("/api/v1/devices/A:1/paramsets/MASTER", status=204)
    await ops.relay(**_relay_kwargs())
    assert daemon.requests[-1].raw_path == "/api/v1/devices/A%3A1/paramsets/MASTER"


async def test_relay_sets_central_over_the_callers(editor) -> None:
    ops, t, daemon = editor
    # The bound central of the transport must not win over the relay's either.
    t.bind_device_central(source=lambda: "bound-central")
    daemon.get("/api/v1/devices/A:1", payload={})
    await ops.relay(
        **_relay_kwargs(method="GET", path="/devices/A%3A1", body=None, query={"central": "evil", "lang": "de"})
    )
    assert daemon.requests[-1].query == {"central": "ccu-main", "lang": "de"}


async def test_relay_headers(editor) -> None:
    ops, _, daemon = editor
    daemon.put("/api/v1/devices/A:1/paramsets/MASTER", status=204)
    await ops.relay(
        **_relay_kwargs(
            headers={
                "authorization": "Bearer stolen",
                "COOKIE": "s=1",
                "Host": "elsewhere",
                "x-on-behalf-of": "Mallory",
                "X-Edit-Token": "etag-1",
                "Accept-Language": "de",
            },
            on_behalf_of="Alex (ha:1f2e)",
        )
    )
    seen = {k.lower(): v for k, v in daemon.requests[-1].headers.items()}
    assert seen["authorization"] == "Bearer testtoken1234"
    assert "cookie" not in seen
    assert seen["host"] == f"{daemon.host}:{daemon.port}"
    assert seen[ON_BEHALF_OF_HEADER.lower()] == "Alex (ha:1f2e)"
    assert seen["x-edit-token"] == "etag-1"
    assert seen["accept-language"] == "de"
    assert daemon.requests[-1].json() == {"LEVEL": 1}


async def test_relay_drops_the_components_on_behalf_of_without_a_label(editor) -> None:
    ops, _, daemon = editor
    daemon.put("/api/v1/devices/A:1/paramsets/MASTER", status=204)
    await ops.relay(**_relay_kwargs(headers={"X-On-Behalf-Of": "Mallory"}))
    assert ON_BEHALF_OF_HEADER.lower() not in {k.lower() for k in daemon.requests[-1].headers}


async def test_relay_raises_when_the_daemon_is_not_reached(editor) -> None:
    ops, _, daemon = editor
    # Slower than the 1 s request budget: the answer never arrives.
    daemon.post("/api/v1/devices/A:1/paramsets/MASTER", status=204, delay=1.5)
    with pytest.raises(LoomTransportError):
        await ops.relay(**_relay_kwargs(method="POST"))


# ---- fetch_module ----


async def test_fetch_module_reads_from_the_origin(editor) -> None:
    ops, _, daemon = editor
    daemon.get("/app/component/editor-3f9a.js", body=b"export {};", content_type="text/javascript")
    assert await ops.fetch_module(module_url="/app/component/editor-3f9a.js") == b"export {};"
    assert daemon.requests[-1].path == "/app/component/editor-3f9a.js"


@pytest.mark.parametrize(
    "module_url",
    [
        "/api/v1/info",
        "/app/component/editor.css",
        "/app/component/../../etc/passwd.js",
        "/app/component/%2e%2e/x.js",
        "https://evil.example/app/component/x.js",
        "/app/component/x.js?y=.js",
        "//evil.example/app/component/x.js",
    ],
)
async def test_fetch_module_refuses_other_urls(editor, module_url: str) -> None:
    ops, _, daemon = editor
    before = len(daemon.requests)
    with pytest.raises(LoomEditorModuleUrlError):
        await ops.fetch_module(module_url=module_url)
    assert len(daemon.requests) == before


async def test_fetch_module_error_raises(editor) -> None:
    ops, _, _ = editor
    with pytest.raises(LoomNotFoundError):
        await ops.fetch_module(module_url="/app/component/missing.js")


async def test_fetch_module_through_box_ingress(mock_daemon: MockDaemon) -> None:
    cfg = LoomConfig(
        host=mock_daemon.host,
        tls=False,
        auth=NoAuth(),
        box_ingress=BoxIngressConfig(token="box-token-1", port=mock_daemon.port),
        request_timeout_seconds=1.0,
    )
    t = HttpTransport(config=cfg, backoff_sequence=(0.0,))
    mock_daemon.get("/addons/loom/api/v1/info", payload=_INFO)
    mock_daemon.get("/addons/loom/app/component/editor-3f9a.js", body=b"m", content_type="text/javascript")
    await t.connect()
    try:
        body = await EditorOperations(transport=t).fetch_module(module_url="/app/component/editor-3f9a.js")
    finally:
        await t.close()
    assert body == b"m"
    assert mock_daemon.requests[-1].path == "/addons/loom/app/component/editor-3f9a.js"
    assert mock_daemon.requests[-1].headers["Authorization"] == "Bearer box-token-1"


async def test_fetch_module_direct_sends_no_daemon_credential(editor) -> None:
    ops, _, daemon = editor
    daemon.get("/app/component/editor-3f9a.js", body=b"m", content_type="text/javascript")
    await ops.fetch_module(module_url="/app/component/editor-3f9a.js")
    assert "authorization" not in {k.lower() for k in daemon.requests[-1].headers}


# ---- resolve_central ----


def _ccu(name: str, serial: str | None) -> dict[str, object]:
    entry: dict[str, object] = {
        "name": name,
        "host": "10.0.0.1",
        "available": True,
        "is_ha_app": False,
        "configured_interfaces": [],
        "readiness": {"phase": "ready", "ready": True, "interfaces_loaded": 1, "interfaces_total": 1},
    }
    if serial is not None:
        entry["serial"] = serial
    return entry


async def test_resolve_central_by_serial(editor) -> None:
    ops, _, daemon = editor
    daemon.get(
        "/api/v1/system/ccu",
        payload={"entries": [_ccu("other", "SER0"), _ccu("ccu-main", "SER1"), _ccu("manual", None)]},
    )
    assert await ops.resolve_central(serial="SER1") == "ccu-main"


@pytest.mark.parametrize(
    ("serial", "entries"),
    [
        ("", [_ccu("ccu-main", "SER1")]),
        ("SER9", [_ccu("ccu-main", "SER1")]),
        ("SER9", []),
    ],
)
async def test_resolve_central_unresolved(editor, serial: str, entries: list[dict[str, object]]) -> None:
    ops, _, daemon = editor
    daemon.get("/api/v1/system/ccu", payload={"entries": entries})
    with pytest.raises(LoomEditorCentralUnresolvedError):
        await ops.resolve_central(serial=serial)


async def test_resolve_central_empty_serial_entry_never_matches(editor) -> None:
    ops, _, daemon = editor
    daemon.get("/api/v1/system/ccu", payload={"entries": [_ccu("manual", "")]})
    with pytest.raises(LoomEditorCentralUnresolvedError):
        await ops.resolve_central(serial="SER1")


# ---- wiring ----


def test_client_exposes_the_editor(config: LoomConfig) -> None:
    assert isinstance(LoomClient(config=config).editor, EditorOperations)


async def test_adapter_exposes_the_clients_editor(config: LoomConfig) -> None:
    from openccu_loom_client.compat.aiohomematic.central.adapter import LoomCentralAdapter

    client = LoomClient(config=config)
    assert LoomCentralAdapter(client=client, name="home").editor is client.editor
