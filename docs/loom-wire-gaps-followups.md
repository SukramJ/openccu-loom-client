# Loom wire-gap follow-ups (client + HA integration)

**As of:** 2026-06-21
**Trigger:** openccu-loom PR #156 — _close HA-client wire gaps G2/G4/G5/G6_
(daemon `0.7.1 → 0.8.0`, **APIVersion `1.17.0 → 1.18.0`**). The daemon now
serialises previously-internal data into the north-bound contract. This note
sketches the matching client-side work so the
[`ha-client-wire-gaps`](https://github.com/SukramJ/openccu-loom/blob/main/docs/parity/ha-client-wire-gaps.md)
catalogue can be driven to ✅.

Two repos are involved:

- **`openccu-loom-client`** — the compat / transport layer (this repo).
- **`homematicip_local`** — the Home-Assistant integration that consumes it.

> **Prerequisite:** regenerate **`openccu-loom-types`** from the new
> `assets/openapi.yaml` + `assets/wsapi.json` first. That brings the typed
> models (`EventGroupSummary`, `HubDataPoints`, `HubCountChangedPayload`,
> `HubMetricChangedPayload`, `HubConnectivityChangedPayload`, the new
> `TextDisplayState` fields) into the client. All the work below assumes those
> types exist.

## Status legend

- **REQUIRED** — net-new wiring needed to consume a daemon addition.
- **OPTIMISATION** — already works via another path; the daemon addition lets
  the client do it more cheaply.
- **VERIFY / LIFT-GUARD** — no daemon change pending; confirm behaviour and
  remove a defensive fallback that is now unnecessary.

## Summary

| Gap    | Daemon now provides                               | Client change                                                                     | Kind                      | Repo        |
| ------ | ------------------------------------------------- | --------------------------------------------------------------------------------- | ------------------------- | ----------- |
| **G1** | `color: {h,s}` + `color_mode` in light state      | Read nested `color` (not flat `hue`/`saturation`)                                 | REQUIRED                  | client      |
| **G2** | 5 extra `available_*` lists in text-display state | Read them in the text-display getters                                             | REQUIRED                  | client      |
| **G3** | `SysvarSummary.is_extended` (already shipped)     | none — re-verify on a real CCU                                                    | VERIFY                    | —           |
| **G4** | `GET /hub/data-points` (single aggregate)         | (a) optionally fetch it instead of 6 calls; (b) lift the orphan-cleanup skip      | OPTIMISATION + LIFT-GUARD | client + HA |
| **G5** | `GET …/event-groups` (authoritative groups)       | Wire it for `last_triggered`/`available`; drop the `NotImplementedError` fallback | VERIFY + OPTIMISATION     | client + HA |
| **G6** | WS push topics for hub singletons                 | Subscribe, drop the 30 s poll loop                                                | REQUIRED                  | client      |
| **G7** | generic `ON_TIME` via the value route             | Route `set_on_time` to it                                                         | REQUIRED                  | client      |

---

## G1 — Light colour read-back (shape alignment)

**Reclassified:** not "state not exposed". The daemon already emits
`color_temp_kelvin` (int) and `effect` (string), and the client reads those
keys correctly. Only **HS colour** is a shape mismatch.

**Daemon emits** (light CDP `state`):

```json
{
  "color_mode": "hs",
  "color": { "h": 210, "s": 0.8 },
  "color_temp_kelvin": 4000,
  "effect": "Rainbow"
}
```

**Client today** — `compat/aiohomematic/model/custom/__init__.py:349-368`
(`CustomDpLight`):

```python
# hs_color reads FLAT keys that the daemon does not emit:
hue = self._state.get("hue")
sat = self._state.get("saturation")
```

**Change (REQUIRED):** read the nested object.

```python
color = self._state.get("color") or {}
if color:
    hue = color.get("h")
    sat = color.get("s")  # daemon sends saturation in [0,1]
# color_temp_kelvin / effect already map 1:1 — leave them.
```

Optionally key off `color_mode` (`"hs"` vs `"color_temp"`) to choose which
mode to report. Add a unit test with a `color`/`color_mode` state fixture.

---

## G2 — Text-display option lists

**Daemon emits** (text-display CDP `state`, all `[]string`, omitted when empty):
`available_background_colors`, `available_text_colors`, `available_alignments`,
`available_repetitions`, `available_intervals` — alongside the existing
`available_icons` / `available_sounds`.

**Client today** — `compat/aiohomematic/model/custom/__init__.py:965-996`
(`CustomDpTextDisplay`): every option getter returns a hardcoded empty tuple
`()`.

**Change (REQUIRED):** read from `self._state`, mirroring however
`available_icons` will read its list.

```python
@property
def available_background_colors(self) -> tuple[str, ...]:
    return tuple(self._state.get("available_background_colors") or ())


# same for available_text_colors / available_alignments
# (+ available_repetitions / available_intervals if the notify entity uses them)
```

Then the notify entity's per-option `ActionSelect`s populate. Unit-test with a
state fixture carrying the lists.

---

## G3 — Sysvar `extended` marker (verify only)

**No client change pending.** The marker is wired end-to-end already:
`SysvarSummary.is_extended` (types) → read at
`compat/aiohomematic/model/hub/__init__.py:225`
(`extended=bool(getattr(summary, "is_extended", False))`) →
`resolve_sysvar_class(...)`.

**Action (VERIFY):** on a real CCU, confirm a variable carrying the _extended_
description marker now surfaces as the writable flavour (switch / number /
select / text). If it still reads read-only, the cause is the CCU description
or the daemon's `parseSysvarDescription`, **not** a missing wire field.

---

## G4 — Hub singletons

**Reality is ahead of the original gap text.** The client `_HubCoordinator`
(`compat/aiohomematic/central/adapter.py:201-465`) _already_ defines the
singleton data points (`_alarm_messages_dp`, `_service_messages_dp`,
`_inbox_dp`, `_update_dp`, `_metrics_dps`, `_connectivity_dps`,
`_install_mode_dps`) and `fetch_hub_singleton_data()` populates them from
**six** REST calls (`list_alarm_messages`, `list_service_messages`,
`list_inbox`, `get_hub_metrics`, `get_system_update`, `list_interfaces` +
install-mode).

**Daemon now provides** `GET /hub/data-points` — one response per central:

```json
{
  "central": "home",
  "alarm_messages": { "legacy_name": "alarm_messages", "value": 2 },
  "service_messages": { "legacy_name": "service_messages", "value": 1 },
  "inbox": { "legacy_name": "inbox", "value": 0 },
  "update": {
    "legacy_name": "system_update",
    "update_available": false,
    "in_progress": false
  },
  "metrics": [{ "legacy_name": "system_health", "value": 95, "unit": "%" }],
  "connectivity": [{ "interface_id": "HmIP-RF", "reachable": true }],
  "install_mode": [
    {
      "interface_id": "HmIP-RF",
      "enabled": false,
      "remaining_s": 0,
      "observed": true
    }
  ]
}
```

**Change (a) — OPTIMISATION (client):** collapse `fetch_hub_singleton_data()`'s
six calls into a single `GET /hub/data-points` and fan the fields onto the
existing `*_dp.update_*` setters. Lower round-trip cost and atomic consistency;
the message/inbox _lists_ are still fetched on demand (the aggregate carries
counts only).

**Change (b) — LIFT-GUARD (homematicip_local):** the orphan-cleanup sweep is
skipped for the loom backend at
`custom_components/homematicip_local/control_unit.py:500-507`
(`_async_cleanup_orphaned_entity_registry_entries`) with the comment _"exposes
only a partial hub-coordinator surface …"_. With the full singleton set now
modelled (client) and coherently exposed (daemon), **re-evaluate and remove the
`BACKEND_LOOM` early-return** so the per-singleton accounting (control_unit.py
~523-544) runs. Guard it behind a real check that the singletons are present
rather than a blanket backend skip.

---

## G5 — Per-device event groups

**Reality is ahead of the original gap text.** The client query facade
`get_event_groups()` (`compat/aiohomematic/central/adapter.py:663-681`) is
**implemented** — it builds groups locally via `build_event_groups(...)` and no
longer raises `NotImplementedError`.

But the HA integration still defends against the old behaviour at
`custom_components/homematicip_local/event.py:55-70`:

```python
try:
    event_groups = control_unit.central.query_facade.get_event_groups(event_type=event_type, registered=False)
except NotImplementedError:
    continue  # platform set up without bootstrap entities
```

**Daemon now provides** `GET /devices/{addr}/channels/{no}/event-groups`:

```json
[
  {
    "channel_address": "NEQ0123456:1",
    "kind": "keypress",
    "event_types": ["press_short", "press_long"],
    "parameters": ["PRESS_SHORT", "PRESS_LONG"],
    "available": true,
    "last_triggered_event": {
      "parameter": "press_short",
      "value": null,
      "triggered_at": "2026-06-21T10:15:30Z"
    }
  }
]
```

**Change — VERIFY + OPTIMISATION:**

1. Confirm `get_event_groups()` actually yields groups on the loom backend
   (it builds from already-loaded channels). If it does, **remove the
   `NotImplementedError` fallback** in `event.py` so the `event` platform gets
   its bootstrap entities.
2. Optionally back `get_event_groups()` (or enrich it) with the new REST
   endpoint so `last_triggered_event` and `available` are authoritative rather
   than derived locally.

---

## G6 — Drop the hub poll loop

**Daemon now pushes** these WS broadcast topics (envelope `type` in parens):

| Topic                                       | Type                   | Payload                                          |
| ------------------------------------------- | ---------------------- | ------------------------------------------------ |
| `hub.<central>.alarm_messages`              | `hub.alarm_message`    | `{central, count}`                               |
| `hub.<central>.service_messages`            | `hub.service_message`  | `{central, count}`                               |
| `hub.<central>.inbox`                       | `hub.inbox_changed`    | `{central, count}`                               |
| `hub.<central>.metrics`                     | `hub.metrics_changed`  | `{central, metric, value, unit}`                 |
| `hub.<central>.connectivity.<interface_id>` | `connectivity.changed` | `{central, interface_id, reachable, latency_ms}` |

(`hub.<central>.install_mode`, `…sysvars.<name>`, `…programs.<id>` already
existed.)

**Client today** — `compat/aiohomematic/central/adapter.py`: `_HUB_REFRESH_INTERVAL = 30`
(line 104) drives a loop (≈1121-1128) that calls
`fetch_hub_singleton_data(scheduled=True)` every 30 s.

**Change (REQUIRED):**

- Subscribe to `hub.<central>.*` (the topic prefix already used for sysvars /
  programs / install-mode) and route each `type` to the matching singleton:
  - count topics (`alarm_messages` / `service_messages` / `inbox`) →
    `*_dp.update_value(count)`; refetch the _list_ lazily only when an entity
    needs the entries.
  - `metrics` → `metrics_dps[<metric>].update_value(value)`.
  - `connectivity.<interface_id>` → `connectivity_dps[id].sensor.update_value(reachable)`
    (the lazily-attached tracker means this rides the bus; see the daemon note).
- Once subscriptions are live, **delete the 30 s poll loop** (keep a single
  initial `fetch_hub_singleton_data()` at startup for the cold-state snapshot).

This removes the last polling island in an otherwise push-driven client.

---

## G7 — Generic `set_on_time`

**Daemon provides** a generic write for _every_ parameter, including `ON_TIME`:
`PUT /api/v1/devices/{addr}/channels/{no}/data-points/ON_TIME/value` with body
`{ "value": <seconds> }`.

**Client today** — `compat/aiohomematic/model/generic/__init__.py:215-227`:

```python
async def set_on_time(self, *, on_time: float) -> None:
    _LOGGER.debug(...)  # no-op
```

The switch surface already has `send_value(...)` (writes the channel's primary
`STATE`); `set_value(address, channel, parameter, value)` lives at
`openccu_loom_client/operations/datapoints.py:20`.

**Change (REQUIRED):** route `set_on_time` to the value endpoint targeting the
`ON_TIME` parameter on the same channel:

```python
async def set_on_time(self, *, on_time: float) -> None:
    await self._client.datapoints.set_value(
        address=self._address,
        channel=self._channel,
        parameter="ON_TIME",
        value=on_time,
    )
```

(Confirm the channel actually exposes `ON_TIME`; mirror whatever
address/channel accessors the surrounding generic entity already uses.)

---

## Suggested sequencing

1. Regenerate `openccu-loom-types`.
2. **G2, G1, G7** — small, self-contained state/parameter wirings.
3. **G6** — push subscriptions + delete the poll loop (largest client change).
4. **G4(a)** — fold the six hub fetches into `/hub/data-points`.
5. **G5** + **G4(b)** — in `homematicip_local`: drop the `event.py`
   `NotImplementedError` fallback and lift the `control_unit.py` orphan-cleanup
   `BACKEND_LOOM` skip; verify on a real CCU.
6. **G3** — verification pass on a real CCU (no code).
