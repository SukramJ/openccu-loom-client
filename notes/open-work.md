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

- **Adopt the onboarding release state (daemon 0.66.1 / api 7.19.0).** The
  daemon closed its half; this client has not consumed it yet. Blocked only on
  an `openccu-loom-types` release for 7.19.0 — `DeviceReleasedPayload` is its
  own schema, so the event cannot be bound against 0.5.8.

  The drift guard already says so: with the daemon checked out beside this
  repo, `tests/unit/test_events_dispatcher.py::TestRegistryCoverage::test_every_daemon_broadcast_has_a_python_binding`
  fails with "missing bindings for: ['device.released']". It is skipped where
  the daemon repo is absent, so CI stays green — the reminder is deliberate and
  should not be silenced; binding the event is the fix. Its payload
  (`central`, `interface_id`, `device_address`) is identical in shape to
  `DeviceRemovedPayload`, which makes borrowing that model tempting: don't. Wire
  types come from `openccu-loom-types` (see `CLAUDE.md`), and a binding whose
  type name names the wrong event is worse than a guard that stays red until
  the real one exists.

  Background: 0.66.0 made pairing a wizard so a device is named and placed
  before any ecosystem sees it, but enforced the hold on MQTT, Matter and the
  webhook only — REST/WS was left showing unreleased devices because the Config
  UI has to see them. That conflated transport with role, and this backend is
  the case it misses: an ecosystem reached over the configuration channel. A
  device therefore arrived in Home Assistant un-named, the outcome the release
  step exists to prevent. 0.66.1 fixes it by putting the state on the device
  rather than inferring it from the channel.

  What to consume, all of it additive — `released` is `true` for every device
  that never entered the wizard, so an installation not using it behaves
  exactly as before:

  1. **`DeviceSummary.released`** on `GET /devices`, `GET /devices/{addr}` and
     `GET /snapshot` — skip unreleased devices in `bootstrap()` so they never
     enter the store.
  2. **`released` on the `device.created` payload** — ignore the frame when it
     is false: no store stub, no reconcile. The daemon put the flag on the
     frame deliberately, because looking it up separately is a race the
     consumer cannot win: the push can arrive before a snapshot read completes.
  3. **The new `device.released` broadcast**, on the same
     `device.{address}.lifecycle` topic — bind it, then load and announce the
     device through the path `_reconcile_new_device` already provides. This is
     the piece that cannot be compensated for locally: the consumer that needs
     it is exactly the one that was connected and filtered the device out.
  4. **`datapoint.value_changed` is deliberately NOT filtered** by release
     state — the Config UI needs those values to verify a device before
     releasing it — so an unreleased device streams values at us. No work
     needed: with the device kept out of the store the frames find no
     data-point and `apply_value_changed` drops them at DEBUG
     (`store.py:1047`). Worth a test pinning that, since the contract states
     the requirement explicitly and the current behaviour satisfies it by
     accident rather than by design.

### Reconnect / recovery — closed (2026-08-27)

All nine gaps (G1–G9) are fixed, and the two cross-repo asks shipped in daemon
0.65.2/0.65.3. The scenario matrix, the reasoning per gap and the table of what
shipped where are in [`reconnect-recovery.md`](./reconnect-recovery.md); it stays
as the record of why each change exists.

Nothing here is open. Two design choices in it look like omissions and are not,
so they are recorded rather than re-litigated: `available` stays True while the
central is `Degraded` (a WS drop makes the store stale, not wrong, and flipping
every entity unavailable on a five-second reconnect is worse than the staleness),
and `wait_until_ready()` giving up is not an error (the bootstrap runs anyway and
the daemon's resync re-bootstraps when the CCU arrives).

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
