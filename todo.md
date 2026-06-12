# TODO — aiohomematic drop-in (open items)

Status after the `feat/drop-in-completion` work: the `LoomCentralAdapter`
has **zero `NotImplementedError` stubs**. What remains is one feature model,
a few cross-repo follow-ups, and some deferred refinements.

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
