# TODO — aiohomematic alternative backend (open items)

**Strategy (2026-06-21):** `openccu-loom*` is an **alternative backend** for
`homematicip_local`, coexisting with `aiohomematic` — _not_ a replacement.
Runtime reuse of `aiohomematic` is deliberate; the `compat/aiohomematic/`
shim (integration form A1) is the chosen plug-in point. See `CLAUDE.md` →
"What this is" and `docs/architecture-review.md` §2.1.

Status: the `LoomCentralAdapter` has **zero `NotImplementedError` stubs**;
openccu-loom-types **0.1.26** pinned (daemon api 1.19.0 / v0.9.1). All
daemon-blocked client work and the **clean-architecture follow-ups** (§2.1
import seam, §2.2 typed mixins / drift-masking removal) are now done. What
remains is **cross-repo `homematicip_local` follow-ups** and
**verify-on-real-CCU** notes, plus **G5** (deferred _by design_ — see below).
The file is kept because those open items remain.

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
      (2026-06-21). `pyproject.toml` and `requirements.txt` both pin
      `aiohomematic==2026.6.4` (exact pin = hard upper bound; the compat shim
      couples to aiohomematic internals, so a range would let an unverified
      series leak in). aiohttp (`>=3.14.1`) / pydantic (`>=2.13.4`) floors match
      across both files. Bump the pin and the drift-guard snapshot together when
      adopting a new aiohomematic series.
- [x] **Bundle the aiohomematic imports in one module.** DONE (2026-06-22).
      `compat/aiohomematic/_upstream.py` is now the single seam onto the
      aiohomematic internals the compat layer reuses (`async_support.Looper`,
      `central.events.*`, `const.*`, `model.custom.*`). All ~9 consumers import
      from it; the bit-identical routing-key contract stays isolated in
      `canonical.py` (the documented routing seam, deliberately not folded in).
      One grep-able surface for the version bound + drift-guard + selective-reuse
      reasoning.
- [x] **Drift-masking removed; imitation is now typed, not blind (§2.2).** DONE
      (2026-06-22). The original concern was the **brittleness**: ~74
      `type: ignore[attr-defined]` + `getattr(model, "field", default)` on typed
      pydantic models, which masked wire-schema drift as silent `None`/`False`
      and defeated `mypy --strict`. Each protocol-surface / entity-surface mixin
      now declares the host contract it depends on in an `if TYPE_CHECKING:`
      block (typed against `DataPoint` / `CustomDataPoint` / `Sysvar` /
      `Program`), so mypy checks the mixins properly: **type: ignore in compat
      74 → 7** (remaining are genuine — cross-kind `data_point_type`, dynamic
      `_value_override`/`_registered`, the singleton structural-reuse divergence,
      all commented), and known-field `getattr` became direct typed access.
      Surfaced + fixed two latent bugs the masking hid (sysvar `set_value`
      called positionally against a kw-only signature; program `last_execute_time`
      read a non-existent field — daemon ships `last_executed`).
      _On "selective reuse of aiohomematic to shrink the ~50 % stubs": assessed
      and **not pursued by design** — those stubs are protocol-tail members
      (`config_payload`, `state_path`, `service_methods`, …) whose aiohomematic
      implementations need a live `CentralUnit` / paramset descriptors the
      daemon-mediated client doesn't hold, so their neutral defaults are correct.
      The fix was making the imitation **typed/loud**, not removing it._

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
      **✅ Former known gap — RESOLVED (2026-06-22).** `system_update` had no WS
      push; the daemon now ships a `hub.system_update_changed` broadcast (D1,
      openccu-loom v0.9.1). Bound client-side as `HubSystemUpdateChangedEvent`
      and routed onto the system-update singleton via
      `SystemUpdateDp.update_from_push` (`adapter.py` `_on_system_update_push`).
      Every hub singleton is push-driven now; the reconcile loop (G6-followup)
      is reframed as a pure missed-push backstop. Test:
      `TestHubPushRouting::test_system_update_push_updates_singleton_and_emits`.
- [x] **G6-followup — keep `system_update` fresh post-bootstrap.** DONE
      (2026-06-21). A deliberately coarse reconcile loop
      (`_HUB_RECONCILE_INTERVAL = 300`, `_hub_reconcile_loop`, ~70x slower than
      the retired 30 s poll) re-seeds the singletons from the aggregate, so
      `system_update` (no daemon push) stays fresh and missed pushes are
      backstopped. Cancelled in `stop()`.
  - [x] **G4(a) — single `GET /hub/data-points`** (OPTIMISATION). DONE
        (2026-06-21). Detail: `docs/g4a-hub-data-points-consumption.md`.
        `SystemOperations.get_hub_data_points()` added; `fetch_hub_singleton_data`
        now seeds every singleton from the single aggregate call (inbox, metrics,
        connectivity, install-mode), refetching message bodies only on a count
        delta and keeping `get_system_update` for the firmware strings. The
        per-endpoint `_fetch_*` fan-out is deleted. While here, the previously
        unconsumed central-wide `InstallModeChangedEvent` push was wired onto the
        install-mode sensors (it was poll-only before). Tests live in
        `test_compat_model.py` (`TestHubAggregateFetch`, `TestHubPushRouting`).
- [ ] **G5 — enrich `get_event_groups()` from REST** — DEFERRED **by design,
      not as a gap** (re-affirmed 2026-06-22). `get_event_groups` already builds
      groups locally (no `NotImplementedError`); `last_triggered_event` is
      **live** (the refresh bridge calls `record_trigger` on every
      `device.trigger` push) and `available` tracks the device. Backing it with a
      per-trigger-channel `GET …/event-groups` fetch at bootstrap is the **exact
      N×M cost the project just eliminated** (P3 nested snapshot) — for a daemon
      snapshot no fresher than the live trigger feed. Implementing it would make
      the architecture _worse_; the deferral is the clean choice. Revisit only if
      event-groups ever join the nested snapshot. The remaining client-visible
      part (drop the `event.py` `NotImplementedError` fallback) is cross-repo
      (homematicip_local, below).
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

## P1 — HA-side (homematicip_local)

- [ ] **`websocket_api.py`**: the config-UI paramset description getters are
      **async** on the loom backend (daemon serves over REST), whereas
      aiohomematic's are sync/cached. `await` them on the loom path:
      `get_paramset_description`, `get_link_paramset_description`.

## P2 — verification gaps (e2e)

- [ ] `rename_device(ise_id)`: implemented + unit-tested, but godevccu does
      not assign `ise_id`, so it can't be e2e-tested against the simulator.
      Verify against a real CCU.
- [ ] `test_device_trigger_pushed_over_ws` (xfail): needs the daemon
      `interface_id` for `FireEvent`; wire it up from the snapshot.
- [ ] `test_optimistic_rollback_pushed_over_ws` (xfail): model a
      non-confirming DP in the godevccu harness to drive it deterministically.
- [ ] hub `sysvar_changed` / `program_executed` broadcasts (xfail). _xfail
      reasons corrected against daemon source (2026-06-21):_ - `sysvar_changed` **is** broadcast on client-initiated writes too
      (`PatchSysvar → UpdateSysvar → SysvarChangedEvent`, with same-value
      dedup — `coordinators/hub.go`). The xfail is a simulator/value-dedup
      artefact (godevccu doesn't effect a real value change), not a daemon
      gap. Drive it with a genuine value delta or against a real CCU. - `program_executed` is **CCU-originated only** by design; a client
      `execute` gets REST 202 with no push (`coordinators/hub.go`
      `NotifyProgramExecuted`). Not self-initiated-testable — verify via a
      CCU-side program run.

## P3 — wire-contract / refinement

> **Re-verified against the daemon source (`../openccu-loom`, 2026-06-21).**
> Three items previously filed as "deferred daemon asks" turned out to be
> **client-side gaps** — the daemon already ships the data/endpoints; the
> client doesn't consume them. Corrected below.

- [x] **N×M bootstrap — adopt the nested snapshot (CLIENT, high impact).**
      DONE (branch `feat/consume-daemon-wire-fields`). `get_snapshot` takes an
      `include` param; `bootstrap()` requests `?include=data_points` and
      attaches each channel's DPs from `Snapshot.device_channels` instead of one
      `GET …/data-points` per channel — collapsing the formerly dominant N×M
      fan-out into the single snapshot round trip. Per-device detail calls stay
      (firmware / rich `availability` are detail-only, absent from the snapshot
      summaries); falls back to the per-channel fetch when the daemon returns no
      `device_channels` (older daemon) and on the `device.created` reconcile
      path. The stale "asks.md H1 daemon ask" comments are corrected. Test:
      `test_client_bootstrap.py::TestNestedSnapshotBootstrap` (asserts no
      `/data-points` call and `?include=data_points`).
- [x] **Value accuracy (Strategy B) — consume the room already on the wire
      (CLIENT).** DONE (same branch). `_protocol_surface.py` `room` now resolves
      the DP's channel room (generic: `device.get_channel(channel_number)`;
      custom: the primary channel) and `rooms` derives `{room}` from it; hub
      sysvar/program twins keep the no-channel `None` default. (`translated_name`
      was already consumed via `generic_translated_name`.) Test:
      `test_compat_model.py::TestProtocolSurfacePresentation`.
      **✅ Formerly daemon-blocked fields — now consumed (2026-06-22, types
      0.1.26 / daemon v0.9.1):** per-channel `functions` (D3 — `Channel.functions`
      accessor; the generic + custom surfaces resolve `function` from the first
      label) and `value_translations` (D2 — `_GenericProtocolSurface.value_translations`
      reads `DataPointSummary.value_translations`). Same test class.
- [x] `config_admin.get_schema()` xfail — **removed, verified (CLIENT).** DONE
      (same branch). The daemon's `{sections, fields}` validates against
      `SchemaResponse` (Pydantic ignores the undocumented per-field `default`).
      Replaced the e2e xfail with a deterministic parse test mirroring the
      daemon payload (incl. `default`):
      `test_operations_admin.py::TestConfigOperations::test_get_schema_parses_daemon_shape`.
      The remaining `SchemaField.default` OpenAPI doc omission is a daemon-repo
      fix (D4 in `../openccu-loom/todo.md`).
