# Architecture

This document describes the **v0.3.x** design. It is intended for
contributors who want to extend the plugin or trace a bug end-to-end.
For installation see [INSTALL.md](INSTALL.md); for usage see
[USER_GUIDE.md](USER_GUIDE.md).

---

## 1. Component diagram

```
                              ┌──────────────────────────────────────────┐
                              │            ComfyUI main process          │
                              │                                          │
   ┌──────────────────┐       │   aiohttp event loop                     │
   │  ComfyUI frontend│       │   ┌──────────────────────────────┐       │
   │  (PrimeVue+Pinia)│       │   │  /prompt    /history/{id}    │       │
   │                  │       │   │  /queue     /system_stats    │       │
   │  ┌────────────┐  │       │   └──────────────────────────────┘       │
   │  │ sidebar    │  │       │              ▲           ▲                │
   │  │ tab (UI)   │◄─┼───────┼──────────────┼───────────┼────────────────┤
   │  └────────────┘  │       │              │           │                │
   │  ┌────────────┐  │       │   ┌──────────┴───┐   ┌───┴────────┐       │
   │  │ topbar     │  │       │   │  scheduler   │   │  Prompt    │       │
   │  │ "Schedule" │  │       │   │  thread      │   │  executor  │       │
   │  │ button     │  │       │   │  (daemon)    │   │  workers   │       │
   │  └────────────┘  │       │   └──────┬───────┘   └────────────┘       │
   │                  │       │          │   ▲                              │
   │  HTTP fetch /    │       │          │   │                              │
   │  POST body ──────┼──────►│  /api/schedule/*                           │
   └──────────────────┘       │          │   │                              │
                              │   ┌──────┴───┴───────────────┐              │
                              │   │  ScheduledQueueDB        │              │
                              │   │  (SQLite, WAL mode)      │              │
                              │   └──────────┬───────────────┘              │
                              │              │                              │
                              └──────────────┼──────────────────────────────┘
                                             │
                                             ▼
                              $COMFYUI_ROOT/user/scheduled_queue.sqlite3
                                  ├─ .sqlite3   (main DB)
                                  ├─ .sqlite3-wal (write-ahead log)
                                  └─ .sqlite3-shm (shared memory)
```

**Four actors, three trust boundaries:**

| Actor | Lives in | Trust |
|---|---|---|
| Frontend (sidebar / topbar) | browser tab, served by ComfyUI static router | untrusted input → goes through `_validate_add_item`, `_strip_payload`, status-whitelist |
| Plugin HTTP routes | ComfyUI aiohttp event loop | trusted inside ComfyUI; exposes `/api/schedule/*` |
| Scheduler daemon thread | ComfyUI process, `daemon=True` | trusted; the only writer of `status='dispatched'` |
| ComfyUI core (`/prompt`, `/history`) | ComfyUI process | treated as the source of truth for job *outcomes* — scheduler never invents a `done` |

---

## 2. Process model

```
                    ComfyUI main process (PID)
                    ├─ aiohttp event loop           ← /api/schedule/* routes
                    ├─ prompt executor workers
                    └─ ScheduledQueue daemon thread
                       ├─ tick() every 1.0 s (default; shortened to IDLE_DISPATCH
                       │       right after a dispatch so reconcile catches it fast)
                       │   ├─ read paused state from scheduler_state
                       │   ├─ claim_next_due_job()  (UPDATE … RETURNING)
                       │   ├─ apply pre-dispatch hooks
                       │   │     - UI→API format conversion (workflow_format.py)
                       │   │     - control_after_generate → seed randomization
                       │   └─ POST /prompt to ComfyUI via urllib
                       │
                       └─ reconcile() every RECONCILE_INTERVAL=5.0 s
                           ├─ GET /history/{prompt_id} for each running job
                           ├─ status "success" → mark_done → job_history
                           └─ status "error"   → mark_failed → job_history
```

The scheduler thread is `daemon=True`, so it dies with ComfyUI. We
register `atexit(scheduler_thread.stop)` as belt-and-braces cleanup, but
the daemon flag is the actual safety guarantee.

**Adaptive tick cadence.** Tick interval collapses to `IDLE_DISPATCH`
(~1 s) right after a dispatch so the *next* reconcile loop picks up the
just-POSTed prompt quickly. Once nothing is `running` it stretches back
to the normal `TICK_INTERVAL` to avoid burning CPU on idle ComfyUI
instances. See `scheduler._run()` for the exact bookkeeping.

---

## 3. Why HTTP for `/history` and not WebSocket?

`/history/{prompt_id}` is the public, documented ComfyUI API for
"did this prompt finish and what was the result". WebSocket status
messages are also published by ComfyUI's event server but require
long-lived client connections; for a one-second poll cadence the HTTP
endpoint is simpler and easier to mock in tests.

If ComfyUI's WebSocket proves unreliable in practice, we can switch
later **without changing the database schema** — the scheduler would
just receive callbacks instead of polling.

---

## 4. Frontend mount lifecycle

```
ComfyUI frontend (PrimeVue + pinia)
  └─ loadExtensions()
       └─ for each script under /extensions/<plugin>/<script>.js
            await import(fileURL)
                │
                └─ runs our sidebar_tab.js top-level:
                     app.registerExtension({ name, actionBarButtons })
                     app.extensionManager.registerSidebarTab({ id, ..., render })
```

`render: (container) => Element` is the framework-managed hook. Every
time the user activates our sidebar tab, the framework:

1. clears the `sidebar-content-container` div
2. calls `render(container)`
3. attaches the returned element

When the user switches tabs, the framework simply removes the returned
element from the DOM. The plugin observes that removal by subscribing
to `app.extensionManager.sidebarTab.$subscribe` (which fires when
`activeSidebarTabId` changes) and uses it to clean up its `setInterval`.
We never touch `document.body` — earlier versions did, and we removed
it because the cost scales with every DOM mutation across the whole
page.

---

## 5. Data model

```sql
scheduled_jobs:
  id              TEXT PRIMARY KEY
  payload         TEXT          -- raw ComfyUI prompt JSON
  workflow_title  TEXT          -- from Pinia activeWorkflow.filename (NULL ok)
  scheduled_at    REAL          -- unix-seconds when it should fire
  dispatched_at   REAL          -- when /prompt accepted it (NULL until then)
  finished_at     REAL          -- when moved to job_history
  status          TEXT          -- scheduled | dispatched | running
                                 -- done | failed | cancelled | interrupted
  queue_order     INTEGER       -- manual reorder key
  priority        INTEGER       -- higher wins inside the same ready bucket
  note            TEXT
  prompt_id       TEXT          -- set when ComfyUI accepts /prompt
  client_id       TEXT          -- who scheduled it
  auto_retry      INTEGER       -- 0..N
  created_at      REAL

job_history:
  id              TEXT PRIMARY KEY
  prompt_id       TEXT
  finished_at     REAL
  status          TEXT          -- done | failed | cancelled | interrupted
  outputs         JSON (TEXT)   -- decoded at list time
  error           TEXT
  workflow_title  TEXT          -- denormalised for sidebar label
  dispatched_at   REAL          -- synthesised for legacy rows

scheduler_state:
  key             TEXT PRIMARY KEY
  value           TEXT          -- "paused" lives here as "1" / "0"
```

### 5.1 Schema migrations

`ScheduledQueueDB.__init__()` runs idempotent `ALTER TABLE … ADD COLUMN`
statements on every boot. Upgrades from 0.3.5 → 0.3.10 have added
`queue_order`, `priority`, `auto_retry`, `workflow_title`,
`dispatched_at`, and `finished_at` without ever requiring a destructive
migration.

### 5.2 Who writes what

| Status transition | Writer | Code |
|---|---|---|
| (none) → `scheduled` | `/api/schedule/add`, `/add-batch` | `database.add_job` |
| `scheduled` → `dispatched` | scheduler daemon, atomic `UPDATE … RETURNING` | `claim_next_due_job` |
| `dispatched` → `running` | scheduler after `/prompt` returns 200 | `mark_running` |
| `*` → `done` / `failed` | reconcile loop after `/history` returns a definitive status | `_finish` |
| `scheduled` → `cancelled` | `/api/schedule/cancel/{id}` | `cancel_job` |
| `*` → `interrupted` | startup orphan recovery | `recover_orphans` |
| move to `job_history` | `_finish` (one transactional write) | `_finish` |

`claim_next_due_job` is the **only** writer for status `dispatched`.
Every other status transition goes through `update_job` or `_finish` so
the audit trail stays consistent.

---

## 6. Job state machine

```
                    ┌──────────────┐
                    │  (created)   │
                    └──────┬───────┘
                           │ add / add-batch
                           ▼
                    ┌──────────────┐    cancel         ┌────────────┐
                    │  scheduled   │ ─────────────────►│ cancelled  │
                    └──────┬───────┘                   └─────┬──────┘
                           │ tick() claims a due row         │ /history
                           ▼                                 │ (never moves back)
                    ┌──────────────┐                         │
                    │  dispatched  │                         │
                    └──────┬───────┘                         │
                           │ /prompt returns 200             │
                           ▼                                 │
                    ┌──────────────┐                         │
                    │   running    │ ────────────────────────┤
                    └──────┬───────┘                         │
                           │ /history status="success"      │
                           ▼                                 │
                    ┌──────────────┐                         │
                    │     done     │ ──► job_history ────────┘
                    └──────────────┘
                           │ /history status="error"
                           ▼
                    ┌──────────────┐
                    │    failed    │ ──► job_history
                    └──────────────┘

              On startup, recover_orphans() finds rows stuck in
              dispatched/running from a previous ComfyUI run and
              flips them to `interrupted`, waiting for explicit
              resume_all() / reset_all_interrupted() to retry.
```

**Terminal states** (no outgoing edges except to `job_history`):
`done`, `failed`, `cancelled`. The same status string is reused for
the `job_history.status` column.

**Why `interrupted`?** When ComfyUI crashes, any prompt that was
already accepted (`dispatched`) may or may not have actually executed.
We refuse to invent a `done` — instead we mark the row `interrupted`
and require an explicit human `Resume` or `Reset` action.

---

## 7. HTTP route surface

All routes are mounted under `/api/schedule/`. Bodies are JSON unless
noted; responses use `_json_response()` which sets
`Content-Type: application/json`.

| Method + Path | Handler | Purpose |
|---|---|---|
| `POST /add` | `add_handler` | enqueue a single job (body: payload + scheduled_at + priority + note + workflow_title) |
| `POST /add-batch` | `add_batch_handler` | enqueue 1..50 jobs at once (count field) |
| `GET /list?status=…&limit=&offset=` | `list_handler` | paginated list of `scheduled_jobs` (or history rows when filter is empty) |
| `GET /job/{id}` | `job_detail_handler` | full job row + decoded outputs |
| `GET /job/{id}/export` | `export_handler` | raw JSON download of one job |
| `POST /update/{id}` | `update_handler` | patch whitelisted fields on a `scheduled` job |
| `POST /reorder/{id}` | `reorder_handler` | bump `queue_order` up / down |
| `POST /cancel/{id}` | `cancel_handler` | mark a `scheduled` row `cancelled` (running rows 409) |
| `POST /run-now/{id}` | `run_now_handler` | move a `scheduled` row to due-now |
| `POST /repeat/{id}` | `repeat_handler` | clone a `done` / `cancelled` job back into `scheduled` |
| `POST /clear` | `clear_handler` | delete jobs by status (whitelisted) |
| `GET /status` | `status_handler` | paused flag + per-status counts + version |
| `POST /pause-all` | `pause_all_handler` | set scheduler_state.paused = "1" |
| `POST /resume-all` | `resume_all_handler` | set scheduler_state.paused = "0" |
| `POST /orphans/recover` | `orphan_status_handler` | startup orphan sweep, idempotent |

Code reference: `src/comfyui_scheduled_queue/routes.py`.

### 7.1 Whitelist enforcement

- `/add` validates every item through `_validate_add_item` (422 on bad type).
- `/update` rejects any field outside `{scheduled_at, priority, note, auto_retry, workflow_title}`.
- `/list` strips `payload` from rows before sending, so workflow JSON never echoes back over the wire.
- `/cancel` returns 409 when the row is `running` or `done` (truthful 4xx contract).

---

## 8. Frontend data flow

```
sidebar_tab.js
  │
  ├─ mount(container)
  │     ├─ render filter tabs
  │     ├─ render status bar
  │     ├─ fetch GET /status   ──► status_bar (paused, counts)
  │     ├─ fetch GET /list?status=…&limit=50&offset=N
  │     │       └─► renderJobs(jobs)  ←─ renders rows
  │     │           ├─ for each row: GET /job/{id}/thumbnail  (done only)
  │     │           └─ for each row without workflow_title: GET /job/{id}/nickname
  │     ├─ wire event handlers
  │     │     ├─ Pause/Resume      ──► POST /pause-all | /resume-all
  │     │     ├─ Filter tab click  ──► reset offset, refetch /list
  │     │     ├─ Up/Down           ──► POST /reorder/{id}
  │     │     ├─ Run               ──► POST /run-now/{id}
  │     │     ├─ ×                 ──► POST /cancel/{id}
  │     │     ├─ ↻ (Repeat)        ──► POST /repeat/{id}
  │     │     ├─ ⬇ (Export)        ──► GET  /job/{id}/export   (browser download)
  │     │     └─ Thumbnail click   ──► in-page modal (no network)
  │     └─ setInterval(refresh, 5000)
  │
  └─ unmount (when activeSidebarTabId moves off us)
        ├─ clearInterval(refresh)
        └─ remove any modal root left behind
```

The scheduler never reads the sidebar; the sidebar reads the scheduler
through `/list` + `/status`. This one-way data dependency keeps the
scheduler testable without a browser.

---

## 9. Cache-reuse prevention (the `control_after_generate` hook)

ComfyUI's native queue re-runs the same prompt object verbatim. Without
intervention, scheduling the same workflow 5 times yields **5 copies of
the first image** — ComfyUI caches by `(model, seed, sampler_params)`,
and `KSampler` defaults to `control_after_generate = "fixed"`, so the
seed never changes.

The plugin solves this in two layers:

1. **UI → API format conversion** (`workflow_format.py`).
   The prompt object the user pastes / Save-As-ed is the *UI* format:
   `{ node_id: { class_type, inputs: { widget_name: <value>, edge: ["node_id", slot] } } }`.
   ComfyUI's `/prompt` endpoint requires the *API* format where edges
   become integer arrays. Conversion also exposes
   `widgets_values[widget_index]` so the seed is reachable.
   Without conversion, the pre-dispatch hook would never see the seed.

2. **`control_after_generate` rewrite** (`scheduler._apply_pre_dispatch_hooks`).
   For each `KSampler` / `KSamplerAdvanced` node the scheduler reads
   the corresponding `widgets_values` slot. If the slot is unset it
   defaults to `randomize`. Then it computes a new seed via the
   `_next_number_value()` helper:

   ```
   fixed     → seed stays
   increment → seed + 1
   decrement → seed - 1
   randomize → random.randint(0, 2**32-1)
   ```

   The new seed is written back to both the API payload and (where
   present) the matching `widgets_values` entry, so any later round-trip
   through the workflow editor sees the bumped seed.

Result: every scheduled re-run produces a different image even though
the user-visible `seed` widget still reads `42`.

---

## 10. Why no in-process prompt hook?

The 0.3 design uses ComfyUI's `/prompt` HTTP endpoint as the **only**
dispatch surface. This means:

- We never need to monkey-patch `PromptQueue` or hijack the prompt
  server's prompt handler.
- A cancelled job may still run inside ComfyUI if it has already been
  accepted by `/prompt`; cancel is a soft-delete in our DB, not an
  interrupt.
- A future release could add a
  `server.PromptServer.add_on_prompt_handler` hook for finer
  cancellation semantics; until then we document the gap
  ([README §Limitations](../README.md#limitations--局限-read-before-filing-a-bug)).

This decision also makes the scheduler independently testable:
`test_scheduler.py` patches `urllib.request.urlopen` and never imports
ComfyUI.

---

## 11. Key code references

| Concern | File | Function |
|---|---|---|
| DB open / migration | `database.py` | `ScheduledQueueDB.__init__` |
| Enqueue a job | `database.py` | `add_job` |
| Pick the next due job | `database.py` | `claim_next_due_job` |
| Move to history | `database.py` | `_finish` |
| Apply pre-dispatch hooks | `scheduler.py` | `_apply_pre_dispatch_hooks` |
| UI → API conversion | `workflow_format.py` | `to_api_format` |
| POST /prompt | `scheduler.py` | `tick` |
| Reconcile with `/history` | `scheduler.py` | `reconcile` |
| Add route | `routes.py` | `add_handler`, `add_batch_handler` |
| Pause/resume | `routes.py` | `pause_all_handler`, `resume_all_handler` |
| List pagination | `routes.py` | `list_handler` |
| Sidebar UI | `web/sidebar_tab.js` | `buildPanel`, `openScheduleDialog` |
| Topbar button | `web/sidebar_tab.js` | `actionBarButtons: [{icon, tooltip, onClick: openScheduleDialog}]` |

---

## 12. Design decisions log

| Decision | Rejected alternative | Why |
|---|---|---|
| SQLite for state | in-memory dict | ComfyUI crashes would lose queued work; user explicitly asked for persistence |
| Daemon thread for scheduler | cron + filesystem | Same-process scheduler can react instantly to pause/resume; no scheduling skew |
| HTTP `/history` for reconciliation | WebSocket subscribe | Simpler to mock; works against older ComfyUI; reconnect logic isn't needed |
| `/prompt` as dispatch surface | monkey-patching `PromptQueue` | Cleaner isolation; survives upstream refactors; independently testable |
| `control_after_generate = randomize` as default | always-randomize | matches ComfyUI's own first-run default; lets users opt into `fixed` when they want determinism |
| UI → API conversion at dispatch time | convert at enqueue time | User can edit the workflow between enqueue and dispatch; we shouldn't freeze the JSON |
| `interrupted` as a first-class state | auto-retry on startup | User might not want silent re-fires after a crash; require explicit resume |
| Whitelist-only `/update` | free-form patch | Prevents a frontend bug from rewriting arbitrary columns |
| Strip `payload` from `/list` | return full row | Workflow JSON can be megabytes; pagination + 60-px thumbnails already cover UI needs |