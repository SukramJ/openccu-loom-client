# Version 2026.8.28 (2026-08-27)

Syncs the client to daemon 0.66.0 / api 7.17.0 (`openccu-loom-types` 0.5.8).
No client code had to change: the contract version moved two minors, but the
generated models moved by a single optional field, because the daemon ties its
`api_version` to its contract assets rather than to the size of the change they
carry. The schema digest matches the new daemon build exactly, so `connect()`
stays quiet about contract drift.

- **The device inbox gained a state this client cannot yet tell apart.** The
  daemon's new `awaiting_release` flag marks a device that is already accepted
  and fully materialised — it has its CCU ise*id, its channels and its data
  points, and can be renamed and assigned rooms — but is deliberately withheld
  from the ecosystems (MQTT and therefore Home Assistant, the Matter bridge,
  outbound webhooks) until an operator finishes onboarding it. Such entries are
  listed in the same inbox as the ones genuinely waiting for a decision, and
  the daemon's schema says a client must not offer \_accept* for them: they are
  already accepted, and what they need is the new release call.

  Nothing here reads the flag yet. Inbox entries are flattened into
  aiohomematic's record, which has no field to carry it, so Home Assistant is
  offered the same accept action for every entry alike — and there is no client
  call for the release route either. Closing that needs a change on the
  aiohomematic side as well, so it is recorded in `notes/open-work.md` rather
  than half-done here, together with the question to settle first: what the
  daemon actually does with an accept against a device that is already
  accepted.

- `ruff` 0.16.4 → 0.16.5 (hook pin and pre-commit requirement in lock-step).

# Version 2026.8.27 (2026-08-27)

Makes reconnect recovery complete. The backoff machinery was already right —
a 0.5/2/5/15/30 s ladder clamped at 30 s, a healthy-connection gate so
accept-then-drop still escalates, a 60 s inbound-ping deadline, one shared
deadline across REST retries, and a de-duplicated, rate-limited re-bootstrap.
Steady state against a dead daemon stays one TCP attempt every 30 s. Every
defect this release fixes was in what happens _after_ a successful reconnect.
The scenario matrix and the reasoning per fix are in
`notes/reconnect-recovery.md`.

- **The resume cursor never recovered from a daemon restart.** `_last_seq` only
  ever grew, and the `replay_lost` frame's anchor was logged and discarded. A
  restarted daemon begins its seq space at 0, so every live envelope then
  carried a smaller seq than the cursor kept from the previous incarnation and
  the cursor never moved again: each later reconnect re-sent that stale
  `since`, the daemon answered "lost", and the client re-walked the entire
  snapshot — one REST walk per device, per reconnect, indefinitely, where the
  daemon's replay buffer could have served it. The cursor now adopts the anchor
  the daemon names. The queue-overflow path deliberately does not touch it:
  overflow means this client fell behind, not that the daemon's seq space
  moved.
- **Nothing said that push had stopped.** A dropped socket is precisely what a
  daemon cannot announce, and the transport reconnects silently, so a consumer
  kept presenting its last values as live through any outage. Connection
  transitions are now published as `ConnectionStateChangedEvent`, readable as
  `LoomClient.connected`, and mapped onto `CentralState.Degraded` in the compat
  layer. Degraded rather than stopped, and `available` deliberately still
  answers True: a WS drop makes the store stale, not wrong — REST is very
  likely still reachable — and flipping every entity to unavailable on a
  five-second reconnect is worse than the staleness it reports.
- **A rejected credential ended the event stream in silence.** The transport
  stops reconnecting on 401/403, which is right — retrying a dead credential
  can only hammer the daemon — but nothing was wired to the callback that says
  so, and the dispatch loop simply ran out. Now published as `AuthFailedEvent`,
  with the log line naming what has to happen: re-provision, then call
  `start_events()` again.
- **A client that started before the daemon reached the CCU built an empty
  model, permanently.** `GET /snapshot` answers 200 with empty lists while the
  central is still in `waiting_for_ccu` and never 5xx, so the bootstrap
  "succeeded" and announced no entities. Two things were missing and both are
  here: `get_readiness()` / `wait_until_ready()` read the readiness record the
  daemon already served on `GET /system/ccu` (and `get_health()` the liveness
  probe it already served on `/health`), so the walk waits for the bring-up
  instead of paying for an empty one; and when the CCU arrives later, the
  daemon's resync push now reaches the compat layer through
  `set_rebootstrap_hook`, which rebuilds the custom data points, schedules,
  combined numbers and hub catalogue and re-announces. Previously the store
  refilled correctly while Home Assistant learned nothing until it was
  reloaded. Waiting is bounded, and a timeout is not an error: the walk runs
  anyway and the resync covers the late arrival — a daemon whose CCU never
  appears must not hold a consumer's setup open.
- **A daemon upgraded under a live connection went unnoticed.** The `/info`
  handshake ran once, at connect, so a mismatch first surfaced far from its
  cause as a validation error in whichever call met a reshaped payload. It is
  re-checked on every reconnect now, off the reader loop so the round trip
  cannot sit inside the inbound-ping deadline, and a `/info` that is merely
  unreadable keeps the previous handshake — a transient failure is not evidence
  of incompatibility.
- **`LoomIncompatibleVersionError`** separates "this daemon will never work
  with this build" from "the host is unreachable", which used to arrive as the
  same class. A caller retrying a failed setup can now tell a condition that
  clears on its own from one that clears only when somebody upgrades. It
  subclasses `LoomTransportError`, so existing handlers keep working.
- **Reconcile fan-out is bounded.** `device.created` events carrying
  `source == CACHE` — the daemon restoring its description cache at boot, a
  whole fleet at once — are skipped, as are devices the store already holds
  complete; whatever survives is capped at four concurrent reconciles. A daemon
  older than 0.65.3 sends no source, where nothing is skipped and behaviour is
  unchanged.
- **The reconnect ladder has jitter** (±20%). Clients that lived through one
  daemon outage returned in lockstep, and the instant they picked was the worst
  available: a restarting daemon is pulling the CCU exactly then.
- **A second `start_events()` no longer leaks its predecessor's
  subscriptions**, which had been applying every event to the store twice. The
  idempotence guard only holds while the dispatch task is alive — and the one
  path that ends it without `close()` is the credential rejection above, where
  calling `start_events()` again is the natural recovery.

Also in this release:

- Requires `openccu-loom-types` 0.5.7 (daemon 0.65.3 / api 7.15.0). Its schema
  digest matches that daemon build exactly, so `connect()` no longer warns
  about contract drift.
- `aiohomematic` is capped at `<2026.9` in `pyproject.toml`. The comment above
  the dependency and `CLAUDE.md` both said an upper bound was pinned in both
  files; only `requirements.txt` carried one, so a plain `pip install` resolved
  any later series against a shim that couples to aiohomematic internals.
- The package moves to Development Status 4 - Beta, with `README.md` and
  `CLAUDE.md` brought in line — they still said "WIP / Alpha".

# Version 2026.8.26 (2026-08-27)

Syncs the client to daemon 0.65.1 / api 7.13.0 (`openccu-loom-types` 0.5.5).
A small sync: the daemon release behind it is a Matter conformance fix, which
does not reach this client, and the generated types changed in one field's
description rather than in any shape. No client code had to change to keep
working.

- **`connection_latency` measures something different than it used to**, and
  its docstring now says what. The daemon fed that hub metric from a single
  JSON-RPC call on the reconciler's five-minute cadence — one one-way leg, no
  XML-RPC, no BIN-RPC, and not the callback path every event actually returns
  on. It now carries the round-trip of a matched PING/PONG pair over each
  interface's own transport, including the callback reply, on the 30-second
  connection-check cadence. The value a consumer reads is the same field with
  the same unit; what it stands for is no longer a fraction of the path it is
  named after.

# Version 2026.8.25 (2026-08-24)

Syncs the client to daemon 0.65.0 / api 7.12.0 (`openccu-loom-types`
0.5.4). Three daemon API bumps land at once — 7.10.0, 7.11.0 and 7.12.0 —
and the generated types grew by 71 models, almost all of them request and
response shapes that had been written inline in the daemon's OpenAPI paths
and were therefore invisible to every generated client. Nothing here had to
change for those: they were already reachable as plain dicts and are now
also reachable as types.

- **`Capability` names the daemon's capability tokens**, and
  `LoomClient.has_capability()` answers whether one is advertised. The
  tokens were bare strings before, in this package and in every caller.
  That is the shape of mistake nothing catches: a token is only ever
  compared, never parsed, so `required_capabilities=("alram.v1",)` raises
  "daemon is missing required capabilities" against every daemon that will
  ever exist, and reads like the daemon's fault. A name turns that into an
  `AttributeError` at the call site.

  The enum is a `StrEnum`, so a member and its wire string are
  interchangeable and code already carrying strings keeps working. It is a
  convenience for the tokens this client acts on, **not** an allowlist:
  the daemon may advertise tokens this package does not know, and
  `has_capability()` takes a raw string for exactly that case.

- **Four new tokens from api 7.12.0** — `mqtt.raw.v1`,
  `webhook.inbound.v1`, `diagrams.v1` and `admin.persistence.v1`. The last
  one is the one a caller of this package can act on today: `/users` and
  `/areas` are mounted with or without a database behind them, and without
  one every write is refused in a way a client cannot tell apart from a
  permission problem. `admin.persistence.v1` is that distinction.

- **A token means _configured_, not running.** The daemon pinned this
  meaning in 7.12.0 and it is worth repeating at this end: a briefly
  unreachable broker is not a missing capability. For what is working right
  now, read the daemon's `/health` components — they gained `security` and
  `discovery.mdns` in 0.65.0.

The alarm bootstrap now feature-detects through `Capability.ALARM` instead
of a hand-typed `"alarm.v1"`. Behaviour is unchanged; the string is gone
from the one place it was still spelled out.

# Version 2026.8.24 (2026-08-24)

Syncs the client to daemon 0.64.2 / api 7.9.0 (`openccu-loom-types`
0.5.3). The 7.7.0 → 7.9.0 line is additive and small on the wire, but
one of the two additions is a distinction the client could not make
before.

- **`config_admin.put_section` now documents `applied` and
  `apply_error`.** The ack used to say only that a section was stored.
  Storing and taking effect are two different facts, and until api 7.8.0
  the response conflated them: a `north.mqtt` save returned success while
  the running bridge kept the topic base and plane toggles it was built
  with. `applied` says which happened. False on its own is not a failure
  — most sections have no subsystem that can rebuild itself — but false
  _with_ an `apply_error` is the case a caller must not report as a plain
  success, because the daemon is still doing the old thing and only this
  field says so. Both keys are absent against a daemon below 7.8.0: treat
  a missing `applied` as unknown, not as False.

- **`diagnostics.get_wiring()` binds `GET /diagnostics/wiring`**, the
  seams a running daemon declared as it wired them. Each entry carries
  the collaborator, the boot marks it must precede or follow, what stops
  working when it is absent, and any ordering constraint that was
  already broken when it attached. That last field is the one worth
  having: the collaborator IS attached in the broken case, every other
  surface reports healthy, and nothing else can tell you the attach came
  too late. An empty list means the daemon wired none of them — a valid
  answer, not an error. A daemon below 7.7.0 answers 404.

Neither addition produced a generated model: both response schemas are
written inline in the daemon's OpenAPI paths rather than as named
components, and `openccu-loom-types` is generated from
`components/schemas` alone. Both operations therefore return plain
dicts, and the daemon-side gap is reported upstream.

# Version 2026.8.20 (2026-08-23)

Syncs the client to daemon 0.64.1 / api 7.7.0 (`openccu-loom-types`
0.5.2). The 7.1.0 → 7.7.0 line is additive: three new broadcasts, two
new backup endpoints, and four optional fields. The client binds all
three broadcasts — a broadcast nobody consumes is indistinguishable
from one the daemon never sends — and gains the daemon-liveness
singleton the hub aggregate now declares.

The 7.7.0 step was prompted by this sync: `display_value` turned out to
be on the wire but not in the contract, so no generated client could read
it. It was fixed on the daemon rather than patched around here.

## What's Changed

### Added

- **The daemon's own liveness is an entity.** `GET /hub/data-points`
  declares a `daemon_connection` singleton, so the binary sensor can be
  built — and named — without hard-coding a name the daemon owns. It is
  seeded true (a REST answer is itself proof the daemon runs) and flipped
  false by the `daemon_status.changed` broadcast a stopping daemon sends
  over the WebSocket, which is the distinction a WebSocket client
  otherwise cannot draw: until now a stopping daemon and a dropped
  connection looked identical. A killed daemon still cannot announce
  itself — detecting that stays the consumer's own job — and the next
  aggregate poll re-arms the sensor after a reconnect. Like the
  connectivity sensors it carries no availability of its own.
- **A renamed device is re-read live.** `device.metadata_changed` fires
  when a device or one of its channels is renamed, or its room / function
  assignment changes. The payload inlines no new values, so the bridge
  re-reads the device detail first and only then publishes
  aiohomematic's `DeviceLifecycleEvent(UPDATED)` — announcing before the
  re-read would hand the consumer the old name.
- **A week profile invalidates on change.** `schedules.changed` fires when
  a channel's schedule is written through this daemon or observed on the
  CCU. The device's `WeekProfileDp` reloads and the entity is pinged, so
  an edit made in the CCU WebUI reaches Home Assistant instead of waiting
  for the next read. Pushes for another channel, or for a device with no
  schedule entity, cost no fetch.
- **`get_storage_info()` and `delete_backup()`** on `backup`
  (`GET /backups/storage`, `DELETE /backups/{id}`, both admin). The
  archive directory is not derivable client-side: `backup.dir` is empty in
  the common case and a CCU add-on install resolves it per start from the
  CCU's own backup target. `available=False` says the daemon could not
  create the directory at all — an empty backup list then says nothing
  about the CCU.
- **`devices.get_device_icon()`** returns the model artwork as bytes, or
  `None` for a model that has none. `GET /devices/{addr}/icon` is
  authenticated since api 7.6.0 — the artwork is not sensitive, but the
  route answered differently for a known and an unknown address and was an
  unauthenticated existence oracle for the whole inventory. A consumer that
  handed the bare URL to a browser now needs either a same-origin session
  cookie or this method.
- **A stale classification index is visible.** The security severity
  sensor carries `index_healthy: false` while the daemon reports the
  snapshot was folded from an index it knows to be stale. Surfaced only
  while degraded: an attribute that is always present and always true
  stops being read. The zone/class pushes carry no verdict about the
  index, so their silence never clears a degradation the snapshot
  reported.

### Fixed

- **A pushed value no longer keeps the seeded `display_value`.** The
  daemon puts `display_value` (`value × multiplier`) on the REST
  data-point summary, so a summary carried forward with only the new
  `value` announced the bootstrap projection for the rest of the session
  — a dimmer stuck at "42 %" while the raw value moved on. The store now
  takes the projection from the push, where the daemon puts it too.

  That field was missing from the contract: the daemon had emitted it on
  the `datapoint.value_changed` broadcast since api 7.2.0 but declared it
  only on the REST half, so the generated payload type had no such field
  and this client could not read it. Fixed daemon-side in 0.64.1 (api
  7.7.0, SukramJ/openccu-loom#607) rather than worked around here — a
  client recomputing the daemon's own arithmetic is a second opinion
  waiting to disagree with it.

  An absent value means `value` already is the displayable number — a
  trivial multiplier, or a value no projection applies to — and is copied
  through as such. A projection of `0.0` is a reading and not an absence,
  so a dimmer at 0 % keeps rendering in percent. The raw `value` stays
  untouched either way, because the write path sends it back unchanged.

### Changed

- **Pinned `openccu-loom-types` to 0.5.2** (daemon 0.64.1 / api 7.7.0),
  carrying the refreshed `SCHEMA_DIGEST` / `DAEMON_API_VERSION` the
  transport checks at connect. The version gate means this client now
  requires a daemon at api ≥ 7.7.0, which is what lets it trust the
  push's `display_value` without a local fallback.
- The "no CCU serial" warning names both of its causes. Since api 7.6.0
  the CCU's network coordinates on `GET /system/ccu` — the serial among
  them — are admin-only, so a viewer/operator token reads them as empty
  strings, which is indistinguishable from a CCU the daemon has not
  reached yet. The serial is the central-id slot of every hub / internal
  / virtual-remote routing key, so a credentials question that presents
  as an outage costs the entity registry.

# Version 2026.8.19 (2026-08-18)

Syncs the client to daemon 0.63.0 / api 7.1.0 (`openccu-loom-types`
0.5.0). The api line moves 6.2.1 → 7.1.0, but the generated wire surface
barely moved: the major step was a specification correction on the daemon
side (`GET /diagnostics/capture` declared an object while it had always
answered an array), which does not touch any model this client consumes.
Four additions do.

- **A data point reports its real multiplier.** The compat layer returned a
  hard-coded `1.0`, so every data point whose CCU unit is `100%` rendered
  at a hundredth of its value in Home Assistant — a level of 42 % showed as
  0.42 %, and a written number went back unscaled. HA scales min, max, step
  and the value itself by this factor. The daemon now reports it on the
  data-point summary, and the compat layer passes it through; absent still
  means 1.
- **A CCU backup is named by the daemon.** `create_backup_and_download`
  takes the archive name from the backup entry when the daemon recorded
  one. That name is built from the CCU's hostname and firmware version at
  the moment the backup was taken, which is what the name is supposed to
  state; rebuilding it here reads the _current_ system information, so an
  archive downloaded after a firmware update claimed the new version. The
  local construction stays as the fallback for an older daemon.
- **`create_zone` accepts `AlarmZoneCreate`.** The daemon declares it as
  the `POST /alarm/zones` body: it omits `id`, which the server mints
  itself and ignores when sent. The full `AlarmZone` stays accepted.
- `MatterExposure` carries `device_type_label` and `EditSessionRequest`
  makes `token` optional — both flow through the models untouched.

### Fixed

- **The CCU's firmware version is no longer the daemon's build version.**
  `SystemInformation.version` was filled from `GET /info`, which reports
  the OpenCCU-Loom build, instead of the CCU entry on `GET /system/ccu`.
  Home Assistant showed that as the CCU device's `sw_version`, and the
  backup filename carried it too — `Otto-0.61.4-…` where
  `Otto-3.87.6.20260404-…` belongs. It now reads the CCU entry and falls
  back to the daemon's value only before the first successful CCU connect.

# Version 2026.8.18 (2026-08-17)

Syncs the client to daemon 0.61.4 / api 6.2.1 (`openccu-loom-types`
0.4.2). The 6.2.0 → 6.2.1 bump is a backward-compatible value
correction: `GET /hub/data-points` now reports
`HubInstallModeDataPoint.interface_id` as the wire id
`<central>-<interface>` (matching the sibling
`HubConnectivityDataPoint.interface_id` and `GET /interfaces`) instead
of the bare interface name. The client already keyed its install-mode
index off `GET /interfaces` by that wire id, so this is the daemon side
finally lining up with what the client expected — the install-mode
sensors now seed on a cold start instead of staying blank until the
first pairing window. No client code change is required; the type is
unchanged (only its documentation) and the transport's major.minor
compatibility check still matches (6.2). Bumping the pinned types —
whose `SCHEMA_DIGEST` and `DAEMON_API_VERSION` the transport checks at
connect — is the only change the contract requires.

## What's Changed

### Changed

- **Pinned `openccu-loom-types` to 0.4.2** (daemon 0.61.4 / api 6.2.1).
  Carries the corrected `HubInstallModeDataPoint.interface_id`
  wire-id semantics + refreshed `SCHEMA_DIGEST` / `DAEMON_API_VERSION`.

# Version 2026.8.17 (2026-08-17)

Syncs the client to daemon 0.61.3 / api 6.2.0 (`openccu-loom-types`
0.4.1) and fixes the backup-download surface (#579). The 6.1.0 → 6.2.0
bump is a set of backward-compatible additions (the new optional
`SecuritySourceView.override_included` field, documented sysvar
404/409 responses, WS `args`), so bumping the pinned types — whose
`SCHEMA_DIGEST` the transport checks at connect — is the only change
the contract itself requires.

## What's Changed

### Fixed

- **`LoomCentralAdapter.create_backup_and_download` now returns a
  `BackupData`** instead of the daemon's raw trigger dict (#579). HA's
  "create backup" button — and the backup agent, the update entity and
  the WS API — read `.filename` / `.content` off the result exactly as
  they do for aiohomematic's `CentralUnit.create_backup_and_download`,
  so the raw dict raised `AttributeError` on every press. The compat
  method now mirrors aiohomematic: it triggers the backup, waits for the
  archive to appear in `GET /backups`, downloads it, and returns
  `BackupData(filename, content)` (or `None` on failure/timeout). The
  filename follows aiohomematic's `hostname-version-timestamp.sbk` form.
- **`BackupOperations.list_backups` parses the array it receives.**
  `GET /backups` returns a JSON array of `BackupEntry`, but the method
  built `dict(payload)` over it — which raises on a non-empty list. It
  now returns `list[BackupEntry]`; the method had no callers, so the
  latent break surfaced only now that the backup flow polls it.

### Changed

- Pinned `openccu-loom-types` to `0.4.1` (regenerated from daemon
  0.61.3, api 6.2.0), so the connect-time `SCHEMA_DIGEST` check matches
  the daemon build.

# Version 2026.8.13 (2026-08-16)

Syncs the client to daemon 0.61.1 / api 6.1.0 (`openccu-loom-types`
0.4.0): the availability push reaches HA, the Matter diagnostics
surface is wrapped, the classify opt-in is available on the transport,
and the edit-lock and startup-capture calls follow the contract the
6.0.0 breaking bump finally declares.

## What's Changed

### Added

- **`DeviceAvailabilityChangedEvent`** — the daemon's
  `device.availability_changed` broadcast (api 5.27.0, riding the
  existing `device.{address}.lifecycle` topic) is bound end-to-end. The
  wire→store bridge flips the device summary's `available` flag
  (`LoomStore.apply_device_availability_changed`), and the compat
  refresh bridge fans one keyed `DataPointStateChangedEvent` out per
  generic and custom data point of the device — mirroring
  aiohomematic's `publish_device_updated_event(notify_data_points=True)`,
  since entities read `available` live off the store but only re-render
  on their own keyed ping. Until now a device that dropped out (or an
  interface that lost its CCU) kept its bootstrap availability until the
  next reconcile.

- **Matter diagnostics + maintenance operations** — the daemon's
  0.59.x/0.60.0 diagnostics build-out, wrapped 1:1 on
  `MatterOperations`: `list_sessions` (secure sessions + id-space
  occupancy, api 5.21.0/5.29.0), `list_endpoints` (the topology as a
  controller sees it), `get_mdns_diagnostics` (what mDNS announces and
  what would keep a controller from finding it), `get_compatibility`
  (fabric→ecosystem classification + findings; all api 5.22.0),
  `list_diagnostic_events` (the bounded pairing/session/discovery trace,
  api 5.33.0), `force_sync` (rebuild endpoints without touching a
  pairing) and `factory_reset` (api 5.31.0). `factory_reset` requires
  the caller to pass `confirm="remove-all-fabrics"` explicitly —
  deliberately not defaulted, matching the daemon's own guard against a
  replayed generic confirmation.

- **`WsTransport(classify=True)`** — opt into the daemon's inline
  classification (api 5.34.0): every subscribe frame carries
  `classify: true` and `datapoint.value_changed` payloads then include
  `category` / `data_point_type`. Default off; this client classifies
  through the store's data-point factories, so the flag exists for
  consumers reading the wire payloads directly.

### Changed

- **`SessionsOperations.release` / `.heartbeat` take `key` + `token`**
  (BREAKING for direct callers). `DELETE /sessions/edit` and
  `POST /sessions/edit/heartbeat` have always demanded both — api 6.0.0
  finally declares it, and the client now sends them. The edit-lock
  release inside `put_link_paramset` names its lock and proves ownership
  on the same call; without a token there is nothing to prove, so the
  lock is left to its 5-minute TTL instead of sending a release the
  daemon would refuse.

- **`SystemOperations.set_startup_capture` takes
  `StartupCaptureConfigWrite`** (BREAKING for direct callers). Api 6.0.0
  splits the startup-capture schema into an honest read shape (responses
  always carry the effective `anonymise`) and a write shape (an omitted
  `anonymise` means _true_, the privacy-preserving default).

- **Connectivity keys on the wire id, labels on the bare name.** Daemon
  0.61.1 fixes its side of the per-interface connectivity plane: the
  REST snapshot and the `connectivity.changed` push now carry the wire
  id `<central>-<interface>` — the form `GET /interfaces` reports and
  this client has always keyed its sensors by, so the lookup that never
  matched against pre-0.61.1 daemons matches now (no client code
  change; the test fixtures now pin the documented id form). The
  sensor's _display_ name switches to the bare interface name
  (`Konnektivität HmIP-RF`), matching the daemon's own MQTT discovery
  labels; the `parameter_slug` → unique_id keeps building from the wire
  id, so entity-registry ids stay put.

- **`openccu-loom-types` 0.4.0 / aiohomematic 2026.8.3.** The 0.3.17
  regeneration shifts the auto-numbered enum aliases: the `WsEnvelope`
  kind enum is now `Kind2` (`Kind1` names the new Matter
  diagnostic-event kind), so every `Kind1 as Kind` import moved to
  `Kind2 as Kind`. `MatterFabric` gains the daemon-resolved
  `vendor_name`, `InboxDevice` the `pending_creation` marker (api
  5.28.0 — the already-wrapped `DevicesOperations.accept_device`
  materialises such a device), the alarm-journal class enum gains
  `maintenance`, and `AlarmTriggeredPayload.mode` can now be `disarmed`
  for always-on triggers. 0.4.0 (api 6.1.0) adds the exact 64-bit
  `fabric_id_hex` / `node_id_hex` on `MatterFabric` (a JSON number
  carries 53 bits, so the numeric ids round for real controllers —
  render the hex fields) and documents that `CentralRow`'s connection
  fields are masked below the admin role — all consumed through the
  types with no code change.

# Version 2026.8.12 (2026-08-12)

Names the two entities a consumer builds beside an alarm panel out of
the daemon's catalogue instead of its own. No types change.

## What's Changed

### Added

- **`AlarmPanel.reset_motion_name` / `AlarmPanel.triggered_motion_name`** —
  the daemon's names for the motion-reset button and the latched-detector
  counter a consumer puts next to a panel, composed as
  `<zone> — <label>` exactly like `alarm_discovery.go` composes them for
  MQTT discovery. `None` when the catalogue was never read or does not
  carry the key, which tells the caller to fall back to its own wording.

  The words were in the daemon's i18n catalogue all along
  (`discovery.alarm_reset_motion`, `discovery.alarm_triggered_motion`)
  and `GET /i18n/entities` has served them since api 5.2.0 — but only
  the hub singletons read that route, so `homematicip_local` worded the
  same two entities a second time in its `strings.json`. That is the
  copy ADR 0046 exists to remove.

- **`LoomStore.entity_names` / `set_entity_names`** — the catalogue now
  lands in the store, not only on the singleton instances. Panels come
  and go: a catalogue reconcile re-seeds every one of them and an
  `alarm.panel_changed` push builds a zone created at runtime from a bare
  stub. Pushing names onto the instance would have to be repeated on both
  paths, so the panel reads them back instead — which also means a
  renamed zone renames its companions without a restart.

  `_HubCoordinator._apply_entity_names` fills it on the same read that
  names the singletons, one REST call for both, and still before
  `LoomCentralAdapter.start` announces the panels — the deadline that
  matters, since Home Assistant records an entity's name when the entity
  is added.

# Version 2026.8.11 (2026-08-12)

Completes the motion-reset surface from 2026.8.10 with the count that
belongs beside it. No types change.

## What's Changed

### Added

- **`AlarmPanel.triggered_motion_count`** — how many detectors a reset
  would clear, per zone, with the total on the master panel. It answers
  "why will this zone not arm" without a REST call of the consumer's own,
  and it is the state behind the counter entity `homematicip_local` puts
  next to the reset button
  ([#88](https://github.com/SukramJ/openccu-loom-client/issues/88)).

  Kept fresh by re-reading, not by a push, because there is no latch
  broadcast to subscribe to — `assets/wsapi.json` carries nine `alarm.*`
  events and none for this. `LoomClient.refresh_triggered_motion()` owns
  the read; `LoomStore.apply_triggered_motion()` is pure mutation, so the
  store stays I/O-free like every other `apply_*`.

  Bootstrap seeds the counts once. Without that, "no event yet" and
  "nothing latched" would be indistinguishable for as long as the alarm
  stayed quiet — which is exactly when someone consults the count.

  Every panel is written on each refresh, not just the ones the answer
  names: the endpoint reports what _is_ latched, never what stopped
  being, so skipping the absent ones would leave a cleared zone showing
  its last non-zero count forever.

- **The compat adapter schedules that re-read off the alarm events.**
  Subscribed to the three that can plausibly move a latch —
  `alarm.readiness_changed`, `alarm.triggered`, `alarm.panel_changed` —
  and deliberately not the countdown tick, which fires every second and
  never changes the latch set.

  It runs as one coalesced background task. The wire bus fans out
  sequentially, so awaiting a REST round-trip inside a handler would
  stall every later subscriber, and an arm sequence emits several of
  these events back to back; while a refresh is in flight further events
  are dropped rather than queued, since they would all read the same
  endpoint and write the same answer. The task is cancelled in `stop()`
  alongside the reconcile loop.

  A reset needs no special casing: clearing the latches produces the
  readiness change that triggers the next refresh.

  A refresh never raises. The route only exists from daemon 0.58.0, and a
  missing optional surface must neither fail a bootstrap nor escape a
  background task; the counts then keep their previous value.

# Version 2026.8.10 (2026-08-12)

Types 0.3.14 (daemon API 5.20.0). Adds the alarm system's motion-reset
surface, on all three layers a consumer reaches it through.

## What's Changed

### Added

- **Latched motion detectors can be listed and reset.** A motion
  detector holds its `MOTION` flag until the device's own blocking time
  expires or the reset parameter is written. Until then the sensor reads
  as open, which blocks an arm or forces an auto-bypass — and there was
  no way to clear it from this client short of waiting. The daemon has
  offered the routes since 0.58.0; nothing here bound them.

  Three REST methods on `LoomClient.alarm`:

  | Method                                | Wire                                  |
  | ------------------------------------- | ------------------------------------- |
  | `list_triggered_motion(zone_id=None)` | `GET /alarm/triggered-motion`         |
  | `reset_zone_motion(zone_id=…)`        | `POST /alarm/zones/{id}/reset-motion` |
  | `reset_all_motion()`                  | `POST /alarm/reset-motion`            |

  The verbs are also on the store (`reset_alarm_zone_motion`,
  `reset_all_alarm_motion`) and on the domain wrapper
  (`AlarmPanel.reset_motion`), which is the layer an HA entity actually
  reaches — the compat `LoomDpAlarmControlPanel` inherits it. The master
  panel delegates to the daemon's aggregate route rather than looping the
  zones, mirroring `silence`: a detector enrolled in two zones is then
  written once, and the caller gets one set of counters instead of
  several partial ones to reconcile.

  `reset_motion` takes no code. It clears a blocker without changing the
  armed state, so it is not an authorization-bearing verb.

  Coverage follows the daemon's own predicate — currently active _and_
  the channel exposes a writable reset parameter — so a count shown to an
  operator can never name a detector the reset would skip. Motion
  detectors (`MOTION` → `RESET_MOTION`) and presence detectors
  (`PRESENCE_DETECTION_STATE` → `RESET_PRESENCE`) are covered; door
  contacts fall out by construction.

  Unlike the other alarm verbs these return the daemon's result rather
  than `None`. There is no `alarm.*` broadcast for a reset pass, so the
  counters are the only report there is — and `reset == 0 and failed == 0`
  ("nothing was latched") has to stay distinguishable from `failed > 0`
  ("detectors did not answer"). Collapsing the two would tell an operator
  "nothing to do" in exactly the case where the latch survives and the
  zone still refuses to arm. The daemon reports a failed write in the body
  rather than as an HTTP error: the verb ran, and a partial result is
  actionable.

  Neither reset is retried — they write to devices, so a blind replay is
  real radio traffic.

  Requested in [#88](https://github.com/SukramJ/openccu-loom-client/issues/88).
  The Home Assistant side of that issue (a reset button and a counter
  entity per zone) and all of
  [#89](https://github.com/SukramJ/openccu-loom-client/issues/89) (a
  device-registry object of their own) belong to `homematicip_local` and
  are not part of this release.

### Changed

- **openccu-loom-types 0.3.14** (daemon API 5.20.0), pinned identically in
  `pyproject.toml` and `requirements.txt`. The motion-reset models
  (`AlarmTriggeredMotionSensor`, `AlarmMotionResetResult`) arrived in
  0.3.12, so the bump is a prerequisite for the above and not a routine
  regeneration.

  **This release refuses to connect to a daemon below openccu-loom
  0.58.3** — the existing `_check_api_version` floor, not a new rule.
  Motion reset additionally wants ≥ 0.58.1: 0.58.0 shipped the routes but
  a type assertion made them inert on real hardware, so a client tested
  against exactly that release sees the calls succeed and report nothing.

- **aiohomematic 2026.8.2** in `requirements.txt`. The compat drift-guard
  (`tests/compat/test_aiohomematic_protocol_parity.py`) passes unchanged
  against it.

# Version 2026.8.9 (2026-08-09)

Types 0.3.10 (daemon API 5.14.0). Adds a public accessor for the daemon's
`/info` payload and, on the aiohomematic-compat central, the
browser-reachable Config-UI address the daemon now reports.

## What's Changed

### Added

- **`LoomClient.info`** — the `/info` payload the handshake already
  validates and caches. Without a public accessor, a consumer that wants
  one field from it either re-requests `/info` or reaches into the
  transport; both are worse than exposing what is already held.

- **`LoomCentralAdapter.config_ui_url`** — the address a browser can use
  to reach the daemon's Config UI, from the daemon's
  `north.rest.public_url` (empty when unconfigured or against a daemon
  below API 5.14.0).

  Deliberately distinct from `url`, and the distinction is the point:
  `url` is how _this process_ reaches the daemon — a container address, a
  LAN host behind a reverse proxy — which a browser on someone's desk may
  not be able to follow. A consumer that linked a person at `url` would
  send them somewhere unreachable.

  Empty stays empty. The fallback belongs to the caller, which knows its
  own network better than this package does.

### Fixed

- **An old daemon now reports its version rather than a missing field.**
  The API-version guard runs _before_ `Info` is validated. The types
  package mirrors one daemon API version, so a payload field a types
  release requires is simply absent on an older daemon — validating first
  turned "your daemon is too old" into a pydantic error naming whichever
  field happened to be added last, which sends the reader after the wrong
  thing. Surfaced by `config_ui_url` being a required field in 0.3.10;
  the same trap would have opened on any future addition.

### Changed

- **openccu-loom-types 0.3.10** (daemon API 5.14.0), pinned identically in
  `pyproject.toml` and `requirements.txt`.

# Version 2026.8.8 (2026-08-08)

Types 0.3.6 (daemon API 5.9.0). No behaviour change in this package; the
pin raises the daemon this client will connect to.

## What's Changed

### Changed

- **openccu-loom-types 0.3.6** (daemon API 5.9.0), pinned identically in
  `pyproject.toml` and `requirements.txt`. The regeneration adds two fields
  to the surface-profile payload — `SurfacesResponse.centrals` and
  `SurfaceInfo.multi_central_visible`, which let a Config UI explain why a
  shipped default differs on a daemon serving several CCUs. This package
  consumes neither: it speaks the device, hub and event surfaces, not the
  Config UI's.

### Compatibility

- **This release requires a daemon on API 5.9.0 or newer (openccu-loom
  0.55.1+).** `_check_api_version` raises on a lower minor of the same
  major, so `connect()` fails cleanly rather than half-initializing — the
  pin is what makes that check strict, not a new rule. Consumers that must
  support older daemons stay on 2026.8.7 / types 0.3.5 until they can
  require the newer daemon.

# Version 2026.8.7 (2026-08-08)

Types 0.3.5 (daemon API 5.8.0). No behaviour change: the working documents
are consolidated into one backlog, and the pin CI installs from now matches
the one `pip install` resolves.

## What's Changed

### Changed

- **openccu-loom-types 0.3.5** (daemon API 5.8.0), now pinned identically in
  `pyproject.toml` and `requirements.txt`. The two had drifted — CI installs
  from `requirements.txt` (0.3.5) while a plain `pip install` resolved
  `pyproject.toml` (0.3.3), which is how this repository has produced
  conftest import errors before.
- **The working documents are one backlog.** `notes/open-work.md` replaces
  `todo.md` and `docs/optimization-needs.md`, which overlapped: an item could
  be open in one and closed in the other, with nothing saying which to trust.
  `notes/README.md` records what belongs there. The name mirrors the daemon
  repository, where `notes/` is the working set and `docs/` the published
  site — this package publishes no site.

### Removed

- **Three executed implementation plans and one stale review.** Each was
  verified closed against the tree before deletion: the loom wire-gap
  follow-ups (G1–G7 all shipped — nested `color`, the `available_*` lists,
  locally built event groups, WS push instead of the poll loop, `set_on_time`
  routed to `ON_TIME`), the `GET /hub/data-points` consumption plan (the
  aggregate seeds the hub coordinator today), and a graded architecture
  review measured against daemon 0.11.0 / types 0.1.29 — forty-odd releases
  back, so its scores read as a current assessment and were not one. Every
  still-open item moved into the new backlog first; git history keeps the
  rest.

### Fixed

- **Three dead cross-repository links.** The daemon reorganised its
  documentation, so `README.md` pointed at a moved external-client contract
  and at ADR-0022 under a filename it never had (`0022-ws-resume-cursor.md`;
  it is `0022-ws-resume-and-kind.md`), and a working document referenced a
  daemon page that has since been deleted. Daemon pages are now cited by URL
  rather than by repo-relative path, which is what broke.

### Verified, no change needed

- **Daemon PR #509 (`/ui/surfaces`, daemon 0.55.0).** It adds one REST path
  pair and three schemas, and no WebSocket surface at all. The endpoints
  configure the daemon's own Config-UI navigation, so this client has no
  reason to read or write them. Its embedded-profile write refusal gates
  exactly one identity — the Home Assistant Ingress passthrough — while this
  client authenticates with Basic, Bearer or session credentials and never
  presents an Ingress assertion, so a hidden surface cannot refuse its
  writes. Both findings are recorded in the backlog so they are not
  re-litigated.

# Version 2026.8.6 (2026-08-07)

Types 0.3.3 (daemon API 5.5.0): the class sensors now carry the daemon's
arm-aware grade.

## What's Changed

### Added

- **Each Security & Safety class sensor exposes its graded `severity`.**
  `active` says that something reported; the new `severity` attribute says
  how bad it is — graded arm-aware by the daemon (API 5.5.0), so an active
  intrusion source in a disarmed zone reads `info`, not `alarm`, and
  `warning` flags an unresolvable arm state. A consumer colouring the class
  reads this attribute, never the boolean. Only the REST snapshot carries
  the grade — the `security.class_changed` push does not — so a source-set
  push keeps the last known grade until the next snapshot re-grades.

### Changed

- **openccu-loom-types 0.3.3** (daemon API 5.5.0, regenerated from
  openccu-loom v0.54.6), pinned in `pyproject.toml` and `requirements.txt`.

# Version 2026.8.5 (2026-08-06)

Context for the Security & Safety entities: every one of them now names the
detectors behind its state, and the severity is readable rather than a raw
token.

## What's Changed

### Added

- **Every Security & Safety entity carries its sources.** "Opening or motion
  detected: on" is not actionable without the answer to "which detector?".
  The class sensors, the severity sensor, the fault count and both report
  sensors now expose the same attribute set the daemon's MQTT plane
  publishes: `sources[]` with the full identity of each contributing data
  point — including the `ref` that `PUT /security/sources/{ref}` takes back
  to correct a misclassification — plus `source_names[]` for a message,
  `count`, `total` and `truncated`. The list is bounded at 20 and says so
  rather than pretending to be complete.

  The class sensors previously carried bare names under `sources`; that key
  now holds the objects and the names moved to `source_names`.

### Fixed

- **The security state showed the raw token `alarm`.** Home Assistant
  renders an enum sensor — and translates its values — only when the data
  point declares a value list. The severity sensor declared a plain string,
  so the operator read the wire vocabulary instead of their own language. It
  now declares the severity ladder (`ok`, `info`, `warning`, `alarm`,
  `critical`) as its options.

### Changed

- **The class device classes match the MQTT plane** (`technical` →
  `problem`, `intrusion` / `panic` → `safety`). Note the limit: Home
  Assistant resolves a hub entity's device class from its own entity
  descriptions, never from the data point, so this reaches non-HA consumers
  of this library and keeps the two planes from diverging — it does not by
  itself put an icon on the HA entity.

- **The entity names come from openccu-loom 0.54.2**, which renamed the
  hazard classes to say what was detected rather than what it means:
  "Intrusion" became "Opening or motion detected", because that sensor
  stands on as soon as a monitored door, window or motion detector reports —
  including while the alarm system is disarmed. No change is needed here;
  the names are read from the daemon catalogue at bootstrap.

# Version 2026.8.4 (2026-08-06)

Follows the daemon to **API 5.2.0** (openccu-loom 0.54.0). The Security &
Safety domain becomes a live Home Assistant surface, the CCU's message
records gain the fields they always should have carried, and an alarm
output could never be enrolled.

## What's Changed

### Added

- **Security & Safety reaches Home Assistant, and it pushes.** The daemon's
  hazard-and-fault domain — smoke, water, gas, CO, tamper, battery,
  technical, intrusion, panic — had no Python surface at all, and until
  openccu-loom 0.54.0 it had no WebSocket push either: its five events
  reached MQTT, the webhook plane and the metrics collector while every
  REST/WebSocket consumer had to re-read `GET /security` on its own
  schedule to learn that a smoke detector had fired.

  This release binds all five broadcasts and spawns the entities off them:

  - one **binary sensor per hazard class**, carrying the HA device class
    that turns it into a real smoke/moisture/gas/CO alarm on the
    dashboard, plus the names of the detectors currently reporting;
  - a **severity sensor** with the folded verdict, so "is anything wrong"
    is one entity rather than nine;
  - a **fault-count sensor** over the standing ledger — an unreachable
    detector, a flat battery, a blocked radio — with one attribute per
    fault. Acknowledging never clears it: the condition is still there;
  - **two report sensors**, last hazard and last fault, carrying the
    daemon's rendered sentence plus the i18n key and args so a consumer
    can render in its own locale instead.

  A class the installation has no source for is never built — the daemon
  omits it rather than reporting it inactive, so a home without gas
  detectors gets no permanently-off gas alarm. A class that appears
  mid-session (a newly-paired detector) gets its sensor on the spot.

  **A covert report stays off this surface.** A duress code or silent
  panic trigger reaches the notification broadcast only when the daemon
  runs `alarm.duress_visibility: full` — the WebSocket is a local screen
  surface, and a wall tablet showing "duress code entered" while the
  attacker reads it defeats the trigger the feature exists for. Under the
  other levels the report still goes out over the daemon's webhook and
  raw MQTT event topic.

- **`client.security` — the full REST façade.** The folded snapshot, the
  per-class view, the classified source inventory with its filters and
  the operator override that corrects a misclassification, and the fault
  ledger with its acknowledgement. The domain runs with or without an
  alarm engine, so nothing here gates on the `alarm.v1` capability.

- **`alarm.list_sensor_candidates()`, `list_incidents()`,
  `get_incident()`.** Sensor enrolment was the one alarm surface without
  a candidate list — outputs and remote keys had one, sensors were
  unvalidated free text over (central, interface, channel address,
  parameter), so a typo produced a sensor that silently never fired. Each
  candidate now carries the suggested role, the hazard class, the value
  list and the recommended active values. The incident reads answer "what
  else went off while the alarm ran" after the fact.

- **Entity names now come from the daemon — `client.i18n`.** The daemon
  has been the single naming authority since 0.45.0 and names its own hub
  and Security & Safety entities in both locales, but those names only
  ever reached the MQTT discovery plane. This layer therefore rendered
  Home Assistant's copy of the same words, and the two drifted the moment
  either side was edited alone.

  Every hub singleton now adopts the daemon's name (`resolved_name`) from
  `GET /i18n/entities`, read once at bootstrap: alarm and service
  messages, inbox, the metric sensors, connectivity, both install-mode
  entities, system and add-on update, plus the whole Security & Safety
  family. Templates are completed here — the daemon hands out
  `Konnektivität {iface}` because it does not know which interface is
  being named.

  `name` deliberately keeps its stable English token: Home Assistant
  matches its entity descriptions against it (`var_name_contains`), and a
  localized token there would cost the entity its icon, device class and
  category. A daemon older than api 5.2.0 answers 404 and every entity
  keeps its own rendering — the same fallback an unknown key takes.

### Fixed

- **An alarm output could never be enrolled.** `replace_zone_outputs` sent
  the output's class under the field name `class_` rather than its wire
  name `class`. The generated model renames the field because `class` is
  a Python keyword, and the daemon's schema marks the property required,
  so every enrolment failed validation — a zone that arms and then stays
  silent. The request serialiser now dumps by alias throughout, which is
  the wire name by definition; `EnergyResponse`'s `from` was exposed to
  the same defect.

### Changed

- **Alarm messages dropped the device fields they never had, and gained
  real timestamps** (daemon API 4.0.0). An alarm entry is backed by an
  alarm system variable a program raises, not by a device — the CCU
  reports its trigger data point as the 65535 "unknown" sentinel — so
  `device_name`, `last_trigger` and `rooms` never carried data. The hub
  sensor's per-alarm attribute now shows the translated message and when
  it was raised instead of a device name it had to invent.

- **Service messages gained real rooms, functions and timestamps** (daemon
  API 5.0.0). All three have been on the aiohomematic record since it was
  written and stayed empty: the CCU script emitted them, the loader never
  read them. `description` and `priority` went the other way — the script
  never emitted either, so both are gone.

- **Pin `openccu-loom-types==0.3.1`** (regenerated from openccu-loom
  0.54.0, API 5.2.0). The transport's API guard derives from
  `DAEMON_API_VERSION`, so `connect()` now requires a daemon on API major
  5 with minor ≥ 2 — deploy openccu-loom 0.54.0+ alongside this release.

# Version 2026.8.3 (2026-08-02)

Follows the daemon to **API 3.14.0** (openccu-loom 0.52.10) and fixes the
program switch, which never worked — plus the reason no hub entity could
follow the CCU at all.

## What's Changed

### Fixed

- **The program switch now switches.** `ProgramDpSwitch` carried neither a
  state nor a write path: Home Assistant reads a program switch's state from
  `value` and toggles it through `turn_on` / `turn_off`, and none of the
  three existed on the loom twin. The switch reports the CCU's activity flag
  and writes it back through `PATCH /programs/{id}`, then re-reads the
  program so a consumer looking straight after the call sees the settled
  state.

- **Hub entities were frozen at their bootstrap value.** The categorised
  sysvar and program twins were built next to the store's own objects and
  cached in the hub coordinator, so every refresh and every push updated a
  copy while Home Assistant kept reading the original. A sysvar sensor never
  moved after start-up, and a program's execute button never learned that
  the program had been deactivated.

  The store now owns the twins — the same rule the generic, custom and
  calculated data points already follow — so there is exactly one live object
  per entity and an update reaches the one a consumer holds. The coordinator
  reads through instead of caching.

- **A deactivated program greys out its "run now" button.** With the two
  above fixed, the rule the daemon has reported since API 3.12.0 finally
  arrives: toggling the switch — in Home Assistant or in the CCU WebUI —
  updates both of the program's entities.

### Added

- **`hub.program_changed` binding.** The daemon's new broadcast carries a
  program's `active` and `execute_available` together, so an activity change
  made anywhere reaches Home Assistant within a message instead of at the
  next catalogue poll. Both program entities re-render off it; they share
  the canonical key, and HA scopes unique_ids per platform.

- **The "fetch system variables" service re-renders what it changed.** The
  manual poll merged the fresh catalogue into the model and told Home
  Assistant nothing, so the one action an operator takes when a value looks
  stale appeared to do nothing. It now emits a state change per system
  variable whose value moved, and one per program whose activity flag
  moved — keyed on the program's canonical id, since its two entities share
  it and the execute button carries no value of its own.

### Changed

- **Pin `openccu-loom-types==0.2.7`** (regenerated from openccu-loom
  0.52.10, API 3.14.0). The transport's API guard derives from
  `DAEMON_API_VERSION`, so `connect()` now requires a daemon on API major 3
  with minor ≥ 14 — deploy openccu-loom 0.52.10+ alongside this release.

# Version 2026.8.2 (2026-08-02)

Follows the daemon to **API 3.13.0** (openccu-loom 0.52.9): a calculated
sensor is only as trustworthy as the readings it was computed from, and the
daemon now says which of the two it is.

## What's Changed

### Fixed

- **A calculated sensor computed off a broken reading no longer counts as
  valid.** Dew point, frost point, enthalpy, apparent temperature, vapour
  concentration and the derived battery level are derived from a channel's
  ordinary parameters. Those can be _read but unusable_ — the CCU flags a
  measurement fault through the paired `…_STATUS` parameter, or the reading
  falls outside the bounds the device declares. The derived number keeps
  updating right through such a fault, so the generic rule this client
  applied — "a value is present, therefore it is valid" — could not see it:
  a dew point computed off a thermometer stuck at `OVERFLOW` read as a
  perfectly ordinary measurement.

  The daemon answers this as `available` on the calc-dps record (API
  3.13.0), and `CalculatedDpSensor` / `CalculatedDpBinarySensor` now take
  `is_valid` from it instead of re-deriving. Home Assistant restores an
  entity's previous state exactly when `is_valid` is False and stops
  rendering the live value — which is the point: the last known good
  reading beats a confident wrong one. Device availability is untouched;
  this is a verdict about the value, not about reachability.

  **Scope.** The flag is carried by the calc-dps record, so the client
  learns it at bootstrap and on every re-read (`load_data_point_value`,
  which Home Assistant calls when an entity is added and on a manual
  refresh). The `datapoint.value_changed` push carries no availability
  today, so a fault that starts mid-session is picked up at the next
  re-read rather than on the push that reports the new value.

### Changed

- **Pin `openccu-loom-types==0.2.6`** (regenerated from openccu-loom
  0.52.9, API 3.13.0). The transport's API guard derives from
  `DAEMON_API_VERSION`, so `connect()` now requires a daemon on API major
  3 with minor ≥ 13 — deploy openccu-loom 0.52.9+ alongside this release.

# Version 2026.8.1 (2026-08-02)

Follows the daemon to **API 3.12.0** (openccu-loom 0.52.7): a CCU program
is two controls, and the daemon now says outright whether the second one
would do anything.

## What's Changed

### Added

- **`Program.execute_available` + a gated execute button.** A CCU program
  is an activity flag deciding whether it reacts at all, plus an execution
  that runs it once — and the CCU refuses the execution while the flag is
  off. The daemon answers that as `execute_available` on the REST and WS
  program payloads (api 3.12.0) instead of leaving every consumer to
  re-derive it from `active`: it is CCU semantics, not presentation.

  The compat layer already spawned two entities per program, so this lands
  where it belongs: `ProgramDpButton.available` now follows the daemon's
  answer, and pressing a button that could only fail is no longer offered.
  `ProgramDpSwitch` stays available on purpose — gating it exactly when
  the program is off would strip out the only control that turns it back
  on.

  **Fails open.** A pre-3.12.0 daemon omits the field and a CCU that has
  not reported the flag leaves it unset; both read as available, because a
  control must never be greyed out on missing information. The flag rides
  the periodic program refresh (`fetch_program_data` →
  `_upsert_program`), which replaces the summary in place, so it tracks
  the activity flag without a new push binding.

### Changed

- **Pin `openccu-loom-types==0.2.5`** (regenerated from openccu-loom
  0.52.7, API 3.12.0). The transport's API guard derives from
  `DAEMON_API_VERSION`, so `connect()` now requires a daemon on API major
  3 with minor ≥ 12 — deploy openccu-loom 0.52.7+ alongside this release.

### Note — a daemon-side unique_id fix changes entity identity

No client change, but it lands with the daemon this release requires.
openccu-loom 0.52.4 fixed the external `unique_id` of a parameter forced
to a read-only sensor — `LEVEL` on HmIP-eTRV and HmIP-HEATING — to keep
its disambiguating `_sensor` suffix, which the external key builder had
been dropping.

This client takes the `unique_id` from the daemon on both ends (the twin
reads `summary.unique_id`, the refresh bridge prefers
`payload.unique_id`), so the two stay in lock-step by construction and
nothing here had to move. But the value itself changes across the daemon
upgrade, so **HA orphans the old entity for those data points and creates
a new one beside it**. Affected setups will want to remove the stale
entity from the registry.

### Fixed

- **A cover with inverted control no longer reports the wrong direction.**
  `is_opening` / `is_closing` were derived from the payload's `direction`
  field, which carries the CCU's raw travel direction. The daemon's `state`
  token already accounts for a channel wired with inverted control, where
  "up" on the wire means closing — so on those channels the client reported
  the opposite of what the daemon had determined, and the raw field won
  because it was checked first. Both now read the token. `is_closed` does
  too, keeping position 0 as the fallback for a payload without one; the
  daemon derives "closed" from exactly that, so the two agree.
- **The colour fallback for pre-0.8.0 daemons is gone.** `hs_color` read the
  nested `color: {h, s}` object and, failing that, flat `hue`/`saturation`
  keys — which it passed through _unscaled_, so a payload carrying both
  shapes answered on a different saturation scale depending on which branch
  ran. Those daemons are dozens of releases old; the nested object is the
  only source now.

# Version 2026.8.0 (2026-08-01)

Dependency release: `aiohomematic` moves to the **2026.8.0** series. No
client code changes — this is the reference-stack refresh the compat shim
rides on.

## What's Changed

### Changed

- **Pin `aiohomematic==2026.8.0`** (was `2026.7.11`), and raise the
  `pyproject.toml` floor to match — the CI pin is the tested set, and an
  ancient floor is exactly where the compat shim's coupling to
  aiohomematic internals would bite a fresh `pip install`.

  Nothing in the 2026.8.0 series reaches this client. Its one signature
  change (`_GenericProperty.__init__` takes `cached` / `log_context`
  keyword-only) affects code that constructs `_GenericProperty`, which
  this package never does. Its raised `openccu-data` floor (≥ 2026.7.2,
  pulled in transitively) ships CCU translation and easymode artifacts
  that only the direct-XML-RPC path reads; here the model→icon mapping is
  resolved daemon-side and arrives on `DeviceSummary.model_icon`. The
  remaining entries are an `AI_POLICY.md` addition and a logging-call
  detail.

  Verified rather than assumed: the drift-guard suite (`tests/compat`) —
  which snapshots aiohomematic's `@runtime_checkable` model protocols and
  the per-twin member surface so a silent upstream rename fails CI
  instead of HA — passes against the new series, as does the full unit
  suite (780 tests).

# Version 2026.7.20 (2026-07-31)

Catches the client up with the daemon's API 3.4.0 → 3.11.0 run
(openccu-loom 0.51.0 – 0.52.1). The substance is the **CCU maintenance
surface**: a CCU can now be powered off, restarted into safe mode or into
its recovery system, its astro reference position can be corrected, an
externally produced `.sbk` archive can be imported, and a firmware update
can be preceded by a full backup. Everything is admin-gated ops surface on
`client.system` / `client.backup`; no HA entity changes.

One behaviour change reaches HA: the CCU's own security posture
(`auth_enabled` / `https_redirect_enabled`) is now read from the daemon
instead of being asserted by this client — see below.

## What's Changed

### Added

- **`client.system` CCU maintenance verbs.** `reboot_ccu()` (which
  existed on the daemon but never here), `poweroff_ccu()`,
  `restart_ccu_safe_mode()` and `restart_ccu_recovery_mode()` on
  `POST /system/ccu/{central}/{reboot,poweroff,safe-mode,recovery-mode}`.
  All take `central=` (path-encoded — a central name is operator-chosen
  free text), answer 202 the moment the CCU accepted, and none are
  retried: each has a side effect on real hardware that the daemon does
  not deduplicate. A central whose backend cannot host the action (CUxD,
  Homegear, stock CCU3 for recovery) answers 422 →
  `LoomValidationError`. Check `SystemCCUEntry.recovery_mode_supported`
  before offering recovery rather than letting the operator find out.
  Requires daemon api ≥ 3.9.0.
- **`client.system.set_ccu_position()`.** `PUT /system/ccu/{central}/position`
  writes the CCU's astro reference latitude/longitude. Every
  sunrise/sunset time the CCU computes — for its own programs and for the
  weekly profiles this client edits — derives from it, so a wrong value
  skews astro schedules silently rather than failing. The daemon reads
  the values back and compares, so a successful call means the CCU holds
  exactly what was sent; that read-back is also why this one _is_
  retried. The time zone is read-only (`SystemCCUEntry.timezone`).
  Requires daemon api ≥ 3.8.0.
- **`client.backup.upload_backup()`.** `POST /backups/upload` imports an
  externally produced `.sbk` so it restores through the ordinary
  `restore_backup()` path. The daemon inspects the archive first — a
  structural check (readable tar carrying `usr_local.tar.gz` and its
  signature), so picking the wrong file fails here rather than at restore
  time when the CCU is already being wiped. The signature itself is _not_
  verified (that needs the CCU's key material) and the daemon does not
  claim otherwise. Returns the stored entry plus the `firmware_version` /
  `product` read out of the archive, to compare against the target CCU.
  Requires daemon api ≥ 3.10.0.
- **`HttpTransport.request_upload()`.** The mirror image of
  `request_bytes`: one `multipart/form-data` part, JSON (or
  `problem+json`) back. Like a download it does not inherit the
  session-wide total timeout — a real archive is tens of megabytes and
  that timeout would guarantee failure on a slow link — but a stalled
  transfer still fails fast on the per-chunk timeout. Never retried:
  re-sending the body wastes the whole transfer and the daemon's upload
  route is not idempotent.
- **`install_system_update(backup_first=True)`.** The daemon takes a full
  CCU backup and starts the update only once it is durably stored; a
  failed backup aborts and the update does not run. The call then
  **blocks for as long as the backup takes** — minutes on a large
  configuration — because its response is what tells the caller whether
  the safety net exists. Raise `LoomConfig.request_timeout_seconds`
  accordingly. Off by default, and the body is omitted entirely unless
  asked for, so a pre-3.11.0 daemon sees the request shape it validated
  before.

### Changed

- **CCU security flags come from the daemon (compat).** The compat
  adapter's `SystemInformation` took `auth_enabled=True` unconditionally
  (reasoning: this client cannot connect without an auth method) and left
  `https_redirect_enabled` unset because the daemon did not report one.
  Both now come off the `/system/ccu` entry (api 3.5.0), which is the
  flag's actual meaning: whether the **CCU** requires auth / redirects
  HTTP on its own interfaces. They stay `None` on an older daemon or
  before the first successful CCU connect — the CCU dashboard renders
  that as unknown, which is honest, where the old `True` was a claim
  about the wrong subject.
- **Pin `openccu-loom-types==0.2.4`** (regenerated from openccu-loom
  0.52.0, API 3.11.0). The transport's API-version guard derives from the
  types' `DAEMON_API_VERSION`, so `connect()` now requires a daemon on
  API major 3 with minor ≥ 11 — deploy openccu-loom 0.52.0+ alongside
  this release. The new `SystemCCUEntry` fields (`auth_enabled`,
  `https_redirect_enabled`, `longitude`, `latitude`, `timezone`,
  `recovery_mode_supported`, `ccu_interfaces` — the CCU-side counterpart
  to `configured_interfaces`, where a _difference_ between the two lists
  is the interesting signal) ride along on `list_system_ccus()`.

### Not carried over

- **WS `addon_update.check` / `addon_update.install` (api 3.4.0).** The
  daemon added WebSocket twins of two verbs this client already drives
  over REST (`check_addon_update()` / `install_addon_update()`), and its
  WS transport is deliberately receive-only apart from
  subscribe/unsubscribe. Both routes report through the same
  `addon_update.state_changed` broadcast that is already bound, so
  nothing is missing.
- **`GET /history` tier fallback + energy tariff (api 3.x, SV04/SY18).**
  The `X-History-Tier` response header and the `price_per_kwh` /
  `currency` fields land on daemon endpoints this client does not expose
  — history and energy are CCU-WebUI surfaces, not HA ones. The types
  carry them for whoever needs them.

# Version 2026.7.19 (2026-07-29)

The daemon's add-on self-updater (openccu-loom 0.50.0, API 3.3.0) reaches
HA over this backend too: on platforms with the firmware-side installer
(OpenCCU / RaspberryMatic) the compat layer now renders a second hub
update entity for the daemon's own CCU add-on package, next to the CCU
firmware update — same install button, progress and backup toggle,
without any `homematicip_local` change.

## What's Changed

### Added

- **`client.system` add-on self-update verbs.** `get_addon_update_status()`
  (`GET /system/addon-update`), `check_addon_update()`
  (`POST /system/addon-update/check`) and `install_addon_update()`
  (`POST /system/addon-update/install`). The install restarts the daemon
  and — like the check — is deliberately not retried; while an install is
  already running the daemon answers 409.
- **`AddonUpdateStateChangedEvent`.** The daemon's
  `addon_update.state_changed` broadcast (topic `system.addon_update`) is
  bound to a typed event carrying the shared `AddonUpdateStatus` payload
  (REST and WS use one model). Daemon-global — no routing key.
- **`AddonUpdateDp` hub singleton (compat).** A second `HmUpdate` twin in
  the `HubUpdate` category, so the HA update platform spawns the add-on
  entity through the identical hub-update path as the CCU firmware
  update. Capability-gated: the coordinator builds it only when
  `GET /system/addon-update` reports `supported` (a pre-3.3.0 daemon
  answers 404 → no entity, no dead install button). Seeded at build time,
  refreshed by the 30 s reconcile as a missed-push backstop, kept live by
  the `addon_update.state_changed` push routing. `install()` flips the
  state optimistically to `installing` — terminal from the caller's view:
  the daemon restarts on success and the post-reconnect fetch shows the
  new version. `state`, `release_url` and `error` are exposed for
  diagnostics.

### Changed

- Pin `openccu-loom-types==0.2.2` (regenerated from openccu-loom v0.50.0,
  API 3.3.0). With the types generated against API 3.3.0, the transport's
  API-version guard requires a daemon on API major 3 with minor ≥ 3 at
  `connect()` — deploy openccu-loom 0.50.0+ alongside this release.

# Version 2026.7.18 (2026-07-28)

The alarm system's armable unit is a **zone** — the daemon's API 3.0.0
rename (`area` → `zone`, openccu-loom 0.49.2) followed through the whole
client surface. Deliberately breaking, with no alias layer: the daemon
frees `area` up for the coming room-grouping concept above CCU rooms, and
a half-renamed client would only invite drift.

That concept has since landed as well: `area` is now a **room grouping**
above CCU rooms (openccu-loom 0.49.3, API 3.2.0) and reaches the client
as a new `client.hub` surface — the two meanings never overlap in this
release.

## What's Changed

### Changed (BREAKING)

- **`client.alarm` — every area verb is now a zone verb.**
  `get_area_statuses` → `get_zone_statuses`, `get_area_readiness` →
  `get_zone_readiness`, `list_areas`/`get_area`/`create_area`/
  `update_area`/`delete_area` → `list_zones`/`get_zone`/`create_zone`/
  `update_zone`/`delete_zone`, `list_area_sensors`/`replace_area_sensors`
  and `list_area_outputs`/`replace_area_outputs` → their `zone_`
  counterparts, `arm_area`/`disarm_area`/`silence_area`/
  `acknowledge_area` → `arm_zone`/`disarm_zone`/`silence_zone`/
  `acknowledge_zone`, and the walk-test trio takes `zone_id`. The
  keyword argument `area_id` is `zone_id` throughout; `create_zone` /
  `update_zone` take `zone=`. Routes moved to `/alarm/zones/{id}/…`, the
  `GET /alarm/state` envelope key is `zones`, and the journal filter
  query parameter is `zone`.
- **`LoomStore` alarm surface.** `get_alarm_panel_by_area` →
  `get_alarm_panel_by_zone`, `attach_alarm_area_statuses` →
  `attach_alarm_zone_statuses`, `arm_alarm_area`/`disarm_alarm_area`/
  `silence_alarm_area`/`acknowledge_alarm_area` →
  `*_alarm_zone`, `silence_all_alarm_areas` → `silence_all_alarm_zones`.
- **`AlarmPanel` domain wrapper.** `AlarmPanel.area_id` → `.zone_id`, the
  exported constant `MASTER_AREA_ID` → `MASTER_ZONE_ID` (value unchanged:
  `"master"`). The compat panel's extra state attribute `area_id` is now
  `zone_id`; every other HA-facing identity is untouched — the
  daemon-computed `unique_id` keeps the `openccu-loom_alarm_<zone-id>`
  format, so no HA entity is re-created by this release.
- **Alarm events.** `AlarmStateChangedEvent`, `AlarmCountdownEvent`,
  `AlarmReadinessChangedEvent`, `AlarmTriggeredEvent`,
  `AlarmJournalAppendedEvent`, `AlarmWalkTestProgressEvent`,
  `AlarmReminderEvent` and `AlarmNotificationEvent` key off
  `payload.zone_id` (was `payload.area_id`) — the wire payload field
  renamed with the daemon; the `alarm.panel` topic and every event
  `type_id` are unchanged.
- Pin `openccu-loom-types==0.2.1` (regenerated from openccu-loom v0.49.3,
  API 3.2.0). With the types generated against a new major, the
  transport's API-version guard now demands a daemon on API major 3 with
  minor ≥ 2 at `connect()` — deploy openccu-loom 0.49.3+ alongside this
  release; an older daemon is refused rather than silently half-working.

### Added

- **Areas — room groupings above CCU rooms (API 3.2.0, additive).** An
  area bundles CCU rooms one level up (a floor, a shed, a terrace roof),
  lives in the daemon's database only and is unrelated to an alarm zone.
  `client.hub` gained `list_areas`, `create_area`, `update_area`,
  `delete_area` and `replace_area_rooms` over `GET/POST /areas`,
  `PUT/DELETE /areas/{id}` and `PUT /areas/{id}/rooms`. Rooms are
  `(central, room)` pairs with one area per room, so the room PUT is a
  full-set replace that moves a room away from whichever area held it.
  Operator-scoped administration — no store mirror and no WS binding,
  because the daemon broadcasts nothing for areas.
- **Room/function labels on alarm output candidates (API 3.1.0,
  additive).** `AlarmOutputCandidate` gained the channel's optional
  `rooms` and `functions`, which flow through the unchanged
  `client.alarm.list_output_candidates()` to consumers — a picker can
  filter and label without a second lookup.

# Version 2026.7.17 (2026-07-27)

Dependency alignment with daemon 0.48.7 (API 2.56.0) — group members now
arrive name-enriched on the wire; no client code changes were required.

## What's Changed

### Changed

- Pin `openccu-loom-types==0.1.69` (regenerated from openccu-loom v0.48.7,
  API 2.56.0). `GroupMemberEntry` gains four optional fields — `device_name`,
  `device_model`, `channel_name` and `rooms` — which flow through the
  unchanged `client.groups` read surface (`list_groups` /
  `suitable_members`) to consumers automatically.
- With the types now generated against API 2.56.0, the transport's
  API-version guard requires a daemon reporting API ≥ 2.56 (same major) at
  `connect()`; deploy openccu-loom v0.48.7+ alongside this release.

# Version 2026.7.16 (2026-07-25)

Heating-group administration reaches the client — the daemon's `/groups`
surface (daemon 0.48.0, API 2.53.0) is now a first-class operations module.

## What's Changed

### Added

- **`client.groups` operations.** A new `GroupsOperations` facade wraps the
  daemon's heating-group REST surface: `list_groups`, `list_types`,
  `suitable_members`, `create_group`, `update_group` and `delete_group`. Reads
  and writes take an optional `central` selector; `suitable_members`
  additionally requires the `type_id` of the group being built. Requests and
  responses use the `openccu_loom_types.rest` group models (`GroupCentralEntry`,
  `GroupEntry`, `GroupTypeEntry`, `SuitableMembersResponse`,
  `CreateGroupRequest`, `UpdateGroupRequest`). `create_group` is not retried
  (it has side effects); `update_group` is idempotent and is.

### Changed

- Pin `openccu-loom-types==0.1.68` so the group models resolve.

# Version 2026.7.15 (2026-07-20)

Custom data point names come from the daemon wire too — completing the
daemon-as-single-naming-authority move started in 2026.7.14 (daemon
0.45.0, API 2.29.0).

## What's Changed

### Changed

- **Custom DP entity names come verbatim from the daemon wire.** The
  CDP summary's `translated_name`/`parameter_name` (custom channel
  names, `ch<no>`/`vch<no>` group markers, button-lock postfix labels)
  are rendered as shipped; the client-side profile-registry heuristics
  are gone. The fields reach this client with the matching
  openccu-loom-types release; against older types or a pre-0.45.0
  daemon, CDP names gracefully collapse to the device name alone.

### Removed

- `compat.aiohomematic.model.naming.custom_name_parts`,
  `strip_device_prefix`, and the `_ignore_multiple_channels_for_name`
  class markers — the daemon owns these rules now.

# Version 2026.7.14 (2026-07-20)

The daemon is the single naming authority for generic data points
(daemon ≥ 0.45.0, API 2.28.0).

## What's Changed

### Changed

- **Generic entity names come verbatim from the daemon wire.** The
  compat layer no longer composes generic data point names client-side:
  `translated_name` arrives fully composed from the daemon — including
  the ambiguity-gated `ch<no>` multi-channel marker (daemon ≥ 0.44.3 /
  aiohomematic 2026.7.10 semantics) and, since daemon 0.45.0, the
  channel-level collapsed name for label-omitted primary parameters.
  Against a pre-0.45.0 daemon, label-omitted data points gracefully
  collapse to the device name alone.
- aiohomematic floor raised to 2026.7.10 (the ambiguity-gated postfix
  release) so both backends of `homematicip_local` produce identical
  entity names.

### Removed

- `compat.aiohomematic.model.naming.generic_translated_name` and
  `LoomStore.is_parameter_in_multiple_channels` — obsolete now that the
  daemon ships composed names.

# Version 2026.7.13 (2026-07-18)

Alarm follow-ups for daemon ≥ 0.43.x (types 0.1.61, API 2.27.0). Both daemon
asks from 2026.7.12 shipped upstream (openccu-loom #357/#358) and are consumed
here; the contract delta is otherwise additive.

- Feature: **effective code policy on the panel.** `AlarmPanelEntity` and every
  `alarm.panel_changed` push now carry `code_arm_required` /
  `code_disarm_required` (required fields — daemon-computed: area policy AND an
  applicable enabled PIN exists; the master aggregates any-area). The domain
  `AlarmPanel` exposes both, the compat twin's documented placeholder-False
  properties are removed, the store propagates live policy edits from the push
  (stub-seed included — building the entity without the fields would now fail
  validation).
- Feature: **`alarm.v1` capability gate.** `_bootstrap_alarm_panels` now gates
  on the `/info` capability token (daemon ≥ 0.43.1 emits it exactly when the
  `/alarm` surface is mounted; the types pin makes such a daemon a connect()
  precondition). The 404 probe stays as fallback for info-less injected
  transports.
- Feature: **`alarm.notification` bound** (daemon ≥ 0.43.1): notification-class
  outputs firing become `AlarmNotificationEvent` (keyed by area) — one-shot,
  mode-filtered, never cancelled by silence. The broadcast drift-guard is green
  again (34 broadcasts).
- Feature: **setup-wizard candidate routes**: `client.alarm.list_output_candidates()`
  (`GET /alarm/output-candidates` — capability-derived output classes incl.
  localised tone/pattern/soundfile labels) and `list_remote_key_candidates()`
  (`GET /alarm/remote-key-candidates` — keyfob/wall-button key channels for
  guided bindings).
- Feature: **`client.devices.refresh_firmware_data()`**
  (`POST /devices/firmware/refresh`, daemon ≥ 0.42.8) — forces the daemon-wide
  firmware sweep backing the update entities.
- Chore: `openccu-loom-types` 0.1.56 → 0.1.61 (pins daemon ≥ 0.43.2 via the
  same-major/minor-≥ handshake).

# Version 2026.7.12 (2026-07-16)

Alarm control panel, client side complete (daemon ≥ 0.42.0 / API 2.22.0,
types 0.1.56). The daemon's native alarm system ("Alarmanlage") is now a
first-class surface of this client — REST, WebSocket, store, domain model and
the aiohomematic-compat layer. The HA platform itself lands in
`homematicip_local` (loom-only — aiohomematic has no alarm engine); the
cross-repo remainder is tracked in `todo.md`.

- Feature: **`operations/alarm.py`** — all 21 `/alarm` routes as `client.alarm`
  (areas, sensors/outputs, arm/disarm/silence/acknowledge, silence-all,
  readiness, panels, journal, walk test, output test fire, codes). Retries only
  on idempotent calls; the verbs and the output test fire are never retried.
- Feature: **nine `alarm.*` WS events** bound end-to-end (`alarm.state_changed`,
  `.countdown`, `.readiness_changed`, `.triggered`, `.journal_appended`,
  `.walktest_progress`, `.health_changed`, `.panel_changed`, `.reminder`) —
  the broadcast drift-guard is green again. `alarm.*` joined the default WS
  subscriptions.
- Feature: **store panel section + `model/AlarmPanel`.** Panels are keyed by the
  daemon-computed `unique_id` (`openccu-loom_alarm_<area>`, consumed verbatim —
  never re-derived) with an area secondary index; live detail (mode, countdown,
  readiness, incident) held as plain fields (the payload auto-enums are
  class-distinct across schemas). Master verbs mirror the daemon's MQTT
  semantics: `arm` fans out to mode-capable areas only, `silence` uses
  `/alarm/silence-all`.
- Feature: **compat `LoomDpAlarmControlPanel`** — categorised
  `alarm_control_panel` twin (loom-native, no aio class) held live in the store
  via the factory hook; adapter announces panels (batch + runtime area-added)
  and the refresh bridge pings `DataPointStateChangedEvent` keyed by the panel
  `unique_id`. Announces gate gracefully while the installed aiohomematic lacks
  the category.
- Feature: **bootstrap feature-detection** — `/alarm` is unmounted when the
  daemon's alarm subsystem is off (no capability token yet), so bootstrap
  treats a 404 on `/alarm/panels` as "no alarm" instead of an error.
- Fix: **`_category_for_type` missed aiohomematic's SCREAMING_CASE enums.** The
  member-_name_ lookup returned `None` for `DataPointType.SIREN` & friends
  (making type-only queries unfiltered); matching now happens on the shared
  string value, pinned by tests.
- Chore: `openccu-loom-types` 0.1.55 → 0.1.56; `aiohomematic` 2026.7.6 →
  2026.7.7 (ships the `ALARM_CONTROL_PANEL` vocabulary, so the compat announce
  paths now deliver panels — the version gate stays covered by a simulated-old
  test); `category_golden.json` gains `alarm_control_panel`; dev-toolchain
  bumps (mypy 2.3.0, ruff 0.15.22, coverage 7.15.2, prek 0.4.10,
  codespell 2.4.3).

# Version 2026.7.11 (2026-07-14)

Hotfix: the config panel's **CCU** and **Integration** tabs did not load at all
on a loom-backed entry. Both dashboards fetch their sections in a single
`Promise.all`, so one failing command takes the whole tab down — and each tab had
exactly one.

- Fix: **`system_information` was missing five of the eight members the CCU
  dashboard reads.** The hand-rolled stand-in carried only
  serial/version/available_interfaces/ccu_type, so `ws_get_system_information`
  died with `AttributeError: hostname` — killing the entire CCU tab. It now
  builds aiohomematic's **real `SystemInformation`** record, which also supplies
  `auth_enabled` / `https_redirect_enabled` / `is_ha_app` and the `has_backup` /
  `has_system_update` **computed properties** (derived from `ccu_type`).
  `hostname` and `is_ha_app` come from the daemon's `/system/ccu` entry.
- Fix: **`central.health` returned the daemon's health probe, not the shape the
  card renders.** The integration dashboard's health card is typed against
  `SystemHealthData` — `central_state` + `overall_health_score` + `client_health`
  — which is what aiohomematic's `CentralHealth.to_dict()` emits. It was handed
  the daemon's `{status, components}` probe instead, so the card found none of
  its fields. `central.health` now builds the **real `CentralHealth`** from the
  live state: the lifecycle state plus one connection record per wired interface
  (connected → healthy), so `overall_health_score` and the healthy/failed client
  lists are meaningful.
- Chore: `client_coordinator` keeps the daemon's full per-interface state records
  (`states`), not just the ids — the health record needs the `connected` flag.

Both fixes reuse the upstream records instead of mirroring them, so the shape
cannot drift from what `homematicip_local` reads.

# Version 2026.7.10 (2026-07-13)

Frontend compat, parts 3 + 4: the **CCU dashboard**, plus the tail of the audit
(direct-link paramset writes, the unsupported `determine_parameter`, and the
cache-clear scope). The dashboard was unreachable on a loom entry and, once
reachable, most of its commands broke on shape mismatches.

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

- Fix: **a LINK-paramset write now takes the daemon's edit lock.** The daemon
  gates `PUT /devices/{addr}/link-ps/{peer}` behind a per-resource edit lock and
  rejects a token-less write with `423 Locked`. `LinksOperations.put_link_paramset`
  opens the session (`POST /sessions/edit`, key `channel:{addr}:LINK:{peer}`),
  passes the token as `X-Edit-Token` and releases the lock again — even when the
  write fails.
- Fix: **`DeviceClient.put_paramset` accepts `paramset_key_or_link_address`.**
  `homematicip_local` spells the selector that way on the _write_ path
  (`ws_put_link_paramset`) and `paramset_key` on the read path, mirroring
  aiohomematic. The write previously died with a `TypeError` at argument binding
  (the required `paramset_key` was never passed) before the daemon was reached.
- Fix: **`DeviceClient.determine_parameter` raises a catchable error.** The
  daemon exposes no determine-parameter endpoint (aiohomematic drives it over
  raw XML-RPC), and the missing method surfaced as an `AttributeError` — which
  escapes `except BaseHomematicException` and reaches the panel as a generic
  `unknown_error`. It now raises the new `LoomUnsupportedOperationError` (a
  `BaseLoomException`, hence an aiohomematic exception), so the handler reports
  `determine_failed` with a message that says why. Real support needs a daemon
  endpoint.
- Fix: **`cache_coordinator.clear_all` clears what aiohomematic clears.** It
  reset only the daemon's persistent VALUES cache, whereas the reference drops
  device + paramset descriptions, device details and the data cache. It now calls
  the daemon's `POST /admin/cache/clear`; the narrower values-cache reset stays
  available on `client.diagnostics.reset_values_cache`.

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
