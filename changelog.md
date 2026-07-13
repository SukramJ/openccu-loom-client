# Version 2026.7.10 (2026-07-13)

Frontend compat, part 3: the **CCU dashboard**. It was unreachable on a loom
entry and, once reachable, most of its commands broke on shape mismatches.

- Fix: **the CCU tab was hidden on loom entries.** The config panel gates it on
  `permissions.backend === "CCU"`, and `backend` is `central.model` — which the
  loom adapter reports as `"openccu-loom"` (its backend-form identity;
  deliberately not `"CCU"`, since that value also drives entity dispatch). The
  whole dashboard was therefore invisible to loom-backed entries even though the
  adapter serves every one of its commands. Fixed frontend-side by gating on the
  _set_ of backends that serve the `ccu/*` surface (companion PR).
- Fix: **message / inbox commands return records and bools.** `json_rpc_client`
  now hands back aiohomematic's `ServiceMessageData` / `AlarmMessageData` /
  `InboxDeviceData` dataclasses (the handlers call `dataclasses.asdict()` on the
  lists) and `bool` from the mutations. Returning `None` made every
  acknowledge/accept report `*_failed` to the panel **even when the CCU-side
  write had succeeded**. `acknowledge_message` now covers both stores — the HA
  service _and_ alarm handlers route through the one aiohomematic primitive,
  while the daemon splits the endpoints. `rename_device` takes the reference's
  `new_name` kwarg and raises a catchable error for an unknown ise_id.
- Fix: **integration dashboard.** `central.health` is a _property_ with
  `to_dict()` (an async method raised `AttributeError: 'method' object has no
attribute 'to_dict'`); `client_coordinator.clients` yields records with
  `interface_id` + `command_throttle` (bare id strings raised `AttributeError`);
  `incident_store.get_incidents_by_interface` returns records with `to_dict()`;
  and `clear_incidents` now actually reaches the daemon (`DELETE /incidents`)
  instead of being a client-side no-op. Each of these took down the _whole_ tab,
  because the panel fetches its four sections in one `Promise.all`.
- Feat: **device surface the dashboard reads.** `Device.availability` returns an
  `AvailabilityInfo` record with snake_case members (`is_reachable`, …; the wire
  record spells them PascalCase), `Device.firmware_updatable`,
  `Device.get_generic_data_point(parameter=…)` (the signal-quality view looks the
  RSSI up by bare parameter) and `Device.update_firmware()` → `bool`.

# Version 2026.7.9 (2026-07-13)

Frontend compat, part 2: **direct links** and — the load-bearing one — the
**exception hierarchy**. Continues the end-to-end audit of
`homematicip-local-frontend` → `homematicip_local` → `LoomCentralAdapter` that
started in 2026.7.8 (schedule cards + paramset/sessions).

- Fix: **loom exceptions are now aiohomematic exceptions.** `BaseLoomException`
  derives from `aiohomematic.exceptions.BaseHomematicException` (a hard runtime
  dependency). `homematicip_local` wraps every backend call in
  `except BaseHomematicException` — imported from the _real_ aiohomematic, not
  the compat shim's alias — and maps it to a typed websocket error
  (`write_failed`, `read_failed`, …). Loom failures sat outside that hierarchy,
  so they escaped the handlers and reached the config panel as a generic
  `unknown_error`, losing both the error code and the daemon's problem+json
  title. This one change repairs error translation across links, sessions,
  paramset and the panel bootstrap at once. `str(err)` is preserved (the base
  eats a leading `name` argument; the concrete class name is passed for it), and
  the shim now re-exports upstream's `BaseHomematicException` verbatim — the
  strict superset, since loom errors are subclasses.
- Fix: **`central.link` matches aiohomematic's `LinkCoordinator` signature.**
  `add_link` / `remove_link` take `sender_channel_address` /
  `receiver_channel_address` (full `"<device>:<channel>"` addresses, the device
  part derived for the daemon path) and return `bool` — the handlers render
  `add_link_failed` / `remove_link_failed` on a falsy result, and previously got
  a `TypeError` at argument binding instead. `get_device_links` takes
  `device_address`; `get_linkable_channels` takes `interface_id` /
  `source_channel_address`. Both now return aiohomematic's `DeviceLink` /
  `LinkableChannel` **dataclasses** (the wire models are field-for-field
  identical) because the handlers call `dataclasses.asdict()` on the results,
  which throws on a pydantic model.
- Feat: **`Device.get_channel(channel_address=…)`, `Device.sub_model` and
  `Channel.type_name`.** The config-form and link handlers address channels by
  full address and read the reference's spellings. `get_channel` keeps accepting
  `number` for the loom-internal callers; a foreign or malformed address
  resolves to `None` like the reference's keyed lookup. `sub_model` is a real
  wire field (`DeviceSummary.sub_model`), not a stub.

Cross-repo companion: `homematicip_local`'s `ws_get_linkable_channels` needs the
`isawaitable` dual-await (the daemon computes link candidates server-side, so
the loom call is async where aiohomematic's is sync) — the same accommodation it
already makes for `get_paramset_description`.

# Version 2026.7.8 (2026-07-13)

Tracks the `openccu-loom-types` 0.1.55 build (daemon API 2.18.0 → 2.19.0,
openccu-loom 0.40.0). Unlike the last few maintenance bumps this one is **not**
wire-neutral: the daemon now surfaces per-CCU **operational readiness**, which
lands as one new broadcast and one new _required_ REST field, so the client
needs matching bindings.

(0.1.55 is generated from openccu-loom **v0.39.0**, which is where readiness
landed; **v0.40.0 is contract-identical** — same API 2.19.0, no `openapi.yaml`
/ `pkg/hmapi` delta, its only change being a daemon-internal JSON-RPC session
-leak fix. So this types build is the right pin for a 0.40.0 daemon.)

- Feat: **bind the new `central.readiness_changed` broadcast** as
  `CentralReadinessChangedEvent` (payload `CentralReadinessChangedPayload`:
  `central`, `phase`, `ready`, `interfaces_loaded`, `interfaces_total`; routed
  on the central name like `CentralStateChangedEvent`). The daemon reports the
  southbound bring-up phase (`waiting_for_ccu` → `loading_hub` →
  `loading_devices` → `ready`) plus the latched `ready` flag and interface
  wiring progress, so consumers can gate on real readiness instead of inferring
  it from the lifecycle state alone. This also closes the registry-coverage
  drift check, which had started failing as soon as the daemon advertised the
  broadcast.
- Chore: **bump `openccu-loom-types` to 0.1.55.** The breaking part of the wire
  delta is a new **required** `readiness` object on `SystemCCUEntry`
  (`GET /system/ccu`) — a `Readiness` model (`phase`, `ready`,
  `interfaces_loaded`, `interfaces_total`) plus the `Phase` / `ReadinessPhase`
  enums. `SystemCCUEntry` is validated as a whole (`system.list_system_ccus`),
  so an entry without it now fails validation; this release adds the field to
  the fixtures and keeps the pass-through untouched. Note this is what broke
  the serial read-out (`_refresh_system_information` parses `/system/ccu`)
  against a 0.1.55-shaped payload.

# Version 2026.7.7 (2026-07-12)

Maintenance release tracking the daemon's `openccu-loom-types` 0.1.54 build
(daemon API 2.18.0). The wire delta is a single additive, optional field —
no functional client change is required; the release exists to keep the
version pins, changelog and `const.VERSION` in lock-step with the types build.

- Chore: **bump `openccu-loom-types` to 0.1.54** (daemon API 2.17.0 → 2.18.0,
  new `SCHEMA_DIGEST`). The only contract change vs. 0.1.53 is a new optional
  `note_key: str | None` on `Component` (the `Health.components[]` entry
  returned by `GET /health`) — an i18n catalogue key for the localized display
  of a static component note (absent for interpolated notes; render `note`
  verbatim). `enums` and `ws` are byte-identical to 0.1.53, so there is no
  event/bridge impact. The client validates `Health` as a whole
  (`system.get_health`) and passes it through untouched, so Pydantic surfaces
  the field automatically; the API-version guard accepts 2.18.0 unchanged.

# Version 2026.7.6 (2026-07-12)

Sysvar/program → device attachment: the daemon (v0.36.0) now resolves which
device channel a CCU system variable or program belongs to (explicit CCU WebUI
channel assignment "Kanalzuordnung", or a device identifier matched in the
name) and ships it as two optional fields — `channel` (canonical `"ADDR:idx"`)
and `device_address` — on the REST summaries and the WS broadcasts. This
release surfaces the link end-to-end so HA consumers can route the entity's
`device_info` to the physical device instead of the central hub device.

- Feat: **`Sysvar` / `Program` wrappers expose the device link.** New
  properties `channel_address` (canonical `"ADDR:idx"`, `None` when the
  entity belongs to no device — empty strings normalize to `None` too),
  `device_address` (device part, for device grouping) and `channel` (the
  linked `Channel` resolved from the store graph, `None` when unlinked or
  not loaded). New store lookup `get_channel_by_address` resolves a
  canonical channel string defensively (malformed/unknown → `None`).
- Feat: **live pushes carry the link.** `SysvarChangedPayload` /
  `ProgramExecutedPayload` now include `channel` + `device_address`;
  `apply_sysvar_changed` folds them into the summary on every push (absent =
  unlinked, so a removed channel assignment propagates live), and
  `apply_program_executed` folds a _present_ link into the program summary
  (absence is ambiguous on that event — daemon hub model may not be loaded —
  so it never clears a known link; the next catalogue refresh is
  authoritative for unlinking).
- Feat: **compat hub twins route `device_info` to the device.** The
  `Sysvar*`/`Program*` twins' `channel` property (previously a hard `None`)
  resolves the linked store channel, so `homematicip_local`'s
  `_get_device_info` walks `channel.device.identifier` and attaches the
  entity to the physical device — mirroring aiohomematic's
  `channel_lookup.identify_channel` behaviour. Hub singletons (no wire
  summary) keep returning `None` and stay on the hub device.
- Fix: **security — `find_free_port` probes on loopback** instead of a
  transient wildcard bind (`0.0.0.0`), closing GitHub code-scanning alert #2
  (`py/bind-socket-all-network-interfaces`). The result (a free port number)
  is unchanged.
- Chore: **bump `openccu-loom-types` to 0.1.53** (daemon API 2.17.0,
  regenerated from daemon v0.36.0) — carries the new `channel` /
  `device_address` fields on `SysvarSummary`, `ProgramSummary` and the two
  hub WS payloads.
- Chore: bump `aiohomematic` to 2026.7.6 (floor in `pyproject.toml`, CI pin
  in `requirements.txt`; drift-guard suite passes unchanged) and `prek` to
  0.4.9.

# Version 2026.7.5 (2026-07-10)

Dependency maintenance: follow aiohomematic's protocol-surface consolidation.

- Chore: **bump `aiohomematic` to 2026.7.5** (floor in `pyproject.toml`, CI pin
  in `requirements.txt`). Upstream folded the 19 fine-grained `Channel*` /
  `Device*` facet protocols (plus `PayloadProtocol`) into the aggregates —
  `ChannelProtocol` now carries its members directly and `DeviceProtocol`
  inherits only `DeviceIdentityProtocol` + `DeviceChannelAccessProtocol`. The
  drift-guard snapshot in `tests/compat/test_aiohomematic_protocol_parity.py`
  is updated to the new 30-protocol surface. The four protocols the compat
  twins must satisfy (`GenericDataPointProtocol`, `CustomDataPointProtocol`,
  `GenericSysvarDataPointProtocol`, `GenericProgramDataPointProtocol`) are
  unchanged, so no twin follows — all parity cases pass as-is.
- Chore: bump `ruff` to 0.15.21 (pre-commit hook rev + pinned lint
  requirement).

# Version 2026.7.4 (2026-07-07)

Reconnect/reload robustness: fail fast on an incompatible daemon and stop a
replay-lost / queue-overflow burst from storming the log and the store.

- Feat: **hard daemon API-version guard at `connect()`.** `HttpTransport`
  now compares the daemon's `/info` `api_version` against the
  `openccu-loom-types` `DAEMON_API_VERSION` and raises `LoomTransportError`
  when they are incompatible (needs the same major and a minor ≥ the one the
  installed types were generated against). Previously `api_version` was only
  logged; an incompatible daemon (e.g. a version skew after a daemon update)
  half-initialized the client instead of failing cleanly, which downstream
  manifested as bootstrap/dispatch failures and event storms. Skipped when
  either version is absent or unparsable; the `schema_digest` handshake
  still warns on build drift within a compatible API line.
- Fix: **WS envelope-queue overflow no longer re-fires the resync per dropped
  event.** An `_overflowing` latch forces exactly one resync per overflow
  episode; the consumer clears it once the queue drains below the
  low-watermark. The client's "re-bootstrap already in progress" de-dup log
  drops to DEBUG. A sustained flood previously emitted thousands of warnings
  per second.
- Fix: **replay-lost re-bootstrap cooldown.** `_on_replay_lost` — the single
  funnel for both the daemon's `replay_lost` frame and the queue-overflow
  resync — now drops a trigger arriving within 30 s of the previous
  re-bootstrap finishing, so a burst collapses to at most one snapshot walk
  per (walk duration + cooldown) instead of re-walking back-to-back.
- Chore: raise the `aiohomematic` floor to `>=2026.7.1` to match the
  CI-pinned version in `requirements.txt`.

Security-hardening pass (findings from a multi-agent audit against the
"compromised daemon / MITM" client threat model, each adversarially verified).
No wire-contract or public-API changes.

- Fix: **auth credentials no longer leak through a dataclass `repr`.**
  `BasicAuth` / `BearerAuth` / `SessionAuth` are declared `repr=False` and
  inherit a redacting `AuthMethod.__repr__` that delegates to `identity_hint`,
  so the plaintext password / token / session cookie can no longer surface in
  a debug log, an exception traceback capturing locals, or an HA diagnostics
  dump that recurses into `LoomConfig.auth`.
- Fix: **HTTP transport refuses daemon-controlled redirects.** Both the JSON
  request path and `request_bytes` pass `allow_redirects=False`; the daemon
  contract has no redirects, and aiohttp would not strip the manually-set
  `Authorization` / `Cookie` header across a cross-origin hop — so a hostile
  3xx could otherwise exfiltrate the credential or steer the client at an
  internal endpoint (SSRF).
- Fix: **binary downloads are size-capped.** `request_bytes` streams with a
  running byte tally and aborts past `max_bytes` (default 512 MiB) instead of
  buffering an unbounded body, so a daemon streaming forever within the
  per-chunk read timeout can no longer drive the host to OOM.
- Fix: **malformed WS frames are logged truncated.** The rejected-envelope
  warnings now emit a length-bounded repr (`_short_frame`) like the sibling
  non-JSON paths, so a peer cannot amplify near-`max_msg_size` frames into
  unbounded log volume. An explicit 1 MiB `max_msg_size` caps each WS frame
  (aiohttp defaults to 4 MiB).
- Fix: **the WS envelope queue is bounded by bytes as well as item count.**
  A running `_queued_bytes` tally forces the same drop-and-resync once the
  aggregate `_ENVELOPE_QUEUE_MAX_BYTES` (64 MiB) ceiling is crossed, so a
  burst of large valid frames during a slow re-bootstrap can't grow transient
  memory toward multi-GB before the item-count cap engages.
- Fix: **the store caps net-new devices.** `load_snapshot` and the live
  `device.created` push refuse a net-new address once `max_devices` (20000,
  far above any real CCU) is reached — updates to known devices are never
  refused — so a daemon streaming unique addresses can't grow the store
  without bound.

# Version 2026.7.3 (2026-07-07)

Robustness-hardening pass across the transport, store, event, and compat
layers (findings surfaced by a multi-agent review and adversarially
verified). No wire-contract or public-API changes.

- Fix: **live wrappers are now updated in place on every (re)bootstrap,
  never rebuilt.** `attach_channel_data_points`, `_upsert_channel` and
  `attach_custom_data_points` reuse the existing `DataPoint` / `Channel` /
  `CustomDataPoint` instance and replace its summary, matching the
  never-rebuild-on-update discipline the sysvar/program paths already
  follow. Rebuilding orphaned the reference HA held, so after a
  replay-lost re-bootstrap every pre-existing entity silently froze at its
  snapshot value. Calculated DPs (attached by a separate start-time path)
  are now tracked and no longer collaterally dropped by the generic
  per-channel re-attach.
- Fix: **REST refreshes clear the optimistic `_value_override`** (as the
  push path already did) so a freshly-fetched daemon value is no longer
  masked by a stale HA-written one; `refresh_data_point` also guards the
  dp-existence and non-dict-body cases before validating.
- Fix: **CDP refresh lost-update guard.** `refresh_custom_data_point`
  captures a per-CDP apply generation before its GET and drops the now-stale
  REST snapshot if a live `state_changed` push landed during the round-trip.
- Fix: **week-profile data points are purged on device removal** (were a
  slow leak across unpair events).
- Fix: **`request_bytes` no longer inherits the 30 s total timeout** —
  backup/capture archive downloads use a per-chunk read timeout with no
  total cap (optional `total_timeout_seconds` override), so a large
  transfer over a slow link is no longer guaranteed to time out.
- Fix: **`HttpTransport.connect()` no longer leaks the aiohttp session** on
  a failed capability handshake (only an owned, just-opened session is
  closed; a caller-supplied one is left intact).
- Fix: **`LoomCentralAdapter.start()` / `validate_config_and_get_system_information()`
  tear the client down on partial-init failure** instead of orphaning the
  session and the WS reader / dispatch / reconcile tasks across HA setup
  retries.
- Fix: **WS reconnect backoff only resets after a healthy connection** — an
  accept-then-immediately-close daemon no longer causes a 0.5 s reconnect
  storm.
- Fix: **WS handshake auth rejection (401/403) stops the reconnect loop**
  (and fires `on_auth_failed`) instead of spinning forever against a dead
  credential.
- Fix: **an unknown WS envelope `kind` is coerced to the default
  live-update kind** rather than dropping the whole frame, mirroring the
  graceful unknown-`type` degradation (forward compatibility with newer
  daemons).
- Fix: **the hub message-list refresh is serialised per singleton** so the
  300 s reconcile loop and a count push can't interleave a fetch/apply and
  regress the list.
- Fix: **`batch_read` skips a malformed result item** instead of discarding
  the whole batch (matching the documented per-item error contract).
- Fix: **free-form identifiers are percent-encoded into URL path segments**
  (sysvar names, CDP names/operations), so reserved characters can't inject
  path or query structure.

# Version 2026.7.2 (2026-07-07)

- Chore: **bump `openccu-loom-types` to 0.1.51 (daemon API 2.15.0).**
  Daemon 0.27.2 added the required `addon_build` field on `GET /info`
  (the only wire change since 2.14.0); the `/info` test fixtures carry
  the field now. This also repairs the `pyproject.toml` pin, which the
  Dependabot merge (#47) — branched before #45 — had downgraded from
  0.1.50 back to 0.1.46 (API 2.6.0), reintroducing the `schema_digest`
  mismatch #45 had fixed. `requirements.txt` moves to types 0.1.51 and
  `aiohomematic` 2026.7.1.

# Version 2026.7.1 (2026-07-04)

- Chore: **bump `openccu-loom-types` to 0.1.50 (daemon API 2.14.0).**
  The pinned types package had trailed the daemon by ten minor API
  revisions (2.4.0, digest `72a7c0…`); the client's capability
  handshake logged a `schema_digest` mismatch against every current
  daemon. The pin now matches the daemon's exported schema digest
  (`b01d74…`), silencing the warning. No wire-shape changes were
  required — every path and event the client uses was already valid.

# Version 2026.6.25 (2026-06-28)

- Feat: **wire the daemon's `model_icon` into HA's device-icon handler.**
  `openccu-loom-types` 0.1.44 ships the eQ-3 icon filename per device on
  `DeviceSummary.model_icon` (the daemon resolves the model→icon mapping
  server-side). New `Device.icon` surfaces it (mirroring aiohomematic's
  `Device.icon` / `DeviceIdentityProtocol.icon`), and
  `ccu_translations.get_device_icon(model=...)` — which previously always
  returned `None` — now reads a process-wide, central-independent
  `model → filename` lookup that `build_configurable_devices` refreshes from
  the live store on every config-panel listing. Together with the new
  `LoomConfig.create_central_url()` (scheme + host + port, no API path) this
  completes the icon proxy path the HA integration drives.
- Fix: `LoomStore.apply_device_created` seeded its stub `DeviceSummary`
  without the fields 0.1.44 made required (`updatable`, `update_available`,
  `master_pushes_config_pending`, `has_sub_devices`), which raised a
  `ValidationError` on every `device.created` broadcast — freshly paired
  devices now register again. The stub also adopts the payload's new
  `central` field.
- Change: drop the global install-mode REST methods (`HubOperations`
  `get_install_mode` / `set_install_mode`). The daemon removed the global
  `GET`/`POST /install-mode` endpoints — install mode is per-interface
  (`/install-mode/interfaces`, already covered) or per-device — and
  `openccu-loom-types` 0.1.44 removed the `InstallModeState` model.
- Bump `openccu-loom-types` 0.1.29 → 0.1.44 and `aiohomematic`
  2026.6.5 → 2026.6.8.

# Version 2026.6.24 (2026-06-23)

- Fix dependency version of aiohttp, pydantic

- # Version 2026.6.23 (2026-06-23)

- Fix dependency version of aiohomematic

# Version 2026.6.22 (2026-06-21)

- Feat: **expose the aiohomematic device-level service surface on the loom
  backend**, so the HA integration's raw device service handlers dispatch
  unchanged. New lazily-built `Device.client` shim (`set_value`, `get_value`,
  `get_paramset` / `put_paramset` — a peer-address `paramset_key` routes to the
  link-paramset surface —, `get_link_peers`, `add_link`, `remove_link`) over the
  existing data-point and link operations; `Device.channels` is now a
  mapping-like view (`.get("ADDR:1")` plus number-order iteration);
  `Device.week_profile_data_point` returns the adapter-built `WeekProfileDp`
  (now registered on the store at bootstrap) instead of `None`, unblocking the
  climate-schedule services; `Device.set_forced_availability` overrides the
  reported availability. aiohomematic-only knobs (`wait_for_callback`,
  `rx_mode`, `check_against_pd`, `retry`, `convert_from_pd`) are accepted and
  ignored — the daemon owns write serialization and value typing.
- Feat: wire the device/channel config-reload + export methods onto the model —
  `Device.reload_device_config()`, `Channel.reload_channel_config()` (over the
  v0.7.1 `DevicesOperations.reload_*` REST endpoints) and
  `Device.export_device_definition()` (new `DevicesOperations.export_device_definition`,
  `GET /devices/{address}/export-definition`, returning the raw zip archive).

# Version 2026.6.21 (2026-06-21)

- Feat: cover openccu-loom v0.7.1's REST config-reload endpoints —
  `DevicesOperations.reload_device_config(address)` (`POST
/devices/{address}/reload`) and `reload_channel_config(address, channel)`
  (`POST /devices/{address}/channels/{channel}/reload`), the surgical
  counterparts to `refresh_all`. Bumps `openccu-loom-types` 0.1.23 → 0.1.24.

# Version 2026.6.20 (2026-06-20)

- Feat: cover openccu-loom v0.7.0's device-action services. New operations:
  `SchedulesOperations.copy_schedule` / `copy_climate_profile` (copy a device
  schedule or a climate profile between channels/profile slots),
  `HubOperations.fetch_system_variables` (force a CCU system-variable re-pull),
  and `LinksOperations.create_central_links` / `remove_central_links` /
  `central_links_status`. The cdp device actions (climate away-mode, on-time,
  cover combined, siren, text-display) ride the existing
  `CustomDataPointsOperations.invoke`. WS-only daemon commands
  (`reload_channel_config`, `recording.*`) have no REST surface and are not
  wrapped by this REST-only client.
- Chore: dependency bump — `openccu-loom-types` 0.1.22 → 0.1.23 (the v0.7.0
  contract). The regenerated types renamed the WS-envelope `Kind` enum to
  `Kind1` (a new `Kind` scope enum took the name); imports were migrated to
  `Kind1 as Kind`, preserving the public `Kind` name.

# Version 2026.6.19 (2026-06-19)

- Chore: dependency bumps — `openccu-loom-types` 0.1.21 → 0.1.22, `ruff` 0.15.17 → 0.15.18 (pre-commit hook + pinned dev requirement), `pytest` 9.1.0 → 9.1.1.

# Version 2026.6.18 (2026-06-16)

- Chore: **adopt aiohomematic's lint suite**. New prek hooks — `pylint`, `kwonly-lint` (keyword-only enforcement), `lint-all-exports` (grouped/sorted `__all__` validation), plus `no-commit-to-branch` (main), `check-executables-have-shebangs` and `python-typing-update` (manual stage). `script/lint_kwonly.py` and `script/lint_all_exports.py` are ported from aiohomematic and adapted to loom: the export linter treats `openccu_loom_client` **and** the sister `openccu_loom_types` wire-type package as first-party re-exports, collects only top-level names (skipping Protocol members and `TYPE_CHECKING`-guarded imports), and supports `__version__`/`__all__: Final`. `pyproject.toml` aligns with aiohomematic (ruff `line-length` 120, `RUF022` ignored on package `__init__.py`, targeted pylint disables for the documented private-access / registration-mixin / Protocol-stub patterns).
- Chore: enforce keyword-only parameters across the public surface (≈120 signatures + their call sites) and group/sort every package facade's `__all__`.
- Fix: `make_sysvar_data_point` called `set_enabled_default()` positionally — the categorised sysvar entity now passes `enabled=` (the keyword-only signature), so sysvar `enabled_default` is applied instead of raising at spawn time.

# Version 2026.6.17 (2026-06-14)

- Feat: **stop client-side sysvar/program marker filtering — read `enabled_default` from the daemon**. The daemon (api ≥ 1.9.0) now applies the marker + internal inclusion filter and resolves the enabled-by-default flag itself, and strips the markers from the description before sending. The client therefore renders every sysvar/program the daemon sends (minus its own hard exclusions — `${…}`, `OldVal`/`pcCCUID`, fixed IDs 40/41) and reads `enabled_default` off `SysvarSummary`/`ProgramSummary` (absent → disabled, for older daemons). Removed `resolve_hub_inclusion` and the `sysvar_markers`/`program_markers` parameters from `CentralConfig` / `LoomCentralAdapter` / `_HubCoordinator` (any markers the integration still passes are absorbed and ignored), plus the now-unused `_HubCoordinator._is_internal`. Requires `openccu-loom-types==0.1.20`.

# Version 2026.6.16 (2026-06-14)

- Feat: **`list_ccus` config-flow helper** — `compat.aiohomematic.central.list_ccus(host, token, port, tls, …)` connects to a daemon, reads `GET /system/ccu` and returns a plain-dict projection (`name`, `serial`, `host`, `model`, `available`). The Home Assistant config flow uses it for mDNS-discovered daemon setup: after the user supplies the bearer token, it lists the daemon's CCUs so the user picks one (name/serial) instead of typing an instance name. Raises `LoomAuthError`/`LoomTransportError` on bad token / unreachable host (mapped to invalid_auth/cannot_connect by the flow); always closes the client.

# Version 2026.6.15 (2026-06-14)

- Fix: **per-channel schedule switches follow the WEEK_PROFILE channel, not the absence of a climate CDP**. A device may carry a climate CDP and still expose its schedule on a dedicated `*_WEEK_PROFILE` channel (HmIP-WGTC): such a schedule is a plain (non-climate) week profile and must spawn its `ScheduleChannelSwitch`es. `_bootstrap_schedules` now gates the switches on the presence of a WEEK_PROFILE channel (`with_switches=week_profile_channel_no is not None`) rather than `climate_cdp is None`; the schedule channel still falls back to the climate CDP's own channel when no WEEK_PROFILE channel exists (climate path, no switches — unchanged). Pairs with the daemon-side normalize pass that detaches the climate week profile from `WEEK_PROGRAM_CHANNEL_LOCKS` devices so the switch-bearing schedule is built. Surfaced by the three-way godevccu parity e2e harness.

# Version 2026.6.14 (2026-06-13)

- Fix: **per-channel schedule switches are named after their target channel** — e.g. "Schedule SHUTTER_VIRTUAL_RECEIVER" instead of the bare "Schedule", matching the direct-CCU twin. The daemon now ships the channel-lock key → actuator-channel mapping in the week-profile response (`available_target_channels`, api 1.7.0); `ScheduleChannelSwitch.name_data.channel_name` is sourced from it (was hard-coded `None` because the mapping was previously not on the wire). Falls back to the bare schedule name on daemons older than api 1.7.0. Requires openccu-loom-types ≥ 0.1.19. Surfaced by the three-way godevccu parity e2e harness.

# Version 2026.6.13 (2026-06-13)

- Fix: **calculated data-point names** now match the direct-CCU twin (e.g. "… Dew Point", "… Intrusion Alarm" rather than "… DEW_POINT", "… INTRUSION_ALARM"). The calc DPs already exposed the daemon's locale-aware label via `translated_name`, but their `name` still fell back to the generic surface's `parameter_label or parameter` (raw, since calc DPs carry no `parameter_label`). The HA integration builds the entity description's `name` (hence HA's resolved `super().name`) from `name_data.name`, then re-injects it into the composed entity name — so the raw parameter leaked back into an otherwise-correct name. `_CalculatedKeyMixin` now sources `name` from the daemon's `translated_name`, falling back to the raw parameter only when the daemon ships none. Surfaced by the three-way godevccu parity e2e harness.

# Version 2026.6.12 (2026-06-13)

- Fix: **switch state robustness** — `CustomDpSwitch.value` / `is_on` fall back to the CDP channel's generic `STATE` data point when the daemon's CDP `state` dict carries no `is_on`, mirroring the climate `current_temperature` field-DP fallback (`_generic_channel_value`, now shared on `_CustomEntitySurface`). The refresh bridge already pings the channel's custom data point on every member `datapoint.value_changed` (`on_value` → `get_custom_data_point_by_channel`), so the HA switch re-renders on a ch-`STATE` event and reads the freshly-observed wire value even if a `custom_data_point.state_changed` for the CDP is delayed or missing — the failure mode seen on channel-group switches (HMIP-PS/PSM, `STATE@3`/`@4`/`@5`) before the daemon-side wire-name fix. An unobserved `STATE` DP reads `None`.
- Note: cover/lock/dimmer CDPs keep reading purely from the daemon `state` dict — their generic-DP → state-key mapping (LEVEL scaling, multi-DP composition) is not a risk-free 1:1 like the switch's `STATE` → bool, and the daemon now emits `custom_data_point.state_changed` for every CDP, so no client fallback is warranted there.

# Version 2026.6.11 (2026-06-12)

- Feat: **HA sub-device support** — the domain model exposes the channel-group surface the HA integration's `sub_devices_enabled` option consumes: `Channel.group_no` / `is_group_master` / `is_in_multi_group` / `room` (daemon api 1.6.0 fields), `Channel.group_master` (aiohomematic-shaped view: `name` with `ChannelNameData` strip semantics, `room`, `group_no`) and a real `Device.has_sub_devices` (≥ 2 multi-member channel groups, aiohomematic counting). Daemons older than api 1.6.0 degrade gracefully (no groups → no split).
- Fix: `query_facade.get_un_ignore_candidates` is now **synchronous** with the `include_master` keyword (aiohomematic signature) and serves a cache prefetched during central start — the old async shape crashed HA's advanced-settings options step (where `sub_devices_enabled` lives) for the loom backend.
- Chore: require openccu-loom-types 0.1.18.

# Version 2026.6.10 (2026-06-12)

- Feat: calculated/combined data points consume the daemon's locale-aware `translated_name` (api 1.5.0) — resolves the last 10+2 friendly-name diffs vs the reference twin ("Taupunkt", "Zeitdauer"); the suppressed calculated `DURATION` entry donates its label to the combined number.
- Chore: pin `openccu-loom-types==0.1.17`.

# Version 2026.6.9 (2026-06-12)

- Fix: `SystemInformation` carries `ccu_type` (defaults to `CCUType.OPENCCU`) — the HA hub-update entity branches on it for the release-notes URL and crashed with `AttributeError`, so the system-update entity never spawned on the loom backend.

# Version 2026.6.8 (2026-06-12)

- Fix: **climate card temperatures** — `BaseCustomDpClimate.current_temperature` / `target_temperature` / `current_humidity` fall back to the CDP channel's generic data points (`ACTUAL_TEMPERATURE`, `SET_POINT_TEMPERATURE`, `HUMIDITY`) when the daemon's CDP state dict does not carry them (it ships only hvac/preset/action); unobserved DPs read `None`. The refresh bridge additionally pings the channel's custom data point on every member field-DP `datapoint.value_changed` (aiohomematic re-renders CDP entities on field events), so the HA climate card updates live.
- Fix: **'none' preset** — the climate `profiles` tuple always carries `ClimateProfile.NONE`, inserted after the control-mode block (boost/comfort/eco/away) and before the week-program names, exactly where aiohomematic places it; an empty daemon list yields `('none',)`.
- Fix: **combined duration scope** — the seconds-typed combined number spawns only on channels hosting a plain siren CDP that carries the `DURATION_VALUE`/`DURATION_UNIT` pair (aiohomematic's only _visible_ combined timer is `CustomDpIpSiren._dp_duration`; sound players declare the pair invisibly). Previously every channel with the pair spawned one (50 surplus numbers vs the ccu twin's 2).
- Fix: **schedule discovery** — week-profile/schedule entities require a custom data point on the device (aiohomematic initialises week profiles only through CDPs; drops the HmIP-MIO16-PCB's 24 surplus switches + surplus sensor); climate devices, which have no `*_WEEK_PROFILE` channel, probe `GET …/week_profile` on their climate CDP's channel (404 tolerated) and spawn the `WeekProfileDp` sensor — but never `ScheduleChannelSwitch`es, matching aiohomematic's climate path (+16 week-profile sensors for eTRV/BWTH/STHD in the live comparison). Bootstrap now loads CDPs before schedules/combined numbers.
- Fix: **foreign-central leak** — `LoomStore._infer_central_id` no longer adopts the snapshot's first interface `central_id` blindly: it picks the candidate equal to the configured central name, falls back to the single unique candidate, and stays unset (with a warning) when the list is ambiguous. In the live multi-central deployment the store carried the _other_ central's id, so `_matches_central` accepted both and 39 foreign sysvars/programs plus 2 foreign connectivity sensors spawned.
- Fix: **aiohomematic display-name schema** (new `compat/aiohomematic/model/naming.py`) — generic and custom data points now build their `translated_name` exactly like aiohomematic's `model/support.py`: generic DPs compose the (possibly user-renamed) CCU channel name, the daemon's locale label (suppressed on `label_omitted`) and the ` chN` postfix when the parameter spans several channels ('Belüftungsanlage Schaltzustand ch2', 'Lüftung Hoch ch18'); custom DPs derive `ch<no>`/`vch<no>` markers from aiohomematic's `DeviceProfileRegistry` primary channels keyed by the _resolved_ category (the device's only primary channel collapses to the device name, secondaries read 'vch4'/'vch5', multi-primary actuators 'ch6'/'ch10'…), renamed channels keep their custom name, and button locks render their postfix through `name_data.parameter_name` so HA's translation replace applies. Event groups now carry the channel-derived name (`ChannelEventGroup.name` semantics: 'Galerie aus' instead of 'Galerie keypress'). Resolves 239 of the 277 live friendly-name diffs against the ccu twin.
- Feat: **locale plumbing** — `CentralConfig(locale=…)` threads HA's UI language onto the store; `Device.config_provider.config.locale` reads it back (was hard-coded "en"), so the HA week-profile sensors render the localized schedule name ('Zeitplan' instead of 'Schedule') once the integration passes `locale=hass.config.language` to the loom `CentralConfig`.
- Known daemon-side rest classes (documented, not client-fixable): calculated-DP display names lack a locale translation on the wire (ccu: 'Fensterzustand'/'Rauchalarm', 10 entities) and the combined duration number's 'Zeitdauer' label is equally locale-bound (2); three `WATER_SWITCH_WEEK_PROFILE` channels (HmIP-WSM/ELV-SH-WSM) 404 on `GET …/week_profile` (3 missing week-profile sensors + 9 switches); the daemon's `schedule_enabled` channel keys differ from aiohomematic's actor/sub-channel map for HmIP-MP3P and HmIP-WRC6-230 (2 surplus / 8 missing switch keys).

# Version 2026.6.7 (2026-06-12)

- Feat: **hub singletons** — the loom backend now spawns aiohomematic's per-central hub entities: alarm-messages, service-messages and inbox count sensors (with `alarm_N`/`message_N` attributes), the metrics diagnostics (system health %, connection latency ms, last event age s — `None` until the daemon observed them), one connectivity binary sensor per interface (`connectivity-<slug(interface_id)>`), the CCU system-update entity (`POST /system/update/install`) and the per-interface install-mode sensor/button pairs (`install_mode` pseudo-address, slugs `hmip`/`bidcos` + `*-button`; `hub_coordinator.install_mode_dps` now returns the real `InstallModeDpType` pairs). All unique_ids match aiohomematic's registry format; values are polled every 30 s via `hub_coordinator.fetch_hub_singleton_data` and changed singletons ping their keyed `DataPointStateChangedEvent`.
- Feat: **schedule layer** — channels whose type ends in `WEEK_PROFILE` spawn a `WeekProfileDp` sensor (uid `loom_week_profile_<addr>_week_profile`; value = active entry count, climate counts the active profile's periods, simple schedules their entries; attributes from the daemon's week-profile descriptor) plus one `ScheduleChannelSwitch` per `schedule_enabled` key (uid `loom_schedule_channel_switch_<addr>_schedule_channel_lock_<key>`, disabled by default) toggling the week-program participation via `PUT …/week_profile/channel-locks/{key}` with optimistic state.
- Feat: **combined duration number** — channels carrying both `DURATION_VALUE` and `DURATION_UNIT` get one seconds-typed number (uid `loom_combined_<addr>_<channel>_duration`; reads convert through the unit factor s/min/h, writes pin the unit to seconds then send the integer value). The daemon's calculated `DURATION` sensor is suppressed (the ccu twin has none).
- Feat: **operations** — `system.get_system_update` / `system.install_system_update` / `system.get_hub_metrics`, `hub.list_install_mode_interfaces` / `hub.set_install_mode_interface`, `schedules.set_channel_lock`.
- Fix: `hub.list_inbox` returns the daemon's list shape (`list[dict]`) — the previous `dict()` coercion raised on a non-empty inbox.
- Feat: `Device.config_provider` exposes the minimal `config.locale` surface the HA schedule entities read.
- Chore: pin `openccu-loom-types==0.1.16` (daemon api 1.4.0: hub singleton + schedule channel-lock contract); `_HubEntitySurface` extracted to `model/hub/_surface.py`.

# Version 2026.6.6 (2026-06-11)

- Fix: **sysvar hard exclusions** — names carrying `OldVal`/`pcCCUID` (CCU calculation scratch values, hub.py `_EXCLUDED`) and the fixed CCU IDs 40/41 (alarm/service messages; dedicated hub singletons) never spawn generic sysvar entities, mirroring the reference stack.
- Chore: pin `openccu-loom-types==0.1.15` (`SysvarSummary.vid`, first stamped schema digest, daemon api 1.3.0).

# Version 2026.6.5 (2026-06-11)

- Feat: **usage=event filter** — physical devices' PRESS\_\* parameters (daemon verdict `usage=event`) no longer spawn generic button entities; they surface through keypress event groups only, matching aiohomematic (212 surplus buttons in the HA parity run). Virtual remotes keep their buttons.
- Feat: **sysvar wire flags** — `SysvarSummary.is_internal` is preferred over the `${…}` name heuristic when classifying CCU-internal variables; `is_extended` spawns the writable entity flavour (switch/select/number/text) instead of the read-only default.
- Fix: **event-group suppression** — DPs with usage `no_create`/`ignored` (e.g. HmIP-PS\* click parameters via `IGNORE_DEVICES_FOR_DATA_POINT_EVENTS`) are excluded from `build_event_groups`; the reference stack never spawns events for them.
- Chore: pin `openccu-loom-types==0.1.14` (sysvar `is_internal`/`is_extended`, required `Info.schema_digest`).

# Version 2026.6.4 (2026-06-11)

- Chore: **exact types pin** — `openccu-loom-types` is now pinned `==` (deterministic resolution; every shipped client/types combination is a tested one). Bump the pin together with each types release.
- Feat: **schema-digest handshake** — `connect()` compares the daemon's `schema_digest` (`GET /api/v1/info`, daemon ADR 0028) against the value stamped into the installed `openccu-loom-types` package and logs a warning when the types were generated from a different daemon build; skipped silently when either side predates the digest.

# Version 2026.6.3 (2026-06-11)

- Feat: **hub layer** — sysvars and programs spawn HA entities. The bootstrap merges the complete catalogue via `GET /sysvars`/`GET /programs` (the snapshot's hub block only carries the daemon's first central), filters by central and skips CCU-internal `${…}` variables; ALARM/LOGIC sysvars read as binary sensors, everything else as read-only sensors (aiohomematic default mapping); LIST sysvar indices resolve to their option string. Hub data points ride along in the bootstrap `DataPointsCreatedEvent`.
- Feat: **marker-driven hub visibility + enabled_default parity** — `resolve_hub_inclusion` mirrors aiohomematic's `_resolve_sysvar_enabled_default` (description-prefix match); without markers everything non-internal spawns disabled-by-default (the ccu reference registers all sysvar/program entities with `disabled_by=integration`); CCU-internal helper programs (`prgEnergyCounter_…`, `is_internal`) never spawn; each program spawns both a button (execute) and a switch (active toggle). `CentralConfig` accepts `sysvar_markers`/`program_markers`.
- Feat: **per-device firmware updates** — one `DpUpdate` per updatable device (uid `loom_<address>_update`), HmIP ready/in-progress gating like aiohomematic, install via `POST /devices/{addr}/firmware/update`; `Device` gains `available_firmware`/`firmware_update_state`.
- Feat: **event-group entities** — announced with the bootstrap batch, `loom_`-namespaced unique*ids (`loom_event_group*<type>\_<channel_uid>`), and the refresh bridge records every device trigger on the matching group (`last_triggered_event`) and pings its keyed `DataPointStateChangedEvent` so the HA event entity fires.
- Feat: **calculated data points** — fetched per channel from `GET …/calc-dps`, subclass the generic twins (uid prefix `calculated`, parity with aiohomematic's `calculated_<address>_<channel>_<parameter>`), live in the regular `(address, channel, parameter)` map so `datapoint.value_changed` pushes route to them; unobserved reads `None`.
- Feat: **channel-group CDP support** — the daemon disambiguates colliding wire names as `PARAM@<channel>`; group members no longer collapse to the last list entry (a dimmer spawns all three lights), names round-trip through invoke/refresh, and custom DPs derive their display name from the CCU channel name (primary → device name, virtual members → `vch5`/`vch6`).
- Feat: **usage verdict** — the daemon ships its visibility verdict on `DataPointSummary.usage`; generic spawn paths skip `no_create`/`ignored` (same gate as the MQTT discovery plane), removing the surplus SECTION/ACTIVITY*STATE/climate-internal/press*\* entities versus the ccu twin.
- Fix: climate `modes`/`profiles` return real aiohomematic `ClimateMode`/`ClimateProfile` members (HA reads `.value`; bare strings crashed entity setup and every climate came up unavailable).
- Fix: capability aliases `brightness`→`dimmable` and `tones`→`acoustic` (dimmers rendered onoff-only; siren TONES feature).
- Fix: `Channel.name` exposed (the HA event entity reads it; an `AttributeError` killed the whole event-group dispatch batch).
- Fix: smoke sirens (`siren_smoke`) and sound players (`siren_sound`) class correctly; `/system/ccu` entry selected by central name (ccus[0] stamped the wrong serial in multi-central deployments).
- Chore: requires `aiohomematic>=2026.6.2` and `openccu-loom-types>=0.1.12`; several features need the redeployed daemon (`usage`, `PARAM@<channel>` names, hub-summary fields, custom-DP categories).

# Version 2026.6.2 (2026-06-11)

- Fix: **no value or CDP-state push ever reached Home Assistant** — the default WS subscriptions lacked the `datapoint.*` and `custom_data_point.*` topic prefixes, so every entity froze on its bootstrap value (sensors stuck at 0, climate at "off", locks at "unlocked"). Both prefixes are now subscribed by default.
- Fix: CDP operations without params (`turn_on`, `turn_off`, `lock`, …) failed with 400 "Invalid JSON: EOF" — the invoke path now always POSTs a JSON body (`{}` when empty).
- Fix: binary sensors built from ENUM parameters were inverted (a door `STATE` of `CLOSED` resolved to the truthy option string "CLOSED" → permanently "on"). `DpBinarySensor` now maps ENUM values to `bool` via aiohomematic's TRUE-value table (`CLOSED/OPEN` → `OPEN`, `DRY/RAIN` → `RAIN`, `STABLE/NOT_STABLE` → `NOT_STABLE`); unknown lists fall back to `bool(index)`.
- Fix: unobserved data points (`observed=false`) read `None` ("unknown" in HA) instead of the wire default 0/False — mirroring aiohomematic's `NO_CACHE_ENTRY` semantics.
- Fix: climate static data (`min_temp`/`max_temp`/`temp_step`/`hvac_modes`/`preset_modes`) and siren `available_tones`/`available_lights` are now read from the CDP `config` block (they never appear in the live `state` dict the compat layer previously read — HA saw `hvac_modes=[]`, 4.5/30.5 default bounds and no PRESET_MODE). `hvac_modes` guarantees at least `("heat",)`; `capabilities.profiles` aliases the daemon's `profile` flag.
- Feat: CDP state is seeded from the summary snapshot at bootstrap (daemon ≥ `fix/cdp-rest-state` includes it in `GET …/cdps`), so lock/climate/light entities start on the real state instead of class defaults until the first WS push.
- Feat: generic data points expose `translation_key` (parameter lower-cased, mirrors aiohomematic's `generate_translation_key`); button locks expose `data_point_name_postfix` "BUTTON_LOCK" so HA's entity-description registry applies the button-lock rule (entity_category=config, translation_key=button_lock).
- Feat: the central adapter warns when neither HA nor the daemon provides a CCU serial — canonical unique_ids for hub/internal/virtual-remote data points would carry an empty central-id slot (`loom__…`).
- Refactor: dropped the retired `aiohomematic_contract` package (aiohomematic 2026.6.2 reverted the contract extraction, #3221, and no longer pulls it in — every remaining import was a latent `ImportError`). New `openccu_loom_client.canonical` calls `aiohomematic.model.support.generate_unique_id`/`generate_channel_unique_id` directly (plain `central_id` wrapped into the `ConfigProviderProtocol` shape) and hosts the loom-specific wrappers (`canonical_unique_id`, `serial_suffix`, `hub_slug`, `LOOM_NAMESPACE`); `PROGRAM_ADDRESS`/`SYSVAR_ADDRESS` come from `aiohomematic.const`. Golden fixtures are vendored under `tests/fixtures/`; `python-slugify` is a direct dependency.
- Chore: requires `aiohomematic>=2026.6.2` and `openccu-loom-types>=0.1.10` (`CustomDPSummary.config`/`.state`).

# Version 2026.6.1 (2026-06-10)

- Fix: entity names collapsed to the device name for every data point. The compat surface returned `None` for `translated_name`/`translated_full_name`; it now reads the daemon's locale-aware `DataPointSummary.translated_name` (identical to the MQTT discovery `name`) and honours `label_omitted` — a "primary" parameter returns `None` so HA uses the device name alone, every other parameter gets its localised label (e.g. "Batterie", "Betriebsspannung", channel-aware `" chN"` suffix). Requires the daemon and `openccu-loom-types` that expose these fields.
- Fix: read-only data points were mis-categorised (e.g. a door contact `STATE`, a read-only ENUM, surfaced as a sensor instead of a binary_sensor). `make_generic_data_point` now spawns the entity off the daemon's authoritative `DataPointSummary.category` instead of re-deriving it from `(type, operations)`; the heuristic `resolve_generic_class` remains the fallback only when the daemon omits the category.
- Fix: `Device.firmware` returned the raw `openccu_loom_types.rest.Firmware` object, which Home Assistant rejected as a non-string `sw_version`. It now returns the installed version string (`Firmware.Current`, default `"0.0"`); the raw record is available as `Device.firmware_detail`. The dead `_firmware_str` helper (read the wrong, lower-cased attributes off the summary) was removed in favour of `Device.firmware`.
- Fix: climate and light custom entities failed to load on the loom backend. The compat `CustomDpIpThermostat` now exposes `_peer_level_dp`/`_peer_state_dp` (`None`; loom has no CCU link peers — `activity` already drives the HVAC action), and `CustomDpSoundPlayerLed`/`CustomDpIpFixedColorLight` expose `available_colors`/`color_name`/`channel_color_name` (`None` until the daemon surfaces colour state) — closing the aiohomematic surface the HA platforms read.
