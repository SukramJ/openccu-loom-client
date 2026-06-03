# Optimization needs — openccu-loom-client

Running log of follow-up work surfaced while reconciling the client with
`aiohomematic-contract` and the `openccu-loom` daemon. Each item notes
priority, the evidence (file:line / commit), and the suggested fix.

> Done so far:
> - All unique_id / slug construction routes through `aiohomematic_contract`
>   (generic, custom, sysvar, program); golden cross-checks in
>   `tests/unit/test_contract_parity.py`.
> - **Canonical `loom_`/serial scheme adopted** (the daemon's resolved
>   direction, ADR-0024 + `ha-unique-id-migration.md`): keys are
>   `canonical_unique_id` = `loom_` + routing key, with the **CCU serial
>   suffix** in the central-id slot (not the snapshot central / not
>   `entry_id`). The store holds `serial_suffix` (set from `/system/ccu`
>   by the adapter); the refresh bridge and self-keying events **consume
>   `payload.unique_id`** when the daemon supplies it and rebuild via the
>   contract otherwise.
> - The two new daemon broadcasts are bound and (for rollback) bridged:
>   `device.trigger` → `DeviceTriggerEvent`, `datapoint.optimistic_rolled_back`
>   → `DataPointOptimisticRolledBackEvent` → public `OptimisticRollbackEvent`.
> - The daemon-broadcast drift check now actually runs on this layout (its
>   hardcoded macOS path had been silently skipping — and was masking the
>   two missing bindings above).

---

## P1 — HA registry migration + remaining equivalence checks

The drop-in does **not** reproduce aiohomematic's keys verbatim; it
migrates HA's registry to the canonical `loom_`/serial scheme on setup
(daemon decision, `ha-unique-id-migration.md`). The string-level rewrite
lives in **homematicip_local** (`async_migrate_entries`), not here.

Open in this repo / to verify:
- **(homematicip_local) one-time registry migration** + serial wiring;
  the old `entry_id[-10:]` `central_id` injection is obsolete.
- **`legacy_name` equivalence (still assumed).** Hub keys use
  `hub_slug(name)`; the daemon's sysvar/program `name` must equal
  aiohomematic's `legacy_name` (ReGa name) *before* slugify. Verify on a
  real CCU.
- **serial equivalence.** The client keys off the `/system/ccu` serial;
  the registry migration keys off `entry.unique_id`. Both are the real
  CCU serial, so they match — confirm once end-to-end.

The earlier "snapshot `central_id` as key prefix" concern is **resolved**:
the central slot is now the CCU serial suffix, and the daemon ships the
finished `payload.unique_id` for the client to consume + verify against
its rebuild (drift check in `tests/unit/test_compat_model.py`).

Evidence: `compat/aiohomematic/model/hub/__init__.py` (sysvar/program
helpers), `store.py:set_serial`, `compat/.../central/adapter.py`
(`_refresh_system_information` sets the serial).

## P1 — bootstrap via the daemon's nested snapshot

`client.bootstrap()` still does an N+1 walk: `get_device_detail` per
device, then `list_data_points` per channel. The daemon now exposes a
**nested snapshot** (channels + data points inline) for exactly this
drop-in (openccu-loom `236658c`). Consuming it would collapse bootstrap
to a single round-trip.

Evidence: `openccu_loom_client/client.py` bootstrap loop;
`store.load_snapshot` docstring ("Channels and data-points are not part
of the snapshot envelope") is now outdated.
Suggested: extend `load_snapshot` to ingest nested channels/DPs when
present, fall back to per-device fetch otherwise.

## P2 — model device triggers as HA event groups

`device.trigger` is now bound to `DeviceTriggerEvent`, but
`query_facade.get_event_groups` still raises (stub message updated to
point here). aiohomematic exposes keypress/impulse events as HA *device
triggers*, with the event data point carrying an `event`/button **prefix**
in its `unique_id` (`generate_unique_id(prefix="event", …)`).

Two pieces of work:
- Build the event-group surface on top of `DeviceTriggerEvent` and
  un-stub `get_event_groups`.
- The trigger `event_key` is now the daemon-supplied `payload.unique_id`
  (canonical `loom_…`). Confirm the daemon emits the `event`-prefixed
  form for keypress/impulse data points so HA device-trigger routing
  matches; `canonical_unique_id` already supports a `prefix` argument.

Evidence: `compat/aiohomematic/central/adapter.py` `get_event_groups`;
`events/types.py` `DeviceTriggerEvent.__post_init__`.

## P2 — apply optimistic rollback to the store model + de-dup sources

The rollback broadcast is bridged to the public `OptimisticRollbackEvent`,
but the **store→model bridge does not revert the optimistic value** in the
in-memory model on rollback — a reader could still see the un-confirmed
value until the next `value_changed`. The daemon sends `present` (the
reverted value); the store should apply it.

Also: `new_optimistic_rollback_event` still synthesizes the same public
event locally from REST `set_value` failures. Now that the daemon emits
the authoritative broadcast, decide whether to keep both (risking a
double emit for one rollback) or retire local synthesis.

Evidence: `events/synthetic.py` (local synthesis),
`compat/aiohomematic/central/refresh.py` `on_rollback` (daemon bridge).

## P3 — mypy cannot resolve editable first-party deps

Under `strict = true`, mypy reports "Cannot find implementation or library
stub" for both `openccu_loom_types.*` and `aiohomematic_contract` even
though both ship `py.typed` (editable installs aren't followed). This
cascades into spurious `no-any-return` errors (74 baseline → 84 after the
contract imports). Logic is unaffected; the type gate is just noisy.

Suggested: set `mypy_path` / `explicit_package_bases`, or install the
first-party deps non-editable in the type-check environment, so strict
mode becomes meaningful again.
