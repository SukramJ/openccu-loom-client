# G4(a) — consume `GET /hub/data-points` in the hub coordinator

**As of:** 2026-06-21
**Daemon side:** shipped in openccu-loom 0.8.0 (PR #156, APIVersion 1.18.0).
**Goal:** collapse `_HubCoordinator.fetch_hub_singleton_data`'s **seven** REST
calls into **one** aggregate call, plus _conditional_ follow-ups only when
something actually changed.

> **Prerequisite:** regenerate **`openccu-loom-types`** from the new
> `assets/openapi.yaml`. It must add a `HubDataPoints` model — it does **not**
> exist yet (`openccu-loom-types/openccu_loom_types/rest.py` only has the
> per-singleton entries). All code below assumes it exists.

## What the aggregate returns

`GET /api/v1/hub/data-points` → **array, one entry per central**:

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
  "metrics": [
    { "legacy_name": "system_health", "value": 95, "unit": "%" },
    { "legacy_name": "connection_latency_ms", "value": 12, "unit": "ms" },
    { "legacy_name": "last_event_age_seconds", "value": 3, "unit": "s" }
  ],
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

**Key constraint:** `alarm_messages` / `service_messages` carry **only the
count**, not the message bodies — and `update` carries only the two booleans,
not the firmware version strings. This is by design (the aggregate is a
lightweight coordinator snapshot, not a bulk dump).

## Coverage — what the aggregate replaces

| Singleton            | Aggregate field                            | Existing DP setter                                                                    | Replaces the call?                               |
| -------------------- | ------------------------------------------ | ------------------------------------------------------------------------------------- | ------------------------------------------------ |
| **Inbox**            | `inbox.value`                              | `inbox_dp.update_value(value=…)`                                                      | ✅ fully (`list_inbox` → drop)                   |
| **Metrics** ×3       | `metrics[*]` by `legacy_name`              | `metrics_dps.{system_health,connection_latency,last_event_age}.update_value(value=…)` | ✅ fully (`get_hub_metrics` → drop)              |
| **Connectivity**     | `connectivity[*]`                          | `connectivity_dps[id].sensor.update_value(value=reachable)`                           | ✅ fully (`list_interfaces` → drop)              |
| **Install-mode**     | `install_mode[*]`                          | `install_mode_dps[id].sensor.update_value(value=remaining_s if enabled else 0)`       | ✅ fully (`list_install_mode_interfaces` → drop) |
| **Alarm messages**   | `alarm_messages.value` (count)             | `alarm_messages_dp.update_messages(messages=…)` needs the **list**                    | ⚠️ count only — fetch list on count-delta        |
| **Service messages** | `service_messages.value` (count)           | `service_messages_dp.update_messages(messages=…)` needs the **list**                  | ⚠️ count only — fetch list on count-delta        |
| **System update**    | `update.update_available` / `.in_progress` | `update_dp.update_data(entry=SystemUpdateEntry)` needs firmware strings               | ⚠️ flags only — see note                         |

Net effect: **7 calls → 1** in steady state; **→ 3** only when an alarm/service
count changed; the `update` flag lets you skip `get_system_update` unless the
entity needs firmware strings.

## Step 1 — add the operation (`operations/system.py`)

Mirror the existing per-central `get_hub_metrics` (`system.py:107`):

```python
async def get_hub_data_points(self) -> list[HubDataPoints]:
    """Aggregated hub-singleton snapshot, one entry per central."""
    payload = await self._transport.request(method="GET", path="/api/v1/hub/data-points")
    return [HubDataPoints.model_validate(e) for e in (payload or [])]
```

## Step 2 — rewrite `fetch_hub_singleton_data` (`adapter.py:444-465`)

**Before** (7 calls):

```python
await self._ensure_singletons()
changed = []
changed.extend(await self._fetch_messages())  # list_alarm_messages + list_service_messages
changed.extend(await self._fetch_inbox())  # list_inbox
changed.extend(await self._fetch_metrics())  # get_hub_metrics
changed.extend(await self._fetch_system_update())  # get_system_update
changed.extend(await self._fetch_install_mode())  # list_install_mode_interfaces
changed.extend(await self._fetch_connectivity())  # list_interfaces
```

**After** (1 call + conditional follow-ups):

```python
await self._ensure_singletons()
data = self._select_central(await self._client.system.get_hub_data_points())
if data is None:
    return
changed: list[Any] = []

# Fully covered by the aggregate — direct scalar fans:
changed += self._apply_inbox(data.inbox)  # inbox_dp.update_value(count)
changed += self._apply_metrics(data.metrics)  # 3 metrics sensors, matched by legacy_name
changed += self._apply_connectivity(data.connectivity)  # per-interface .update_value(reachable)
changed += self._apply_install_mode(data.install_mode)  # per-interface .update_value(remaining_s if enabled else 0)

# Cheap count → fetch the heavy list only when it actually changed:
changed += await self._refresh_messages_if_count_changed(
    alarm_count=data.alarm_messages.value,
    service_count=data.service_messages.value,
)
# Update flags drive the entity; firmware strings fetched only if needed:
changed += self._apply_update_flags(data.update)  # or call _fetch_system_update() on a flag flip
```

- `_select_central(...)` picks this coordinator's entry from the per-central
  list (same pattern `get_hub_metrics` already needs).
- `_apply_metrics`: build a `{legacy_name: value}` dict from `data.metrics`, then
  fan onto the three sensors (`system_health`, `connection_latency_ms`,
  `last_event_age_seconds`).
- `_refresh_messages_if_count_changed`: keep `self._client.hub.list_alarm_messages()`
  / `list_service_messages()` (they feed `update_messages`), but call them **only
  when** `value` differs from the currently-held count. Steady state → zero list
  fetches.

## Notes / caveats

- **Don't stuff message bodies into the aggregate.** The list endpoints already
  serve the bodies; gate them on count-delta instead. (If you'd rather have a
  true single call, that's a _daemon_ change to add the lists — heavier payload,
  not recommended.)
- **`update` firmware strings.** `update_dp.update_data` wants a full
  `SystemUpdateEntry` (current/available firmware). The aggregate gives only
  `update_available` / `in_progress`. Either keep `get_system_update` for the
  version strings, or fetch it lazily only when `update_available` flips true.
- **Per-central.** The aggregate is a list; a multi-CCU client maps each entry to
  its coordinator.

## Pairs with G6 (push)

Once the G6 push topics (`hub.<central>.{inbox,metrics,connectivity,…}`) are
wired, the aggregate becomes the **cold-start snapshot** taken once at setup,
and the 30 s poll loop (`_HUB_REFRESH_INTERVAL`, `adapter.py:104`) is dropped
entirely — pushes carry the incremental counts/values, and the `count`-delta
rule fetches a message list only when a push says the count moved.

## Sequencing

1. Regen `openccu-loom-types` → `HubDataPoints`.
2. Add `system.get_hub_data_points()` (Step 1).
3. Rewrite `fetch_hub_singleton_data` + add the `_apply_*` / `_select_central`
   helpers; delete `_fetch_inbox`/`_fetch_metrics`/`_fetch_connectivity`/
   `_fetch_install_mode`; keep `_fetch_messages` behind the count-delta guard.
4. Decide the `update` firmware-string handling (keep `get_system_update` lazily
   vs. drop if the entity only needs the flags).
