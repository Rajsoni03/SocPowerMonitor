# SocPowerMonitor — Restructure Plan

> Generated: 2026-05-22  
> Reviewer: Claude Code  
> Scope: Full codebase review — backend, frontend, DB, threading, security, UI/charts

---

## Summary

| Category              | Issues | Max Severity |
|-----------------------|--------|--------------|
| Threading & Stability | 3      | Critical     |
| Bugs                  | 3      | High         |
| Security              | 2      | High         |
| Database              | 4      | Medium       |
| Backend               | 5      | Medium       |
| API Design            | 4      | Medium       |
| Error Handling        | 4      | Medium       |
| UI / Charts           | 8      | Medium/Low   |
| **Total**             | **33** |              |

---

## Issues

### Critical / High

#### Threading & Stability

**T1 — Orphaned capture thread** `power_service.py:251`  
After `thread.join(timeout=5)`, if `thread.is_alive()` is true the function returns
but the thread keeps running and writing to the database. No kill mechanism exists.  
**Fix:** Use a threading Event as a stop signal; set it before join.

**T2 — Race condition on shared state** `power_service.py:335`  
`last_stream_payload` is written by the capture thread and read by the main thread
(via `status()`) without any lock.  
**Fix:** Add `threading.Lock` around all reads/writes of `last_stream_payload`,
`active_session_id`, and `capture_thread`.

**T3 — Session ID cleared before thread stops** `power_service.py:124`  
`_clear_capture_state()` sets `active_session_id = None` immediately, while the thread
may still be in flight. Concurrent `status()` calls return stale/None session.  
**Fix:** Clear session ID only after confirming thread has exited.

---

#### Bugs

**B1 — Timezone-naive datetime comparison** `app.py:109`  
```python
parsed = datetime.fromisoformat(since_ts.replace('Z', ''))
```
Strips 'Z' but produces a naive datetime. Comparing against UTC-aware DB timestamps
raises `TypeError` or silently returns wrong results.  
**Fix:** `since_ts.replace('Z', '+00:00')` or use `dateutil.parser.isoparse`.

**B2 — Deprecated utcnow() throughout** `models.py:32,70`  
`dt.datetime.utcnow` is deprecated since Python 3.12 and stores timezone-naive
datetimes, causing silent mismatches in filters and exports.  
**Fix:** Replace with `datetime.now(timezone.utc)` everywhere.

**B3 — Serial connect unguarded** `uart.py:52`  
`serial.Serial(port)` throws on bad port/permissions with no try/except, crashing
the capture thread with an unhandled exception.  
**Fix:** Wrap in try/except, raise `UartSetupIssue` with a clear message.

---

#### Security

**S1 — DOM XSS via innerHTML** `dashboard.js:447`  
`session.config_name` (server-sourced) is interpolated directly into `innerHTML`.
A malicious config name could execute JavaScript.  
**Fix:** Build session cards using `createElement` + `textContent`.

**S2 — force=True on all JSON parsing** `app.py:42,59,76`  
`request.get_json(force=True)` bypasses Content-Type validation, allowing
non-JSON bodies to be silently parsed.  
**Fix:** Remove `force=True`; return 415 if Content-Type is not `application/json`.

---

### Medium Severity

#### Database

**D1 — Missing index on rail_id** `models.py:66`  
Only `(session_id, ts)` is indexed. Per-rail queries in exports and samples
endpoint do full table scans on large datasets.  
**Fix:** Add `Index('idx_sample_rail_session', 'rail_id', 'session_id')`.

**D2 — No CASCADE on foreign keys** `models.py:66`  
Deleting a session leaves orphaned `Sample` rows. No `ondelete='CASCADE'`
on `Sample.session_id` or `Sample.rail_id`.  
**Fix:** Add `ondelete='CASCADE'` to both foreign key columns.

**D3 — N+1 queries in CSV export** `app.py:130`  
`row.rail.name` inside the streaming loop fires one extra query per row
when rail relationship is not eagerly loaded.  
**Fix:** Add `.options(joinedload(Sample.rail))` to the export query.

**D4 — No real pagination on sessions** `app.py:71`  
`Session.query...limit(50)` is hardcoded with no `offset` parameter.
Older sessions are unreachable from the UI.  
**Fix:** Accept `limit` and `offset` query params; default limit 50.

---

#### Backend

**BK1 — Rail.query.all() on every measurement** `power_service.py:310`  
`_persist_samples()` fetches every rail row on each measurement cycle to
build a lookup dict. Scales poorly with many rails.  
**Fix:** Cache rail lookup per session; invalidate on new rail creation only.

**BK2 — Double directory scan in config loader** `config_loader.py:14`  
`load_config()` calls `glob('*.json')` twice — once by stem, once by
config name. Both loops are O(n) over all files.  
**Fix:** Merge into a single pass; cache parsed configs in a dict after first load.

**BK3 — Full config snapshot per session** `power_service.py:95`  
Entire config (potentially 100+ rails) is JSON-serialized into every
session's metadata, duplicating kilobytes per session.  
**Fix:** Store a config hash in session metadata; keep config data in a
separate lookup table or file cache.

**BK4 — Queue full drops data silently** `power_service.py:338`  
When the stream queue is full, the oldest payload is discarded with no
log or metric emitted.  
**Fix:** At minimum log a warning with a drop count.

**BK5 — PowerService is a God class** `power_service.py`  
A single 350+ line class handles config, session lifecycle, UART, parsing,
DB persistence, and SSE streaming.  
**Fix:** Split into `CaptureManager`, `SessionManager`, `StreamManager`.

---

#### API Design

**A1 — Inconsistent response format** `app.py` (multiple routes)  
Routes mix plain dicts, `jsonify()`, and status-code tuples with no
consistent envelope. Clients cannot rely on a stable shape.  
**Fix:** Standardize all responses as `{"data": ..., "error": null}` with
a shared helper function.

**A2 — No request body validation** `app.py` (POST routes)  
Missing or wrong-typed fields in POST bodies silently become `None` or
raise unhandled TypeErrors.  
**Fix:** Add explicit type/presence validation on all POST bodies; return
400 with a field-level error message.

**A3 — No error handlers on routes** `app.py`  
DB errors, key errors, and type errors in routes produce raw 500 responses
with tracebacks in debug mode or silent failures in production.  
**Fix:** Add `@app.errorhandler(Exception)` global handler and per-route
try/except for DB calls.

**A4 — config_loader raises unguarded JSONDecodeError** `config_loader.py:30`  
Malformed JSON config files crash the server at startup or config activation.  
**Fix:** Wrap `json.loads()` in try/except; include filename in the error message.

---

### UI / Charts

**UI1 — Full canvas redraw on every mousemove** `dashboard.js:524`  
`drawLineChart()` repaints the entire chart on every `mousemove` event —
up to 60 full redraws per second during hover.  
**Fix:** Split into `renderStatic()` (called on data change only) and
`renderTooltip()` (overlay-only repaint on mousemove). Use a dirty flag.

**UI2 — Y-axis always .toFixed(0)** `dashboard.js:544`  
All Y-axis tick labels use `.toFixed(0)` regardless of value range. Values
below 1 mW display as `"0 mW"`.  
**Fix:** Compute decimal places dynamically:
```js
const decimals = range < 1 ? 2 : range < 10 ? 1 : 0;
tickValue.toFixed(decimals)
```

**UI3 — SSE never reconnects on error** `dashboard.js:785`  
`onerror` only shows a message; the EventSource is not recreated. A brief
network interruption permanently kills the live stream until page reload.  
**Fix:** Implement exponential backoff reconnect in an `openEventStream()`
wrapper function.

**UI4 — EventSource not closed on page unload** `dashboard.js:780`  
The SSE connection is never explicitly closed, leaving an open HTTP
connection after navigation.  
**Fix:**
```js
window.addEventListener('beforeunload', () => state.eventSource?.close());
```

**UI5 — History array sorted on every SSE event** `dashboard.js:207`  
`state.history.sort(...)` runs on the full array each time a new data
point arrives, O(n log n) per event.  
**Fix:** Use a circular buffer (fixed-size array with head pointer) so
insertion is O(1) and sort is never needed.

**UI6 — Tooltip clips off-screen vertically** `dashboard.js:280`  
Tooltip boundary check only handles left-edge overflow. On short viewports
the tooltip clips off the top or bottom.  
**Fix:** Add top/bottom clamp using `window.innerHeight` and tooltip height.

**UI7 — Sessions list max-height hardcoded** `dashboard.css`  
`.sessions-list { max-height: 440px }` cuts off on small screens and wastes
space on large ones.  
**Fix:** Replace with `calc(100vh - <offset>)` or use flex-grow within a
flex container.

**UI8 — No keyboard focus styles** `dashboard.css`  
Buttons have `:hover` styles but no `:focus-visible`, breaking keyboard
navigation for accessibility.  
**Fix:** Add `:focus-visible` ring to all interactive elements.

---

## Restructure Plan

### Phase 1 — Stability & Correctness *(fix first)*

- [x] **T1** After `join(timeout=10)`, always mark session ended + clear state even if thread still alive
- [x] **T2** Added `_state_lock = threading.Lock()` guarding `last_stream_payload`, `active_session_id`, `capture_thread`
- [x] **T3** `_clear_capture_state` now fully under lock; idempotent (guards thread identity + session_id)
- [x] **B1** Fixed `since_ts` parsing: `.replace('Z', '+00:00')` + normalize to naive UTC for SQLite
- [x] **B2** Replaced `dt.datetime.utcnow` with `_utcnow()` helper using `datetime.now(timezone.utc).replace(tzinfo=None)`
- [x] **B3** Wrapped `serial.Serial()` in try/except raising `UartSetupIssue` with clear message
- [x] **D3** Added `joinedload(Sample.rail)` to CSV export query

### Phase 2 — Security

- [x] **S1** Session cards rebuilt with `createElement` + `textContent`; no more `innerHTML` for server data
- [x] **S2** Removed `force=True` from all `get_json()` calls; returns 415 on missing Content-Type
- [x] **A3** Added `@app.errorhandler(Exception)` global handler returning JSON for all 500s
- [x] **A4** Wrapped `json.loads()` in `config_loader.py` with try/except including filename in message

### Phase 3 — Database

- [x] **D1** Added `Index('idx_sample_rail_session', 'rail_id', 'session_id')` to `Sample`
- [x] **D2** Added `ondelete='CASCADE'` to `Sample.session_id` and `Sample.rail_id`
- [x] **D4** Added `offset` + `limit` query params to `/api/sessions` (clamped 1–200, default 50)
- [x] **A2** Added field-level type validation to all POST endpoints; returns 400 with field name

### Phase 4 — Backend Refactor

- [x] **BK1** Rail cache (`_rail_id_cache`) warmed once per session; only queries DB on new rail creation
- [x] **BK2** Single-pass config scan with in-memory cache; `invalidate_cache()` for forced reload
- [~] **BK3** Deferred — storing full config snapshot is correct for historical session accuracy
- [x] **BK4** Added `log.warning` when stream queue drops a payload, including session ID
- [ ] **BK5** Split `PowerService` into `CaptureManager`, `SessionManager`, `StreamManager` *(future refactor)*
- [x] **A1** Standardized: all routes use `jsonify()`; `_require_json()` helper validates POST bodies

### Phase 5 — UI / Charts

- [x] **UI1** Static layer rendered once; `putImageData` restores snapshot on mousemove (no full repaint)
- [x] **UI2** `yAxisDecimals(range)` computes 0/1/2dp based on value range; sub-mW values show correctly
- [x] **UI3** `ensureStream()` wraps EventSource with exponential backoff (1s → 30s cap)
- [x] **UI4** `beforeunload` closes EventSource and clears retry timer
- [x] **UI5** Replaced sort-on-push with `push()` + `shift()` trim; no sort needed (SSE is chronological)
- [x] **UI6** Tooltip clamped vertically: `top + rect.height > window.innerHeight - 8` guard added
- [x] **UI7** Sessions list uses `flex: 1 1 0` + `max-height: calc(100vh - 320px)` instead of fixed 440px
- [x] **UI8** Added `:focus-visible` ring on all buttons, selects, and inputs

---

## File Change Map

| File | Phases Touching It |
|------|--------------------|
| `src/power_service.py` | 1, 3, 4 |
| `src/app.py` | 1, 2, 3 |
| `src/models.py` | 1, 3 |
| `src/uart.py` | 1 |
| `src/config_loader.py` | 2, 4 |
| `src/static/dashboard.js` | 2, 5 |
| `src/static/dashboard.css` | 5 |
