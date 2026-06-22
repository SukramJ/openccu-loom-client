# Architektur-Bewertung — openccu-loom-client

> Tiefenreview aller Schichten, Stand `2026.6.22` (Branch `feat/device-client-shim-schedules`).
> Bewertungsskala 1–10. Methodik: schichtweise Quellcode-Analyse (alle `openccu_loom_client/*`
>
> - `tests/`, ohne `build/lib/`) plus übergreifende Messungen (Kopplung, Typsicherheit, Stubs).
>   ~14,4k LOC Source, ~9,4k LOC Tests, 568 Tests (530 unit/compat + 38 e2e deselected), 6 xfail.

---

## 0. Re-Review (2026-06-22, Branch `refactor/compat-clean-architecture`)

> Nachprüfung der geschlossenen Lücken + neue Potenziale. Methodik: 4 parallele
> Analyse-Agenten (Verifikation / Transport / God-Objects / Fresh-Eyes), jeder
> Befund am Code gegengeprüft (`mypy openccu_loom_client` sauber, 80 Dateien;
> 568 Tests grün). **Aktualisierte Gesamtnote: 7,6 / 10** (+0,2: B1–B8 + §2.1/§2.2
> geschlossen; teilweise neutralisiert durch zwei neu entdeckte Kern-/Transport-Risiken).

### 0.1 Verifikation: geschlossene Lücken — **alle bestätigt** ✅

| Lücke                              | Status                    | Beleg                                                                                                                                                                                                                                  |
| ---------------------------------- | ------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **§2.2 Drift-Maskierung (Compat)** | ✅ geschlossen            | `type: ignore` in `compat/` **74 → 7** (6 aktiv, 1 Kommentar), jede justifiziert + kommentiert; mypy strict sauber. Typisierte Mixin-Host-Deklarationen in allen 5 Surface-Dateien.                                                    |
| **§2.2 Latente Bugs**              | ✅ gefixt                 | Sysvar `set_value` keyword-only (`hub/__init__.py:108`); Program `last_executed` statt nicht-existentem Feld (`_protocol_surface.py:618`).                                                                                             |
| **§2.1 Import-Seam**               | ✅ geschlossen            | `compat/aiohomematic/_upstream.py` ist die einzige Seam; **0** direkte `from aiohomematic`-Imports in `compat/` außerhalb davon. Routing-Key bleibt in `canonical.py`. Seam selbst vorbildlich (vollständiges `__all__`, kein Zyklus). |
| **Bugs B1–B8**                     | ✅ alle gefixt            | Jeweils mit **echtem** Regressionstest, der den spezifischen Fehlermodus prüft (nicht Happy-Path) — verifiziert in `test_client_bootstrap.py`, `test_ws_transport.py`, `test_store.py`, `test_device_client.py`, `test_exceptions.py`. |
| **§4.1 Transport-Resilienz**       | ⚠️ **größtenteils offen** | Nur `replay_lost`/Rebootstrap + `system_update`-Push sind inzwischen getestet; Reconnect/Resume/Heartbeat-Timeout/reauth/HTTP-Timeout-Retry weiterhin **ungetestet**. `self._closing` ist jetzt gelesen (kein Dead-State mehr).        |

### 0.2 Neue / re-affirmierte Befunde (priorisiert, am Code verifiziert)

| #       | Tag        | Ort                                                                                                | Befund                                                                                                                                                                                                                                                                                                                                                                                                                                            | Schwere                  |
| ------- | ---------- | -------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------ |
| **N1**  | ROBUSTNESS | `model/channel.py:88,94,105,118,130`, `model/data_point.py:82`, `store.py:419`                     | **§2.2-Drift-Maskierung leckt in den KERN.** 7 `getattr(self._summary, "<feld>", default)` auf **typisierte** Modelle (alle Felder existieren: `group_no`/`is_group_master`/`is_in_multi_group`/`room`/`functions`/`observed`/`state`). Die §2.2-Bereinigung fasste nur `compat/` an — das _Kern ist sauber_-Urteil (§2.2) ist damit **falsch**. Wire-Rename → stilles Default statt `AttributeError`; jeder Read wird `Any`. Fix: Direktzugriff. | **Hoch** (konzeptionell) |
| **N2**  | ROBUSTNESS | `transport/ws.py:100`                                                                              | **Unbounded `_envelope_queue`** (`asyncio.Queue()` ohne `maxsize`). Bei `replay_lost`→Rebootstrap (N×M REST, Sekunden) liest der Reader weiter in die Queue → OOM-Pfad auf großer/aktiver CCU. Fix: bounded Queue + Drop-oldest/Resync + `seq`-Dedup.                                                                                                                                                                                             | **Hoch**                 |
| **N3**  | ROBUSTNESS | `store.py:338-343` + `central/refresh.py:137`                                                      | **O(events × CDPs) auf dem Hotpath.** `get_custom_data_point_by_channel` ist ein Linearscan über _alle_ CDPs, aufgerufen bei **jedem** `DataPointValueChangedEvent`. Quadratisch. (§4.2 sah nur Bootstrap-Scans.) Fix: Sekundärindex `(addr, channel_no) → CDP`.                                                                                                                                                                                  | **Hoch/Mittel**          |
| **N4**  | TEST       | `transport/`, `tests/unit/test_ws_transport.py`, `test_http_transport.py`                          | **Transport-Resilienz weiterhin praktisch ungetestet**: kein Test für Reconnect, Resume-after-disconnect (`since=last_seq` auf 2. Connect), Heartbeat-/Inbound-Ping-Timeout, reauth-Ack/-Fehler, HTTP-Timeout-Retry. Größtes Coverage-Risiko (durchzieht N2/N5/N6).                                                                                                                                                                               | **Hoch**                 |
| **N5**  | ROBUSTNESS | `transport/http.py:66-68,127,227`                                                                  | **Kein Gesamt-Deadline-Budget:** jeder der 3 Versuche bekommt volle `request_timeout_seconds` → Worst-Case ≈ 92 s, nicht die im Kommentar genannten „~3,5 s". Class-Docstring beschreibt retrybare Fehler falsch (nennt nur 502/503; faktisch alle gewrappten Netz-/Timeout-Fehler auf idempotenten Verbs). Fix: ein durchgereichtes Deadline-Budget + Doku.                                                                                      | **Mittel**               |
| **N6**  | ROBUSTNESS | `transport/ws.py:346-349,369-377`                                                                  | **`reauth()` ohne Ack-Pfad.** `reauth_failed` nur geloggt; neues Token nicht in `config.auth` gespiegelt → Reconnect nutzt altes Token → Endlos-Reconnect mit totem Token, kein Consumer-Signal. Fix: Ack-Future + `on_auth_failed`-Callback + `config.auth`-Update.                                                                                                                                                                              | **Mittel**               |
| **N7**  | SIMPLIFY   | `central/adapter.py` (1409→**1508**), `store.py` (858→**885**)                                     | **God-Objects gewachsen.** `adapter.py`: `_HubCoordinator` (453) + `LoomCentralAdapter` (452) = 60 % — Push-Routing (~90 Z.) und 6 Bootstrap-Methoden (~200 Z.) sauber extrahierbar, Blast-Radius ≈ 1 Test-Import. `store.py`: die Write-Back-/`refresh_*`-Logik (§2.3-Ask) **ist noch im Store** — Delegations-Helper (~180 Z., 0 API-Änderung). `custom/__init__.py` (1126): kohäsiv, **so lassen**.                                            | **Mittel**               |
| **N8**  | SIMPLIFY   | `operations/*`                                                                                     | **Boilerplate:** `[Model.model_validate(i) for i in (payload or [])]` **22×**, `model_dump(mode="json", exclude_none=True)` 20×. Ein `_request_list(path, model=)` + `_to_json_body(model)` auf `_OperationsBase` zentralisiert das (und die B7-nahe `payload or ()`-Null-Guard).                                                                                                                                                                 | **Mittel**               |
| **N9**  | CLEANUP    | `model/custom/__init__.py:866,875`; `central/refresh.py:163`; `ws.py:195-217`; `client.py:close()` | **Kleinkram:** tote exportierte Klassen `PlaySoundArgs`/`SirenOnArgs` (nur in `__all__`); totes `getattr(event.payload, "value", None)` auf `CustomDataPointStateChangedPayload` (hat kein `value`) → `value=None`; `events()` erzeugt 2 Tasks pro Envelope (Hotpath-Overhead); `close()`-Reihenfolge (bg-Tasks vor Dispatch) hat theoretisches Reconcile-Spawn-Race (durch `_closing`-Guard entschärft).                                         | **Niedrig**              |
| **N10** | CLEANUP    | `docs/architecture-review.md:7,290`                                                                | Doku-Metriken veraltet: „6 xfail" sind alle in `tests/e2e/` (0 Unit-xfails); §4.6-Transport-Gap teilweise geschlossen (replay_lost/system_update getestet).                                                                                                                                                                                                                                                                                       | **Niedrig**              |

### 0.3 Empfohlene Reihenfolge (Re-Review)

1. **N1 — `getattr`-Disziplin im Kern** (channel/data_point/store): direkter Zugriff, schließt das §2.2-Muster überall. Billig, hoher Konzept-Wert.
2. **N2 + N4 — bounded WS-Queue + Transport-Resilienz-Tests**: das größte _Produktions_-Risiko, zusammen anzugehen (der Test deckt den Fix ab).
3. **N3 — Sekundärindex für CDPs** (Hotpath-Quadratik).
4. **N5 + N6 — HTTP-Deadline-Budget + reauth-Ack** (Korrektheit/Resilienz).
5. **N7 + N8 — adapter/store-Extraktion + Operations-Boilerplate** (Lesbarkeit, mechanisch).
6. **N9 + N10 — Cleanups + Doku-Re-Baseline.**

**Bewusst-nicht-Befunde** (für künftige Reviews überspringbar): die 10 `except Exception` im Adapter (alle `noqa`, keep-last-value an optionalen Endpoints), `events/types.py` Degrade-to-`UnknownLoomEvent` (gewollt), `_upstream.py` (saubere Seam), der Typed-Mixin-Refactor (netto _entfernte_ Maskierung), `custom/__init__.py` (kohäsiv).

---

## 1. Gesamturteil

**Gesamtnote: 7,4 / 10** — _„Solide geschichtete Architektur mit exzellentem Tooling; das eigentliche Risiko liegt nicht im Kern, sondern im Compat-Shim und in der Transport-Resilienz."_

Der Kern (Transport → Store → Events → Model → Operations) ist sauber entkoppelt, gut dokumentiert
und größtenteils korrekt — die in `CLAUDE.md` beschriebene Einbahn-Datenflussarchitektur ist real
umgesetzt, nicht nur behauptet. Die Schwächen konzentrieren sich an drei Stellen:

1. **Compat-Shim** (`compat/aiohomematic/`) — der größte, brüchigste und am stärksten an
   Fremd-Interna gekoppelte Teil; trägt die Hälfte der Komplexität.
2. **Transport-Resilienz** — mehrere reale Races/Leaks im WS-Reconnect/Replay-Pfad, dünn getestet.
3. **Drift-Brüchigkeit** — systemische Maskierung von Schema-Drift durch `getattr(…, default)`,
   neutrale Stub-Defaults und Doku/Dependency-Inkonsistenzen.

### Bewertungsmatrix

| Schicht                                                                   |  Note   | Kernaussage                                                                                                          |
| ------------------------------------------------------------------------- | :-----: | -------------------------------------------------------------------------------------------------------------------- |
| Transport & Lifecycle (`transport/`, `client.py`, `auth.py`, `config.py`) | **7,0** | Lesbar, aber echte Races/Leaks im Replay/Reconnect-Pfad                                                              |
| Store + Events + Bridge (`store.py`, `events/`, `bridge.py`)              | **8,0** | Sauberes Decoupling; Store driftet zum God-Object                                                                    |
| Model + Operations (`model/`, `operations/`, `exceptions.py`)             | **8,0** | Konsistent, gut dokumentiert; 2 Bugs, etwas Über-Engineering                                                         |
| Compat — Central-Adapter (`central/adapter.py` u. a.)                     | **7,0** | 1409-Zeilen-God-Object; `getattr`-Wildwuchs                                                                          |
| Compat — Model-Layer (`model/custom`, `_protocol_surface.py`, `hub/`)     | **7,5** | Saubere Imitation; 50 % der Protocol-Surface sind Stubs                                                              |
| Tests, Tooling, Packaging, Doku                                           | **7,5** | Reifes Tooling; Doku-/Dependency-Drift, dünne Resilienz-Tests                                                        |
| **Querschnitt: Kopplung & Typsicherheit**                                 | **7,0** | aiohomematic-Wiederverwendung ist gewollt (Backend-Strategie, §2.1); Typsicherheit bleibt Thema (74× `type: ignore`) |

---

## 2. Übergreifende Architektur-Themen (Querschnitt)

Diese Punkte schneiden durch mehrere Schichten und sind strategisch wichtiger als jeder Einzelbefund.

### 2.1 Strategie: alternatives Backend mit bewusster aiohomematic-Wiederverwendung — **7/10**

> **Strategie-Festlegung (2026-06-21):** `openccu-loom*` ist auf absehbare Zeit _kein_ Ersatz für
> `aiohomematic`, sondern ein **alternatives Backend** für `homematicip_local` — beide koexistieren,
> HA wählt zwischen ihnen. Die Laufzeitnutzung von `aiohomematic` ist damit eine bewusste,
> architektonisch tragfähige Entscheidung, kein Paradox. Das frühere „Paradox"-Framing dieses
> Abschnitts ist überholt; siehe `CLAUDE.md` → „What this is".

Der Client **nutzt `aiohomematic` zur Laufzeit**: `canonical.py:39-43` zieht `generate_unique_id` /
`generate_channel_unique_id` / `ConfigProviderProtocol` direkt aus dem Paket; der Compat-Layer nutzt
weitere Symbole (`async_support.Looper`, `central.events.*`, `const.*`, `interfaces.model.*`-Protocols).
`pyproject.toml:14` führt `aiohomematic>=2026.6.2` als Runtime-Dependency.

- **Richtig so:** Routing-Keys müssen _bit-identisch_ sein, sonst routen Events ins Leere. Die
  Referenzimplementierung aufzurufen statt sie nachzubauen vermeidet stille Drift zwischen zwei
  Implementierungen desselben Vertrags. Dass beide Pakete **einen Maintainer** haben, macht die
  Kopplung _koordiniert_ statt extern erzwungen — das senkt das Risiko erheblich.
- **Integrationsform (entschieden): die compat-Shim (A1).** HA dispatcht über Typidentität
  (isinstance-Tupel `(AioClass, LoomTwin)` + `@runtime_checkable`-Protocols). Das macht die Shim zur
  pragmatischen Form; die Twins erfüllen aiohomematics Oberfläche _strukturell_ (kein Subclassing, da
  aiohomematics Modellklassen an einen lebenden `CentralUnit` gebunden sind). Ein sauberes
  Backend-Interface in `homematicip_local` (Variante B) bleibt aufgeschoben — es würde die produktive
  aiohomematic-Integration umbauen, für unklaren Gewinn.
- **Bleibende Absicherung (billig, hoher Schutz):** Es ist Kopplung an _Interna_, nicht an eine stabile
  öffentliche API (Präzedenz: `aiohomematic-contract` zurückgezogen mit `#3221`). Daher: obere
  Versionsschranke (`>=2026.6.2,<2026.7`; `requirements.txt` pinnt aktuell widersprüchlich
  `==2026.6.2`), die genutzten aiohomematic-Symbole an _einer_ Stelle bündeln, ein CI-Drift-Test gegen
  die genutzte Protokoll-/Signaturfläche, und mittelfristig die Imitation (Stubs, `getattr`, §2.2)
  durch **selektive Wiederverwendung** zurückbauen. → Aufgaben in `todo.md`.

### 2.2 Drift-Maskierung als systemisches Brüchigkeitsmuster — **5/10**

Der gefährlichste Quell stiller Fehler ist nicht ein einzelner Bug, sondern ein _Muster_:

- **74× `getattr(obj, "feld", default)`** im Compat-Layer — **alle** mit Default — auf
  _typisierte_ `openccu_loom_types`-Pydantic-Modelle, deren Felder bekannt sind. Jede
  Feld-Umbenennung im Wire-Schema wird so zu einem stillen `None`/`False` statt zu einem lauten
  `AttributeError`. Gleichzeitig wird `mypy --strict` ausgehebelt (alles wird `Any`).
- **74× `type: ignore`** im Source, davon **60× `[attr-defined]`**, konzentriert im
  Compat-Model-Layer (`_protocol_surface.py` 34, `generic/__init__.py` 22, `calculated.py` 9,
  `hub/__init__.py` 7). Der Kern (transport/store/events/model) hat nur **2** — die Typsicherheit
  bricht ausschließlich im Shim.
- **51 von 102 Properties** in `_protocol_surface.py` liefern neutrale Defaults
  (`None`/`()`/`{}`/`""`/`False`). Bewusst (Strategy-B-Refinement, `todo.md`), aber die Hälfte der
  nachgebildeten Protocol-Surface ist Platzhalter.

Zusammengenommen: Die `@runtime_checkable`-Protocol-Imitation bricht **still** bei jedem Upstream-
Protokoll-Zuwachs (kein Compile-Fehler, nur Laufzeit-`AttributeError` in HA). **Wichtigste
Gegenmaßnahme:** Ein **Drift-Wächter-Test**, der jeden `Dp*`/`CustomDp*`/`Sysvar*`/`Program*`-Twin
gegen die echten `aiohomematic.interfaces.model`-Protocols prüft, plus `getattr`-Disziplin
(direkter Attributzugriff bei bekannten Feldern, Default nur für echt-optionale, kommentiert).

### 2.3 God-Objects — **6/10**

Drei Dateien tragen unverhältnismäßig viel:

| Datei                                 | Zeilen | Problem                                                |
| ------------------------------------- | :----: | ------------------------------------------------------ |
| `compat/.../central/adapter.py`       |  1409  | 12 Koordinator-Klassen + Facade + 6 Bootstrap-Methoden |
| `compat/.../model/custom/__init__.py` |  1112  | 20 Custom-DP-Klassen (keine Explosion, aber ein File)  |
| `store.py`                            |  858   | ~45 öffentliche Methoden, 5 Verantwortlichkeiten       |

Alle drei sind _intern_ sauber strukturiert — die Aufteilung ist mechanisch, kein verworrenes
Knäuel. Konkrete Schnitte siehe §4.

### 2.4 Konsistenz & Decoupling — **8/10 (positiv)**

Was nachweislich gut ist und nicht angefasst werden sollte:

- Die Einbahn-Architektur (daemon → transport → event → store → model) ist real; Store, Bus und
  Bridge sind je einzeln unit-testbar (Store kennt den Bus nicht).
- Der EventBus isoliert Handler-Fehler korrekt (ein werfender Handler bricht den Fan-out nicht ab),
  iteriert über einen Snapshot (mutationssicher), Unsubscribe ist idempotent.
- Der `unique_id`-Lockstep — in `CLAUDE.md` als Risiko markiert — ist **faktisch entschärft**:
  `refresh.py` dupliziert keine Formatierung, sondern ruft dieselben Helper
  (`data_point_event_key`/`custom_unique_id`/`sysvar_unique_id`) wie die Model-Schicht.
- Die keyword-only-Konvention ist ausnahmslos eingehalten; das In-place-Mutationsmuster
  (`_replace_summary`/`_update_summary`) ist überall korrekt (kein Wrapper-Rebuild).

---

## 3. Echte Bugs (priorisiert)

> **Status (2026-06-21): B1–B8 BEHOBEN**, jeweils mit Regressionstests
> (`test_client_bootstrap.py`, `test_ws_transport.py`, `test_store.py`,
> `test_device_client.py`, `test_exceptions.py`). Die Cleanups C1–C3
> (bridge/bus-Docstrings, `getattr`→Direktzugriff) sind ebenfalls erledigt.
> Die Tabelle bleibt als Befund-Historie stehen.

Diese sind funktional, nicht kosmetisch. Sortiert nach Risiko.

| #   | Schwere  | Ort                                         | Problem                                                                                                                                                                                                                                                                                                                             |
| --- | -------- | ------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| B1  | **Hoch** | `store.py:553-574` + `bridge.py`            | **`device.created` löst keinen Reconcile aus.** `apply_device_created` seedet nur einen Stub mit `channels_count=0`; nichts ruft danach `attach_device_detail`/`refresh_device` auf. Frisch gepairte Geräte erscheinen **nie** als HA-Entities bis zum nächsten Voll-Bootstrap. Docstring suggeriert fälschlich, das sei erledigt.  |
| B2  | **Hoch** | `transport/ws.py:330-336`                   | **`replay_lost` wird verschluckt, wenn `oldest_seq` fehlt/kein int.** `isinstance(oldest, int)` als Vorbedingung → ohne das optionale Feld wird `_on_replay_lost` nie gefeuert, der Store driftet still ab. Die zentrale Resync-Garantie hängt an einem optionalen Feld.                                                            |
| B3  | **Hoch** | `transport/ws.py:336` + `client.py:301-317` | **`bootstrap()` blockiert den WS-Read-Loop.** `_on_replay_lost` wird _inline_ im Reader ge-awaited; `bootstrap()` macht N×M REST-Calls (Sekunden–Minuten) → Inbound-Ping-Deadline (60 s) läuft ab → Reconnect → ggf. erneut `replay_lost` → Schleife. Docstring behauptet das Gegenteil. Fix: in eigenen getrackten Task auslagern. |
| B4  | Mittel   | `model/device_client.py:67-71`              | **`get_value` gibt Error-Dict als Wert zurück.** `batch_read` legt bei Fehlern `{"error": …}` ab (`datapoints.py:79`); `get_value` reicht das ungeprüft durch → der HA-Servicehandler erhält `{"error": …}` als „Wert".                                                                                                             |
| B5  | Mittel   | `__init__.py:24-38` + `exceptions.py:117`   | **`LoomInternalError` wird nie exportiert** (definiert + im Mapping, fehlt in `__all__`). Consumer können es nicht ohne Tiefimport fangen — inkonsistent zu 11 anderen Klassen.                                                                                                                                                     |
| B6  | Mittel   | `client.py:255-260`                         | **Externes `ws_transport` ignoriert `subscriptions`.** Bei injiziertem Transport läuft der `if self._ws is None`-Zweig nie; an `start_events(subscriptions=…)` übergebene Topics werden still verworfen.                                                                                                                            |
| B7  | Mittel   | `operations/datapoints.py:74`               | **Fragiles Batch-Parsing.** `(payload or {}).get("results", payload or [])` iteriert bei einem Dict ohne `results`-Key über die Keys (Strings) → `item["address"]` crasht mit `TypeError`. Per `isinstance` unterscheiden.                                                                                                          |
| B8  | Mittel   | `store.py:242-258` u. a.                    | **Lost-Update bei `refresh_*`.** Zwischen `await transport.request(...)` und `_replace_summary` kann der Dispatch-Loop ein neueres `apply_value_changed` einspielen, das der ältere REST-Wert dann überschreibt. Billig zu fixen via `modified_at`-Guard.                                                                           |

---

## 4. Schicht-für-Schicht-Detail

### 4.1 Transport & Lifecycle — **7,0**

| Teilaspekt                                               | Note |
| -------------------------------------------------------- | :--: |
| Robustheit (Retry/Backoff/Timeout/RFC9457)               |  7   |
| WS-Resume (seq/since, Reconnect, Heartbeat, replay-lost) |  6   |
| Lifecycle (connect/bootstrap/start_events)               |  7   |
| Cleanup (Session/Task/Leaks)                             |  6   |
| Lesbarkeit                                               |  9   |

Neben B2/B3/B6:

- **[ROBUSTNESS] `ws.py:100,320` — unbounded `_envelope_queue`.** Stockt der Consumer (z. B. langer
  Re-Bootstrap), wächst die Queue unbegrenzt → OOM bei Event-Sturm auf großer CCU. Resume kann zudem
  Duplikate liefern, die ungefiltert reinlaufen. → bounded Queue + Drop-oldest + Watermark-Log + Dedup über `seq`.
- **[ROBUSTNESS] `http.py:65-69,227` — Retry-/Timeout-Modell inkonsistent.** Jeder der 3 Versuche
  hat sein eigenes `request_timeout_seconds` (30 s) → Worst-Case ≈ 92 s, nicht die kommentierten
  „~3,5 s". Klassen-Kommentar nennt nur 502/503 als retrybar, faktisch werden alle gewrappten
  Netzwerkfehler (inkl. `TimeoutError`) auf idempotenten Verbs wiederholt. → Gesamt-Deadline-Budget + Doku angleichen.
- **[ROBUSTNESS] `ws.py:362-370` — `reauth()` ohne Ack-/Fehlerpfad.** Bei `reauth_failed` wird nur
  geloggt; Token-Update wird nicht in `config.auth` gespiegelt → Endlos-Reconnect mit totem Token möglich.
- **[CLEANUP] `client.py:116,272` — `self._closing` wird gesetzt, nie gelesen** (Dead-State).
- **[SIMPLIFY] `ws.py:195-217` — `events()` erzeugt+cancelt 2 Tasks pro Envelope;** subtiler
  Item-Verlust, wenn der `waiter` gewinnt. → Sentinel (`None`) in die Queue bei `stop()`.
- **[SIMPLIFY] `http.py:310-348` vs. `252-296`** — `_do_once` und `request_bytes` duplizieren den
  kompletten Error-/Problem-Pfad → gemeinsamer Helper.
- **[NIT]** `assert` für Invarianten (mit `python -O` entfernt → `AttributeError` statt klarer
  Exception); `config.py:54-61` nutzt unnötig `object.__setattr__` auf nicht-frozen Klasse.
- _Nicht-Befund (geprüft):_ `except A, B:` in `http.py:357` ist **kein** Bug — PEP 758 macht
  klammerlose Except-Tupel ab Python 3.14 gültig.

### 4.2 Store + Events + Bridge — **8,0**

| Teilaspekt   |     Note      |
| ------------ | :-----------: | --------------- | -------------- | ------------------------ | --- |
| Store-Design | 7 · Event-Bus | 9 · Event-Typen | 7 · Decoupling | 9 · Komplexität store.py | 6   |

**Entwarnung Async-Safety:** Der Dispatch-Loop ist Single-Task, `publish` awaitet seriell, die
`apply_*`-Methoden sind synchron → **keine** Races auf dem Normalpfad. Der einzige reale
Interleaving-Pfad ist B8 (`refresh_*` aus HA-Task vs. Dispatch-Loop).

- **[SIMPLIFY] `store.py` ist ein God-Object** (5 Verantwortlichkeiten: Graph, CDP-Katalog, Hub,
  **Write-back-REST**, **REST-Refresh**). (d)+(e) sind eigentlich Transport-/Operations-Belang und
  brechen das eigene Schichtenmodell. → Write-back/refresh in `operations/` auslagern, Store wird
  rein In-Memory + transportfrei testbar.
- **[SIMPLIFY] `store.py:333-346,487,505,580-586` — O(n)-Scans über die DP-Map** bei jeder
  Channel-Mutation → quadratisch über den Bootstrap großer CCUs. → Sekundärindex
  `device_address → keys` oder verschachtelte Map.
- **[SIMPLIFY] Drei parallele Factory-Hooks** (`_data_point_factory`/`_cdp_factory`/
  `_calculated_factory`) mit identischem Muster; die calc-Variante ist asymmetrisch ein stiller
  No-op ohne Factory (`store.py:752-753`).
- **[NIT] `store.py:550-551` — `del dp._value_override` via `hasattr`** greift in Compat-Internals
  (Layering-Leak) → polymorphe `_clear_value_override()`-Methode (No-op in Basis).
- **[CLEANUP]** Veraltete Docstrings: `bridge.py:46-58` („Six… All **three** are scoped"),
  `bridge.py:62-64` (Rollback-Bindung). `bus.py:57-68` — `getattr(event,"event_key",None)` obwohl
  jedes `LoomEvent` das Feld hat.
- **[NIT]** 10× nahezu identische `__post_init__`-Blöcke `event_key = payload.central` in
  `types.py`/`synthetic.py` → optionales Mixin.

### 4.3 Model + Operations — **8,0**

| Teilaspekt    |          Note          |
| ------------- | :--------------------: | -------------- | -------------- | --------------- | --- |
| Model-Wrapper | 8 · Operations-Facades | 8 · Exceptions | 7 · Konsistenz | 7 · Duplikation | 7   |

Neben B4/B5/B7:

- **[SIMPLIFY] Transport-Guard 3× dupliziert** (`device.py:253-284`, `channel.py:176-182`) → ein
  `LoomStore.require_transport()`.
- **[ROBUSTNESS] Inkonsistente `allow_retry`-Markierung.** Mehrere DELETE ohne Markierung (default-
  retried), mehrere PUT _mit_ redundanter `allow_retry=True` (PUT wird ohnehin retried), während
  `links.py:79` explizit markiert. → Eine Regel: nur Abweichungen vom Default markieren.
- **[CLEANUP] `links.py:181-209` — drei 1:1-Alias-Methoden** ohne Mehrwert (spiegeln nur Daemon-
  Command-Namen). `hub.py` (291 Z., 6 Themen) ist der eine Sammelbehälter gegen 16 granulare Module.
- **[NIT] `data_point.py:22` — `SetValuePriority = str`** schützt nichts (Kommentar verspricht
  Typo-Erkennung) → `Literal[…]`. Mehrere `Any`-Lecks in `device.py` (`_forced_availability`,
  stringbasiertes `"FALSE" in forced`).
- **Über-Engineering-Einschätzung:** Die 7 HA-relevanten Operations-Module sind gerechtfertigt; die
  9 Admin-Module (`matter`, `diagnostics`, `backup`, `sessions`, `visibility`, `config`, `centrals`,
  `users`, `auth`) sind für ein Alpha-Backend **deutlich vorgezogen** — sauber gebaut, aber
  Pflegelast/Testfläche, die das Kernziel (kategorisiertes DP-Modell) nicht voranbringt.

### 4.4 Compat — Central-Adapter — **7,0**

| Teilaspekt          |            Note            |
| ------------------- | :------------------------: | ------------------- | ------------------------ | --------------- | --- |
| adapter.py-Struktur | 5 · refresh.py-Korrektheit | 8,5 · Surface-Treue | 8 · Brüchigkeit/Kopplung | 5 · Wartbarkeit | 6   |

- **[SIMPLIFY] `adapter.py` aufteilen** (jede Klasse ist schon isoliert → reines Verschieben):
  - `central/coordinators/hub.py` ← `_HubCoordinator` (Z. 201-572, größter Brocken inkl. 6 `_fetch_*`)
  - `central/coordinators/query.py` ← `_QueryFacade` (575-711)
  - `central/coordinators/devices.py` ← `_DeviceCoordinator`/`_ClientCoordinator`
  - `central/coordinators/cache.py` ← `_IncidentStore`/`_Recorder`/`_CacheCoordinator`
  - `central/coordinators/ccu.py` ← `_JsonRpcClient`/`_LinkCoordinator`/`_Configuration`
  - `central/adapter.py` behält nur `LoomCentralAdapter` + Bootstrap (~450 Z.)
- **[ROBUSTNESS]** Flächendeckendes `getattr(…, default)` (s. §2.2); `except Exception` an ~12
  Stellen (verschluckt Parsing-Bugs wie 404er) → auf `BaseLoomException`/`LoomTransportError`
  einengen.
- **[ROBUSTNESS] `adapter.py:1090` — initialer `fetch_hub_singleton_data()` in `start()`
  ungeschützt** (nur der Loop hat `try/except`) → ein Publish-/Enum-Fehler bricht `start()` ab.
- **[ROBUSTNESS] `adapter.py:234-240` — Zugriff auf private `store._upsert_*`** während
  `_bootstrap_hub_catalogue` die öffentliche `attach_hub_catalogue` nutzt (Inkonsistenz).
- **[SIMPLIFY] `adapter.py:467-572` — die 6 `_fetch_*` sind ~identisch** (try/except → central-Filter
  → `update_value` → changed-Liste) → ein `_poll(fetch, apply)`-Helper.
- **[CLEANUP] `events/__init__.py:66-93` — `DeviceLifecycleEvent` ist Dead Code** (refresh.py
  importiert die aiohomematic-Variante) → zwei konkurrierende Typen im selben Namespace.

### 4.5 Compat — Model-Layer — **7,5**

| Teilaspekt |    Note     |
| ---------- | :---------: | ------------------------------------ | ------------------ | --------------------------- | --- |
| custom     | 7 · generic | 8 · protocol_surface-Vollständigkeit | 6 · hub-singletons | 8 · Duplikation/Boilerplate | 5   |

Grobe Aufteilung: ~55 % echte Logik (Resolver, Climate/Cover/Light-Mapping, Diff-Tracking,
unique_id-Komposition), ~45 % Boilerplate/Stub-Spiegelung.

- **[SIMPLIFY] `_protocol_surface.py` — gemeinsame Member 2–3× redeklariert** in
  `_Generic`/`_Custom`/`_Hub`-Surfaces, obwohl alle von `_CommonProtocolSurface` erben (~30 Z.
  hochziehbar; senkt Drift-Risiko).
- **[ROBUSTNESS] `custom/__init__.py:400-422` — `CustomDpDimmer.turn_on` ist nicht atomar:** bei
  `brightness` + `hs_color`/`color_temp`/`effect` mehrere separate `invoke`-Ops → Flackern. Der
  `CallParameterCollector` existiert bereits in `data_point.py`. → bündeln.
- **[ROBUSTNESS] Inkonsistente generic-Fallbacks:** `CustomDpSwitch.value` (282) re-rendert auf
  Channel-STATE-Event, `CustomDpDimmer.is_on` (321)/`CustomDpCover.is_closed` (493) nicht — prüfen,
  ob bewusst.
- **[SIMPLIFY] `custom/__init__.py` (1112 Z.) entlang der vorhandenen Banner aufsplitten**
  (`light.py`/`cover.py`/`climate.py`/`lock.py`/`siren.py`/`_base.py`) → ~150-200 Z./Modul.
- **[SIMPLIFY] ~10 `_config_value(key=X) or self._state.get(X) or ()`-Properties** (available_tones/
  lights/colors/sounds) → ein `_option_list(key=…)`-Helper (~25 Z.).
- **[NIT] `week_profile.py:267-269` — `available_target_channels` returns hart `{}`** während
  `target_channel_name` (170-173) dieselben Daten korrekt liest (inkonsistenter toter Platzhalter).
- **[NIT] `combined.py:118`** liest `summary.value` (roh) statt `unit_dp.value` (enum-resolved).

### 4.6 Tests, Tooling, Packaging, Doku — **7,5**

| Teilaspekt     |       Note        |
| -------------- | :---------------: | ---------------- | ------------------ | -------- | ------ | --- |
| Test-Abdeckung | 7 · Test-Qualität | 8 · Tooling/Lint | 9 · Packaging/Deps | 5 · Doku | 6 · CI | 8   |

**Stark:** `MockDaemon` (echter aiohttp-Server statt URL-Stubbing), Golden-Fixtures für unique_id/
category, FIFO-Response-Queue für Retry-Tests, 3.14t-Free-Threading-Matrix, Trusted Publishing,
Single-Source-Version (`const.py` → pyproject dynamic). 6 xfails alle mit begründetem `reason=`,
deckungsgleich mit `todo.md`. Die 2 Custom-Linter (`lint_kwonly`/`lint_all_exports`) erzwingen reale
Konventionen, die ruff nicht abdeckt — kein Overkill.

- **[CLEANUP] Dependency-Pins driften 4-fach.** `requirements.txt` `aiohomematic==2026.6.2` vs.
  `pyproject.toml` `>=2026.6.2` (dort doppelt: Runtime + `[dev]`); `aiohttp >=3.14.1` vs. `>=3.9`;
  `pydantic >=2.13.4` vs. `>=2.6`. → `pip install` löst anderes auf als CI testet. **`python-slugify`
  fehlt in `requirements.txt`** (nur transitiv über aiohomematic). → eine Quelle der Wahrheit.
- **[CLEANUP] Tote Dev-Deps:** `aioresponses>=0.7.6` wird nirgends importiert (von `MockDaemon`
  ersetzt); `mypy` doppelt in `requirements_test.txt`; `aiohomematic` in `[dev]` dupliziert; toter
  `orjson`-Kommentar in `requirements.txt:5`.
- **[CLEANUP] ~~README driftet hart~~ — BEHOBEN (2026-06-21):** die zwei „daemon-side gaps" als
  „now live and bound" umformuliert, `python3.11` → `python3.14`, types-Version `0.1.2`/`0.1.3` →
  `0.1.24`. Zugleich Strategie-Satz auf „alternative backend" gestellt (§2.1).
- **[ROBUSTNESS] Transport-Resilienz ist die kritischste Coverage-Lücke:** `ws.py` (Resume,
  `on_replay_lost`, Heartbeat) und `http.py` (Retry/Backoff/Idempotenz-Gating) haben nur
  Happy-Path-Tests — genau die Schichten mit dem höchsten Produktionsrisiko (vgl. B2/B3).
- **[CLEANUP]** `optimization-needs.md` und `todo.md` überschneiden sich (zwei Tracking-Dateien;
  P1-vs-P3-Widerspruch beim nested-snapshot-Bootstrap). `CLAUDE.md:37` nennt noch „aioresponses",
  faktisch `MockDaemon`.
- **[NIT]** Lokaler `build/lib/`-Snapshot (Version 2026.6.17) liegt im Arbeitsbaum (git-ignored,
  harmlos, aber verwirrend) → gelegentlich `rm -rf build *.egg-info`.

---

## 5. Empfohlene Reihenfolge

**Sofort (Korrektheit):**

1. B1 — `device.created` muss einen async Reconcile auslösen (sonst keine neuen Geräte in HA).
2. B2 + B3 — `replay_lost` bedingungslos feuern **und** `bootstrap()` in eigenen Task auslagern.
3. B4/B5/B6/B7 — die vier mittleren Bugs (Error-Dict, Export, ignored subscriptions, batch-parse).

**Kurzfristig (Brüchigkeit):** 4. Drift-Wächter-Test gegen die echten aiohomematic-Protocols (§2.2) — fängt den fragilsten Punkt. 5. `getattr`-Disziplin im Compat-Layer + obere aiohomematic-Versionsschranke (§2.1/2.2). 6. Transport-Resilienz-Tests (WS-Reconnect/Resume/Heartbeat, HTTP-Backoff) — §4.6. 7. Dependency-Single-Source + README-Korrektur — §4.6.

**Mittelfristig (Vereinfachung):** 8. `adapter.py` und `custom/__init__.py` aufteilen (mechanisch, §4.4/4.5). 9. Write-back/refresh aus `store.py` in `operations/` ziehen + Sekundärindex (§4.2). 10. Bounded WS-Queue, Retry/Timeout-Budget, Protocol-Surface-Entdoppelung. 11. 9 Admin-Operations-Module als „eingefroren/optional" markieren; Energie aufs DP-Kernmodell.

**Bewusst nicht ändern:** Einbahn-Datenfluss-Decoupling, EventBus-Fehlerisolierung, der
unique_id-Lockstep (entschärft), keyword-only + In-place-Mutation, MockDaemon, das Lint-Tooling.
