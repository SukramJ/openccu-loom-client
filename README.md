# openccu-loom-client

**Status:** Beta — transport, event bus, domain store, the full daemon
REST surface (HA-relevant + admin/ops), and the `aiohomematic` compat
namespace are in place (see "Status of the wire contract" below).

Async Python REST + WebSocket client for the
[openccu-loom](https://github.com/SukramJ/openccu-loom) daemon.

An **alternative backend** for the `homematicip_local` Home-Assistant
custom component — coexisting with `aiohomematic` rather than replacing
it. Instead of direct XML-RPC/JSON-RPC, it mediates CCU contact through
the openccu-loom daemon. Reusing `aiohomematic` at runtime (routing-key
algorithm, protocols, selected model code) is a deliberate part of this
strategy: it shares one contract between two backends and avoids silent
drift. The `compat/aiohomematic/` namespace shim is how the backend is
plugged in today; `CLAUDE.md` carries the reasoning behind that form.

## Architecture

Wire types are generated into `openccu_loom_client/wire/` (Pydantic
models + enum catalogue, from the daemon's `assets/openapi.yaml`,
`assets/wsapi.json` and `assets/schemas/enums.json`). They shipped as the
separate `openccu-loom-types` distribution until 2026.9; that package is
now a thin alias that requires this one, and nothing needs to depend on it.
Everything under `wire/` is machine output; everything else here is
hand-written:

- `transport/http.py` — async REST client (aiohttp), RFC 9457
  `problem+json` parsing, retry/backoff.
- `transport/ws.py` — WebSocket loop with subscribe/unsubscribe,
  heartbeat, resume-after-reconnect via `seq`/`since` cursor per
  [ADR-0022](https://github.com/SukramJ/openccu-loom/blob/main/docs/adr/0022-ws-resume-and-kind.md).
- `client.py` — `LoomClient` facade: snapshot bootstrap, event bus,
  in-memory store, and the operation modules (`devices`, `datapoints`,
  `custom_data_points`, `hub`, `system`, `schedules`, `links`).
- `compat/aiohomematic/` — namespace shim so existing
  `homematicip_local` imports keep working during the cutover. This
  includes a `LoomCentralAdapter` that presents aiohomematic's
  `CentralUnit` + coordinator surface, and a categorised data-point
  model (generic `Dp*`, hub `SysvarDp*`/`ProgramDp*`, custom
  `CustomDp*` for light/cover/climate/lock/siren/valve/switch) with
  `unique_id`/`category`/`registered` bookkeeping. A refresh bridge
  fans the daemon's value/sysvar/custom events into the single
  `DataPointStateChangedEvent` (keyed by `unique_id`) HA entities
  subscribe to.

## Connecting through an openccu-lite box

An openccu-lite box can serve the daemon through its own web server at
`https://<box>/addons/loom/`, behind a gate that lets nothing through without
a credential the box accepts. Since openccu-lite 1.0.0-dev.36 that can be a
**box API token** holding the add-on's gate scope `addon:openccu-loom` — no
box password needs to be stored. Get one by pairing with the box; the box's
administrator compares a six-digit code on the box's status page and
approves:

```python
from openccu_loom_client import BoxIngressConfig, LoomConfig, NoAuth, start_box_pairing

session = await start_box_pairing(host="openccu-lite.local", app="my-app", instance="nas")
show_to_user(session.code)          # the six digits
result = await session.wait()       # approved / rejected / expired
if result.state == "approved":
    config = LoomConfig(
        host="openccu-lite.local",  # the box
        auth=NoAuth(),              # the box token is the only credential
        box_ingress=BoxIngressConfig(token=result.token),
    )
```

The token rides every REST request and WebSocket (re)connect as
`Authorization: Bearer`; the gate hands it on, and the daemon signs the
request in with it after asking the box (daemon ADR 0080): the add-on scope as
operator, a Full-access token as admin. A daemon credential cannot travel
beside it, so `LoomConfig.auth` must be `NoAuth()`. When the gate refuses the
token the client raises at once, without retrying: `LoomBoxTokenError` for an
unknown, expired or revoked token (401 — pair again), `LoomBoxGateError` for
a token without the add-on's scope (403) or a box that redirects. Pairing
refusals raise `LoomBoxPairingError` with the box's `code` (`pairing-off`,
`not-local`, `limit`, `invalid`). In this mode `tls` describes the box's
listener, `BoxIngressConfig.port` defaults to 443 (80 without TLS),
`path_prefix` to `/addons/loom`, and `LoomConfig.port` is unused.

Home Assistant reaches this through the aiohomematic compat layer:
`CentralConfig` and `list_ccus` take `box_token`, `box_port` and
`box_path_prefix` (`check_config` validates them) and build the
`BoxIngressConfig` from them; a daemon credential passed beside `box_token`
raises `ValueError`. An unknown `box_*` keyword — `box_username` and
`box_password` included — raises `TypeError` instead of being ignored.

## Status of the wire contract

The daemon's external-client contract is tracked in
[`notes/reference/external-client-asks.md`](https://github.com/SukramJ/openccu-loom/blob/main/notes/reference/external-client-asks.md)
in the daemon repo. All push-event
payloads needed by Home Assistant (`DataPointValueChanged`,
`CustomDataPointStateChanged`, `CentralStateChanged`,
`SystemStatusChanged`, `SysvarChanged`, `ProgramExecuted`,
`InstallModeChanged`, `DeviceCreated`, `DeviceRemoved`) ship typed and
are bound in the event registry.

The full daemon REST surface is wrapped — typed end-to-end against the
generated wire models:

- **HA-relevant:** devices/channels/data-points, paramsets, batch
  reads, custom data points, programs and sysvars (incl. create /
  metadata-patch / lifecycle), alarm/service messages (incl. ack),
  install-mode, interfaces, rooms/functions, firmware updates,
  calculated data points, climate **schedules** / week-profiles, and
  direct/central **links**.
- **Admin / ops:** auth + API-token provisioning (`client.auth`),
  users (`client.users`), centrals (`client.centrals`), config
  management (`client.config_admin`), diagnostics / log-levels /
  capture / RPC-recording / metrics / values-cache / MQTT-reload /
  audit (`client.diagnostics`), backups incl. importing an externally
  produced `.sbk` (`client.backup`), CCU maintenance — reboot / power
  off / safe mode / recovery mode / astro position (`client.system`),
  edit-lock sessions (`client.sessions`), the Matter bridge
  (`client.matter`), and parameter visibility (`client.visibility`).

The schedule, link and calculated-data-point schemas live in the
daemon's `openapi.yaml` (`components.schemas`) and are regenerated into
`openccu_loom_client/wire/`, so they are typed rather than free-form
dicts.

Two broadcasts that were once daemon-side gaps are **now live and
bound**:

- `datapoint.optimistic_rolled_back` — broadcast by the daemon, consumed
  as `DataPointOptimisticRolledBackEvent` and bridged to the HA-facing
  `OptimisticRollbackEvent`. Local synthesis from REST `set_value`
  failures remains available as a fallback.
- Device **trigger / keypress** events — emitted on the
  `device.{address}.channels.{channel}.trigger` topic and bound to
  `DeviceTriggerEvent`; the HA event-group surface is served by
  `query_facade.get_event_groups`.

## Development

```sh
python3.14 -m venv venv
source venv/bin/activate
pip install -e '.[dev]'
pytest
```

Parts of openccu-loom-client are developed with agentic AI assistance,
primarily [Claude Code](https://www.anthropic.com/claude-code). Submitted
issues are also triaged and analysed with agentic help. Every change is
still reviewed by a human maintainer and has to pass the project's tests
before it lands — the AI accelerates the work, it does not replace the
review gate.

## Contributing

AI-assisted contributions are welcome, but you must review, understand
and stand behind everything you submit — see
[`AI_POLICY.md`](./AI_POLICY.md) for the rules.

## License

MIT. See [LICENSE](./LICENSE).
