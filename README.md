# openccu-loom-client

**Status:** WIP / Alpha — transport, event bus, domain store, the full
daemon REST surface (HA-relevant + admin/ops), and the `aiohomematic`
compat namespace are in place (see "Status of the wire contract"
below).

Async Python REST + WebSocket client for the
[openccu-loom](https://github.com/SukramJ/openccu-loom) daemon.

An **alternative backend** for the `homematicip_local` Home-Assistant
custom component — coexisting with `aiohomematic` rather than replacing
it. Instead of direct XML-RPC/JSON-RPC, it mediates CCU contact through
the openccu-loom daemon. Reusing `aiohomematic` at runtime (routing-key
algorithm, protocols, selected model code) is a deliberate part of this
strategy: it shares one contract between two backends and avoids silent
drift. The `compat/aiohomematic/` namespace shim is how the backend is
plugged in today (see [`docs/architecture-review.md`](./docs/architecture-review.md) §2.1).

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
in the daemon repo. As of `openccu-loom-types==0.1.24`, all push-event
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
  audit (`client.diagnostics`), backups incl. importing an externally
  produced `.sbk` (`client.backup`), CCU maintenance — reboot / power
  off / safe mode / recovery mode / astro position (`client.system`),
  edit-lock sessions (`client.sessions`), the Matter bridge
  (`client.matter`), and parameter visibility (`client.visibility`).

The schedule, link and calculated-data-point schemas live in the
daemon's `openapi.yaml` (`components.schemas`) and are regenerated into
`openccu-loom-types` (currently `0.1.24`), so they are typed rather than
free-form dicts.

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
