# Changelog

All notable changes to **ComfyUI-ScheduledQueue** are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/) and the
project adheres to [Semantic Versioning](https://semver.org/).

## [0.3.11] - 2026-08-23

Patch release on top of 0.3.10: bug fixes reported within hours of the
push, sidebar UX polish, and a small scheduler default change. No schema
changes; in-place upgrade from 0.3.10 is safe.

### Fixed (live bugs reported after 0.3.10)

- **Sidebar status bar showed only relative time.** Done / failed rows now
  also render an absolute timestamp (`2026-08-23 22:31:46`) and a duration
  suffix, matching the dialog's display style.
- **`workflow_title` could be silently overwritten by the async nickname
  hydrate path.** The sidebar now refuses to replace the title text when a
  `workflow_title` is already set; the legacy `_meta.title` lookup still
  runs only for rows without one.
- **`list_jobs_paginated` returned zero history rows when the sidebar
  queried with an empty status filter.** Now merges `scheduled_jobs` and
  `job_history` correctly; pagination cursor still works on the union.
- **Legacy `job_history` rows missing `dispatched_at` were rendered with
  `--` and looked broken.** Synthesised `dispatched_at = finished_at` for
  any history row that doesn't have one (one-shot backfill at query time).
- **`outputs` field came back as a string instead of a dict** for the
  history union. `list_jobs_paginated` now `json.loads` it before returning.
- **Thumbnail preview was empty when outputs were nested.**
  `get_job_with_outputs` now descends into `outputs[*][*].images[*]` (and
  walks similar shapes) until it finds the first image entry.
- **Schedule dialog accepted times earlier than now + 5 s.** The submit
  handler now clamps `scheduled_at` to at least `now + 5` so the scheduler
  has a chance to pick it up.
- **Pre-dispatch `control_after_generate` hook defaulted to `fixed`**
  when the widget slot was absent. Now defaults to `randomize`, matching
  ComfyUI's own first-run behaviour and preventing the silent cache-hit
  bug documented in `docs/USER_GUIDE.md §7`.

### Changed

- **Schedule dialog two-row time controls.** Decrement buttons sit above
  increment buttons; both columns are right-aligned so the eye reads a
  single vertical column on the right. Decrement flashes the input
  border blue; increment flashes green. Dialog width increased from 420 px
  to 480 px.
- **Adaptive scheduler tick cadence.** Tick interval collapses to ~1 s
  right after a dispatch so the next reconcile loop catches the just-POSTed
  prompt quickly. Stretches back to the normal interval when no jobs are
  `running`. Same overall cost; lower wall-clock latency for short jobs.
- **Widget lifecycle preservation in UI → API conversion.** Removed
  `widgets_values` fields are no longer dropped on the floor when the
  matching `inputs[name]` is absent in the source graph; they're passed
  through to the API payload so ComfyUI's downstream code can still see
  them.
- **Workflow title fallback chain.** When `workflow_title` is empty *and*
  the async nickname hydrate fails, the sidebar falls back to the first
  non-empty `_meta.title` or `class_type` instead of `untitled`. Preferable
  to literally the word `untitled` in mixed-language installations.

### UX

- **Sidebar `[Print version]` watermark** in DevTools console (`SQ_VERSION`
  log line) so users reporting bugs can copy-paste the exact build number.
- **Per-click `console.log` for action buttons** (Pause/Resume, Run,
  Cancel, Repeat, Export, Up/Down, Clear) so users can self-diagnose
  button-event issues without a debugger.

### Tests

- 4 new scheduler tests (`TestControlAfterGenerateFallback`,
  `TestScheduleClamp`).
- 2 new database tests (`TestHistoryUnion`, `TestOutputsDecoded`).
- 1 new routes test (`TestThumbnailDeepWalk`).
- Full suite: **140 / 140 pass** (was 133).

### Known limitation

- `workflow_title` will read `"Unsaved Workflow"` whenever the user
  submits a Schedule before ComfyUI's "Save As…" dialog has given the
  workflow a real filename. That matches ComfyUI's own display and is
  documented in `docs/USER_GUIDE.md §2.2 ①`.

## [0.3.10] - 2026-08-23

### Added (workflow title tracking)

- **Database**: new `workflow_title TEXT` column on both `scheduled_jobs`
  and `job_history` (idempotent ALTER for live upgrades from 0.3.9).
- **Database**: `add_job`, `update_job`, `_finish` (`mark_done` /
  `mark_failed`), and `repeat_job` accept / propagate the field. Empty
  string is normalised to NULL.
- **Routes**: `add`, `add-batch`, `update` accept `workflow_title` (string
  or null; other types → 400). `list_jobs` and `get_job_with_outputs`
  return the field for sidebar rendering.
- **Schedule dialog**: on submit, reads the active workflow title from
  ComfyUI's Pinia store via `app.extensionManager.workflow.activeWorkflow`
  (fields tried: `filename` → `fullFilename`) and forwards it to the
  POST body so the server can store it next to the payload.
- **Sidebar job row title**: now picks `workflow_title` first, then
  `note`, then `"untitled"`. Old nickname path (async hydrated from
  `getNodeTitle`) still runs but only overwrites if `workflow_title` is
  empty.
- **Sidebar header note**: small annotation pointing the workflow title
  source at ComfyUI's `app.graph.activeWorkflow.filename` for users
  reading the source.

### Tests
- 14 new database tests (`TestWorkflowTitle`).
- 15 new routes tests (add / add-batch / update / list / get_job /
  repeat).
- Full suite: **133 / 133 pass** (was 104).

### Known limitation
- `workflow_title` will read `"Unsaved Workflow"` whenever the user
  submits a Schedule before ComfyUI's "Save As…" dialog has given the
  workflow a real filename. That matches ComfyUI's own display.

## [0.3.9] - 2026-08-23

### Fixed (live bugs reported after 0.3.8 push)

- **Clear panel stopped auto-collapsing on every 5 s refresh.** Previously
  `refresh()` reset `clearPanelEl.style.display = "none"` on every poll,
  so the moment the user opened the checkbox list a background tick
  closed it again. The toggle button is now the only place that mutates
  the panel's display state.

- **Sidebar job title resolves to the workflow nickname, not the first
  arbitrary node title.** The lookup now prefers `SaveImage`,
  `PreviewImage`, then `VAEDecode` — the nodes a user mentally labels a
  workflow by — and only falls back to the first `_meta.title` or
  `class_type` if none of those exist. Job title + 8-char id is now
  shown above the status badge.

- **Schedule dialog time editor buttons had no click handlers.** The
  `data-delta="-3600"` etc. buttons rendered but did nothing because no
  listener was attached. All ten +/- buttons now wire through a single
  `currentWhenTs += parseInt(delta)` + `refreshWhenDisplay()` flow, and
  the editable centre input now listens to `input` events to parse
  `YYYY-MM-DD HH:MM:SS` / ISO / `YYYY/MM/DD HH:MM:SS` and writes the
  canonical unix-seconds into the hidden input.

### Known limitation (unchanged from 0.3.8)
- The sidebar still rebuilds its `jobsEl` innerHTML on every 5 s poll,
  which can visibly flicker on slow sessions. The fix would require
  row-level diffing; deferred to a follow-up since the underlying data
  is always up to date.

## [0.3.8] - 2026-08-23

### Added (new REST endpoints + UI controls)

#### Backend
- `POST /api/schedule/add-batch` — queue up to 50 jobs in one request;
  per-item failures are isolated and the successful ones are returned in
  `added[]`.
- `GET /api/schedule/job/{job_id}` — single-job detail that returns
  decoded `payload` and `outputs` (no JSON string leakage).
- `DELETE /api/schedule/clear?statuses=...` — bulk delete from
  `scheduled_jobs` and `job_history` by status list. Default =
  `done,failed,cancelled`.
- `POST /api/schedule/repeat/{job_id}` — clone the payload of a finished
  job back into `scheduled_jobs` with a new uuid and immediate dispatch.
- `GET /api/schedule/export/{job_id}` — download the job's payload as
  a JSON attachment (`Content-Disposition: attachment; filename="..."`).
- `GET /api/schedule/list` extended with `?status=a,b,c`, `?limit=N`,
  `?offset=N` and now returns `{jobs, total, limit, offset, has_more}`.

#### Database
- `list_jobs_paginated(statuses, limit, offset)` — paginated query.
- `count_jobs(statuses)` — total count for pagination.
- `get_job_with_outputs(job_id)` — merged live + history view, both
  `payload` and `outputs` decoded to Python objects.
- `clear_by_status(statuses)` — bulk delete from both tables.
- `repeat_job(job_id)` — clone payload from history into a new
  `scheduled_jobs` row.

#### Scheduler
- `claim_next_due_job` ORDER BY now treats priority as the dominant
  tie-breaker after `queue_order`, ahead of `scheduled_at` —
  previously a high-priority task that was scheduled slightly later
  could be skipped over by a lower-priority earlier task.

#### Frontend (sidebar panel)
- Status filter tabs (`All` / `Scheduled` / `Running` / `Done` /
  `Failed` / `Cancelled`) with live counts.
- A `Clear...` button that expands a status-checklist panel with
  live counts and a `Clear selected` action.
- Prev / Next pager, driven by the new `total / limit / has_more`.
- Per-job `Repeat` (`↻`) and `Export` (`⬇`) buttons; Repeat hits the
  POST endpoint, Export triggers a browser download.
- Job title now resolves to the workflow nickname (`_meta.title` from
  the KSampler / SaveImage / … node) with a short id suffix.
- 60x60 thumbnails for done jobs; click opens a fullscreen modal that
  closes on click or Esc.

#### Frontend (Schedule dialog)
- Count input (1–50) routes to either `/add` (single) or `/add-batch`
  (multiple) depending on the entered value.
- Three-section time editor (left = subtract presets, centre =
  editable `YYYY-MM-DD HH:MM:SS / ISO / slash` field, right = add
  presets) keeps the unix-seconds hidden input as the submission
  source of truth.

#### Helpers
- `workflow_format.get_node_title(api_dict, node_id)` for the sidebar
  nickname lookup.

### Tests
- 20 new unit tests covering all of the above. Full suite:
  **104 / 104 pass** (was 84).

### Known limitations (unchanged from 0.3.7)
- `cancel` does not interrupt an in-flight ComfyUI prompt.
- `reconcile` runs every 5 s; sub-second completions may briefly appear
  as `running` before flipping to `done`.
- Browser cache `execution_cached` reuse is per-ComfyUI; the scheduler
  still cannot force a cache miss on its own — must rely on seed /
  control_after_generate differentiation.
- ComfyUI `/history/{prompt_id}` is in-memory; restarts wipe history.
- `extra_pnginfo[0]` warning persists for some workflows that read
  extra_pnginfo in a custom shape — out of scope for this plugin.

## [0.3.7] - 2026-08-22

### Fixed (Schedule did not run native widget queue callbacks)
- Schedule submission now follows ComfyUI's native queue lifecycle:
  `widget.beforeQueued()` -> `graphToPrompt()` -> POST `/api/schedule/add`
  -> `widget.afterQueued()`.
- The fix preserves each widget's real
  `control_after_generate` mode (`fixed`, `randomize`, `increment`,
  `decrement`) instead of forcing every seed node to `randomize`.
- Schedule now forwards `graph.workflow` through
  `extra_data.extra_pnginfo.workflow` for nodes that consume workflow
  metadata.
- Widget callback failures are isolated and logged with `console.warn`.

### Verified
- Two consecutive scheduled submissions used different seeds:
  `1006994118007054` and `760400493873003`.
- Both submissions performed full sampling (`75.41s` and `48.22s`),
  instead of the previous `0.03s`/`0.05s` cache-hit path.

### Known issue
- ComfyUI still logs `extra_pnginfo[0] is not a dict or missing
  'workflow' key` for this workflow. The scheduler request now includes
  `extra_data.extra_pnginfo.workflow`, but the affected node or its
  expected metadata shape needs a separate compatibility investigation.

## [0.3.6] - 2026-08-22

### Fixed (plugin silently failed to load in ComfyUI's importlib context)

Two follow-on bugs revealed once the dispatch path was wired together
end-to-end against a real running ComfyUI 0.33.0 (frontend 1.49.6):

- **scheduler.py raised `ModuleNotFoundError: No module named
  'ComfyUI-ScheduledQueue'` at module load.** ComfyUI loads custom
  nodes via `importlib.util.spec_from_file_location(..., name="...
  -ScheduledQueue.scheduler", ...)`. A plain
  `from .workflow_format import ...` then asks Python to look up the
  parent package `ComfyUI-ScheduledQueue` plus child
  `workflow_format` in `sys.modules` — but only `scheduler` itself
  was registered there, so the relative import failed. `__init__`'s
  `_load_sibling` swallowed the error and `setup_routes` was never
  called, so every `/api/schedule/*` route returned 404/405 plain-text
  responses (and the dialog submit surfaced as `Network error:
  Unexpected non-whitespace character after JSON at position 3`).

  Fix: scheduler.py now mirrors `__init__._load_sibling` and uses
  `importlib.util.spec_from_file_location` to self-load
  `workflow_format.py` into `sys.modules` under the expected dotted
  name before binding the symbols. Works both in production (where
  ComfyUI drives the loader) and in the unit-test context (where
  `sys.path.insert(0, 'src')` resolves them via the package).

- **`convert_ui_to_api` could silently drop the
  `control_after_generate` sentinel** when the schema-driven
  positional-fallback path resolved the value to whatever schema
  key sat at that index (`steps`, `cfg`, etc.). `widgets_values_named`
  paths were already correct, but any workflow that relied on the
  fallback could land in a state where the hook could not see the
  sentinel and thus could not randomise `seed`. Added a defensive
  coercion that writes `inputs["control_after_generate"]` whenever a
  widget value matches `{fixed, randomize, increment, decrement,
  increment-wrap}`.

### Tests

- New `test_user_workflow_hook_yields_large_random_seed_and_strips_cag`
  feeds the user's real 83-node workflow through the hook and asserts
  KSampler 28's seed becomes a 64-bit integer different from the
  saved `458839675645881`, with `control_after_generate` removed from
  inputs.
- New `test_two_hook_runs_yield_different_seeds` runs the hook twice
  on independent copies and asserts the seeds differ — guards against
  any future regression where the hook returns early.
- Full suite: **84 / 84 pass** (was 82).

## [0.3.5] - 2026-08-22

### Fixed (plugin ignored UI-format workflows → cache hit on every dispatch)
- The plugin was passing the user's `payload` straight to ComfyUI.
  Users who copy-paste a workflow saved from the editor ship the
  **UI format** (`{"nodes": [...], "links": [...], "widgets_values":
  [...]}...`), not the **API format** ComfyUI's `/prompt` endpoint
  accepts. The previous `_apply_pre_dispatch_hooks` walked the API-format
  tree only and therefore never found `seed` / `noise_seed` to randomise,
  so every dispatch kept the same seed, triggered `execution_cached`,
  and produced the same image.
- New module `src/comfyui_scheduled_queue/workflow_format.py`
  (~285 LOC, 4 public helpers + 4 private helpers) implements a minimal
  UI → API converter. Dispatch-time hook now:
  ```python
  if not is_api_format(prompt):
      prompt = convert_ui_to_api(prompt)
  ```
  Strategy:
  1. `widgets_values_named` (modern frontend, key=value per widget name).
  2. Fallback: `comfy.nodes.NODE_CLASS_MAPPINGS[type].INPUT_TYPES()`
     schema with default values.
  3. Links first (already in API format: `[node_id, output_index]`).
- Verified end-to-end against the user's real workflow at
  `/home/a27exe/Downloads/SD工作流 无强化.json` (UI format, 83 nodes):
  post-conversion, KSampler / UltimateSDUpscale / FaceDetailer nodes
  all expose `inputs.seed` AND `inputs.control_after_generate` for the
  existing seed-randomisation hook to mutate.

### Tests
- New `tests/test_workflow_format.py` (~416 LOC, 26 tests):
  is_api_format detection, UI→API roundtrip on the real user fixture,
  widgets_values_named path, INPUT_TYPES fallback path, link priority,
  API-format passthrough, dangling link handling, empty widgets_values.
- Full suite: **82 / 82 pass** (was 56, + 26 new).

### Known limitations
- Empty `widgets_values` + missing INPUT_TYPES schema emits a warning
  and drops the widget's value. ComfyUI then complains at execution
  about a missing input; users see a clear server-side error.
- Dangling links (referencing a non-existent source node) are
  silently dropped with a warning. ComfyUI's own execution report
  will flag the missing dependency.
- Link-vs-widget duplicates: link value wins, matching the frontend's
  `graphToPrompt` behaviour.
- The converter does not currently auto-normalise `widgets_values_named`
  when both are present; modern frontend is the default.

## [0.3.4] - 2026-08-22

### Fixed (scheduler ignored frontend `control_after_generate`)
- `scheduler.tick()` was POSTing the user-supplied `payload` to
  ComfyUI verbatim. ComfyUI's frontend treats `inputs.control_after_generate`
  as a frontend-only directive (see `settingStore-CwkLtSKP.js ->
  applyWidgetControl -> computeNextNumberValue`); it is consumed before
  the prompt is serialised and never seen by the server. Our plugin
  sat on the server side, so the seed-equivalent fields (`seed`,
  `noise_seed`) were never advanced — three consecutive dispatches of
  the same workflow all used the same seed and ComfyUI's
  `execution_cached` reused the first run's outputs.

  Scheduler now applies the same transformation the frontend does,
  before POSTing:
    - mode `randomize`  -> uniform random in [0, 2**64 - 1]
    - mode `increment`  -> +1 (with overflow clamping to the JS safe-int range)
    - mode `decrement`  -> -1 (with floor at 0)
    - mode `fixed`      -> no-op
  The directive is stripped from the payload before POST so the request
  ComfyUI sees is byte-identical to one a user clicked through the UI.

### Tests
- 20 new tests in `tests/test_scheduler.py` covering every mode, both
  seed fields, the strip-the-directive behaviour, and a `test_tick_*`
  regression that asserts two consecutive real `tick()` calls each
  apply the increment once.

### Known limitation
- The reimplementation handles only `seed` / `noise_seed`. COMBO-type
  widgets with `control_after_generate` are not transformed because the
  plugin does not load the node spec table. None of the shipped builtin
  nodes use COMBO with `control_after_generate` today, but third-party
  custom nodes might.

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
