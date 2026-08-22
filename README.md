# ComfyUI-ScheduledQueue

**Status: under development.** 本插件仍在快速迭代中。最近修复(0.3.5):
- **新增 UI format 支持**:之前只处理 API format,从编辑器复制的 UI
  format workflow 会导致 cache 命中(只出 1 张图)。现 scheduler.py 在
  dispatch 时自动 convert UI → API,让 control_after_generate hook 真
  正能...[truncated>

CLI (`comfy-schedule`) 与 HTTP API 一直工作正常。Web 端已修到接近可用,
但仍可能有边缘问题。生产环境请先用 CLI/curl。仓库已开放,欢迎试用
+ 反馈,但请勿将其作为唯一队列管理路径。

---

持久化 + 暂停 + 重排序 + 任意时间定时投递的 ComfyUI 队列扩展。

> **Beta:** read [Compatibility](#compatibility) before installing. The
> bundled frontend assumes ComfyUI ≥ 1.49.6 with `app.registerExtension` and
> `app.extensionManager.registerSidebarTab`.

## Features

- **Persist everything** — every queued job lives in a SQLite database
  (`<ComfyUI>/user/scheduled_queue.sqlite3`). ComfyUI crashes and reboots
  never lose queued work.
- **Pause / resume** — stop dispatching new work in one HTTP call. ComfyUI
  prompts already in flight are not interrupted (we cannot reach across
  the boundary).
- **Reorder** — pending jobs carry a `queue_order`; the sidebar lets you
  swap priorities up / down with immediate persistence.
- **Schedule any time** — `--in 10m`, `--in 2h`, or an ISO timestamp. A
  toolbar **Schedule** button submits the *current* workflow to the
  ScheduledQueue API (ComfyUI's native Run button is untouched).
- **Run-now** — bypass the timer and dispatch a specific job immediately.
- **Cancel** — soft-delete pending rows; running rows are *not* silently
  removed (truthful 4xx contract — see Limitations).
- **Orphan recovery** — on the next start, every `dispatched` / `running`
  job from the previous ComfyUI run is flipped to `interrupted` and
  waits for an explicit resume.
- **Honest reconciliation** — every ~5 s the scheduler asks ComfyUI
  `/history/{prompt_id}` whether each running job is `success` or `error`
  and moves it to `job_history`. Jobs whose status is unknown stay
  `running` — we never fabricate success.

## Compatibility

| ComfyUI | Frontend | Status |
|---|---|---|
| 0.33.0 + 1.49.6 frontend | 1.49.6 | ✅ supported (this release was developed here) |
| 0.33.0 + 1.48.x frontend | < 1.33.9 | ❌ frontend sidebar not registered |
| master (≥ v0.34) | ≥ 1.49.x | likely OK; run the test suite to verify |

The backend requires Python ≥ 3.10. The frontend requires the new
PrimeVue + pinia layout. There are no third-party Python dependencies
(the scheduler uses `urllib.request` from the standard library).

## Installation

## Install

> Paths below use placeholders. Set them before running:
>
> | placeholder | meaning |
> |---|---|
> | `$COMFYUI` | Your ComfyUI checkout directory (the parent of `custom_nodes/` and `user/`) |
>
> Common values: `/opt/ComfyUI`, `/srv/ComfyUI`, `~/projects/ComfyUI`. The
> plugin makes **no assumptions** about where ComfyUI lives — only that
> `custom_nodes/` and `user/` exist under that directory.

```bash
# 1) clone
git clone https://github.com/27-exe/ComfyUI-ScheduledQueue
cd ComfyUI-ScheduledQueue

# 2) copy / symlink into ComfyUI's custom_nodes directory
ln -s "$(pwd)/src/comfyui_scheduled_queue" \
       "$COMFYUI/custom_nodes/ComfyUI-ScheduledQueue"

# 3) (optional) install the CLI for shell scripting
ln -s "$(pwd)/scripts/comfy-schedule" "$HOME/.local/bin/comfy-schedule"

# 4) start ComfyUI; you should see:
#    [ScheduledQueue] Stage 3 initialised. db=...
```

After the first start, the database is at
`$COMFYUI/user/scheduled_queue.sqlite3`. Default state is **paused**.
Call `comfy-schedule resume` to start dispatching.

## Upgrade

```bash
git pull
# restart ComfyUI
```

`add_job` payload schema is stable across 0.1 → 0.3. Old rows survive.

## Rollback

```bash
rm "$COMFYUI/custom_nodes/ComfyUI-ScheduledQueue"
# the SQLite file is left in place; delete only if you want a clean slate
```

The plugin is **completely self-contained**. Removing the directory does
not affect any other custom_node or ComfyUI state.

## Usage

### HTTP API

```bash
# add a job
curl -X POST http://127.0.0.1:8188/api/schedule/add \
  -H "Content-Type: application/json" \
  -d '{
        "payload": {"3": {"class_type": "KSampler", "inputs": {"seed": 42}}},
        "scheduled_at": 1755840000.0,
        "note": "morning batch"
      }'

curl -X POST http://127.0.0.1:8188/api/schedule/pause-all
curl -X POST http://127.0.0.1:8188/api/schedule/resume-all
curl -X POST http://127.0.0.1:8188/api/schedule/cancel/<id>
curl -X POST http://127.0.0.1:8188/api/schedule/run-now/<id>
curl http://127.0.0.1:8188/api/schedule/status
curl http://127.0.0.1:8188/api/schedule/orphan-status
curl http://127.0.0.1:8188/api/schedule/list
```

### CLI (`comfy-schedule`)

```bash
comfy-schedule status
comfy-schedule list --ids-only
comfy-schedule add workflow.json --in 10m --note "morning"
comfy-schedule add -              --in 1h --priority 200      # stdin
comfy-schedule pause
comfy-schedule resume
comfy-schedule orphans
comfy-schedule cancel <job_id>
comfy-schedule update <job_id> --in 2h --priority 50
comfy-schedule run-now <job_id>
comfy-schedule watch --interval 2
```

`COMFYUI_URL` overrides the default `http://127.0.0.1:8188`.

### In the UI

1. **Schedule button** — left of *Run* in the top bar. Opens a modal with
   preset offsets (30 s / 5 min / 30 min / 2 h / tomorrow 9 am) plus a
   priority and note field. Click *Schedule* → job is queued.
2. **Sidebar tab "Scheduled Queue"** — shows pending + running jobs with
   per-row Cancel / Run / ↑ / ↓ buttons, plus global Refresh and Pause /
   Resume. Auto-refreshes every 5 s. Toggling tabs preserves the panel;
   no leftover DOM, no double panels.
3. **Native Run is untouched** — clicking *Queue Prompt* in ComfyUI
   still dispatches through ComfyUI's native queue.

## State machine

```
                    add                dispatch (tick)
   nothing ─────────────► scheduled ─────────────► dispatched ─────────► running
                              │                          │                    │
                              │ cancel                   │ reconcile: success  │
                              ▼                          ▼                    │
                          cancelled                  running (kept)           ▼
                                                         │                done (history)
                              resume                    │ reconcile: error
                              │                         ▼
                              ▼                     failed (history)
                          scheduled (retry)

   On startup, every dispatched/running row becomes interrupted, then waits.
```

## Project layout

```
ComfyUI-ScheduledQueue/
├── src/comfyui_scheduled_queue/
│   ├── __init__.py            # entry point (try_install, deferred wiring)
│   ├── database.py            # SQLite + state machine
│   ├── routes.py              # aiohttp handlers
│   ├── scheduler.py           # background dispatcher thread
│   ├── prompt_interceptor.py  # placeholder for future native-prompt hook
│   └── web/sidebar_tab.js     # frontend toolbar + sidebar tab
├── scripts/
│   └── comfy-schedule         # Python CLI wrapper
├── tests/
│   ├── test_database.py       # state machine, ordering, cancellation
│   ├── test_scheduler.py      # mock ComfyUI dispatch + reconcile
│   └── test_cli.py            # CLI subprocess through a fake server
├── docs/                      # historical design specs
├── pyproject.toml
├── README.md
├── CHANGELOG.md
└── LICENSE
```

## Running the test suite

```bash
python -m unittest discover tests -v
```

The tests are stdlib-only (unittest + http.server + tempfile). They do
*not* require aiohttp or a running ComfyUI.

## Security

- **No payload over the wire from `/list`** — list endpoints strip
  `payload` so workflow JSON is never leaked back.
- **Whitelist-only updates** — `/update` rejects any field outside
  `{scheduled_at, priority, note, auto_retry}`.
- **Cancel only touches pending rows** — running jobs are not silently
  removed; a 404 / 200 contract is preserved.
- **No shell hooks** — the scheduler uses `urllib.request`, not `subprocess`.

## Limitations (read this before filing a bug)

- **Cancel ≠ ComfyUI native interrupt.** A row in `scheduled_jobs` with
  status `running` cannot be recalled once ComfyUI's worker thread has
  started processing it. We mark our copy as `cancelled` but ComfyUI
  will still finish the actual prompt. To abort a running ComfyUI job
  you must use ComfyUI's own `/interrupt` endpoint — ScheduledQueue
  doesn't proxy that.
- **Reconciliation latency** is bounded by `RECONCILE_INTERVAL = 5 s` and
  `HISTORY_TIMEOUT = 4 s`. A 1 s job may stay `running` for ~9 s before
  being marked `done`.
- **No WebSocket subscription.** The reconciliation loop is HTTP only;
  it survives ComfyUI restarts but adds modest polling load. A future
  release may switch to ComfyUI's `status` message stream.
- **Frontend assumes v1.49.6+.** The legacy queue menu
  (`[DEPRECATED] The legacy queue/history menu`) is not addressed by
  this extension.
- **Pre-flight order matters.** ComfyUI's custom_node loader imports
  our `__init__.py` BEFORE `PromptServer` is ready. Our entry point
  defers all ComfyUI / aiohttp work to `try_install()` and is safe
  to call multiple times.
