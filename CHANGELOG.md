# Changelog

All notable changes to **ComfyUI-ScheduledQueue** are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/) and the
project adheres to [Semantic Versioning](https://semver.org/).

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
