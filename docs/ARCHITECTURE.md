# Architecture

## Process model

```
                    ComfyUI main process (PID)
                    ├─ aiohttp event loop           ← /api/schedule/* routes
                    ├─ prompt executor workers
                    └─ ScheduledQueue daemon thread
                       ├─ tick() every 1.0 s
                       │   └─ read paused state
                       │   └─ claim_next_due_job()
                       │   └─ POST /prompt to ComfyUI (urllib)
                       └─ reconcile() every 5.0 s
                           └─ GET /history/{prompt_id} for each running job
                           └─ mark_done / mark_failed via _finish() → job_history
```

The scheduler thread is `daemon=True`, so it dies with ComfyUI. We
register `atexit(scheduler_thread.stop)` as a belt-and-braces cleanup,
but the daemon flag is the actual safety guarantee.

## Why HTTP for /history and not WebSocket?

`/history/{prompt_id}` is the public, documented ComfyUI API for
"did this prompt finish and what was the result". WebSocket status
messages are also published by ComfyUI's event server but require
long-lived client connections; for a one-second poll cadence the HTTP
endpoint is simpler and easier to mock in tests.

If ComfyUI's WebSocket proves unreliable in practice, we can switch
later without changing the database schema.

## Frontend mount lifecycle

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
element from the DOM. We *observe* that removal with a tiny
`MutationObserver` on the *immediate parent* (cheap) and use it to
clean up our `setInterval`. We never touch `document.body` — earlier
versions did, and we removed it because the cost scales with every
DOM mutation across the whole page.

## Data model

```
scheduled_jobs:
  id TEXT PK, payload TEXT, scheduled_at REAL,
  status TEXT, queue_order INTEGER, priority INTEGER, ...
job_history:
  id TEXT PK, prompt_id TEXT, finished_at REAL, status TEXT,
  outputs JSON, error TEXT
scheduler_state:
  key TEXT PK, value TEXT
```

`claim_next_due_job` is the only writer for status `dispatched`. Every
other status transition goes through `update_job` so the audit trail
remains consistent.

## When does a job move to `job_history`?

Only after `reconcile()` has positive evidence from ComfyUI's
`/history/{prompt_id}`: a record with `status: success` or
`status: error`. We do not move rows on timeouts or HTTP failures
during reconciliation — those rows stay `running` and the next
reconcile tick will try again.

## Why no in-process prompt hook?

The 0.3 design uses ComfyUI's `/prompt` HTTP endpoint as the
**only** dispatch surface. This means:

- We never need to monkey-patch `PromptQueue` or hijack the
  prompt server's prompt handler.
- A cancelled job may still run inside ComfyUI if it has already been
  accepted by `/prompt`; cancel is a soft-delete in our DB, not an
  interrupt.
- A future release could add a `server.PromptServer.add_on_prompt_handler`
  hook for finer cancellation semantics; until then we document the
  gap.
