# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Async Python REST + WebSocket client for the [openccu-loom](https://github.com/SukramJ/openccu-loom) daemon. Status: Beta.

**Strategy — alternative backend, not a replacement.** For the foreseeable future `openccu-loom*` is _not_ meant to replace `aiohomematic`; it is an **alternative backend** for `homematicip_local` that mediates CCU contact through the daemon instead of direct XML-RPC/JSON-RPC. Both backends coexist and HA can choose between them. Because of this, **depending on `aiohomematic` at runtime is deliberate and architecturally sound**: the routing-key algorithm, the `@runtime_checkable` protocols and (selectively) model code are _reused_ from `aiohomematic` rather than re-implemented, which avoids silent drift between two implementations of the same contract. `aiohomematic` and `openccu-loom*` share one maintainer, so the coupling is coordinated, not external.

**Integration form (decided) — the compat shim.** HA dispatches on type identity: concrete `Dp*`/`CustomDp*` classes (via isinstance _tuples_ `(AioClass, LoomTwin)` in `homematicip_local`'s `backend_types.py`) plus `@runtime_checkable` protocols. That isinstance coupling makes the namespace shim under `compat/aiohomematic/` the pragmatic integration form — the twins satisfy aiohomematic's surface _structurally_ (not by subclassing, since aiohomematic's model classes are bound to a live `CentralUnit`). A clean backend abstraction inside `homematicip_local` is the deferred alternative: it would mean rebuilding the production aiohomematic integration for unclear gain, so it is only pursued if `homematicip_local` grows such a layer for its own reasons. The three follow-ups that keep the shim form robust have all shipped: a drift-guard test against the real aiohomematic protocols (`tests/compat/test_aiohomematic_protocol_parity.py`), a single import seam onto the reused internals (`compat/aiohomematic/_upstream.py`), and a version floor in `pyproject.toml` with the exact CI pin in `requirements.txt` (a library must not pin a dependency its own consumer pins too — `homematicip_local` depends on `aiohomematic` directly). Bump the pin and the drift-guard snapshot together when adopting a new aiohomematic series. Anything still open is in `notes/open-work.md`.

**Where documents live.** This repository publishes no documentation site: `README.md` is the public face, and every contributor-facing working document is in `notes/` — a single backlog, `notes/open-work.md`, plus [`notes/README.md`](./notes/README.md) explaining what belongs there. The name mirrors the daemon repo, where `notes/` is the working set and `docs/` the published MkDocs site. When citing a daemon document, use its GitHub URL rather than a repo-relative path: paths only read correctly from inside that repository and break silently when it reorganises.

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

ruff check .                             # lint (line-length 120, py314 target)
ruff format .                            # format
mypy openccu_loom_client                 # strict mode is on
pylint openccu_loom_client               # extra checks beyond ruff

python script/lint_kwonly.py openccu_loom_client  # enforce keyword-only params
python script/lint_all_exports.py                 # validate grouped/sorted __all__

prek install                             # one-time: enable pre-commit hooks
prek run --all-files                     # run all hooks manually
prek run --hook-stage manual --all-files # manual-stage hooks (python-typing-update, …)
```

Requires Python ≥ 3.14. Tests use `pytest-asyncio` (auto mode — no `@pytest.mark.asyncio` needed) and an in-process aiohttp mock daemon (`tests/helpers/mock_daemon.py` `MockDaemon`) for the daemon's HTTP surface; the WS transport tests run a real `aiohttp` test server.

## Architecture

Data flows in one direction at runtime: **daemon → transport → typed event → store mutation → domain wrapper → (compat) HA event**. Write-back goes the reverse way through the store's `set_*` methods straight to the transport. The layers are deliberately decoupled so each is unit-testable in isolation.

### Layers (bottom to top)

- **`config.py`** — `LoomConfig` dataclass: host/port/tls/auth/timeout. The single place transport knobs live. Defaults: HTTP 8080 / HTTPS 8443, base path `/api/v1`.
- **`auth.py`** — `BasicAuth` / `BearerAuth` / `SessionAuth` auth methods.
- **`transport/http.py`** — `HttpTransport` wrapping `aiohttp.ClientSession`. Owns RFC 9457 `problem+json` → typed-exception parsing, retry/backoff (only on idempotent verbs `GET/HEAD/PUT/DELETE` unless `allow_retry=True` is passed), and a one-shot capability handshake against `GET /info` at `connect()`.
- **`transport/ws.py`** — `WsTransport`: WebSocket loop with subscribe/unsubscribe, heartbeat, and resume-after-reconnect via a `seq`/`since` cursor. When the daemon's replay buffer has aged out, it calls back `on_replay_lost`, which triggers a fresh bootstrap.
- **`store.py`** — `LoomStore`: in-memory mirror of one daemon's CCU model (devices → channels → data-points, plus device-scoped custom data points, programs, sysvars). Populated by `load_snapshot` + `attach_*` during bootstrap and mutated by `apply_*` from live WS events. Write-back via `set_value` / `set_sysvar` / `execute_program` / `invoke_custom_data_point`. **The store does not depend on the event bus** — the `LoomClient` wires them together. It also holds the two consumer-facing settings the model layer reads back — the HA locale (`set_locale`) and the daemon's entity-name catalogue (`set_entity_names`, `GET /i18n/entities`) — because the objects that read them are rebuilt by reconciles and pushes, and a value pushed onto an instance would have to be re-delivered on every such path.
- **`events/`** — `bus.py` (`EventBus` + `SubscriptionGroup`), `types.py` (one `LoomEvent` subclass per wire `type` discriminator), `synthetic.py`. `EventBus.publish` fans out sequentially in registration order; one handler raising is logged but never aborts the fan-out. Subscriptions optionally filter by `event_key` (typically a per-data-point routing key — see `data_point_event_key`).
- **`bridge.py`** — `bind_ws_events_to_store`: the glue subscribing the six wire→store handlers (`apply_value_changed`, `apply_device_created`, etc.) on one `SubscriptionGroup`. Kept separate from both store and bus so each tests alone.
- **`model/`** — store-aware domain wrappers (`Device`, `Channel`, `DataPoint`, `CustomDataPoint`, `Program`, `Sysvar`). Each holds a mutable wire summary that the store model-copies on update; consumers read `.value` and never touch the summary. `DataPoint.send_value` routes back through `store.set_value`.
- **`operations/`** — stateless REST façades, one module per surface area, all extending `_OperationsBase` (just a transport handle). Exposed as attributes on `LoomClient`: HA-relevant (`devices`, `datapoints`, `custom_data_points`, `hub`, `system`, `schedules`, `links`, `alarm`, `security`) and ops (`diagnostics`, `backup`, `visibility`). The six admin façades that no consumer reached — `auth`, `users`, `centrals`, `config_admin`, `groups`, `matter` — were removed from the wheel; the daemon still serves those endpoints, and `node-red-contrib-openccu-loom` speaks to them through its own client. `SessionsOperations` survives as a module because `operations/links.py` instantiates it, but it is no longer a client attribute.
- **`client.py`** — `LoomClient`, the single facade consumers use. Composes everything above. Lifecycle: `connect()` (HTTP + handshake only) → `bootstrap()` (populate store from `/snapshot`, then per-device detail + per-channel data-points) → `start_events()` (open WS, wire bridge, run the dispatch loop). `connect`/`start_events` are split so callers can choose snapshot-only (REST polling) vs. full push mode.

### The bootstrap cost

`bootstrap()` used to be N×M REST calls — one detail call per device, one data-point call per channel. The M is closed: the nested snapshot carries the channel data points (`include=data_points`, wired at `client.py:456-472`). The N is one `GET /devices/{address}` per device, still made at `client.py:665` — and no longer necessary, because daemon api 7.23.0 lifts `firmware` and `availability` onto `DeviceSummary`. Removing it is tracked as #127.

Two related capabilities exist and are deliberately unused. The daemon serves `/snapshot` as an NDJSON stream under `Accept: application/x-ndjson` (see the note in `operations/system.py`); this client consumes the nested JSON envelope instead. And `GET /devices` and `GET /snapshot` both accept `?central=`, which would filter server-side; the client fetches the whole fleet and filters in the compat layer. On a daemon fronting two CCUs that means each Home Assistant entry carries the other CCU's device tree. Neither is a daemon gap.

### The compat shim — `compat/aiohomematic/`

A namespace shim that presents `aiohomematic`'s public surface so existing `homematicip_local` imports keep working during the cutover. Two key pieces:

- **`central/adapter.py`** — `LoomCentralAdapter` presents aiohomematic's `CentralUnit` + coordinator surface (`device_coordinator`, `hub_coordinator`, `query_facade`, …) on top of a `LoomClient`. The entity-spawn surface runs on aiohomematic's _categorized data-point model_: the store exposes `set_data_point_factory` / `set_custom_data_point_factory` hooks the compat layer uses to make the store hold categorized `Dp*` / `CustomDp*` subclasses (so HA-side `isinstance` dispatch works on the live store objects), and the adapter additionally builds the hub singletons (`model/hub/singletons.py`: alarm/service messages, inbox, metrics, connectivity, system update, add-on self-update (capability-gated on `GET /system/addon-update` `supported`), install mode — polled every 30 s via `hub_coordinator.fetch_hub_singleton_data`), the schedule layer (`model/week_profile.py`: `WeekProfileDp` sensor + `ScheduleChannelSwitch` per channel key) and the combined duration number (`model/combined.py`: `DURATION_VALUE` + `DURATION_UNIT` → one seconds-typed number).
- **`central/refresh.py`** — `install_refresh_bridge`: fans the daemon's three distinct value events (`DataPointValueChangedEvent`, `CustomDataPointStateChangedEvent`, `SysvarChangedEvent`) into the single `DataPointStateChangedEvent` (keyed by `unique_id`) that HA entities subscribe to. The unique-id format must stay in lock-step between this bridge and the compat data-point layer — both derive it from `data_point_event_key` / `*_unique_id`.

## Conventions

- All public functions use keyword-only arguments (`*,`). Follow this when adding to the surface.
- Domain wrappers expose private `_replace_summary` / `_replace_state` / `_update_summary` methods the store calls to mutate live objects in place — never rebuild a wrapper on update.
- Retries: only mark a call `allow_retry=True` when it is genuinely idempotent. `set_value`/`set_sysvar` (PUT, daemon-serialized) are retried; `execute_program` and `invoke_custom_data_point` (POST, side effects like cover-open) are not.
- Every source file carries the SPDX MIT header. `mypy --strict` and the full ruff ruleset (incl. `PL`, `B`, `SIM`, `ASYNC`, `UP`) must pass.

## Daemon broadcasts now live (formerly deferred)

These two broadcasts were once daemon-side gaps; the daemon now emits them and the client binds them:

- `datapoint.optimistic_rolled_back` is broadcast by the daemon and consumed as `DataPointOptimisticRolledBackEvent`. The compat refresh bridge (`compat/aiohomematic/central/refresh.py`) translates it into the HA-facing `OptimisticRollbackEvent`. The `events/synthetic.py` factory `new_optimistic_rollback_event` is still available to synthesize the same event locally from a `set_value` failure when no broadcast is in flight.
- Device trigger/keypress events are emitted on the `device.{address}.channels.{channel}.trigger` topic and bound to `DeviceTriggerEvent`. The HA _event-group_ surface on top of them is served by `query_facade.get_event_groups` (built from the store's trigger DPs).
