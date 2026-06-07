# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Async Python REST + WebSocket client for the [openccu-loom](https://github.com/SukramJ/openccu-loom) daemon. It is meant to become the drop-in replacement for `aiohomematic` inside the `homematicip_local` Home Assistant custom component, once the daemon fully mediates CCU contact. Status: WIP / Alpha.

Wire types are **not** defined here — they come from the sister package `openccu-loom-types` (Pydantic models + enum catalogue, generated from the daemon's `assets/openapi.yaml`). Import wire schemas from `openccu_loom_types.rest`, `openccu_loom_types.ws`, and `openccu_loom_types.enums`. This package adds the transport, store, event-bus, domain-wrapper, and aiohomematic-compat layers on top.

## Commands

```sh
python3.14 -m venv venv
source venv/bin/activate
pip install -e '.[dev]'        # runtime + dev extras from pyproject
script/setup                  # or: bootstrap dev deps (requirements_test.txt) + install pre-commit hooks

pytest                                   # full suite (asyncio_mode=auto)
pytest tests/unit/test_store.py          # one file
pytest tests/unit/test_store.py::test_x  # one test
pytest -k "value_changed"                # by name pattern

ruff check .                             # lint (line-length 100, py311 target)
ruff format .                            # format
mypy openccu_loom_client                 # strict mode is on

prek install                             # one-time: enable pre-commit hooks
prek run --all-files                     # run all hooks manually
```

Requires Python ≥ 3.14. Tests use `pytest-asyncio` (auto mode — no `@pytest.mark.asyncio` needed) and `aioresponses` to mock the daemon's HTTP surface.

## Architecture

Data flows in one direction at runtime: **daemon → transport → typed event → store mutation → domain wrapper → (compat) HA event**. Write-back goes the reverse way through the store's `set_*` methods straight to the transport. The layers are deliberately decoupled so each is unit-testable in isolation.

### Layers (bottom to top)

- **`config.py`** — `LoomConfig` dataclass: host/port/tls/auth/timeout. The single place transport knobs live. Defaults: HTTP 8080 / HTTPS 8443, base path `/api/v1`.
- **`auth.py`** — `BasicAuth` / `BearerAuth` / `SessionAuth` auth methods.
- **`transport/http.py`** — `HttpTransport` wrapping `aiohttp.ClientSession`. Owns RFC 9457 `problem+json` → typed-exception parsing, retry/backoff (only on idempotent verbs `GET/HEAD/PUT/DELETE` unless `allow_retry=True` is passed), and a one-shot capability handshake against `GET /info` at `connect()`.
- **`transport/ws.py`** — `WsTransport`: WebSocket loop with subscribe/unsubscribe, heartbeat, and resume-after-reconnect via a `seq`/`since` cursor. When the daemon's replay buffer has aged out, it calls back `on_replay_lost`, which triggers a fresh bootstrap.
- **`store.py`** — `LoomStore`: in-memory mirror of one daemon's CCU model (devices → channels → data-points, plus device-scoped custom data points, programs, sysvars). Populated by `load_snapshot` + `attach_*` during bootstrap and mutated by `apply_*` from live WS events. Write-back via `set_value` / `set_sysvar` / `execute_program` / `invoke_custom_data_point`. **The store does not depend on the event bus** — the `LoomClient` wires them together.
- **`events/`** — `bus.py` (`EventBus` + `SubscriptionGroup`), `types.py` (one `LoomEvent` subclass per wire `type` discriminator), `synthetic.py`. `EventBus.publish` fans out sequentially in registration order; one handler raising is logged but never aborts the fan-out. Subscriptions optionally filter by `event_key` (typically a per-data-point routing key — see `data_point_event_key`).
- **`bridge.py`** — `bind_ws_events_to_store`: the glue subscribing the six wire→store handlers (`apply_value_changed`, `apply_device_created`, etc.) on one `SubscriptionGroup`. Kept separate from both store and bus so each tests alone.
- **`model/`** — store-aware domain wrappers (`Device`, `Channel`, `DataPoint`, `CustomDataPoint`, `Program`, `Sysvar`). Each holds a mutable wire summary that the store model-copies on update; consumers read `.value` and never touch the summary. `DataPoint.send_value` routes back through `store.set_value`.
- **`operations/`** — stateless REST façades, one module per surface area, all extending `_OperationsBase` (just a transport handle). Exposed as attributes on `LoomClient`: HA-relevant (`devices`, `datapoints`, `custom_data_points`, `hub`, `system`, `schedules`, `links`) and admin/ops (`auth`, `users`, `centrals`, `config_admin`, `diagnostics`, `backup`, `sessions`, `matter`, `visibility`).
- **`client.py`** — `LoomClient`, the single facade consumers use. Composes everything above. Lifecycle: `connect()` (HTTP + handshake only) → `bootstrap()` (populate store from `/snapshot`, then per-device detail + per-channel data-points) → `start_events()` (open WS, wire bridge, run the dispatch loop). `connect`/`start_events` are split so callers can choose snapshot-only (REST polling) vs. full push mode.

### The bootstrap cost

`bootstrap()` is N×M REST calls (one detail call per device, one data-point call per channel) — the dominant cost on large CCUs. This matches the current unstreamed daemon contract; a streamed-snapshot endpoint is a deferred daemon-side ask.

### The compat shim — `compat/aiohomematic/`

A namespace shim that presents `aiohomematic`'s public surface so existing `homematicip_local` imports keep working during the cutover. Two key pieces:

- **`central/adapter.py`** — `LoomCentralAdapter` presents aiohomematic's `CentralUnit` + coordinator surface (`device_coordinator`, `hub_coordinator`, `query_facade`, …) on top of a `LoomClient`. **Important:** the entity-spawn surface that depends on aiohomematic's _categorized data-point model_ (`unique_id`/`category`/`data_point_type`/`registered` bookkeeping) is **stubbed and raises `NotImplementedError`** with a greppable `_MODEL_PORT_TODO` marker. Porting that model onto `LoomStore` is the open, larger workstream. The store exposes `set_data_point_factory` / `set_custom_data_point_factory` hooks the compat layer uses to make the store hold categorized `Dp*` / `CustomDp*` subclasses, so HA-side `isinstance` dispatch works on the live store objects.
- **`central/refresh.py`** — `install_refresh_bridge`: fans the daemon's three distinct value events (`DataPointValueChangedEvent`, `CustomDataPointStateChangedEvent`, `SysvarChangedEvent`) into the single `DataPointStateChangedEvent` (keyed by `unique_id`) that HA entities subscribe to. The unique-id format must stay in lock-step between this bridge and the compat data-point layer — both derive it from `data_point_event_key` / `*_unique_id`.

## Conventions

- All public functions use keyword-only arguments (`*,`). Follow this when adding to the surface.
- Domain wrappers expose private `_replace_summary` / `_replace_state` / `_update_summary` methods the store calls to mutate live objects in place — never rebuild a wrapper on update.
- Retries: only mark a call `allow_retry=True` when it is genuinely idempotent. `set_value`/`set_sysvar` (PUT, daemon-serialized) are retried; `execute_program` and `invoke_custom_data_point` (POST, side effects like cover-open) are not.
- Every source file carries the SPDX MIT header. `mypy --strict` and the full ruff ruleset (incl. `PL`, `B`, `SIM`, `ASYNC`, `UP`) must pass.

## Daemon broadcasts now live (formerly deferred)

These two broadcasts were once daemon-side gaps; the daemon now emits them and the client binds them:

- `datapoint.optimistic_rolled_back` is broadcast by the daemon and consumed as `DataPointOptimisticRolledBackEvent`. The compat refresh bridge (`compat/aiohomematic/central/refresh.py`) translates it into the HA-facing `OptimisticRollbackEvent`. The `events/synthetic.py` factory `new_optimistic_rollback_event` is still available to synthesize the same event locally from a `set_value` failure when no broadcast is in flight.
- Device trigger/keypress events are emitted on the `device.{address}.channels.{channel}.trigger` topic and bound to `DeviceTriggerEvent`. The remaining client-side work is the HA _event-group_ surface on top of them (`query_facade.get_event_groups` still raises `NotImplementedError` — see the compat shim's `_MODEL_PORT_TODO`).
