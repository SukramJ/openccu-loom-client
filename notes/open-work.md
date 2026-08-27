# Open work — openccu-loom-client

The single backlog for this repository. Items land here when they are open;
they leave when they ship (the changelog carries them from there). Decisions
_not_ to do something stay, so they are not re-proposed without new
information.

**Strategy (unchanged since 2026-06-21):** `openccu-loom*` is an _alternative
backend_ for `homematicip_local`, coexisting with `aiohomematic` — not a
replacement. Runtime reuse of `aiohomematic` is deliberate, and the
`compat/aiohomematic/` namespace shim is the chosen plug-in point. The
rationale lives in `CLAUDE.md` → "What this is".

Every claim below was re-verified against the tree on 2026-08-08 (daemon
0.55.0, API 5.8.0, `openccu-loom-types` 0.3.5).

---

## Verify against a real CCU

These are correct as far as the simulator can show; only real hardware can
close them. None is blocked on code.

- **`legacy_name` equivalence.** Hub keys use `hub_slug(name)`, so the
  daemon's sysvar/program `name` must equal aiohomematic's `legacy_name` (the
  ReGa name) _before_ slugify. Assumed, never confirmed on a live CCU.
- **Serial equivalence.** The client keys off the `/system/ccu` serial while
  the HA registry migration keys off `entry.unique_id`. Both should be the
  real CCU serial — confirm once end-to-end.
- **Sysvar `extended` marker.** Wired end-to-end
  (`compat/aiohomematic/model/hub/__init__.py`); confirm an extended variable
  surfaces as the writable flavour.
- **HS colour round-trip.** `CustomDpDimmer.hs_color` reads nested
  `color:{h,s}` and scales saturation `[0,1] → [0,100]` for HA, but the write
  path (`turn_on` → `set_color`) sends HA's `[0,100]` through unscaled.
  Confirm the daemon's `set_color` scale; a write-path fix may follow.
- **`rename_device(ise_id)`.** Implemented and unit-tested, but godevccu
  assigns no `ise_id`, so it cannot be exercised against the simulator.

## Verification gaps in the e2e suite

Currently `xfail`. Each needs harness work rather than production code.

- **`test_device_trigger_pushed_over_ws`** — needs the daemon `interface_id`
  for `FireEvent`; wire it from the snapshot.
- **`test_optimistic_rollback_pushed_over_ws`** — model a non-confirming data
  point in the godevccu harness so the rollback is deterministic.
- **Hub `sysvar_changed`** — not a daemon gap. The daemon _does_ broadcast on
  client-initiated writes (`PatchSysvar → UpdateSysvar → SysvarChangedEvent`),
  with same-value dedup. The `xfail` is a simulator artefact: godevccu never
  effects a real value change. Drive it with a genuine delta or a real CCU.
- **Hub `program_executed`** — CCU-originated by design; a client `execute`
  gets REST 202 with no push. Not self-initiated-testable; verify via a
  CCU-side program run.

## Open in this repository

### Reconnect / recovery (2026-08-27)

The full scenario matrix, with sources and the reasoning per failure mode, is
in [`reconnect-recovery.md`](./reconnect-recovery.md). The backoff, dedup and
cooldown machinery that keeps an unavailable daemon from being hammered is in
place and correct; the gaps are all in what happens _after_ a successful
reconnect. Ordered by severity:

- **G1 — the `since` cursor is not re-anchored on `replay_lost`.**
  `_last_seq` only ever grows (`transport/ws.py:461-468`) and the
  `replay_lost` branch merely logs the daemon's anchor
  (`transport/ws.py:512-521`), which the daemon documents as "the anchor to
  resume from" (`internal/north/rest/ws/hub.go:348`). After a daemon restart
  the cursor keeps the previous incarnation's high value, so _every_ later
  reconnect is answered `Lost` and walks the full snapshot again. Re-anchor in
  the `replay_lost` branch only — the overflow path passes a `-1` sentinel and
  must not touch the cursor.
- **G2 — unbounded `device.created` reconcile fan-out.** Downgraded: the burst
  is gone daemon-side as of 0.65.2 (the CCU's full re-announcement is no longer
  broadcast). `_on_device_created` still spawns one background task per event
  with no semaphore and no check against what the store already holds
  (`client.py:527-560`); keep the cheap guard. The earlier claim that
  `applyPull` drove the burst was wrong — `InitialPull` has no production
  caller; see `reconnect-recovery.md` §5 G2.
- **G3 — `on_auth_failed` is never wired, and the deadline is now readable.**
  `WsTransport` raises the callback correctly (`transport/ws.py:344-361`);
  `start_events()` passes only `on_replay_lost` (`client.py:412-418`). An
  expired or revoked token therefore ends the event stream silently — the daemon
  closes the socket on expiry (`internal/north/rest/ws/client.go:424`), the
  reconnect gets a 401, the loop stops, and no consumer is told. Since api
  7.15.0 the fix is no longer only "notice the 401": `Identity.expires_at` says
  when the credential dies, on `GET /auth/me`, on the login response and on the
  `{op:"reauth_ok"}` ack, so a client can refill in-band before it happens.
  `WsTransport.reauth` currently discards the ack body beyond ok/failed
  (`transport/ws.py:610`).
- **G4 — the compat layer does not re-bootstrap.** `_run_rebootstrap` calls
  `LoomClient.bootstrap()` only; the custom-data-point / schedule / combined /
  hub-catalogue passes and `_emit_data_points_created` live in
  `adapter.start()` alone (`adapter.py:1198-1205`, `:1243`), and nothing
  subscribes the client's `DataPointsCreatedEvent`. A client that bootstraps
  while the daemon is still in `waiting_for_ccu` gets a valid 200 with empty
  lists (`internal/north/rest/handlers/snapshot.go:102`), and when the CCU
  arrives the daemon's `SignalResync` refills the store while HA still sees
  nothing. Also subscribe `central.readiness_changed` — the event exists on
  both sides (`events/types.py:192`) and is currently consumed by nobody.
- **G9 — the client never asks whether the daemon is functional.** The root
  cause behind G4 and G5. `connect()` checks contract compatibility only
  (`api_version`, capabilities, `schema_digest`); the daemon's own contract says
  a capability token means "configured", not "working", and points at `/health`
  for liveness. `system.get_health()` exists (`operations/system.py:38`) and is
  called nowhere in production code. `GET /system/ccu` **is** called
  (`adapter.py:1678`) and everything but `serial` is discarded — including
  `available` and `readiness{phase, ready}`. `/interfaces` is read exactly once
  at `start()` (`adapter.py:1195`), so `central.health` renders the boot moment
  forever. Fix: read `/health` at the end of `connect()` and after a WS
  reconnect, and gate `bootstrap()` on `readiness.ready`.
- **G5 — no availability signal downwards.** `LoomCentralAdapter._state` is set
  only in `start()` / `stop()`, so `available` stays `True` through any outage
  (`adapter.py:1134`, `:1196`, `:1248`) and `CentralState.Degraded` is never
  used.
- **G6 — version incompatibility is neither typed nor re-checked.**
  `_check_api_version` raises a plain `LoomTransportError`
  (`transport/http.py:252`), indistinguishable from "host unreachable", and the
  handshake runs only in `connect()` — a daemon upgraded under a live
  connection is never noticed.
- **G7 — no jitter in the WS reconnect ladder** (`transport/ws.py:56`).
- **G8 — a second `start_events()` leaks the previous `SubscriptionGroup`**
  (`client.py:428`), double-applying every event to the store. Reachable
  exactly on the recovery path G3 leaves open.

- **Optimistic rollback in the store model.** The daemon's
  `datapoint.optimistic_rolled_back` broadcast is bridged to the public
  `OptimisticRollbackEvent` with `restored_value=present`
  (`compat/aiohomematic/central/refresh.py`). Whether the **store's** model is
  reverted on that path — rather than only when the next genuine daemon value
  arrives via the optimistic-drop in `store.py` — is unverified. A reader
  could otherwise still see the un-confirmed value in between. Establish which
  it is before deciding whether anything needs fixing.
- **mypy cannot resolve editable first-party deps.** Under `strict = true`,
  mypy reports "Cannot find implementation or library stub" for
  `openccu_loom_types.*` even though it ships `py.typed`, because editable
  installs are not followed; this cascades into spurious `no-any-return`
  errors. Logic is unaffected — the type gate is just noisy. Current
  workaround: install the package non-editable. Fix by setting `mypy_path` /
  `explicit_package_bases`, or by installing first-party deps non-editable in
  the type-check environment, so strict mode means something again.

## Adopting daemon 0.65.3 / api 7.15.0

Both cross-repo asks from the reconnect/recovery review shipped, and so did the
two follow-ups found while adopting them. Nothing is left on the daemon side.

`openccu-loom-types` 0.5.7 is pinned; its `SCHEMA_DIGEST`
(`sha256:97a44474…`) matches daemon 0.65.3 exactly, so the connect-time drift
warning is quiet. Suite, ruff and mypy green on it. See
[`reconnect-recovery.md`](./reconnect-recovery.md) §6 for what shipped where.

Two notes on the version arithmetic, because they read as inconsistent and are
not: the daemon's `api_version` went 7.14.0 → **7.15.0** for a
contract-text-only change, because ADR 0028 ties the version to the assets
rather than to their semantics — any diff under `assets/` carries at least a
minor bump, and the CI guard enforces it. The types package still went 0.5.6 →
**0.5.7**, a plain patch bump: its version tracks the regeneration, not the
daemon's `api_version`.

What the corrected schema now says, and what a client may rely on: the
`device.created` broadcast is a genuine arrival, not the CCU's fleet-wide
re-announcement, and `source` is one of `NEW` (pairing), `REFRESH`
(factory-reset re-pair), `MANUAL` (operator accept out of the deferred-creation
inbox) or `CACHE` (boot restore from the persisted description cache). `INIT` is
in the enum with no producer on this broadcast. The value `NEW_DEVICE` that the
old description named never existed.

## Open in `homematicip_local` (cross-repo)

- **Registry migration + serial wiring.** The one-time migration to the
  canonical `loom_`/serial scheme lives in `async_migrate_entries`; the old
  `entry_id[-10:]` `central_id` injection is obsolete.
- **Orphan-cleanup guard.** Remove the `BACKEND_LOOM` early-return in
  `control_unit.py` `_async_cleanup_orphaned_entity_registry_entries` now that
  the full singleton set is modelled — guard on singleton presence rather than
  skipping the whole backend.
- **`event` platform bootstrap.** Drop the `NotImplementedError` fallback in
  `event.py` so the platform gets its bootstrap entities.
- **Async paramset getters.** The config-UI paramset description getters are
  _async_ on the loom backend (the daemon serves them over REST) where
  aiohomematic's are sync and cached. `await` them on the loom path:
  `get_paramset_description`, `get_link_paramset_description`.
- **`code_format` prompt UX** for the alarm panel.
- **Entity description for the `daemon_connection` singleton** (daemon api
  7.6.0). The client builds and announces the binary sensor, but HA matches
  its entity descriptions against the stable English token
  (`var_name_contains`), so without a description entry the entity renders
  without icon, device class or category. Token: `daemon_connection`; the
  daemon's own name comes from the catalogue key `discovery.daemon_status`
  ("Daemon connection").
- **The device-icon URL now needs authentication** (daemon api 7.6.0). A
  handler that hands `create_central_url() + /api/v1/devices/{addr}/icon`
  to the browser only still works where the browser carries a same-origin
  session cookie; a bearer-only setup must fetch through
  `client.devices.get_device_icon(address=…)` and serve the bytes itself.
- **The config flow's token has to be an admin one.** `GET /system/ccu`
  narrows the CCU serial and host away for a viewer/operator token since
  api 7.6.0, and the serial becomes the entry's `unique_id` — an empty one
  breaks every hub / internal / virtual-remote routing key. Worth saying so
  in the flow rather than letting it present as "the daemon has not reached
  the CCU yet".

## Decided, and deliberately not done

- **Consume `GET`/`PUT /api/v1/ui/surfaces` (daemon 0.55.0, PR #509).** No.
  That endpoint pair configures the _daemon's own_ Config-UI navigation —
  which views, settings tabs and device tabs an operator wants visible. It is
  not device or hub state, so nothing in the HA integration path reads or
  writes it. `openccu-loom-types` 0.3.5 ships the models (`SurfaceInfo`,
  `SurfacesRequest`, `SurfacesResponse`); leaving them unused is correct, not
  an omission.
- **Handle the embedded-profile write refusal.** Not applicable, verified
  rather than assumed. `SurfaceWrites`
  (`internal/north/rest/middleware/surface_writes.go` in the daemon) gates
  exactly one identity — the Home Assistant Ingress passthrough
  (`auth.SchemeIngress`) — and only write methods; reads are never gated. This
  client authenticates with `BasicAuth`, `BearerAuth` or `SessionAuth`
  (`auth.py`) and never presents an Ingress assertion, so a hidden surface
  cannot refuse its writes. That separation is the daemon's stated intent: a
  navigation switch must not widen or narrow a real credential's rights.
- **Enrich `get_event_groups()` from REST (G5).** `get_event_groups` already
  builds groups locally, `last_triggered_event` is live (the refresh bridge
  calls `record_trigger` on every `device.trigger` push), and `available`
  tracks the device. Backing it with a per-trigger-channel
  `GET …/event-groups` fetch at bootstrap would reintroduce exactly the N×M
  cost the nested-snapshot work removed — for a snapshot no fresher than the
  live trigger feed. Revisit only if event groups ever join the nested
  snapshot.
- **Selective reuse of aiohomematic to shrink the compat stubs.** Assessed and
  rejected. The stubs are protocol-tail members (`config_payload`,
  `state_path`, `service_methods`, …) whose aiohomematic implementations need
  a live `CentralUnit` and paramset descriptors that a daemon-mediated client
  does not hold, so their neutral defaults are correct. The fix was making the
  imitation _typed and loud_, not removing it.
- **A clean backend abstraction inside `homematicip_local`.** The deferred
  alternative to the compat shim. It would mean rebuilding the production
  aiohomematic integration for unclear gain, so it is pursued only if
  `homematicip_local` grows such a layer for its own reasons.
