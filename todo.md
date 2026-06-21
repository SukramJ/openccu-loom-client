# TODO — aiohomematic alternative backend (open items)

**Strategy (2026-06-21):** `openccu-loom*` is an **alternative backend** for
`homematicip_local`, coexisting with `aiohomematic` — _not_ a replacement.
Runtime reuse of `aiohomematic` is deliberate; the `compat/aiohomematic/`
shim (integration form A1) is the chosen plug-in point. See `CLAUDE.md` →
"What this is" and `docs/architecture-review.md` §2.1.

Status after the `feat/drop-in-completion` work: the `LoomCentralAdapter`
has **zero `NotImplementedError` stubs**. What remains is keeping the shim
coupling robust (below), one feature model, a few cross-repo follow-ups, and
some deferred refinements.

## P1 — keep the alternative-backend coupling robust (strategy follow-ups)

These follow from the 2026-06-21 strategy decision (reuse `aiohomematic`
deliberately, keep the compat shim). Rationale: `docs/architecture-review.md`
§2.1 + §2.2.

- [x] **Drift-guard test against the real aiohomematic protocols/classes.**
      DONE (2026-06-21). `tests/compat/test_aiohomematic_protocol_parity.py`
      now also snapshots the full set of `@runtime_checkable` protocols in
      `aiohomematic.interfaces.model` (`test_aiohomematic_model_protocol_surface_has_not_drifted`):
      if aiohomematic adds/removes a model protocol, CI fails so a human
      decides whether a twin must follow — silent runtime-only HA breakage
      becomes a loud CI failure. The per-twin parity cases already catch
      member-level drift for the four protocols the twins must satisfy.
      (The isinstance-tuple partners in `homematicip_local`'s
      `backend_types.py` are cross-repo and still verified there.)
- [x] **Upper aiohomematic version bound + single-source the pin.** DONE
      (2026-06-21). `pyproject.toml` now caps `aiohomematic>=2026.6.2,<2026.7`;
      `requirements.txt` keeps the exact CI pin `==2026.6.2` (within range, the
      standard library-range / CI-lockfile split). aiohttp/pydantic floors in
      pyproject were also raised to match the CI pins. Bump the cap and the
      drift-guard snapshot together when adopting a new aiohomematic series.
- [ ] **Bundle the aiohomematic imports in one module.** The reused symbols
      (`generate_unique_id`, `async_support.Looper`, `central.events.*`,
      `const.*`, `interfaces.model.*`) span 8 files. Funnel them through one
      re-export module so the used surface is explicit and a breaking upstream
      change has a single blast radius. _Lower urgency now that the drift-guard
      above catches upstream protocol changes; this is an organizational
      refactor, not a correctness fix._
- [ ] **Roll back imitation in favour of selective reuse.** Where an
      aiohomematic class/function works _without_ a live `CentralUnit`, reuse it
      instead of mirroring — shrinks the ~50 % stub properties in
      `_protocol_surface.py` and the 60 `type: ignore[attr-defined]` in the
      compat model layer (`docs/architecture-review.md` §2.2). Incremental,
      class by class, guarded by the drift-guard test above. _Large multi-step
      rewrite; deliberately left as future work._

## P1/P2 — loom wire-gap follow-ups (daemon 0.8.0 / API 1.18.0)

Design note: `docs/loom-wire-gaps-followups.md` (openccu-loom PR #156, gaps
G1–G7). All code citations below verified against the tree on 2026-06-21.

**UNBLOCKED (2026-06-21):** `openccu-loom-types` bumped to **0.1.25** (pinned in
`pyproject.toml` + `requirements.txt`); the new models (`EventGroupSummary`,
`HubDataPoints`, `HubCountChangedPayload`, `HubMetricChangedPayload`,
`HubConnectivityChangedPayload`, `TextDisplayState` fields) are present and the
suite + mypy are green on it. The `aiohomematic<2026.7` cap is unchanged (still
2026.6.2) and its protocol drift-guard still passes.

Client-side (this repo) — sequence small → large:

- [x] **G2 — text-display option lists** (REQUIRED). DONE (2026-06-21).
      `custom/__init__.py` `CustomDpTextDisplay`: the five `available_*` getters
      now read from `self._state` via a `_option_list(key=…)` helper (icons +
      sounds included — they were also empty stubs); `has_icons`/`has_sounds`
      derive from the populated lists. Tests in `test_compat_model.py`.
- [x] **G1 — light HS colour read-back** (REQUIRED). DONE (2026-06-21).
      `CustomDpDimmer.hs_color` reads nested `color:{h,s}` (hue passthrough,
      saturation [0,1] → HA [0,100]) with a legacy flat-key fallback. Tests in
      `test_compat_model.py`. **⚠ Verify on a real device:** the read path scales
      saturation ×100 to HA's [0,100], but the _write_ path
      (`turn_on`→`set_color`, `custom/__init__.py:413`) sends HA's [0,100] raw to
      the daemon — confirm the daemon's `set_color` scale so read/write round-trip
      consistently (possible separate write-path fix).
- [x] **G7 — generic `set_on_time`** (REQUIRED). DONE (2026-06-21).
      `DpSwitch.set_on_time` now writes the sibling `ON_TIME` parameter via
      `store.set_value`, with a guard that no-ops (debug log) when the channel
      exposes no `ON_TIME`. Tests in `test_compat_model.py`.
- [x] **G6 (foundation) — bind the 5 new hub broadcasts** (REQUIRED). DONE
      (2026-06-21). `events/types.py`: added `HubAlarmMessageCountChangedEvent`,
      `HubServiceMessageCountChangedEvent`, `HubInboxChangedEvent`,
      `HubMetricsChangedEvent`, `HubConnectivityChangedEvent` (+ registry +
      `events/__init__` exports). The daemon-broadcast drift-guard
      (`test_events_dispatcher.py`) is green again; round-trip tests added.
- [x] **G6 (routing + drop poll loop)** — DONE (2026-06-21).
      `_HubCoordinator.install_push_routing` (`adapter.py`) subscribes the 5 push
      events on the refresh group and fans them onto the singletons (central-
      keyed): inbox/metrics/connectivity → `update_value`; alarm/service counts →
      lazy `update_messages` list refetch, but only on a count delta; each change
      emits the keyed `DataPointStateChangedEvent`. The 30 s poll loop
      (`_HUB_REFRESH_INTERVAL`, `_hub_singleton_refresh_loop`, `_hub_refresh_task`)
      is deleted; one cold-start `fetch_hub_singleton_data()` seeds the values.
      Tests: `test_compat_model.py::TestHubPushRouting`.
      **⚠ Known gap:** `system_update` has **no push** (not among the 5 broadcasts)
      and now refreshes at bootstrap only. Wire it via G4(a)'s aggregate (which
      carries the `update` flags) on a slow cadence, or add a daemon
      `hub.system_update` broadcast. Tracked below.
- [ ] **G6-followup — keep `system_update` fresh post-bootstrap.** With the poll
      loop gone, the firmware-update singleton (`SystemUpdateDp`, refreshed by
      `_fetch_system_update` → `get_system_update`) only updates at bootstrap.
      Drive it from G4(a)'s `/hub/data-points` `update` flags on a slow poll, or
      from a new daemon broadcast. Low urgency (firmware availability changes
      rarely), but a real regression vs. the old 30 s poll.
- [ ] **G4(a) — single `GET /hub/data-points`** (OPTIMISATION).
      Detail: `docs/g4a-hub-data-points-consumption.md`. Collapse
      `fetch_hub_singleton_data()` (`adapter.py:444-465`) from **7 REST calls**
      (across 6 `_fetch_*` helpers — `_fetch_messages` makes 2: alarm + service;
      verified 2026-06-21) to **1** aggregate + conditional follow-ups (→ 3 only
      on a count change). Sub-steps:
  - [ ] **Add the op** `SystemOperations.get_hub_data_points()` →
        `list[HubDataPoints]` (mirror `get_hub_metrics`, `system.py:107`).
  - [ ] **Fully replace** four `_fetch_*` with scalar fans off the aggregate,
        then delete the helpers: `_fetch_inbox` (`adapter.py:490`),
        `_fetch_metrics` (`:502`, match the 3 sensors by `legacy_name`:
        `system_health`/`connection_latency_ms`/`last_event_age_seconds`),
        `_fetch_connectivity` (`:557`), `_fetch_install_mode` (`:538`,
        `remaining_s if enabled else 0`). Add `_select_central(...)` (picks this
        coordinator's per-central entry, like `get_hub_metrics` needs) +
        `_apply_{inbox,metrics,connectivity,install_mode}` helpers.
  - [ ] **Count-delta guard for messages.** Keep `_fetch_messages`
        (`adapter.py:467`, `list_alarm_messages`/`list_service_messages` feed
        `update_messages` with the bodies) but call it **only when**
        `alarm_messages.value` / `service_messages.value` differ from the held
        count — the aggregate carries counts only. Steady state → zero list
        fetches.
  - [ ] **Decide `update` handling.** The aggregate gives only
        `update_available`/`in_progress`; `update_dp.update_data` wants the
        firmware strings. Either keep `_fetch_system_update` (`:524`,
        `get_system_update`) lazily (fetch only when `update_available` flips)
        or drop it if the entity needs only the flags.
  - [ ] Multi-CCU: map each per-central aggregate entry to its coordinator.
        Pairs with **G6** — once pushes land, the aggregate becomes the
        cold-start snapshot and the poll loop is deleted.
- [ ] **G5 — enrich `get_event_groups()` from REST** (OPTIMISATION).
      `adapter.py:663` already builds groups locally (no `NotImplementedError`).
      Optionally back/enrich it from `GET …/event-groups` so
      `last_triggered_event` / `available` are authoritative, not derived.
- [ ] **G3 — sysvar `extended` marker** (VERIFY, no code). Wired end-to-end
      (`compat/aiohomematic/model/hub/__init__.py:225`); confirm on a real CCU
      that an extended variable surfaces as the writable flavour.

Cross-repo (homematicip_local), after the client items:

- [ ] **G4(b)** — remove the `BACKEND_LOOM` early-return in
      `control_unit.py` `_async_cleanup_orphaned_entity_registry_entries`
      (~500-507) now the full singleton set is modelled; guard on singleton
      presence, not a blanket backend skip.
- [ ] **G5** — drop the `NotImplementedError` fallback in `event.py:55-70` so
      the `event` platform gets its bootstrap entities.

## Open branches awaiting merge

- `openccu-loom-client` → `feat/drop-in-completion` (6 commits)
- `openccu-loom` → `feat/channel-name-category-schema` (openapi `ChannelSummary.name` + `category`)
- `openccu-loom-types` → `feat/channel-name-category` (regenerated `rest.py`)

## P1 — HA-side (homematicip_local)

- [ ] **`websocket_api.py`**: the config-UI paramset description getters are
      **async** on the loom backend (daemon serves over REST), whereas
      aiohomematic's are sync/cached. `await` them on the loom path:
      `get_paramset_description`, `get_link_paramset_description`.

## P2 — types release to populate channel names

- [ ] Merge `openccu-loom` `feat/channel-name-category-schema`, release
      **openccu-loom-types 0.1.6**, bump the client dependency. Then
      `get_configurable_devices` `channel_name` populates (client already
      reads it via `getattr`, falls back to `""`).

## P2 — verification gaps (e2e)

- [ ] `rename_device(ise_id)`: implemented + unit-tested, but godevccu does
      not assign `ise_id`, so it can't be e2e-tested against the simulator.
      Verify against a real CCU.
- [ ] `test_device_trigger_pushed_over_ws` (xfail): needs the daemon
      `interface_id` for `FireEvent`; wire it up from the snapshot.
- [ ] `test_optimistic_rollback_pushed_over_ws` (xfail): model a
      non-confirming DP in the godevccu harness to drive it deterministically.
- [ ] hub `sysvar_changed` / `program_executed` broadcasts (xfail): the
      daemon doesn't broadcast them for a client-initiated change against the
      simulator — needs a CCU-side trigger.

## P3 — wire-contract / refinement

- [ ] `config_admin.get_schema()` (Tier A xfail): daemon returns
      `{sections, fields}` which doesn't validate against `SchemaResponse` —
      reconcile the types model with the daemon shape.
- [ ] **Value accuracy (Strategy B refinements)**: the compat protocol surface
      returns neutral defaults for daemon-sourced fields it can't yet derive
      (rooms/functions, translations). The daemon already ships generic
      `data_point_type`/`category`; rooms/translations on the wire would let
      the twins return accurate values instead of `None`/`()`.
- [ ] N×M bootstrap cost: the snapshot lacks nested channels/DPs, so bootstrap
      is one detail call per device + one DP call per channel. A streamed /
      nested snapshot endpoint is a deferred daemon ask.
