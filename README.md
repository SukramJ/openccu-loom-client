# openccu-loom-client

**Status:** WIP / Alpha — transport, event bus, domain store, the full
daemon REST surface (HA-relevant + admin/ops), and the `aiohomematic`
compat namespace are in place (see "Status of the wire contract"
below).

Async Python REST + WebSocket client for the
[openccu-loom](https://github.com/SukramJ/openccu-loom) daemon.

Designed as the drop-in replacement for `aiohomematic` in the
`homematicip_local` Home-Assistant custom component, once the daemon
has fully replaced direct XML-RPC/JSON-RPC contact with the CCU.

## Architecture

Wire types come from the sister package
[`openccu-loom-types`](https://pypi.org/project/openccu-loom-types/)
(Pydantic models + enum catalogue, generated from the daemon's
`assets/openapi.yaml` and `assets/schemas/enums.json`). This package
adds:

- `transport/http.py` — async REST client (aiohttp), RFC 9457
  `problem+json` parsing, retry/backoff.
- `transport/ws.py` — WebSocket loop with subscribe/unsubscribe,
  heartbeat, resume-after-reconnect via `seq`/`since` cursor per
  [ADR-0022](https://github.com/SukramJ/openccu-loom/blob/main/docs/adr/0022-ws-resume-cursor.md).
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

## Status of the wire contract

The daemon's external-client contract is tracked in
[`docs/external-clients/asks.md`](https://github.com/SukramJ/openccu-loom/blob/main/docs/external-clients/asks.md)
in the daemon repo. As of `openccu-loom-types==0.1.2`, all push-event
payloads needed by Home Assistant (`DataPointValueChanged`,
`CustomDataPointStateChanged`, `CentralStateChanged`,
`SystemStatusChanged`, `SysvarChanged`, `ProgramExecuted`,
`InstallModeChanged`, `DeviceCreated`, `DeviceRemoved`) ship typed and
are bound in the event registry.

The full daemon REST surface is wrapped — typed end-to-end against
`openccu-loom-types`:

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
  audit (`client.diagnostics`), backups (`client.backup`), edit-lock
  sessions (`client.sessions`), the Matter bridge (`client.matter`),
  and parameter visibility (`client.visibility`).

The schedule, link and calculated-data-point schemas were added to the
daemon's `openapi.yaml` (`components.schemas`) and regenerated into
`openccu-loom-types` 0.1.3, so they are typed rather than free-form
dicts.

Two gaps remain and are **daemon-side**, not closeable from the client
alone:

- `OptimisticRollback` — not yet broadcast by the daemon; synthesized
  client-side from REST `set_value` failures in the meantime.
- Device **trigger / keypress** events — `central-links` forwarding can
  be enabled via REST, but the daemon does not yet emit the
  corresponding click broadcast (reserved topic namespace). The
  `DeviceTriggerEvent` compat class is ready to bind once it ships.

## Development

```sh
python3.11 -m venv venv
source venv/bin/activate
pip install -e '.[dev]'
pytest
```

## License

MIT. See [LICENSE](./LICENSE).
