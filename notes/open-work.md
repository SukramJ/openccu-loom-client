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
0.55.0, API 5.8.0, `openccu-loom-types` 0.3.5). The daemon-facing claims were
re-checked against daemon 0.66.1 / API 7.21.0 / wsapi 1.7 on 2026-08-28; where
that changed an entry, it says so inline.

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

## Verification gaps in the e2e suite

`rename_device(ise_id)` is covered as of 2026-08-28 and no longer sits under
"only real hardware": the missing piece was `Realism{RegaIDs: true}` on the
`godevccu-e2e` helper (daemon PR #636), without which every `ise_id` is `None`.
The suite itself had stopped running altogether — see the same day's
`test(e2e): make the suite runnable again`.

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

### Architecture review, 2026-08-28 — sequenced work

A two-round review measured the six coupled repositories against the four
deployment goals and recorded its result as a decision document. The
**sequenced** work lives in issues, not here — a step's state is open / in
progress / done, and a Markdown list cannot carry that without going stale the
way this repository's three previous implementation plans did.

Immediate, no dependency: #114 (five disproved statements in this repo),
#115 (`CustomDpGarage` hierarchy), #116 (two light-capability aliases),
#117 (13 unreachable compat modules, −492 LOC), #118 (six caller-less admin
facades, −595 LOC), #119 (turn off the types auto-tag and count for four weeks).

Sequenced: #120 (store delegates to the facades) → #121 (daemon checkout in the
PR gate) → #122 (fold `openccu-loom-types` in as `wire/`) → #123 (generate
`events/types.py`) → #124 (contract test for `operations/`) → #125 (invert the
surface guard) → #126 (sysvar rekey plus the CUxD rule, one wave).

Cross-repo: SukramJ/openccu-loom#637, #638, #639 · SukramJ/homematicip_local#1264,
#1265, #1266 · SukramJ/aiohomematic#3367.

The measured baseline, so a later reader can tell whether it held: the package
is 22,663 LOC, `compat/` 10,439 of them (46.1 %), up from 34.5 % three months
earlier. After the deletions and the fold: 27,578 LOC in one wheel against
28,689 in two, and — the number that matters — **maintained, non-generated LOC
20,750 against 22,663, −8.4 %**.

- **`hs_color` double-scales saturation (read path, not write).** Decidable
  from the daemon's code, so it does not need hardware — and the direction is
  the opposite of what this entry assumed while it sat under "verify against a
  real CCU".

  The daemon's custom-data-point plane already reports HA's scale.
  `ColorLight.Color()` returns `s * 100` (`internal/model/custom/light/color.go:159`)
  and `set_color` divides by 100 again (`:212`) — the only two scaling sites in
  its light package, and they are inverse. `state.color.s` is therefore
  **0..100**, not the wire fraction. The 0..1 is real but lives on the other
  plane: the raw `SATURATION` data point, where the daemon reports whatever the
  CCU sent.

  So `hs_color`
  (`compat/aiohomematic/model/custom/__init__.py:347-360`) multiplies an
  already-scaled value: `sat * 100.0` on a value that arrives as 0..100. Half
  saturation becomes 5000, HA clamps to 100, and every colour renders fully
  saturated. The docstring names the cause — it assumes `color.s` carries the
  wire value.

  The write path needs no change: passing HA's `[0,100]` through unscaled is
  exactly what `set_color` expects.

  Fix: drop the `* 100.0` in the read path and correct the docstring. Worth a
  regression test at a middle saturation, where the bug is visible and a
  full-saturation fixture would hide it. Daemon 0.66.1 states both scales in
  `openapi.yaml` (`CustomDataPointStateChangedPayload.state`) and `wsapi.json`
  (`cdp.invoke`); they lived only in Go comments before, which is what made the
  assumption possible.

### Onboarding release state — closed (2026-08-28)

Consumed in full against daemon 0.66.1 / api 7.21.0 (`openccu-loom-types`
0.5.9). `LoomConfig.released_only` (default on) drives both planes from one
flag, because the daemon's contract asks for the REST query and the WS
subscribe option to be paired or "the two drift"; it rides every subscribe
frame, initial and runtime alike, since the daemon applies it per connection
and a reconnect that dropped it would silently resume delivering withheld
devices. `DeviceReleasedEvent` is bound and adopts the device through the same
reconcile a fresh pairing uses. A consumer that wants the Config UI's role sets
the flag False.

The daemon closed its own half across #632-#635, including the five payloads
its first filter missed; nothing is left on either side.

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

### From the reconnect/recovery and onboarding work (2026-08-28)

Three consequences of what this client now does. None is a defect over there —
they are things that changed underneath it. Verified against
`homematicip_local` at `47b181c`.

- **`CentralState.DEGRADED` is now reached on an ordinary WS drop**, where
  before it was effectively unreachable on this backend. `_on_central_running`
  (`control_unit.py:980`) fires the `homematicip_local.central_state_changed`
  bus event on every transition back to RUNNING, so an automation listening for
  it now runs after each reconnect — including a five-second one. Decide
  whether that event should be debounced, or whether the automations that
  consume it should be.

  Explicitly **not** a problem, having checked: the repair issues are raised
  from `_handle_degraded_state` (`:871`), which hangs off
  `SystemStatusChangedEvent`, and the loom adapter does not publish that event.
  `_on_central_state_changed` (`:992`) only re-signals availability and, on
  RUNNING, _deletes_ issues. So there is no repair-issue flapping — worth
  recording, because "degraded on every reconnect" reads as though there would
  be.

- **`LoomIncompatibleVersionError` deserves `ConfigEntryError`.** Setup catches
  only `AuthFailure` today (`__init__.py:221`), so a daemon whose API version
  this build cannot talk to retries forever with the same result. The client
  now raises a distinct type for exactly that, and it subclasses
  `LoomTransportError` — so it has to be caught _before_ any handler for the
  general transport error, or the specific case is swallowed by the broad one.

- **`start()` can now block for up to three minutes.** The adapter waits for
  the daemon's southbound bring-up before walking the snapshot, because
  bootstrapping earlier "succeeds" into an empty model and spawns no entities
  at all. Bounded and non-fatal — it proceeds on timeout and the daemon's
  resync re-bootstraps when the CCU arrives — but a config-entry setup that
  sits there for minutes is visible in HA's logs. If that is unwanted, the wait
  is a client-side knob rather than something to work around here.

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

### From the architecture review, 2026-08-28

Each of these was worked out as a full proposal and then failed a measurement.
The measurement is given so the entry can be reopened by a better one rather
than by a fresh opinion.

- **Move the compat shim (10,439 LOC) into `homematicip_local`.** No. It trades
  a narrow informal seam for a broad formal one. The shim→core import surface
  grew from 36 to 71 symbols across 69 tags — 13 transitions, and in every one
  `removed = 0`. That is a new symbol to publish every 6,8 days, each of which
  would need an SDK release before the HA-side commit could land. 28 to 34 of
  68 release diffs touch `compat/` and the imported core surface _together_;
  those stay two-repo changes and gain an ordering constraint they do not have
  today. Only the 19 to 22 compat-only diffs come free. Footprint effect: zero.
- **Make the daemon's north surface an HA entity projection.** No — recorded as
  `openccu-loom` ADR 0067 rather than only here, because the daemon is where the
  question kept being answered twice. The contract carries none of the seven
  descriptor fields (each `0×` in 14,384 lines of `assets/openapi.yaml`), the
  cited precedent (`AlarmPanelEntity`) has 10 fields and no descriptor among
  them, it would produce three projections rather than one, and the platform
  reassignments it implies (`WEEK_PROFILE` sensor→select, `TEXT_DISPLAY`
  notify→text) have no migration path in HA's registry — unlike a changed
  `unique_id`, a changed domain orphans the entity.
- **Generate `operations/` (3,539 LOC) from `assets/openapi.yaml`.** No; a
  contract test instead (#124). Only 82 of 224 public method names match
  `snake_case(operationId)` — the ids were minted for the TypeScript side. Code
  generation renames 142 public methods and breaks 125 in-package call sites, or
  the emitter carries a hand-kept 142-line name table. The precedent inside this
  ecosystem points the same way: the daemon's own SPA writes its facade by hand
  (2,660 LOC, 249 methods) and pins it with a 246-LOC contract test. The input
  everyone assumed was the blocker is not one: of 99 `allow_retry` call sites
  exactly 6 are real overrides, the rest repeat the verb default from
  `transport/http.py:60`.
- **Drop the `aiohomematic` runtime import to cut footprint or the release
  cascade.** Both reasons are disproved. In the HA process the saving is 0 bytes
  and 0 ms, because `homematicip_local` loads `aiohomematic` backend-independently
  at module level (`generic_entity.py:9`, `backend_types.py:23-36`); the only
  two-digit MiB lever is `openccu-data`, eagerly loaded by
  `aiohomematic/ccu_translations.py:171-174` (54.3 MiB RSS, 98 ms), and it falls
  only if this client _and_ `homematicip_local` both stop importing. Cascade:
  5 of 460 releases since June. A case about code surface and type identity
  remains available — but it is not a deployment case and must not be sold as
  one.
- **Port the routing-key algorithm out of `aiohomematic` into this client.**
  Doubly moot: no footprint gain (above) and no cascade gain (3 of 18
  `aiohomematic` pin bumps forced a release). The ecosystem coupling would
  survive regardless — `openccu-loom` pins `aiohomematic` in its own parity gate
  (`script/requirements/reference-stack.txt`, ADR 0038) and measures the Go
  model against it on every pull request.
- **Have the daemon serve the hub singletons as entities.** No. The wire DTOs
  carry no `unique_id` at all (`HubCountDataPoint` is
  `required [legacy_name, value]`), and for the same entities the daemon already
  stamps a _different_ key in the MQTT namespace — numeric `ise_id` where this
  client takes the slug, `openccu-loom_` where it takes `loom_`. Adopting it
  would put a third namespace beside the two that BD-Identity-RoutingKeyNamespaces
  deliberately keeps apart.
- **Set `?central=` server-side to drop the `_matches_central` post-filters.**
  Not performable. None of the six endpoints behind those five REST filter sites
  carries the parameter — `/sysvars`, `/programs`, `/interfaces`,
  `/hub/data-points`, `/service-messages`, `/system/update` were each checked
  against the spec with `$ref` resolution. Only `/devices` and `/snapshot` carry
  it, and those do not run through `_matches_central`; setting it there changes
  the _set_ of devices, and with it the set of HA entities. The store holds
  `central_name` (the HA instance name, which `store.py:128-132` says may differ
  from `central_id`), so a mismatch would answer with an empty list.
- **A shared contract package between `aiohomematic` and this client.** Built on
  2026-06-04, withdrawn on 2026-06-10. The introducing PR's own
  `docs/drop-in-optimizations.md` had already deferred it — "drift between the
  three parties is already guarded without a physical split" — and the costs are
  on record: a forced release ordering, an untidy transitive hull
  (`generate_unique_id` needs `ConfigProviderProtocol` out of the heavily coupled
  `interfaces/central.py`), and real downstream damage — this client sat with
  latent `ImportError`s for seven days after adopting it.
- **Virtual subclassing via `ABCMeta.register`.** Measured impossible, and in
  the most dangerous way: `register()` returns without error and `isinstance`
  stays `False`. Two independent measurements in the HA venv. The obvious
  fallback is worthless too — on a subclass that skips `__init__`, 93 of 153
  members raise on access, 42 of them public, including the ones HA reads.
  (Note that the accompanying claim in `homematicip_local`'s
  `backend_types.py:5-8` is half wrong: subclassing works for 15 of 15 dispatch
  classes. Correcting it is #1264 there.)
- **Trim the wire generation to its reachable subset.** The tool cannot:
  `datamodel-code-generator` offers `--openapi-scopes` over categories, not
  individual schemas. A pruner with a `$ref` reachability hull would have to be
  written, and it would be brittle — 36 of 73 enums are unreferenced by name but
  reached through string values, among them the security and update vocabularies.
  Gain against all four deployment goals: nil.
- **Generate `capabilities.py` from the daemon's `enums.json`.** Points at the
  wrong file. The 17 tokens here are daemon _subsystem_ identifiers (`rest.v1`,
  `alarm.v1`) for `connect(required_capabilities=…)`; the drift that prompted the
  idea is in an entirely different vocabulary, `CustomDPSummary.capabilities`, an
  open bool map. And this file states its own reason for being hand-written — "a
  convenience for the tokens we act on, not an allowlist to validate against" —
  while the generated enums reject unknown values outright.
- **Keep `openccu_loom_types` as a top-level import path inside this
  distribution.** No, and it was the one thing #122 must not get wrong. Home
  Assistant never uninstalls abandoned requirements
  (`homeassistant/requirements.py:115-126`), and `openccu_loom_types-0.5.9.dist-info`
  is in every HA venv today. Two distributions owning one top-level package:
  reinstalling the orphan silently reverts `wire/`, uninstalling it deletes the
  package this client ships. Reproduced in a throwaway venv.

  Carried out that way: the bindings live at `openccu_loom_client/wire/`, so the
  two distributions own disjoint top-level packages. Re-measured in a throwaway
  venv on the folded build — `importlib.metadata.files` reports **0 overlapping
  files**, the client imports with `openccu-loom-types==0.5.9` installed on top,
  and it still imports after that orphan is uninstalled. The failure mode is
  gone rather than mitigated.

Decided the same day, and recorded because the code cannot show them:

- **`CustomDPSummary.kind` stays the SPA widget hint of daemon ADR 0016** and
  does not become the HA dispatch anchor. It has a reachable empty value
  (`KindUnknown = ""`), so a closed enum would have to contain it and this
  client's `_CATEGORY_FALLBACK` would survive anyway. The 22 tokens get
  documented as a vocabulary block instead, without `required` — `openccu-loom`
  #637.
- **The CUxD central-scoping rule is landed in `aiohomematic`**
  (SukramJ/aiohomematic#3369), which is the retirement condition
  `by_design.md:118-166` names for the divergence. Until it lands,
  `canonical.py` may not be described as bit-identical (#114) and may not be
  proposed for deletion — the daemon's own closure index says otherwise in two
  places and is itself out of date.

  It reaches further than this repository, which the first draft of #126 missed:
  landing it re-keys CUxD entities on the **direct-CCU** backend as well, so
  `homematicip_local` needs a third registry pass beside
  `_async_migrate_loom_unique_ids` and `_async_migrate_aiohomematic_hub_unique_ids`.
  And the divergence is pinned on both sides in the daemon
  (`cuxd_scoping_golden.json`, `script/routing_key_parity.py`): each guard fails
  once the two stop differing, so a companion PR has to fold the CUxD cases into
  the shared fixtures in the same wave, or the daemon's CI goes red on the day
  aiohomematic ships the rule.

- **`openccu-loom` stays MIT.** ADR 0066 would have made every move of code into
  the daemon one-way. This keeps that direction open; it does not schedule any
  such move.
