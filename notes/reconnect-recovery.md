# Reconnect and recovery behaviour

What the client does when the connection to the daemon breaks, the daemon is
absent, the version does not match, or the daemon itself has not yet reached the
CCU — and where that behaviour is incomplete today.

**The goal of the mechanism:** be reconnected as fast as the daemon allows,
without hammering an unavailable one in the meantime.

**As of 2026-08-27**, verified against this repository's working tree and
against the `openccu-loom` daemon checkout. Every claim below carries its source
as `file:line`. Anything derived rather than measured says so in those words.

---

## 1. The three independent recovery layers

The client has no central "connection state". It has three mechanisms that know
nothing about each other:

| Layer             | Owner                                 | Recovery mechanism                                                                                                                                                                                |
| ----------------- | ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| REST              | `HttpTransport` (`transport/http.py`) | No reconnect loop of its own. `aiohttp` re-establishes TCP per request; failures are retried with backoff inside one shared deadline (`http.py:73`, `http.py:317`) and then raised to the caller. |
| WebSocket         | `WsTransport` (`transport/ws.py`)     | Endless reconnect loop with a backoff ladder, a resume cursor and a heartbeat deadline (`ws.py:335`).                                                                                             |
| Model consistency | `LoomClient` (`client.py`)            | Re-bootstrap on `replay_lost` or queue overflow, de-duplicated and rate-limited (`client.py:562`).                                                                                                |

So the HTTP layer recovers implicitly, the WS layer explicitly, and the model
layer only when the WS layer tells it to. **No path exists where a REST failure
alone triggers recovery** — that is the root of several gaps below.

### What guards against an event storm today (measured)

- **Backoff ladder** `(0.5, 2.0, 5.0, 15.0, 30.0)` s, clamped to 30 s afterwards
  (`ws.py:56`, `ws.py:385`). A permanently dead daemon gets one connection
  attempt every 30 s — no tight loop.
- **Healthy-connection gate:** the ladder resets only after a connection that
  stayed up ≥ 10 s (`ws.py:62`, `ws.py:378`). A daemon that accepts the upgrade
  and immediately closes still escalates the ladder.
- **One shared deadline across all REST retries:** worst case is
  `request_timeout_seconds` (30 s), not N × timeout (`http.py:317`).
- **Retry only on idempotent verbs** `GET/HEAD/PUT/DELETE` (`http.py:58`);
  `execute_program` and `invoke_custom_data_point` are never retried.
- **Re-bootstrap dedup + cooldown:** a second trigger during a running walk is
  dropped, and so is one arriving within 30 s of the previous walk finishing
  (`client.py:119`, `client.py:578-596`).
- **Overflow latch:** one overflow episode produces exactly one warning and one
  resync, not one per dropped event (`ws.py:482`).
- **401/403 ends the loop** instead of spinning forever against a dead
  credential (`ws.py:344-361`).

These are sound and they hold. The gaps are elsewhere: in what happens _after_ a
successful reconnect.

---

## 2. How the client learns the daemon is functional

Short answer: **it does not.** `connect()` establishes that the daemon is
_contract-compatible_, never that it is _working_ — and nothing re-establishes
even that afterwards.

### What `connect()` actually checks (`http.py:130-186`)

One `GET /info`, from which three things are read: `api_version` (hard
compatibility gate, `http.py:219`), `capabilities` (only the caller-required
ones, `http.py:164`), and `schema_digest` (build drift, warning only,
`http.py:190`). A daemon that answers `/info` passes all three while sitting in
`waiting_for_ccu` with zero devices.

The capability list is explicitly not a liveness signal. The daemon's own
contract says so — "A token means the daemon is CONFIGURED for that capability,
not that the subsystem is working at this instant. […] For what is running right
now, read `/health`, whose components report liveness"
(`assets/openapi.yaml`, `Info.capabilities`) — and this client repeats it in
`has_capability`'s docstring (`client.py:246`) and in `capabilities.py:13`.

### What the daemon offers, and what the client does with it

| Signal                           | What it carries                                                                                                                                                                                                                                                                                                                                                                                                                                                   | Client use                                                                                                                                                        |
| -------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET /health`                    | `healthy` / `degraded` / `unhealthy` / `unknown` plus one component per subsystem; HTTP 503 when unhealthy. The collapse rule is already daemon-side: `sqlite` and `central` are critical (any one unhealthy → unhealthy), all south-bound interfaces down → unhealthy, a single interface or the MQTT bridge down → degraded (`internal/health/tracker.go:372-409`, `:460-462`). Unauthenticated, mounted next to `/info` (`internal/north/rest/router.go:717`). | `system.get_health()` exists (`operations/system.py:38`) and is **called nowhere in production code** — only in `tests/e2e/test_daemon_handshake.py:30`.          |
| `GET /system/ccu`                | Per central: `available: bool` and `readiness{phase, ready, interfaces_loaded, interfaces_total}` with `phase ∈ unknown / waiting_for_ccu / loading_hub / loading_devices / ready` (`internal/north/rest/handlers/system_ccu.go:96-101`).                                                                                                                                                                                                                         | The endpoint **is** called (`adapter.py:1678`) — and everything except `serial` is discarded. `available` and `readiness` are read from the wire and thrown away. |
| `central.readiness_changed` (WS) | The same readiness record as a live push.                                                                                                                                                                                                                                                                                                                                                                                                                         | Event type exists (`events/types.py:192`), subscribed by nobody.                                                                                                  |
| `system.status_changed` (WS)     | Aggregated system health (interfaces, connectivity).                                                                                                                                                                                                                                                                                                                                                                                                              | Event type exists (`events/types.py:212`), subscribed by nobody.                                                                                                  |
| `GET /interfaces`                | Per interface: `id`, `interface`, `connected`.                                                                                                                                                                                                                                                                                                                                                                                                                    | Read **exactly once**, in `start()` (`adapter.py:1195`); `_ClientCoordinator._states` is written nowhere else (`adapter.py:371-374`).                             |
| `daemon_status.changed` (WS)     | Graceful-shutdown announcement.                                                                                                                                                                                                                                                                                                                                                                                                                                   | Wired (`hub_coordinator.py:825`) — but only a graceful stop announces itself.                                                                                     |

### The consequence

The only health-ish thing the client consumes is the interface list, and it
consumes it as a boot-time snapshot. `LoomCentralAdapter.health` — the record
behind HA's health card — is built from `_state` plus those frozen interface
states (`adapter.py:1160-1186`), and the comment there says outright that it is
deliberately _not_ the daemon's `/health` probe. So the card renders the moment
of startup, forever.

In practice the client treats "REST answered" as "the daemon is functional".
Those two come apart in exactly the scenarios below: B1/C1 (CCU not reached →
200 with empty lists), B3 (killed daemon → nothing at all), B5 (incompatible
upgrade under a live connection). See G9.

---

## 3. Scenario matrix

Legend: ✅ fully handled · ⚠️ partial · ❌ gap (see §5).

### A — Network and host

| #   | Scenario                                                   | Behaviour today                                                                                                                                               |     |
| --- | ---------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- | --- |
| A1  | DNS resolution fails                                       | WS: `aiohttp.ClientError` → generic branch → backoff ladder (`ws.py:367`). REST: `LoomTransportError` after up to three attempts inside the 30 s budget.      | ✅  |
| A2  | Host unreachable (no route)                                | As A1 — the exception type is irrelevant to the loop, which treats every failure alike.                                                                       | ✅  |
| A3  | TCP `connection refused` (host alive, daemon process dead) | As A1. Steady state: one attempt every 30 s.                                                                                                                  | ✅  |
| A4  | TLS handshake fails (expired cert, `verify_tls=True`)      | As A1 — endless retries every 30 s. A certificate failure is not transient, though; the operator learns of it only from `WARNING` logs.                       | ⚠️  |
| A5  | Short network flap (< 1 s)                                 | First retry after 0.5 s, resume via the `since` cursor (`ws.py:408-419`). Missed events are replayed from the daemon's ring.                                  | ✅  |
| A6  | Half-open connection (NAT timeout, Wi-Fi drop without FIN) | Inbound-ping deadline of 60 s against the daemon's 30 s ping cadence (`ws.py:74`, `ws.py:421-430`; daemon `internal/north/rest/ws/client.go:41`) → reconnect. | ✅  |
| A7  | Reverse proxy answers 502/504 instead of the daemon        | REST: `LoomUpstreamUnavailableError` is retryable (`http.py:62`). WS: a handshake failure other than 401/403 goes on the backoff ladder.                      | ✅  |

### B — Daemon lifecycle

| #   | Scenario                                                     | Behaviour today                                                                                                                                                                                                                                            |       |
| --- | ------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----- |
| B1  | Daemon is starting; REST answers, CCU bring-up still running | `GET /snapshot` returns **200 with empty lists** — the handler never answers 5xx (`internal/north/rest/handlers/snapshot.go:102`). The client bootstraps an empty model successfully.                                                                      | ❌ G4 |
| B2  | Daemon shuts down gracefully                                 | `daemon_status.changed` is broadcast and applied to `DaemonConnectionDp` (`compat/aiohomematic/central/hub_coordinator.py:825`), then the connection drops into the normal reconnect loop.                                                                 | ✅    |
| B3  | Daemon killed hard (no last will)                            | Only the connection loss itself. `DaemonConnectionDp` stays `True`, `LoomCentralAdapter.available` stays `True` (`adapter.py:1134`). HA keeps rendering values as if they were live.                                                                       | ❌ G5 |
| B4  | Daemon restart (`seq` restarts at 0)                         | The daemon recognises a cursor from the previous incarnation (`since > seqNext`) and answers `replay_lost` (`internal/north/rest/ws/hub.go:362`). The client re-bootstraps — but does not adopt the anchor it was handed.                                  | ❌ G1 |
| B5  | Daemon upgraded to an incompatible API version at runtime    | The handshake runs **only** in `connect()` (`http.py:219`). A WS reconnect does not repeat it. The mismatch first shows up as a pydantic validation error in individual calls.                                                                             | ❌ G6 |
| B6  | Version already incompatible at `connect()`                  | `_check_api_version` raises cleanly and `connect()` tears the session down (`http.py:219-243`, `http.py:171`). But the type is `LoomTransportError` — the same as "host unreachable", so a caller cannot tell "retry later" from "hopeless until upgrade". | ⚠️ G6 |

### C — CCU (south of the daemon)

| #   | Scenario                                              | Behaviour today                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |       |
| --- | ----------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----- |
| C1  | Daemon up, CCU never reached (`waiting_for_ccu`)      | As B1: empty model, `state = Running`, `available = True`. The daemon does broadcast `central.readiness_changed` (`pkg/hmevent/catalogue.go:143`) and the client has the event type (`events/types.py:192`) — **nobody subscribes to it** (no hit in `bridge.py`, `refresh.py`, `adapter.py`).                                                                                                                                                                                    | ❌ G4 |
| C2  | CCU drops out at runtime                              | The daemon answers writes with `upstream_unavailable` (502), typed here as `LoomUpstreamUnavailableError` and retryable (`http.py:62`). Device availability rides `device.availability_changed`.                                                                                                                                                                                                                                                                                  | ✅    |
| C3  | CCU returns, daemon re-pulls                          | The daemon fires `CentralSouthboundReadyEvent` → `publishCentralSnapshot` → `Hub.SignalResync()` → `replay_lost` to every WS client (`internal/central/adapter/eventbridge.go:740`, `internal/north/rest/ws/hub.go:109`). The client re-bootstraps its store; the compat layer is not told.                                                                                                                                                                                       | ❌ G4 |
| C3b | The same re-pull, seen from the `device.created` side | **Fixed daemon-side in 0.65.2 / api 7.14.0.** The daemon answers the CCU's `listDevices` with an empty array, so the CCU re-announced its whole inventory through `newDevices` after every reconnect and each device was passed through as a `device.created` broadcast. `HandleNewDevices` now announces only addresses the device registry does not already hold (`internal/central/coordinators/device.go:299-345`). The client-side fan-out guard is still worth having (G2). | ⚠️ G2 |

### D — Stream consistency

| #   | Scenario                                                         | Behaviour today                                                                                                                                                                              |             |
| --- | ---------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------- |
| D1  | Replay ring aged out (> 1024 events missed; daemon `hub.go:47`)  | `replay_lost` → `_on_replay_lost` → rate-limited re-bootstrap (`client.py:562`).                                                                                                             | ✅ (but G1) |
| D2  | The daemon's `SignalResync` (boot snapshot, broker reconnect)    | The same path as D1 — the daemon deliberately reuses the frame.                                                                                                                              | ✅ (but G4) |
| D3  | Local queue overflow (slow consumer, e.g. during a re-bootstrap) | Two ceilings — 4096 envelopes and 64 MiB (`ws.py:80`, `ws.py:90`) — drop plus exactly one resync per episode, latch cleared only below the low-watermark (`ws.py:482`, `ws.py:320`).         | ✅          |
| D4  | Malformed or unknown frames                                      | An unknown `kind` is coerced to `change` rather than dropped (`ws.py:564`); structurally broken frames are dropped with a length-capped log (`ws.py:102`). The reader never dies on a frame. | ✅          |

### E — Authentication

| #   | Scenario                      | Behaviour today                                                                                                                                                                                                                                                                                              |       |
| --- | ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----- |
| E1  | Token expires while connected | The daemon closes the connection itself (`internal/north/rest/ws/client.go:424-437`). The client reconnects with the same expired token → 401 → the loop ends and `on_auth_failed` fires. `LoomClient.start_events()` does **not** wire that callback (`client.py:412-418`). The event stream dies silently. | ❌ G3 |
| E2  | Token revoked                 | As E1.                                                                                                                                                                                                                                                                                                       | ❌ G3 |
| E3  | In-band `reauth` rejected     | `LoomTransportError` to the caller plus `on_auth_failed` (`ws.py:610`) — the caller is explicit here, so it is visible.                                                                                                                                                                                      | ✅    |
| E4  | Wrong credential at startup   | `connect()` raises `LoomHttpError` (401); the adapter's `start()` cleans up and re-raises (`adapter.py:1249-1255`).                                                                                                                                                                                          | ✅    |

---

## 4. The expensive path in detail: "the daemon was gone and came back"

Assembled from the measured constants:

1. `t+0` the daemon dies. The client notices immediately (FIN/RST) or after at
   most 60 s (ping deadline, `ws.py:74`).
2. Reconnect attempts at 0.5 / 2 / 5 / 15 / 30 / 30 … s. **Steady state: one TCP
   attempt every 30 s.** That is the answer to "do not hammer it", and it holds.
3. The daemon returns. The next attempt finds it, on average after 15 s
   (_derived_ from the clamped 30 s cadence; there is no jitter — see G7).
4. The client sends `subscribe` with `since=<old seq>`. The daemon recognises
   the cursor as pre-restart and answers `replay_lost` (`hub.go:362`).
5. The client re-bootstraps: `GET /snapshot?include=data_points`, then one
   `GET /devices/{addr}` **per device** (`client.py:286-328`).
6. In parallel: if the client was connected during the daemon's bring-up, one
   `device.created` per device → one unbounded reconcile per device (G2).
7. Once bring-up completes the daemon additionally fires `SignalResync` → a
   second `replay_lost`. The 30 s cooldown absorbs it, _provided_ the first walk
   is still running or has just finished (`client.py:578-596`).

Step 5 is paid for and accepted (the N×M cost is documented in `CLAUDE.md`).
Step 6 is not — it doubles the load in an uncontrolled way and hits the daemon
in its busiest moment.

---

## 5. Gaps

**All nine are fixed** as of the reconnect-recovery work; the analysis below is
kept because it is the record of _why_ each change exists, and a reader hitting
one of these behaviours years from now needs the reasoning, not just the diff.
Each entry names what shipped. G2's trigger was additionally removed daemon-side
in 0.65.2 (§6).

### G1 — the `since` cursor is not re-anchored on `replay_lost` — **fixed**

`_handle_control` logs `oldest_seq` and fires the resync but never moves the
cursor (`ws.py:512-521`); `_last_seq` grows monotonically only
(`ws.py:461-468`). The daemon documents `OldestSeq` explicitly as "the anchor to
resume from" (`internal/north/rest/ws/hub.go:348`).

Consequence after a daemon restart: `_last_seq` keeps the previous
incarnation's high value, because every new envelope carries a smaller `seq`.
Every later reconnect sends the same too-high `since`, the daemon answers `Lost`
again — and **the client walks the full snapshot each time**, even where the
replay ring could have served it. This persists until the daemon's counter
overtakes the stale value.

Fix: set `_last_seq` to the supplied anchor in the `replay_lost` branch (or to
`None` when the value is missing/invalid). **Not** on the overflow path — the
cursor is correct there, and the `-1` sentinel (`ws.py:504`) would destroy it.

### G2 — unbounded `device.created` fan-out — **fixed**

`_on_device_created` spawns one background task per event with no semaphore, no
coalescing, and no check whether the store already holds the device complete
(`client.py:527-560`). Each task issues `GET /devices/{addr}` plus one
`GET …/data-points` per channel.

**Correction (2026-08-27).** An earlier revision of this document blamed
`applyPull` (`internal/central/coordinators/device.go:198`, `Source: NEW`). That
is a dead path: `InitialPull` — and `RefreshAfterPair`, its only wrapper — has no
caller outside its own tests. The real burst came from `HandleNewDevices`: the
daemon answers the CCU's `listDevices` with an empty array by design
(`internal/central/adapter/callback_handlers.go:527`), so the CCU re-announces
its complete inventory through `newDevices` after every reconnect, and each entry
was published as a `device.created`. The creation source was inverted on top of
that — a re-announced device reported `NEW`, a device the daemon had never seen
reported `REFRESH` — so a client filtering on `NEW` would have received exactly
the noise and missed every real arrival.

Daemon 0.65.2 / api 7.14.0 fixes both: only an address the device registry does
not already hold is announced, and `source` says which kind of news it is — `NEW`
pairing, `REFRESH` factory-reset re-pair, `MANUAL` operator accept, `CACHE` boot
restore from the persisted description cache. At boot the registry is already
filled by `DevicePipeline.Ingest`
(`internal/central/adapter/device_pipeline.go:412`) against the same
`*registry.DeviceRegistry` the coordinator holds (`internal/central/central.go:309`,
`:347`), so the CCU re-announcement that follows produces no frames at all.

What remains here is defence in depth, and cheap: (a) return early from the
reconcile when the store already carries the device with its channels and data
points; (b) an `asyncio.Semaphore` around `_spawn_background`; (c) ignore
`source == CACHE`, which the bootstrap walk covers anyway.

### G3 — `on_auth_failed` is wired nowhere — **fixed**

`WsTransport` offers the callback and fires it correctly (`ws.py:344-361`);
`LoomClient.start_events()` passes only `on_replay_lost` (`client.py:412-418`).
After a 401 the transport sets `_stopped`, `events()` ends, `_dispatch_loop`
exits cleanly (`client.py:515`) — and nobody is told. Push is dead, HA keeps
showing the last values, `available` stays `True`.

Fix: wire the callback and surface it, so the adapter can move to
`Degraded`/`Stopped` and HA can start a re-auth.

### G4 — the compat layer does not re-bootstrap itself — **fixed**

`_run_rebootstrap` calls `LoomClient.bootstrap()` (`client.py:600`). That fills
the store. It does **not** call:

- `_bootstrap_custom_data_points`, `_bootstrap_schedules`,
  `_bootstrap_combined_data_points`, `_bootstrap_hub_catalogue` — those live in
  `adapter.start()` only (`adapter.py:1198-1205`);
- `_emit_data_points_created` (`adapter.py:1243`), the one thing that makes HA
  spawn entities.

The `LoomDataPointsCreatedEvent` the client publishes is subscribed by nobody in
the compat layer (no hit in `refresh.py`).

Consequence in the most important scenario (B1/C1): the client starts while the
daemon has not reached the CCU, bootstraps an empty model and announces zero
entities to HA. The CCU comes up, the daemon sends `SignalResync`, the client
refills its store correctly — **and HA sees none of it** until the config entry
is reloaded. The same holds after every `replay_lost` for devices that appeared
in the meantime.

Fix: the client needs a "re-bootstrap finished" hook where the adapter repeats
the rest of its own bootstrap and re-fires `_emit_data_points_created`. The
announce paths already guard against double-announcing
(`_announced_alarm_panel_ids`, `registered=False`) — _derived_ from
`adapter.py:1327-1385`, not confirmed by a run.

Additionally: subscribe `central.readiness_changed` instead of inferring
readiness from an empty snapshot. The event exists on both sides
(`events/types.py:192`; daemon `pkg/hmevent/catalogue.go:143`) and carries the
phase `waiting_for_ccu → loading_hub → loading_devices → ready`.

### G9 — the client never asks whether the daemon is functional — **fixed**

The root cause behind G4 and G5, and worth stating separately: `connect()`
verifies contract compatibility, not liveness (see §2). `GET /health` — an
unauthenticated endpoint with a ready-made `healthy/degraded/unhealthy` collapse
and a 503 on unhealthy (`internal/health/tracker.go:372`) — has a client method
(`operations/system.py:38`) that production code never calls. `GET /system/ccu`
is called but its `available` and `readiness` fields are discarded
(`adapter.py:1678`). `central.readiness_changed` and `system.status_changed`
have event types here and no subscribers.

Fix: read `/health` at the end of `connect()` and again after a WS reconnect,
and gate `bootstrap()` on `readiness.ready` from the `/system/ccu` entry the
adapter already fetches — instead of bootstrapping an empty snapshot and calling
it success. That single field turns B1/C1 from "silently wrong" into "wait for
the push".

### G5 — no availability signal downwards — **fixed**

`LoomCentralAdapter._state` is assigned exactly three times: `Starting` and
`Running` in `start()`, `Stopped` in `stop()` (`adapter.py:1196`, `:1248`,
`:1576`). A WS drop, a 60 s ping timeout, a dead daemon — none of them changes
`available` (`adapter.py:1134`). `CentralState.Degraded` exists in the enum and
is never assigned.

The only substitute is `DaemonConnectionDp`, moved solely by the
`daemon_status.changed` push (graceful stop only) and by the 300 s hub reconcile
(`adapter.py:122`, `:1318`), whose failures are swallowed at DEBUG.

Fix: surface WS connection-state transitions as callbacks (`connected` /
`disconnected`) and map them onto `Degraded` in the adapter.

### G6 — version incompatibility is neither typed nor re-checked — **fixed**

Two separate halves:

- `_check_api_version` raises `LoomTransportError` (`http.py:252`) — the same
  type as "host unreachable". A caller that wants to distinguish
  `ConfigEntryNotReady` from `ConfigEntryError` cannot, and retries a hopeless
  setup forever.
- The handshake runs only in `connect()`, so a daemon upgraded under a live
  connection is never noticed (scenario B5).

Fix: a dedicated `LoomIncompatibleVersionError`, and an `/info` re-check when a
WS connection comes back after a longer outage.

### G7 — no jitter in the reconnect backoff — **fixed**

`_RECONNECT_BACKOFF` is a fixed sequence (`ws.py:56`). Several clients that
lived through the same outage return in lockstep — on a restart, exactly while
the daemon is pulling the CCU. ±20 % jitter costs nothing.

### G8 — a second `start_events()` leaks the previous SubscriptionGroup — **fixed**

`start_events()` reassigns `self._wire_group` without cancelling the previous
group (`client.py:428`). The old subscriptions stay on the bus
(`events/bus.py:151`) and every event is applied to the store twice. The entry
guard only protects while the dispatch task is alive (`client.py:409`) — after
an auth abort (G3) it has finished, and that is exactly when a second call is
the obvious recovery.

---

## 6. Does the daemon need changes?

Mostly no. Seven of the nine gaps are purely client-side: the daemon already
emits or serves every signal the fix needs, and this client ignores it. The two
that did need daemon work — D1 and D2 below — **shipped in daemon 0.65.2 /
api 7.14.0**; both are verified against that tree (`go build ./...` and the
`internal/central/coordinators`, `internal/north/rest/{handlers,ws}`,
`internal/security` and `tests/contract` suites all pass).

**Already there, nothing to do:**

- **G1** — `replay_lost` already carries `OldestSeq`, documented as "the anchor
  to resume from" (`internal/north/rest/ws/hub.go:348`).
- **G4** — the readiness push exists end to end: produced in
  `internal/central/adapter/ccu_wiring.go:336`, bridged at
  `internal/central/adapter/eventbridge.go:323` → `:2553`, published on topic
  `central.<name>.readiness` (`internal/north/rest/ws/payloads.go:284`), which
  the client's default subscription `central.*` already covers
  (`client.py:_DEFAULT_WS_SUBSCRIPTIONS`). The bridge gates only on
  `wsHub != nil`, not on MQTT, so it fires on an MQTT-less daemon too. The
  `SignalResync` fallback exists as well.
- **G9** — `/health` is unauthenticated and mounted beside `/info`
  (`internal/north/rest/router.go:717`) with the collapse rule already applied
  server-side; `/system/ccu` serves `available` + `readiness`.
- **G6** — `/info` carries `api_version` for the re-check, and `started_at`, with
  which a client can detect a daemon restart without the WS at all.
- **G3 / G5 / G7 / G8** — no daemon involvement.

### D1 — `expires_at` on `GET /auth/me` — **shipped in 0.65.2 / api 7.14.0**

`meResponse` now carries `expires_at` (pointer, so "no server-side expiry" omits
the field rather than marshalling a zero time), `POST /auth/login` reports the
lifetime of the session it just issued rather than the resolver identity's empty
deadline, and `{op:"reauth_ok"}` carries the same instant so a long-lived socket
can schedule its next refill without a REST round trip
(`internal/north/rest/handlers/auth.go:257-270`, `:309-322`, `:368-380`;
`internal/north/rest/ws/client.go:397-405`, `:551-562`).

Client-side follow-up, none of it done yet: regenerate `openccu-loom-types`
against 7.14.0 so `Identity` carries the field, read it at `connect()`, and
schedule an in-band `reauth` before the deadline instead of walking into the 401
(scenario E1, G3).

### D2 — `device.created` on re-announcement — **shipped in 0.65.2 / api 7.14.0**

`HandleNewDevices` now announces only addresses the device registry does not
already hold, and decides the source before the registry write, so `NEW` means a
pairing and `REFRESH` a factory-reset re-pair
(`internal/central/coordinators/device.go:299-345`). Suppression covers the event
only — descriptions and registry writes still happen, so an updated firmware
revision on a re-announcement still reaches the model
(`internal/central/coordinators/device.go:355-370`). `wsapi.json` documents the
four source values on the `device.created` entry.

The fix went further than this document asked for. The ask was "make the two
distinguishable"; the daemon removed the burst instead — which is the better
answer, because it costs every consumer nothing rather than costing each of them
a filter.

## 6. What deliberately stays as it is

- **No reconnect loop on the HTTP layer.** `aiohttp` establishes TCP per
  request; a loop of our own would only duplicate the retry mechanism in
  `request()`.
- **`connect()` does not bootstrap.** The `connect` / `bootstrap` /
  `start_events` split is deliberate (see `CLAUDE.md`) so a caller can choose
  snapshot mode over push mode. Recovery must respect that split rather than
  collapse it into one `reconnect()`.
- **The N×M bootstrap walk.** Known, and addressed daemon-side (a streamed
  snapshot is an open ask); none of it belongs in the recovery logic.

---

## 7. What shipped

| Gap | Fix                                                                                                        | Where                                                 |
| --- | ---------------------------------------------------------------------------------------------------------- | ----------------------------------------------------- |
| G1  | `replay_lost` re-anchors `_last_seq` on the daemon's anchor; the overflow path deliberately does not       | `transport/ws.py`                                     |
| G2  | `source == CACHE` skipped, complete devices skipped, fan-out capped at 4                                   | `client.py`                                           |
| G3  | `on_auth_failed` wired → `AuthFailedEvent`; adapter degrades                                               | `client.py`, `compat/…/adapter.py`                    |
| G4  | `set_rebootstrap_hook` → the compat layer rebuilds its model and re-announces                              | `client.py`, `compat/…/adapter.py`                    |
| G5  | WS connection transitions → `ConnectionStateChangedEvent`, `LoomClient.connected`, `CentralState.Degraded` | `transport/ws.py`, `client.py`, `compat/…/adapter.py` |
| G6  | `LoomIncompatibleVersionError`; `recheck_contract()` on every reconnect                                    | `exceptions.py`, `transport/http.py`, `client.py`     |
| G7  | ±20% jitter on the backoff ladder                                                                          | `transport/ws.py`                                     |
| G8  | `start_events()` cancels the previous wire group                                                           | `client.py`                                           |
| G9  | `get_health()`, `get_readiness()`, `wait_until_ready()`; adapter gates its bootstrap                       | `client.py`, `compat/…/adapter.py`                    |

Two choices worth keeping visible, because both look like omissions:

- **`available` stays True while Degraded.** A WS drop makes the store stale,
  not wrong: REST is very likely still reachable and writes may well succeed.
  Flipping every entity to unavailable on a five-second reconnect would be worse
  than the staleness it reports.
- **`wait_until_ready()` giving up is not an error.** It returns False, the
  bootstrap runs anyway, and the daemon's resync push re-bootstraps when the CCU
  arrives — which now reaches the compat layer too (G4). The wait only avoids
  paying for a walk that is known to be empty.
