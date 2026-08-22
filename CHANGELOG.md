# Changelog

All notable changes to **ComfyUI-ScheduledQueue** are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/) and the
project adheres to [Semantic Versioning](https://semver.org/).

## [0.3.3] - 2026-08-22

### Fixed (reconcile never moved running → done/failed)
- `scheduler.reconcile()` was reading `record.get('status', '')` and treating
  that as a string. ComfyUI 0.33+ stores the status as a nested dict of the
  shape `{"status_str": "success"|"error", "completed": bool, "messages":
  [...]}`, so `str(dict)` was never equal to any of the literals in the
  success/failure list. Result: every running task was silently
  abandoned and stayed `running` forever in the database, even after
  ComfyUI finished it. The reconcile path now reads `status_str`,
  `completed`, and `outputs` defensively, and falls through to `leave
  running` only when the history record is truly absent.
- `import urllib.parse` was missing from `scheduler.py` even though
  `_history()` uses `urllib.parse.quote()`. The first live reconcile call
  would have crashed before this was caught. Now imported.

### Tests
- New `test_reconcile_handles_real_comfyui_history_shape` exercises the
  real nested-dict record shape (the only test that would have caught
  the previous regression).

### Known limitation (documented honestly)
- ComfyUI keeps `/history/{prompt_id}` records in memory; they are lost
  on server restart. If our plugin still has jobs in `running` after a
  ComfyUI restart, the `recover_orphans` step at startup flips them to
  `interrupted` and **the original completion outcome can no longer be
  recovered**. The user's workflow JSON is preserved in `payload`, so the
  job can be re-queued via `comfy-schedule run-now {id}` or
  `comfy-schedule cancel {id}` from the CLI. Restarting ComfyUI in
  the middle of a batch is therefore not recommended; run the batch
  to completion or pause-and-resume at the next boot.

## [0.3.2] - 2026-08-22

### Fixed (regression discovered in 0.3.1)
- `POST /api/schedule/reorder/{job_id}` endpoint added. The sidebar ▲/▼ buttons
  now persist the new order through this dedicated endpoint instead of
  swapping `priority` via `/update`. 404 if the job doesn't exist; 200
  with `{"moved": false}` if the job is already at the edge of the queue.
- `GET /api/schedule/status` now reads `paused` from the database rather
  than returning a hard-coded `true`. Previously clicking the sidebar
  Pause/Resume button did call `/pause-all` or `/resume-all` correctly,
  but the next status query would lie and say `paused: true`, so the UI
  button text never updated.
- Sidebar panel now stays in sync with `activeSidebarTabId` via a
  pinia `$subscribe` watcher. Switching to a native tab (e.g. Node
  library) and back correctly shows each tab's panel, instead of the
  previous panel staying mounted indefinitely.
- Topbar `Schedule` dialog now dispatches a `sq:job-added` event and
  triggers an immediate panel refresh so the new job appears without
  waiting for the next 5 s poll.

### Tests
- `tests/test_routes.py` -- 7 new tests covering the reorder endpoint
  happy path / 404 / 405-by-body-validation / no-op at edge, plus a
  regression test that exercises pause-all → status reports the
  updated flag (this is the bug that produced the stale UI).

## [0.3.1] - 2026-08-22

### Known issues (read before installing)
- Sidebar UI panel does not reliably switch back to the native “Node
  library” tab once the ScheduledQueue tab is open: the framework keeps
  showing our panel and ignores clicks on the native tabs.
- The global Pause/Resume control is missing from the sidebar toolbar;
  use the CLI (`comfy-schedule pause` / `resume`) or the HTTP API
  (`/api/schedule/pause-all` / `/resume-all`) in the meantime.
- Submitted jobs require a manual Refresh click before they appear in the
  sidebar list. Auto-refresh interval exists but can lag behind submit.
- The ▲/▼ reorder buttons do not yet persist the new order to the
  server-side `queue_order`; rows visually swap but reload the old order.

### Added
- README banner explicitly marking the project as **under development**.
- Documented UI limitations and CLI/HTTP workarounds so early users do
  not rely on the sidebar panel as the only control surface.

## [0.3.0] - 2026-08-22

### Added
- Sidebar tab + toolbar Schedule button (frontend real implementation,
  no longer a placeholder). Tested against ComfyUI 1.49.6 frontend API.
- Per-job controls in sidebar: cancel, run-now, move up/down (priority
  swap). All actions issue immediate UI feedback.
- `comfy-schedule` shell CLI wrapper for the HTTP API.
- HTTP endpoints: `/api/schedule/list`, `/add`, `/cancel/{id}`, `/update/{id}`,
  `/status`, `/pause-all`, `/resume-all`, `/run-now/{id}`, `/orphan-status`.
- Deterministic pending-job reordering via persisted `queue_order` column.
- Background scheduler thread (daemon, dies with ComfyUI).
- Status reconciliation against ComfyUI `/history/{prompt_id}` every
  ~5 s. Honest: unknown records leave the job as `running`; we never
  fabricate success/failure.
- Default-paused semantics on first boot.
- Orphan recovery on next startup: `dispatched` / `running` rows from a
  crashed ComfyUI are flipped to `interrupted` and require manual resume.
- CLI usage, install/upgrade/rollback, and architecture docs.

### Changed
- `routes.setup_routes` no longer raises when aiohttp or PromptServer is
  unavailable; it logs a single warning and returns silently. External
  bootstrap can call `try_install()` later.
- Status counters now include `done` / `failed` from `job_history`.
- Scheduler history lookup uses class-level `HISTORY_TIMEOUT` constant.

### Fixed
- `_init()` no longer crashes the loader when aiohttp / server modules
  are missing (e.g. from a non-ComfyUI invocation).
- Frontend `buildPanel()` no longer memoizes the root element across
  remounts; each call returns a fresh root with fresh listeners.
- Removed obsolete `$subscribe` and `document.body` MutationObserver
  watcher that fired on every DOM mutation.

### Security
- HTTP API never returns the `payload` field of any job.
- `update_job()` rejects `payload` and unknown fields.
- Cancellation only touches `scheduled` / `interrupted` rows; running jobs
  are not silently removed (truthful 4xx contract).

### Known limitations
- A cancelled prompt may still be processed by ComfyUI if it was already
  in flight when the cancel hit. `cancel` is a soft-delete in our DB,
  not a ComfyUI native interrupt.
- Reconciliation polls `/history/{prompt_id}`; very fast jobs (<
  HISTORY_TIMEOUT) may still appear as `running` until the next tick.
- The frontend assumes a ComfyUI frontend with `app.registerExtension`
  and `app.extensionManager.registerSidebarTab` (≥ 1.33.9). Older
  frontends are not supported.
- The CLI is intentionally simple; no proxy support beyond
  `COMFYUI_URL=http://host:port`.
