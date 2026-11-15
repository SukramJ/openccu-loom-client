# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Async Python REST + WebSocket client for the [openccu-loom](https://github.com/SukramJ/openccu-loom) daemon. Status: Beta.

**Strategy — alternative backend, not a replacement.** For the foreseeable future `openccu-loom*` is _not_ meant to replace `aiohomematic`; it is an **alternative backend** for `homematicip_local` that mediates CCU contact through the daemon instead of direct XML-RPC/JSON-RPC. Both backends coexist and HA can choose between them. Because of this, **depending on `aiohomematic` at runtime is deliberate and architecturally sound**: the routing-key algorithm, the `@runtime_checkable` protocols and (selectively) model code are _reused_ from `aiohomematic` rather than re-implemented, which avoids silent drift between two implementations of the same contract. `aiohomematic` and `openccu-loom*` share one maintainer, so the coupling is coordinated, not external.

**Integration form (decided) — the compat shim.** HA dispatches on type identity: concrete `Dp*`/`CustomDp*` classes (via isinstance _tuples_ `(AioClass, LoomTwin)` in `homematicip_local`'s `backend_types.py`) plus `@runtime_checkable` protocols. That isinstance coupling makes the namespace shim under `compat/aiohomematic/` the pragmatic integration form — the twins satisfy aiohomematic's surface _structurally_ (not by subclassing, since aiohomematic's model classes are bound to a live `CentralUnit`). A clean backend abstraction inside `homematicip_local` is the deferred alternative: it would mean rebuilding the production aiohomematic integration for unclear gain, so it is only pursued if `homematicip_local` grows such a layer for its own reasons. The three follow-ups that keep the shim form robust have all shipped: a drift-guard test against the real aiohomematic protocols (`tests/compat/test_aiohomematic_protocol_parity.py`), a single import seam onto the reused internals (`compat/aiohomematic/_upstream.py`), and a version floor in `pyproject.toml` with the exact CI pin in `requirements.txt` (a library must not pin a dependency its own consumer pins too — `homematicip_local` depends on `aiohomematic` directly). Bump the pin and the drift-guard snapshot together when adopting a new aiohomematic series. Anything still open is in `notes/open-work.md`.

**Where documents live.** This repository publishes no documentation site: `README.md` is the public face, and every contributor-facing working document is in `notes/` — a single backlog, `notes/open-work.md`, plus [`notes/README.md`](./notes/README.md) explaining what belongs there. The name mirrors the daemon repo, where `notes/` is the working set and `docs/` the published MkDocs site. When citing a daemon document, use its GitHub URL rather than a repo-relative path: paths only read correctly from inside that repository and break silently when it reorganises.

**Wire types are generated, and they live here.** `openccu_loom_client/wire/` holds the Pydantic models and the enum catalogue, generated from the daemon's `assets/openapi.yaml`, `assets/wsapi.json` and `assets/schemas/enums.json`. Import them from `openccu_loom_client.wire.rest`, `.ws` and `.enums`. Everything else in this package — transport, store, event bus, domain wrappers, the aiohomematic compat layer — is hand-written, and that split is the rule: **nothing under `wire/` is edited by hand, and nothing outside it is generated.** Regenerate with `make generate` (needs the daemon repo alongside, `OPENCCU_LOOM_REPO=../openccu-loom`); the generators are ordinary source under `script/gen/`, and `wire/` is exempt from the whole ruff ruleset because its shape is the generator's.

They used to ship as a separate distribution, `openccu-loom-types`. The split cost a release of its own for every daemon release — 84 of 84 in the window measured, none carrying a hand-written line — while no consumer ever imported the package directly. A daemon release now opens a regeneration PR here (`.github/workflows/regenerate-on-daemon-release.yml`) and stops: no version bump, no auto-merge, no tag. Whether a regeneration is worth a release is a decision, and `unreleased-check.yaml` is what reports one being owed.

## Commands

```sh
python3.14 -m venv venv
source venv/bin/activate
pip install -e '.[dev]'        # runtime + dev extras from pyproject
script/setup                  # or: bootstrap dev deps (requirements_test.txt) + install pre-commit hooks

pytest                                   # full suite (asyncio_mode=auto)
pytest tests/unit/test_store.py          # one file
pytest tests/unit/test_store.py::test_x  # one test
pytest -k "value_changed"                # by name pattern

ruff check .                             # lint (line-length 120, py314 target)
ruff format .                            # format
mypy openccu_loom_client                 # strict mode is on
pylint openccu_loom_client               # extra checks beyond ruff

python script/lint_kwonly.py openccu_loom_client  # enforce keyword-only params
python script/lint_all_exports.py                 # validate grouped/sorted __all__

prek install                             # one-time: enable pre-commit hooks
prek run --all-files                     # run all hooks manually
prek run --hook-stage manual --all-files # manual-stage hooks (python-typing-update, …)
```

Requires Python ≥ 3.14. Tests use `pytest-asyncio` (auto mode — no `@pytest.mark.asyncio` needed) and an in-process aiohttp mock daemon (`tests/helpers/mock_daemon.py` `MockDaemon`) for the daemon's HTTP surface; the WS transport tests run a real `aiohttp` test server.

## Architecture

Data flows in one direction at runtime: **daemon → transport → typed event → store mutation → domain wrapper → (compat) HA event**. Write-back goes the reverse way through the store's `set_*` methods straight to the transport. The layers are deliberately decoupled so each is unit-testable in isolation.

### Layers (bottom to top)

- **`config.py`** — `LoomConfig` dataclass: host/port/tls/auth/timeout. The single place transport knobs live. Defaults: HTTP 8080 / HTTPS 8443, base path `/api/v1`.
- **`auth.py`** — `BasicAuth` / `BearerAuth` / `SessionAuth` auth methods.
- **`transport/http.py`** — `HttpTransport` wrapping `aiohttp.ClientSession`. Owns RFC 9457 `problem+json` → typed-exception parsing, retry/backoff (only on idempotent verbs `GET/HEAD/PUT/DELETE` unless `allow_retry=True` is passed), and a one-shot capability handshake against `GET /info` at `connect()`.
- **`transport/ws.py`** — `WsTransport`: WebSocket loop with subscribe/unsubscribe, heartbeat, and resume-after-reconnect via a `seq`/`since` cursor. When the daemon's replay buffer has aged out, it calls back `on_replay_lost`, which triggers a fresh bootstrap.
- **`store.py`** — `LoomStore`: in-memory mirror of one daemon's CCU model (devices → channels → data-points, plus device-scoped custom data points, programs, sysvars). Populated by `load_snapshot` + `attach_*` during bootstrap and mutated by `apply_*` from live WS events. Write-back via `set_value` / `set_sysvar` / `execute_program` / `invoke_custom_data_point`. **The store does not depend on the event bus** — the `LoomClient` wires them together. It also holds the two consumer-facing settings the model layer reads back — the HA locale (`set_locale`) and the daemon's entity-name catalogue (`set_entity_names`, `GET /i18n/entities`) — because the objects that read them are rebuilt by reconciles and pushes, and a value pushed onto an instance would have to be re-delivered on every such path.
- **`events/`** — `bus.py` (`EventBus` + `SubscriptionGroup`), `types.py` (one `LoomEvent` subclass per wire `type` discriminator), `synthetic.py`. `EventBus.publish` fans out sequentially in registration order; one handler raising is logged but never aborts the fan-out. Subscriptions optionally filter by `event_key` (typically a per-data-point routing key — see `data_point_event_key`).
- **`bridge.py`** — `bind_ws_events_to_store`: the glue subscribing the six wire→store handlers (`apply_value_changed`, `apply_device_created`, etc.) on one `SubscriptionGroup`. Kept separate from both store and bus so each tests alone.
- **`model/`** — store-aware domain wrappers (`Device`, `Channel`, `DataPoint`, `CustomDataPoint`, `Program`, `Sysvar`). Each holds a mutable wire summary that the store model-copies on update; consumers read `.value` and never touch the summary. `DataPoint.send_value` routes back through `store.set_value`.
- **`operations/`** — stateless REST façades, one module per surface area, all extending `_OperationsBase` (just a transport handle). Exposed as attributes on `LoomClient`: HA-relevant (`devices`, `datapoints`, `custom_data_points`, `hub`, `system`, `schedules`, `links`, `alarm`, `security`) and ops (`diagnostics`, `backup`, `visibility`). The six admin façades that no consumer reached — `auth`, `users`, `centrals`, `config_admin`, `groups`, `matter` — were removed from the wheel; the daemon still serves those endpoints, and `node-red-contrib-openccu-loom` speaks to them through its own client. `SessionsOperations` survives as a module because `operations/links.py` instantiates it, but it is no longer a client attribute.
- **`client.py`** — `LoomClient`, the single facade consumers use. Composes everything above. Lifecycle: `connect()` (HTTP + handshake only) → `bootstrap()` (populate the store from one `/snapshot`) → `start_events()` (open WS, wire bridge, run the dispatch loop). `connect`/`start_events` are split so callers can choose snapshot-only (REST polling) vs. full push mode.

### The bootstrap cost

`bootstrap()` used to be N×M REST calls — one detail call per device, one data-point call per channel. Both are closed. The M went with the nested snapshot; the N went with daemon api 7.23.0, which lifts `firmware` and `availability` onto `DeviceSummary` — and since the snapshot's `Channel` is a `ChannelSummary` carrying its data points, one `GET /snapshot?include=channels,data_points` yields the whole graph. **A bootstrap is one request.** The per-device detail path is kept as a fallback for a daemon that ignores `include`, and `GET /devices/{address}` still serves a single-device refresh.

One related capability exists and is deliberately unused: the daemon serves `/snapshot` as an NDJSON stream under `Accept: application/x-ndjson` (see the note in `operations/system.py`); this client consumes the nested JSON envelope instead. Not a daemon gap.

**The snapshot is scoped to one CCU.** `GET /devices` and `GET /snapshot` both accept `?central=`, and `bootstrap()` passes it. `_pin_central_id()` resolves the central from `GET /system/ccu` first, by the same rule `LoomStore._infer_central_id` uses to filter the store — the configured central name when the daemon knows it, the sole entry when the daemon mediates one, nothing otherwise. Unresolved means an unscoped snapshot, which is the older behaviour and never an error: sending a name the daemon does not know would filter the response to nothing and leave the consumer with no entities at all. This used to be listed here as deliberately unused, on the grounds that the compat layer filters anyway; on a daemon fronting two CCUs that meant every Home Assistant entry pulled, parsed and discarded the other CCU's whole device tree.

### The compat shim — `compat/aiohomematic/`

A namespace shim that presents `aiohomematic`'s public surface so existing `homematicip_local` imports keep working during the cutover. Two key pieces:

- **`central/adapter.py`** — `LoomCentralAdapter` presents aiohomematic's `CentralUnit` + coordinator surface (`device_coordinator`, `hub_coordinator`, `query_facade`, …) on top of a `LoomClient`. The entity-spawn surface runs on aiohomematic's _categorized data-point model_: the store exposes `set_data_point_factory` / `set_custom_data_point_factory` hooks the compat layer uses to make the store hold categorized `Dp*` / `CustomDp*` subclasses (so HA-side `isinstance` dispatch works on the live store objects), and the adapter additionally builds the hub singletons (`model/hub/singletons.py`: alarm/service messages, inbox, metrics, connectivity, system update, add-on self-update (capability-gated on `GET /system/addon-update` `supported`), install mode — polled every 30 s via `hub_coordinator.fetch_hub_singleton_data`), the schedule layer (`model/week_profile.py`: `WeekProfileDp` sensor + `ScheduleChannelSwitch` per channel key) and the combined duration number (`model/combined.py`: `DURATION_VALUE` + `DURATION_UNIT` → one seconds-typed number).
- **`central/refresh.py`** — `install_refresh_bridge`: fans the daemon's three distinct value events (`DataPointValueChangedEvent`, `CustomDataPointStateChangedEvent`, `SysvarChangedEvent`) into the single `DataPointStateChangedEvent` (keyed by `unique_id`) that HA entities subscribe to. The unique-id format must stay in lock-step between this bridge and the compat data-point layer — both derive it from `data_point_event_key` / `*_unique_id`.

## Which model does which work

Set by the owner for the go-fabric family on 2026-10-10 (go-fabric's
`CLAUDE.md`); the review split of 2026-11-08 (go-fabric #456) likewise.
Adopted here on 2026-11-08; re-cut by the owner on 2026-11-15, mirrored
from go-fabric #529 and openccu-loom #1108 (in the owner's words: "Wichtig
ist, dass die Qualität hoch ist, aber Fable gezielt für die Dinge
eingesetzt wird, die es wirklich besser kann"). The bar is unchanged: a
statement about the daemon's wire contract or aiohomematic's protocol
surface that lands in this repo is cited later as fact — a comment that
names a caller or a consumer, a docstring that states what the other side
does, a `changelog.md` line — so it comes from the strongest model. But
the strongest model does only the part that needs it: reading the
reference and judging. The writing, the mechanics and the code review are
Opus. The agents in [Sub-Agent Delegation](#sub-agent-delegation) carry a
default model each; the rows below decide when a call overrides it
(`model:` on the Agent tool). Implementation defaults to Opus.

| Work                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | Model                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Coordination** of a slice: starting agents, integrating, guards, pull requests, status                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      | **Opus**, in a fresh session per slice (the state is in the notes and the memory, never in a long context)                                                                                                                                                                                                                                                                                                                                                            |
| **Planning and decisions:** the slice plan; the decision briefs for the owner; _which_ guard gets built and its bite line; the verdict where the daemon's wire contract, aiohomematic's protocol surface and a `homematicip_local` expectation contradict each other                                                                                                                                                                                                                                                                                                                          | **Fable**, as a targeted agent                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| **Reading the reference and judging:** what the daemon's wire contract (`assets/openapi.yaml` / `assets/wsapi.json` of the release the wire layer was generated from), aiohomematic at the pin in `requirements.txt` and `homematicip_local` do or expect, and what this repo's own wiring does (who consumes an event, who calls a seam) — for `notes/`, the README, `changelog.md` and every comment or docstring that names an openccu-loom API behaviour, an aiohomematic call shape, a `homematicip_local` expectation, a caller or a consumer; the adversarial read of the `hunt` agent | **Fable** gives the verdict with the lines as a short fact note; **Opus** writes the entry or the comment in the file's form; **Fable** reads the finished text once against its note. Every reference statement comes from Fable                                                                                                                                                                                                                                     |
| **Implementation**, also on the paths the next section keeps out of an agent's hands (auth and transport — `openccu_loom_client/auth.py`, `config.py`; the box gate and pairing — `boxgate.py`, `boxpairing.py`, `pairing.py`; the aiohomematic call-shape parity); a compat twin, a capability check; tests; `changelog.md` and docs; rebases and merge conflicts. The wire layer (`openccu_loom_client/wire/`) is regenerated, never edited                                                                                                                                                 | **Opus** (the `impl` default; also what fast mode runs on) — with the Fable read of the next row before it merges where it touches one of those paths or mirrors a reference                                                                                                                                                                                                                                                                                          |
| **Review** of a slice's pull request                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | **Opus** does the code review — correctness, concurrency, error paths, tests, the fixed report shape. **Fable** reads on top, bounded: behaviour against the daemon's `assets/openapi.yaml` / `assets/wsapi.json` of the release the wire layer was generated from, aiohomematic at the pin in `requirements.txt` and `homematicip_local`, the paths of the row above, and every finding of high or medium severity for confirmation before it reaches an implementer |
| **Citations**                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 | **Opus** checks every range (the file, the lines, the construct the sentence names). **Fable** reads every citation that is the reason for behaviour ("as the daemon serves", "aiohomematic calls", "homematicip_local expects"), every doubtful one and a sample of the rest                                                                                                                                                                                         |
| Work a test or generator checks immediately: `make generate`, `ruff format`, `prek run --all-files`, a table case on an existing pattern; search and first reading of a tree whose result a stronger model or the owner reads again                                                                                                                                                                                                                                                                                                                                                           | **Sonnet** (the `guard` default; `impl` with `model: "sonnet"`)                                                                                                                                                                                                                                                                                                                                                                                                       |
| Mechanical inventories whose answer is a list the caller can re-grep: which files reference X, which links are dead, which entries lack a counterpart                                                                                                                                                                                                                                                                                                                                                                                                                                         | **Haiku** (`sweep` default)                                                                                                                                                                                                                                                                                                                                                                                                                                           |

Sonnet never writes a claim about an openccu-loom API behaviour, an
aiohomematic call shape or a homematicip_local expectation into the repo.
Opus does not either: it writes what Fable's fact note states. Haiku
returns lists, never verdicts. When in doubt whether a statement is a
reference fact, it is one.

---

## Sub-Agent Delegation

As go-fabric's and openccu-loom's rule (owner, 2026-10-10). The main
conversation coordinates: integration, guards, sub-agent steering, final
verification, and push / pull request / tag. Planning, contract and wire
decisions and guard specifications come from Fable (the planning row of
[Which model does which work](#which-model-does-which-work)) and reach the
coordinator as a plan or a fact note. It still does not own typing.

**Delegate what a gate can prove; keep what only a reader can accept.**
Delegate freely: table cases, fakes, scaffolding, fixture updates, doc and
link cleanups, version bumps across the changelog set, inventories and grep
sweeps. Never delegate to an agent: the generated wire layer
(`openccu_loom_client/wire/`, regenerated, never edited), auth and
pairing, the aiohomematic call-shape parity — those are implemented in the
coordinating session, and the stop conditions of `impl`
(`.claude/agents/impl.md`) keep them out; _which_ guard gets built is
Fable's. Locating is delegable; reading is not.

| Agent   | Default model | Use for                                                     |
| ------- | ------------- | ----------------------------------------------------------- |
| `impl`  | Opus          | scoped implementation with a stated acceptance command      |
| `guard` | Sonnet        | tests from a caller-written guard spec, plus the bite proof |
| `sweep` | Haiku         | read-only inventories and grep sweeps, high fan-out         |
| `hunt`  | Fable         | adversarial read-only defect hunt, ranked candidates        |

Each default is one row of
[Which model does which work](#which-model-does-which-work); a call that
falls into another row passes `model:` on the Agent tool — `impl` with
`model: "sonnet"` for work a generator or formatter checks immediately.

**Size CPU-bound fan-out from the host, never from a constant** — the owner
works from a 4-core box and a 14-core box:

```sh
cores=$(nproc); agents=$(( (cores - 1) / share ))   # share = cores per agent
```

Check the 1-minute load first; if it is at or above the core count, run one
CPU-bound agent and queue the rest. Eight concurrent agents is a practical
ceiling whatever the core count. Every CPU-bound agent must **pin** its share
in the command it runs (`pytest tests/<area>` — one process is one core; never a parallel run from a sub-agent) — handing out a number without
pinning it changes nothing. Read-only agents cost no core worth counting;
6–8 is a practical ceiling there.

Two hard rules: **no sub-agent runs the full gate (`make test`, `make lint`) or a
repo-wide lint** — the full gate runs once, in the main conversation, at the
end of the slice — and **no two writing agents in the same package**
(disjoint file sets, or `isolation: "worktree"`).

Every brief carries five things: the files and public surface plus one style
reference; the acceptance command the agent runs itself; the invariants it
touches; **stop conditions** (new dependency, API or wire change, credential
handling, an ADR-shaped decision — a stop is a successful outcome); and a
report format ≤ 250 words, large artefacts to the scratchpad as a path.

**A report is a claim.** Read the diff, run the gate yourself. For `guard`,
the acceptance artefact is not a green test but the **bite proof**: the named
production line removed, the test observed red with its message, the line
restored, green again.

---

## Token use

Set by the owner on 2026-10-14 (efficient use of the tokens matters to the
owner), as go-fabric's rule of the same date. A slice costs on
the order of half a million to a million tokens per implementing agent
before the owner's review; the rules below are what keeps that from
doubling.

- **Read slices, not files.** `sed -n a,bp`, `grep -n` with a tight
  pattern and `head`; a whole file only when the task is the file. Never
  re-read what is already in context; cite the earlier read.
- **Delegate first reads.** A source tree's inventory (openccu-loom's `assets/`, aiohomematic, homematicip_local, this repo)
  is a `sweep` or Sonnet read written to the scratchpad with `file:line`
  for every fact; the owner spot-checks the load-bearing lines and verifies
  every fact about the daemon's wire contract or aiohomematic's protocol surface before it enters a brief or
  `notes/`, the README or `changelog.md` — the agent's result is a map, never a citation.
- **Logs by their summary.** The gate (`make test`, `make lint`) runs in the
  background with a summary file (one line per gate, `FAIL`/`ok` lines
  only); read the summary, and open a log only on failure, with `grep` for
  the failure markers and the error strings — never `cat`. The same for CI:
  the job log is fetched once to a file and grepped.
- **One turn, every independent call.** Reads, checks and background
  starts that do not depend on each other go out together; a wait is a
  `Monitor` or an `until` loop in the background, not polling by hand.
- **Short artefacts.** A brief names files, decisions and the acceptance
  command, not prose; a report follows the fixed shape of
  [Sub-Agent Delegation](#sub-agent-delegation) (≤ 250 words, large
  artefacts as a scratchpad path); status messages to the owner are short,
  outcome first, in German.
- **Skip what cannot have changed.** No regeneration for a comment change;
  no second gate run for a notes-only commit — say what was skipped and
  why.
- **Edit by script.** Several replacements in several files are one
  scripted call with asserted anchors, not a chain of interactive edits;
  a generated file (`openccu_loom_client/wire/`, `spec/consumed_operations.json`) is regenerated, never patched.
- **The strongest model for the part that needs it.** Set by the owner on
  2026-11-15. Fable reads references, judges and plans (the table above);
  it does not coordinate, format, write prose or run mechanics. A Fable
  agent's brief is bounded: the lines to read, the question to answer, a
  fact note back.
- **Few turns in a long session.** A coordinator waits for several agents
  at once and integrates them together; a status message carries new
  information or is not sent; a slice starts in a fresh session.
- **The assistant says when to clear the context.** Set by the owner on
  2026-11-15. It does not wait to be asked: it proposes a fresh session
  when a slice or a pull request is merged; when the context has been
  summarised once and the work that remains is of another kind; or when
  what it still needs fits in a short hand-over and the turns are mostly
  waiting for agents or pipelines. It does NOT propose one while agents of
  the session are running or a decision of the owner is open. The proposal
  comes with the hand-over already written — the state, the open points
  with where each goes, the next step, the lessons — into the memory file
  and the notes (never only into the chat), and with the exact way to go
  on: `/clear`, the model to switch to (`/model`), and the first message
  to send, which names the files to read.
- **The pipeline's guards before the pull request, not after.** The local
  guard set is `prek run --all-files` (what
  `.github/workflows/pre-commit.yml` runs; `make lint` is its ruff, mypy
  and pylint part) and `make test` (`pytest`, which
  `.github/workflows/test-run.yaml` runs with the daemon checked out as
  `OPENCCU_LOOM_REPO`); a regeneration branch (`regen/daemon-*`) adds
  `make generate`, which must leave `openccu_loom_client/wire/` unchanged
  (`.github/workflows/wire-regen-clean.yml`). A red pipeline costs a round
  of coordinator turns.
- **Agents read what the brief names.** An implementing agent gets the
  file list and the pinned sources in the brief, reads those, and reports
  in the fixed shape; it does not survey the tree. One agent per brief;
  two agents only on disjoint paths, at most one Opus agent at a time.
- **Push, pull request and tag are the session's own calls.** An
  implementing agent works locally — branch, commits, guards — and
  reports; the owner's session reads the diff, runs the gate, pushes,
  opens the pull request and tags the merge commit; the owner merges. A
  delegated push, pull request or tag is refused by the auto-mode
  classifier ("Create Public Surface", 2026-11-07), and the review in
  [Which model does which work](#which-model-does-which-work) sits
  between the agent's report and the push anyway.

---

## Conventions

- All public functions use keyword-only arguments (`*,`). Follow this when adding to the surface.
- Domain wrappers expose private `_replace_summary` / `_replace_state` / `_update_summary` methods the store calls to mutate live objects in place — never rebuild a wrapper on update.
- Retries: only mark a call `allow_retry=True` when it is genuinely idempotent. `set_value`/`set_sysvar` (PUT, daemon-serialized) are retried; `execute_program` and `invoke_custom_data_point` (POST, side effects like cover-open) are not.
- Every source file carries the SPDX MIT header and names the copyright holder: `# SPDX-License-Identifier: MIT` then `# Copyright (C) 2026 SukramJ.`. The generators under `script/gen/` stamp the same two lines into `openccu_loom_client/wire/`. `mypy --strict` and the full ruff ruleset (incl. `PL`, `B`, `SIM`, `ASYNC`, `UP`) must pass.

## Daemon broadcasts now live (formerly deferred)

These two broadcasts were once daemon-side gaps; the daemon now emits them and the client binds them:

- `datapoint.optimistic_rolled_back` is broadcast by the daemon and consumed as `DataPointOptimisticRolledBackEvent`. The compat refresh bridge (`compat/aiohomematic/central/refresh.py`) translates it into the HA-facing `OptimisticRollbackEvent`. The `events/synthetic.py` factory `new_optimistic_rollback_event` is still available to synthesize the same event locally from a `set_value` failure when no broadcast is in flight.
- Device trigger/keypress events are emitted on the `device.{address}.channels.{channel}.trigger` topic and bound to `DeviceTriggerEvent`. The HA _event-group_ surface on top of them is served by `query_facade.get_event_groups` (built from the store's trigger DPs).
