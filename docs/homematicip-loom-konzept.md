# Konzept: `homematicip_loom` — eine loom-only Home-Assistant-Integration

> Tiefenanalyse + Architekturkonzept, Stand 2026-06-22. Methodik: 4 parallele
> Code-Analysen (homematicip_local-Konsum, Compat-Shim-Teardown, Migrations-/
> unique_id-Vertrag, Schichtungs-Layering), jeder Befund am Code der Repos
> `openccu-loom-client`, `homematicip_local`, `openccu-loom`, `openccu-loom-types`
> gegengeprüft.

---

## 0. Kernaussage (TL;DR)

1. **Ein „neues" loom-only Projekt ist überwiegend eine _Subtraktion_, kein Neubau.**
   `homematicip_local` ist **heute schon dual-backend** (`BACKEND_CCU` / `BACKEND_LOOM`,
   `const.py:53-60`), listet `openccu-loom-client` als harte Dependency
   (`manifest.json`) und erzeugt produktiv eine loom-backed Central
   (`control_unit.py:1424 _create_loom_central`). Die 16 HA-Plattformen laufen
   bereits gegen den Loom-Backend.

2. **Der Compat-Shim ist ~50 % des Clients und ~46 % davon reine Imitation.**
   Nativer Kern ≈ 7.5k LOC, Compat-Shim ≈ 7.7k LOC. Davon sind ~46 % **reine
   aiohomematic-Imitation, die in einer loom-nativen Welt _verschwindet_** und
   ~53 % **echte Domänenlogik, die nur _umzieht_** (in den Client-Kern oder die
   neue Integration). Genau das ist der Vereinfachungshebel.

3. **Den Client (Kern) braucht man weiter; den Compat-Shim nicht.**
   Der Kern (`transport`/`store`/`events`/`model`/`operations`) importiert
   **nichts** aus `compat/` (verifiziert) — er ist die wiederverwendbare,
   HA-agnostische Loom-Anbindung (RFC-9457-Fehler, Retry/Deadline, WS-Resume,
   Store-Mirror, 18 typisierte REST-Fassaden). Direkt-am-Daemon = ~7.5k LOC
   Infrastruktur neu bauen. **Nicht empfohlen.**

4. **Empfehlung (siehe §9): _gemeinsam_, nicht getrennt.** Ein **separates**
   `homematicip_loom` würde die ~12k LOC HA-Glue duplizieren → zwei
   divergierende Integrationen, Nutzerverwirrung. Zukunftsträchtig ist:
   **eine** Integration (`homematicip_local`), deren Loom-Pfad sich von
   „aiohomematic imitieren" zu „natives Loom-Kategoriemodell" entwickelt — die
   B-Logik in den Client-Kern, der A-Imitations-Layer schrumpft/entfällt, der
   Daemon liefert die `unique_id` und löst die letzte aiohomematic-Abhängigkeit.

---

## 1. Ausgangslage: die heutige 4-Schichten-Architektur

```
┌──────────────────────────────────────────────────────────────────────┐
│  openccu-loom (Go-Daemon)   — REST + WS, mediiert die CCU(s)           │
│     internal/routingkey/{canonical,slug,uniqueid}.go  ← key-Algorithmus│
└───────────────▲────────────────────────────────────────────────────────┘
                │ assets/openapi.yaml + wsapi.json (Vertrag)
┌───────────────┴────────────────────────────────────────────────────────┐
│  openccu-loom-types (PyPI)  — generierte Pydantic-Wire-Modelle + Enums  │
└───────────────▲────────────────────────────────────────────────────────┘
                │ import
┌───────────────┴────────────────────────────────────────────────────────┐
│  openccu-loom-client  (~15k LOC)                                        │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │ KERN ≈ 7.5k LOC  (HA-agnostisch, importiert NICHT aus compat/)    │  │
│  │ transport/ (http+ws, Resilienz)  store.py  events/  model/        │  │
│  │ operations/ (18 typisierte REST-Fassaden)  canonical.py  auth/cfg │  │
│  └──────────────────────────────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │ COMPAT-SHIM ≈ 7.7k LOC  (compat/aiohomematic/)                     │  │
│  │ imitiert aiohomematics CentralUnit/Koordinatoren/Dp*/Protocols     │  │
│  └──────────────────────────────────────────────────────────────────┘  │
└───────────────▲────────────────────────────────────────────────────────┘
                │ import (compat-Pfad) bzw. aiohomematic (CCU-Pfad)
┌───────────────┴────────────────────────────────────────────────────────┐
│  homematicip_local  (HA-Integration, ~16.3k LOC, 16 Plattformen)        │
│  DUAL-BACKEND:  BACKEND_CCU → aiohomematic   |   BACKEND_LOOM → compat   │
└──────────────────────────────────────────────────────────────────────────┘
```

**Belege:**

- Dual-Backend-Seam: `homematicip_local/const.py:53-60`, `control_unit.py:1332 create_central()` → `_create_loom_central()` (`:1424`) vs. `_create_ccu_central()` (`:1361`).
- Loom-Central wird als aiohomematic-`CentralUnit` \_ge-cast_ge­t (`control_unit.py:1452-1455`), weil der Compat-Adapter sie strukturell nachbildet.
- Kern ⟂ Compat: `grep "from openccu_loom_client.compat" openccu_loom_client/` außerhalb `compat/` ⇒ **leer**.

---

## 2. Warum der Compat-Shim existiert (das eigentliche Problem)

`homematicip_local` dispatcht Entities über **Typidentität**: Jede Plattform
macht `isinstance(dp, <Klasse>)`. Da der Loom-Client **nicht** von aiohomematics
Modellklassen erben kann (C-Level-Slot-Konflikt der `@runtime_checkable`-
Protokoll-Metaklasse, `backend_types.py:1-17`), paart `homematicip_local` pro
Plattform **beide** konkreten Klassen:

```python
# homematicip_local/backend_types.py:71
DP_SWITCH = _pair(DpSwitch, "DpSwitch", _g)   # (aiohomematic.DpSwitch, loom-Twin)
# Plattform:  if isinstance(dp, SYSVAR_DP_SWITCH): ...   # switch.py:65
```

Damit die Loom-Twins diese isinstance-Checks _und_ die `@runtime_checkable`-
Protocols erfüllen, baut der Compat-Shim aiohomematics gesamte Oberfläche nach:

- die **kategorisierten Datenpunkt-Klassen** `Dp*` / `CustomDp*` / `Sysvar*` /
  `Program*` (`compat/.../model/{generic,custom,hub}`),
- die **Protocol-Surface** (`_protocol_surface.py`, 621 LOC — über die Hälfte
  neutrale Stub-Properties),
- die **CentralUnit + Koordinatoren** (`central/adapter.py`,
  `central/hub_coordinator.py`),
- die **Event-Bridge** Loom-Events → aiohomematic-`DataPointStateChangedEvent`
  (`central/refresh.py`).

→ **Diese Imitation ist der ganze Daseinszweck des Shims. Fällt aiohomematic im
Loom-Pfad weg, fällt sie weg.**

---

## 3. Compat-Shim-Teardown: was verschwindet, was umzieht

Klassifikation des Shims (≈ 7.7k LOC) in **A** = reine Imitation (verschwindet),
**B** = echte Domänenlogik (zieht um, nativ neu ausgedrückt), **C** =
kern-würdig (HA-agnostisch).

| Datei                                            |  LOC | Klasse  | Warum                                                                                                                                                       |
| ------------------------------------------------ | ---: | :-----: | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `model/_protocol_surface.py`                     |  621 |  **A**  | @runtime_checkable-Stub-Tail; ~50 % neutrale Defaults — existiert nur zur Protokoll-Erfüllung                                                               |
| `central/adapter.py` (LoomCentralAdapter+Koord.) | 1023 | **A/B** | CentralUnit-/Koordinator-_Fassade_ = A; die Bootstrap-/Spawn-Orchestrierung = B                                                                             |
| `central/hub_coordinator.py`                     |  515 |  **B**  | Sysvar/Program-Katalog + Hub-Singleton-Push-Routing — echte Logik                                                                                           |
| `central/refresh.py`                             |  292 |  **A**  | Übersetzt Loom-Events → aiohomematic-Event (entfällt, wenn HA Loom-Events nativ abonniert); _aber_ enthält den unique_id-Lockstep (Migrationsanker, bleibt) |
| `central/events/__init__.py`                     |  139 |  **A**  | Re-Export aiohomematic-Events                                                                                                                               |
| `interfaces/__init__.py`                         |  116 |  **A**  | Re-Export aiohomematic-Protocols                                                                                                                            |
| `_upstream.py`                                   |   80 |  **A**  | Die aiohomematic-Internals-Seam                                                                                                                             |
| `const.py`                                       |  285 | **A/B** | aiohomematic-const-Re-Exports (A) + SystemInformation-Shape (B)                                                                                             |
| `model/custom/__init__.py`                       | 1106 |  **B**  | Climate/Cover/Light/Lock/Siren-State-Ableitung aus dem Daemon-`state`-Dict — _echte_ HA-Logik                                                               |
| `model/hub/singletons.py`                        |  581 |  **B**  | Hub-Singletons (alarm/service/inbox/metrics/connectivity/system-update/install-mode)                                                                        |
| `model/generic/__init__.py`                      |  448 |  **B**  | Enum-/Bool-Wertauflösung, Kategorisierung                                                                                                                   |
| `model/week_profile.py`                          |  375 |  **B**  | Wochenprofile / Schedule-Channel-Switches                                                                                                                   |
| `model/hub/__init__.py`                          |  285 |  **B**  | Sysvar/Program-Kategorisierung + unique_id-Funktionen (Migrationsanker)                                                                                     |
| `model/event_group.py`                           |  253 |  **B**  | Geräte-Trigger-Event-Gruppen                                                                                                                                |
| `model/naming.py`                                |  224 |  **B**  | Namensableitung (nutzt aiohomematic `DeviceProfileRegistry` — der _Aufruf_ ist A, die Logik B)                                                              |
| `central/configurable_devices.py`                |  129 |  **B**  | Configurable-Devices-Liste                                                                                                                                  |
| `model/combined.py`                              |  170 |  **B**  | Kombinierte Dauer-Number (DURATION_VALUE+UNIT)                                                                                                              |
| `model/update.py`                                |  151 |  **B**  | Firmware-Update-Datenpunkt                                                                                                                                  |
| `model/calculated.py`                            |  136 |  **B**  | Berechnete Datenpunkte                                                                                                                                      |
| `central/__init__.py`                            |  224 | **A/B** | CentralConfig-Factory (A) + reale Verdrahtung (B)                                                                                                           |

**Bilanz:** **≈ 46 % A** (≈ 3.5k LOC reine Imitation → _verschwindet_),
**≈ 53 % B** (≈ 4.1k LOC echte Logik → _zieht um_), **< 1 % C**.

> **Pointe:** Die B-Logik (Kategorisierung, Hub-Singletons, State-Ableitung,
> Schedules) braucht **jede** Loom-HA-Integration — sie gehört eigentlich in
> den **Client-Kern** als _natives_ Loom-Kategoriemodell, nicht in eine
> aiohomematic-Imitations-Schicht. Die A-Logik existiert nur, damit Loom-Daten
> für `homematicip_local`s Dual-Backend-Dispatch wie aiohomematic _aussehen_.

---

## 4. Was sich konkret vereinfacht (Code-Stellen)

In einer loom-nativen Integration (kein aiohomematic im Loom-Pfad) entfällt:

1. **`backend_types.py` komplett** (90 LOC, `homematicip_local`): Die
   `(AioClass, LoomTwin)`-Paarung ist nur nötig, weil zwei Klassen-Hierarchien
   koexistieren. Loom-only ⇒ Plattformen machen `isinstance(dp, DpSwitch)`
   direkt gegen die Loom-Klassen. → Kein Slot-Konflikt-Workaround mehr.

2. **`_protocol_surface.py` (621 LOC)**: Die ~50 % neutralen Stub-Properties
   (`config_payload`, `state_path`, `service_methods`, `signature` …) existieren
   nur zur `@runtime_checkable`-Erfüllung. HA liest sie nicht für _Verhalten_.
   → Native Loom-Entity-Klassen exponieren nur, was HA wirklich nutzt.

3. **`central/refresh.py` (292 LOC) Event-Übersetzung**: Heute werden Loom-Events
   in aiohomematics `DataPointStateChangedEvent` umgesetzt, damit HA-Entities
   sie über `event_key=unique_id` empfangen (`generic_entity.py:326-333`). Eine
   loom-native Integration abonniert die Loom-Events direkt vom `EventBus` des
   Kerns → die Übersetzungs-Bridge entfällt (nur der unique_id-Build bleibt, §6).

4. **Die CentralUnit-/Koordinator-Fassade** (`central/adapter.py` LoomCentral­
   Adapter + `cast(CentralUnit, …)` in `control_unit.py:1452`): Loom-only
   instanziiert direkt `LoomClient` (Kern) und ruft `client.store` /
   `client.events` / `client.devices` etc. → keine Nachbildung der
   `device_coordinator`/`hub_coordinator`/`query_facade`-Oberfläche.

5. **Manifest-Dependencies**: `aiohomematic`, `aiohomematic_config`,
   `backend_detection`, die XML-RPC-Callback-Verdrahtung, alle CCU-only
   Config-Flow-Zweige (`config_flow.py` CCU-Hälfte, ~mehrere hundert LOC).

6. **`_upstream.py` (80) + `interfaces/` (116) + `central/events/` (139)** —
   die reinen aiohomematic-Re-Exports.

**Summe Vereinfachung im Loom-Pfad:** die ~3.5k LOC A-Imitation des Shims +
`backend_types.py` + die CCU-Hälften in `control_unit.py`/`config_flow.py`/
`const.py` + 2 Manifest-Dependencies.

---

## 5. Wo Komplexität bleibt / hin­wandert

Komplexität **verschwindet nicht**, sie wird _ehrlicher_:

1. **Die B-Logik (≈ 4.1k LOC) bleibt** — sie ist echte Domänenarbeit:
   Climate-/Cover-/Light-State aus dem `state`-Dict (`custom/__init__.py`),
   Hub-Singletons (`hub/singletons.py`), Wochenprofile (`week_profile.py`),
   Kategorie→Plattform-Zuordnung, Text-Display-Optionslisten. Sie wird nur
   _nativ_ ausgedrückt (ohne Protokoll-Imitation) statt als aiohomematic-Twin.

2. **Die HA-Glue von `homematicip_local` (≈ 10–12k LOC) bleibt fast 1:1**:
   16 Plattform-Module, `generic_entity.py` (681), `entity_helpers/` (~1k),
   `config_flow.py`-Grundgerüst, `__init__.py`-Entry-Lifecycle, `services.py`
   (1579), `websocket_api.py` (2768 — das eigene Frontend-Panel!), `diagnostics`,
   `device_trigger`/`device_action`, `update`, `backup`, `repairs`. Diese laufen
   _heute schon_ gegen Loom — sie hängen nur an der `isinstance`-Quelle.

3. **Neue/verschobene Komplexität:** Das _native_ Loom-Kategoriemodell muss
   irgendwo leben. Sauberster Ort: der **Client-Kern** (`model/`), nicht der
   Shim — dann ist es für jeden Loom-Konsumenten da.

---

## 6. Migration `homematicip_local` ↔ `homematicip_loom` (der harte Teil)

In HA ist eine Migration **nur dann nahtlos**, wenn **Entity-`unique_id`s** und
**Device-`identifiers` bit-identisch** bleiben — sonst verlieren Nutzer Historie,
Automations-Referenzen und Anpassungen.

### 6.1 Der unique_id-Vertrag (`openccu_loom_client/canonical.py`)

- Format: **`loom_<aiohomematic-routing-key>`** (`canonical_unique_id`, `:122-148`).
- `serial_suffix()` (`:107-119`): letzte 10 Zeichen der CCU-Seriennummer,
  lowercased — füllt den `central-id`-Slot für Hub/Internal/Virtual-Adressen.
- `hub_slug()` (`:151-161`): **python-slugify-Defaults** (nicht naives
  `.replace().lower()` — sonst Drift bei Umlauten → stille Entity-Verwaisung).
- **Kritisch:** `generate_unique_id()` (`:86-99`) **delegiert an aiohomematics**
  `model.support.generate_unique_id` → das ist die **letzte harte
  aiohomematic-Abhängigkeit** im Loom-Pfad.

Format je Entity-Art (verifiziert):
| Art | Funktion | Format-Beispiel |
| --- | --- | --- |
| Generic DP | `data_point_event_key` (`events/types.py:58`) | `loom_vcu1234567_1_state` |
| Custom DP | `custom_unique_id` (`custom/__init__.py:52`) | `loom_vcu1234567_1` |
| Sysvar | `sysvar_unique_id` (`hub/__init__.py:37`) | `loom_11a0001234_sysvar_alarm` |
| Program | `program_unique_id` (`hub/__init__.py:48`) | `loom_11a0001234_program_heating` |
| Calc DP | `_CalculatedKeyMixin.unique_id` (`calculated.py:60`) | `loom_calculated_vcu7_1_window_open` |
| Week-Profile | `WeekProfileDp.unique_id` (`week_profile.py:125`) | `loom_week_profile_vcu1_week_profile` |
| Schedule-Switch | `ScheduleChannelSwitch.unique_id` (`week_profile.py:299`) | `loom_schedule_channel_switch_vcu1_…` |
| Device-Identity | `Device.identifier` (`model/device.py:165`) | `VCU1234567@home:HmIP-RF` |

### 6.2 Die gute Nachricht: Migration ist großteils gelöst

- **`homematicip_local` hat die Loom-Migration bereits:**
  `__init__.py:347 _async_migrate_loom_unique_ids` + `_async_restore_aiohomematic_unique_ids`
  - `_async_migrate_aiohomematic_hub_unique_ids` (`:157-204`). Das Loom-Schema
    ist das **kanonische Ziel** — von CCU/aiohomematic _hin_ zu Loom wird schon
    migriert.
- **Entity-Key = `f"{DOMAIN}_{data_point.unique_id}"`** (`generic_entity.py:98`):
  Solange `homematicip_loom` denselben `DOMAIN` und denselben
  `data_point.unique_id` (also dasselbe `canonical.py`) nutzt, sind die Keys
  **identisch** → Migration = nur Config-Entry-Umzug, keine Entity-Neuanlage.
- **Anker:** `canonical.py` ist top-level (nicht im Shim), HA-agnostisch und als
  „Algorithmus-of-record" dokumentiert (`canonical.py:5-31`, Parallel-Impl im
  Daemon `internal/routingkey/`). → **Der gemeinsame Migrationsanker existiert
  bereits.**

### 6.3 Die letzte aiohomematic-Abhängigkeit ablösen (Daemon-Pfad)

- **Verifiziert:** Der Daemon liefert `unique_id` bereits auf den **WS-Payloads**
  (`DataPointValueChangedPayload.unique_id` = True, …), und die Refresh-Bridge
  bevorzugt sie (`refresh.py:125 event.payload.unique_id or …`).
- **ABER** die **REST-Summaries** (`DataPointSummary`, `CustomDPSummary`,
  `SysvarSummary`, `ProgramSummary`) tragen **kein** `unique_id` → für die
  _Entity-Anlage_ muss der Client den Key heute noch selbst rechnen (via
  `canonical.py` → aiohomematic).
- **Der saubere Zukunftspfad:** Der Daemon hat den Key-Algorithmus schon in Go
  (`internal/routingkey/{canonical,slug,uniqueid}.go`). **Wenn er `unique_id`
  auch auf die Summaries (und den Snapshot) legt**, kann der Client/die
  Integration den Key **direkt konsumieren** → `canonical.py` wird zum reinen
  Fallback, und **aiohomematic fällt selbst aus dem Kern** vollständig weg.
  → Das ist ein klar umrissener **Daemon-Ask** (ein neues Feld, kein
  Verhaltensbruch) mit großer Hebelwirkung.

---

## 7. Brauche ich den Client noch? (explizite Frage)

| Schicht                                                             |     Loom-only nötig?      | Begründung                                                                                                                                                                                                                                             |
| ------------------------------------------------------------------- | :-----------------------: | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **openccu-loom-types**                                              |           ✅ ja           | Generierte Wire-Modelle; ohne sie parst niemand den Vertrag                                                                                                                                                                                            |
| **Client-Kern** (`transport`/`store`/`events`/`model`/`operations`) |         ✅ **ja**         | HA-agnostische, getestete Infra: RFC-9457-Fehler, Retry/Deadline-Budget, WS-Resume/Reconnect/Heartbeat/reauth, bounded Queue, Store-Mirror, EventBus, 18 typisierte REST-Fassaden. Direkt-am-Daemon ⇒ ~7.5k LOC Neubau + Verlust der Resilienz-Arbeit. |
| **Compat-Shim** (`compat/aiohomematic/`)                            |        ❌ **nein**        | Reine aiohomematic-Imitation für den Dual-Backend-Dispatch. Loom-only braucht ihn nicht.                                                                                                                                                               |
| **aiohomematic (Laufzeit)**                                         | ⚠️ nur für `canonical.py` | Einzige verbleibende harte Kopplung; via Daemon-`unique_id` (§6.3) ablösbar.                                                                                                                                                                           |

**Fazit:** _Den Kern ja, den Shim nein, den Daemon nicht direkt._ Die
zukunftsträchtige Loom-only-Schichtung ist **3-stufig**:

```
openccu-loom (Daemon) → openccu-loom-types + openccu-loom-client[KERN] → homematicip_loom
```

---

## 8. Vergleich: bestehende Lösung vs. `homematicip_loom`

| Dimension                       | **A) Status quo** — `homematicip_local` dual (CCU + Loom via Shim) | **B) Separat** — eigenes `homematicip_loom` (loom-only)               | **C) Evolutiv** — _eine_ Integration, Loom-Pfad nativ  |
| ------------------------------- | ------------------------------------------------------------------ | --------------------------------------------------------------------- | ------------------------------------------------------ |
| Anzahl HA-Integrationen         | 1                                                                  | **2** (Divergenzrisiko)                                               | **1**                                                  |
| Compat-Shim                     | bleibt komplett (~7.7k)                                            | entfällt im Loom-Pfad (A-Teil ~3.5k weg)                              | A-Teil schrumpft/entfällt, B-Teil wandert in den Kern  |
| `backend_types.py`-Paarung      | bleibt                                                             | entfällt                                                              | entfällt _im Loom-Pfad_, bleibt für CCU                |
| HA-Glue (~12k LOC)              | einmal                                                             | **dupliziert** (zwei Repos, driften)                                  | einmal                                                 |
| aiohomematic-Kopplung           | voll (CCU + canonical)                                             | nur `canonical` (via Daemon ablösbar)                                 | CCU-Pfad behält sie; Loom-Pfad löst sie                |
| Nutzer-Sicht                    | eine Integration, Backend-Wahl                                     | zwei Integrationen für dieselben Geräte ⇒ Verwirrung, Migration nötig | eine Integration, nahtlos                              |
| CCU-Direktbetrieb (ohne Daemon) | weiter möglich                                                     | nicht möglich (loom-only)                                             | weiter möglich                                         |
| Wartungsaufwand                 | mittel (Shim-Pflege)                                               | **hoch** (2× Glue)                                                    | **sinkt** (Shim schrumpft, B im Kern wiederverwendbar) |
| Migrationsaufwand `local`↔neu  | —                                                                  | Config-Entry-Umzug (Keys schon identisch)                             | keiner (dieselbe Integration)                          |

### 8.1 Aufwandsschätzung

- **B (separat):** Kein Neubau der Plattformen (Subtraktion aus `homematicip_local`),
  ABER: Fork pflegen, HA-Glue dupliziert, Frontend-Panel (`websocket_api.py`
  2768 LOC) doppelt, zwei Config-Flows, zwei Release-Zyklen, Nutzer-Migration
  zwischen zwei Integrationen. **Initial gering, laufend hoch.**
- **C (evolutiv):** B-Logik aus dem Shim in den Client-Kern als natives
  Kategoriemodell heben (~4k LOC umziehen, gut getestet); `homematicip_local`s
  Loom-Pfad auf native Loom-Klassen umstellen (`backend_types.py`-Loom-Hälfte →
  direkte isinstance); A-Imitation entfällt schrittweise; Daemon-`unique_id`-Ask
  stellen. **Verteilt, mehrstufig, aber jeder Schritt reduziert Komplexität und
  ist einzeln auslieferbar.** Genau die Richtung, die die §2.2-/N1–N10-Arbeit
  schon eingeschlagen hat (Imitation typisiert/laut statt blind).

---

## 9. Bewertung & Empfehlung

**Empfehlung: Variante C — _gemeinsam_, nicht getrennt.**

1. **Kein separates `homematicip_loom`.** Der einzige echte Vorteil — ein
   schlanker loom-only Einstieg — wiegt die **Duplikation der ~12k-LOC-HA-Glue**
   und die **Nutzerverwirrung zweier Integrationen für dieselben Geräte** nicht
   auf. Zwei Integrationen driften garantiert auseinander (Plattform-Bugfixes,
   Frontend-Panel, Services doppelt).

2. **Eine Integration, evolutionär loom-nativ.** Der zukunftsträchtige Weg ist,
   `homematicip_local` _eine_ Integration zu lassen und den **Loom-Pfad von
   „aiohomematic imitieren" zu „natives Loom-Kategoriemodell" zu entwickeln**:

   - **Schritt 1 (Kern):** Die B-Logik (Kategorisierung, Hub-Singletons,
     Custom-State, Schedules, calc/combined/update) aus `compat/` in den
     **Client-Kern** (`openccu_loom_client/model/`) heben — als native,
     HA-agnostische Loom-Klassen. Der Shim wird zur dünnen Adapter-Hülle.
   - **Schritt 2 (HA):** `homematicip_local`s Loom-Hälfte in `backend_types.py`
     auf **direkte** isinstance gegen die nativen Loom-Klassen umstellen → der
     A-Imitations-Layer (Protocol-Surface, CentralUnit-Fassade, Event-Bridge)
     entfällt im Loom-Pfad.
   - **Schritt 3 (Daemon):** `unique_id` auf die REST-Summaries legen
     (`internal/routingkey/` existiert) → `canonical.py`/aiohomematic als
     Key-Quelle ablösen. Danach ist der **Loom-Pfad vollständig
     aiohomematic-frei**; aiohomematic bleibt nur noch der CCU-Direkt-Backend.

3. **Warum das die robusteste Variante ist:**
   - **Eine** Codebasis HA-Glue, **ein** Migrationsanker (`canonical.py` → später
     Daemon-`unique_id`), **eine** Nutzer-Integration mit Backend-Wahl
     (CCU _oder_ Daemon) — inkl. dem schon vorhandenen, getesteten
     Migrationspfad (`_async_migrate_loom_unique_ids`).
   - Komplexität **sinkt monoton**: jeder Schritt entfernt Imitation, ohne den
     Nutzer zu zwingen, die Integration zu wechseln.
   - Der **Client-Kern bleibt** das wiederverwendbare Herzstück (auch für andere
     Konsumenten: MQTT-Bridge, CLI, Tests) — er ist bereits sauber von der
     HA-/aiohomematic-Welt entkoppelt.

**Wann _doch_ separat (Variante B)?** Nur, wenn ein **bewusst minimaler,
aiohomematic-freier Referenz-/Embedded-Client** für ein _anderes Publikum_
gewünscht ist (z. B. Nicht-HA-Konsumenten, ein schlankes Beispiel) — dann ist
das aber kein zweites HA-Integration-Repo, sondern schlicht die direkte Nutzung
des **Client-Kerns** ohne Shim. Die HA-Integration bleibt dabei trotzdem _eine_.

---

## 10. Konkrete nächste Code-Schritte (priorisiert)

1. **Daemon-Ask:** `unique_id` auf `DataPointSummary`/`CustomDPSummary`/
   `SysvarSummary`/`ProgramSummary` (+ Snapshot) — Algorithmus ist da
   (`internal/routingkey/`). Hebt die letzte aiohomematic-Kopplung.
2. **Kern:** Natives Loom-Kategoriemodell in `openccu_loom_client/model/`
   etablieren; B-Logik schrittweise aus `compat/` dorthin ziehen (jede Datei
   beim nächsten Anfassen — die §2.2-Typisierung hat den Weg geebnet).
3. **HA:** `homematicip_local/backend_types.py` Loom-Hälfte auf native Loom-
   Klassen; A-Imitation (`_protocol_surface.py`, `central/refresh.py`-Übersetzung,
   CentralUnit-Fassade) im Loom-Pfad zurückbauen.
4. **Dokument-Anker:** Die formale unique_id-Migrationsspezifikation
   (`openccu-loom/docs/external-clients/ha-unique-id-migration.md`, in
   `canonical.py:31` referenziert) als verbindlichen Vertrag pflegen.

> **Unterm Strich:** Der Daemon + der Client-Kern sind die tragfähige Zukunft.
> Der Compat-Shim ist die _Brücke_, nicht das Ziel — ~46 % davon ist Imitation,
> die mit jedem nativen Schritt verschwindet. **Eine** evolutiv loom-nativ
> werdende `homematicip_local`-Integration ist robuster als ein getrenntes
> `homematicip_loom` — sie vermeidet Duplikation, hält den Migrationspfad
> trivial und senkt die Komplexität kontinuierlich.
