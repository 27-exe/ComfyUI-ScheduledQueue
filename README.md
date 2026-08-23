# ComfyUI-ScheduledQueue

**Status:** under development (v0.3.10). CLI and HTTP API are production-ready; the bundled sidebar UI is stable against ComfyUI ≥ 1.49.6.

---

## 项目简介 / Overview

**持久化 + 暂停 + 重排序 + 任意时间定时投递的 ComfyUI 队列扩展。**
A ComfyUI queue extension that **persists every job to SQLite, supports pause/resume, drag-free reorder, and arbitrary-time delivery** — without disturbing ComfyUI's native Run button.

提交一个 workflow 不再等于"立刻占用 GPU"。把今晚 23:00 的批渲染、明天 9:00 的风格实验、一周后才会用到的种子复跑都加入队列，关掉浏览器，明天打开 ComfyUI，队列已经在跑了。

> **Beta / 公测提示:** 请先阅读 [Compatibility](#compatibility)。The bundled frontend assumes ComfyUI ≥ 1.49.6 with `app.registerExtension` and `app.extensionManager.registerSidebarTab`.

---

## Sidebar UI (主界面)

```
┌──────────────────────────────────────────────────────────────────────────┐
│ Scheduled Queue                                                          │
│ Managed by ScheduledQueue (not ComfyUI native queue).                    │
│ Workflow title = current app.extensionManager.workflow.activeWorkflow    │
│ .filename from ComfyUI Pinia store.                                      │
│                                                                          │
│ [All*] [Scheduled ] [Running ] [Done ] [Failed ] [Cancelled ]  Clear... │  ← ① status filter tabs
│ ┌────────────────────────────────────────────────────────────────────┐   │
│ │ [ ] Done (12)                                                      │   │  ② clear panel (collapsed by default;
│ │ [ ] Failed (3)                                                     │   │    Clear... button toggles it)
│ │ [ ] Cancelled (5)                                                  │   │
│ │ [ ] Running                                                        │   │
│ │ [ ] Scheduled                                                      │   │
│ │ [ ] Interrupted                                                    │   │
│ │             [ Clear selected ]                                     │   │
│ └────────────────────────────────────────────────────────────────────┘   │
│                                                                          │
│ [ Refresh ]   [ Pause ]                                                  │  ← ③ pause/resume button
│                                                                          │
│ ┌────────────────────────────────────────────────────────────────────┐   │
│ │ paused · scheduled=0  running=0  done=0  failed=0  cancelled=0    │   │  ④ live status bar
│ └────────────────────────────────────────────────────────────────────┘   │
│                                                                          │
│ ┌────────────────────────────────────────────────────────────────────┐   │
│ │ My morning batch (a1b2c3d4)                         [↑] [↓] [Run] │   │
│ │ [scheduled] in 9h 14m                                             │   │  ⑤ per-job row
│ │ scheduled 2026-08-23 22:30:00                                     │   │
│ │                                                  [↻ Repeat] [⬇]   │   │  ⑥ repeat / export buttons
│ ├────────────────────────────────────────────────────────────────────┤   │
│ │ Style variant 3 (9f8e7d6c)                       [× Cancel]      │   │
│ │ [running] running                                                  │   │
│ │ started 22:31:05 · 12.4s                                           │   │
│ ├────────────────────────────────────────────────────────────────────┤   │
│ │ Seed sweep pass 2 (5a4b3c2d)                       [↻ Repeat] [⬇] │   │
│ │ [done] done yesterday · 41.0s                                      │   │
│ │ [thumb 60x60] ← click to zoom                                      │   │  ⑦ 60x60 thumbnail for done jobs
│ └────────────────────────────────────────────────────────────────────┘   │
│                                                                          │
│ [ ‹ Prev ]  Page 1 (1–3 of 17)  [ Next › ]                              │  ← ⑧ pagination
│                                                                          │
│ Use the clock icon in the topbar to add a new scheduled task.           │
└──────────────────────────────────────────────────────────────────────────┘
```

**Sidebar UI element map:**

| # | Element | Code reference |
|---|---------|----------------|
| ① | Status filter tabs (All / Scheduled / Running / Done / Failed / Cancelled) | `data-filter` row, `sidebar_tab.js` L57–L63 |
| ② | Clear panel — checkbox list + per-status live counts | `[data-role="clear-panel"]` L66–L73 |
| ③ | Pause/Resume button (toggles text + green/grey background) | `[data-act="pause-resume"]` L78, handler L706–L716 |
| ④ | Live status bar (paused flag + per-status counts) | `[data-role="status"]` L81 |
| ⑤ | Job row: `workflow_title` (+ short id suffix), status badge, time | L523–L549 |
| ⑥ | Repeat / Export per-row buttons | `↻` POST `/repeat/{id}`, `⬇` GET `/export/{id}` |
| ⑦ | 60×60 thumbnail (done jobs only); click → fullscreen modal | L570–L582, modal L575–L577 |
| ⑧ | Pagination (Prev / Page info / Next) | `[data-role="pager"]` L86–L89 |

Topbar **Schedule** button (clock icon, left of Run) opens a modal for adding new tasks — see [docs/USER_GUIDE.md §3](docs/USER_GUIDE.md#3-添加单个任务--schedule-对话框).

---

## 主要功能 / Features

| 痛点 (Pain point) | 解决 (Solution) | Where |
|---|---|---|
| 想让 prompt 在 GPU 空闲时才跑, 又不想半夜开电脑 | **任务调度 (时间窗)** — 任意 ISO 或相对时间 (`--in 10m`, `--in 2h`, `tomorrow 9 am`) | `scheduler.tick()` → `claim_next_due_job()` |
| ComfyUI 崩溃 / 重启会丢失内存中的队列 | **持久化** — 每个任务都进 `<ComfyUI>/user/scheduled_queue.sqlite3` (WAL 模式) | `database.py` `ScheduledQueueDB` |
| 想做 50 张批量,但不想点 50 次 Run | **重复任务 (Clear / Repeat / Batch add)** — `Count: N` 一次提交 1–50 份; Repeat (↻) 一键克隆已完成任务 | `/api/schedule/add-batch`, `Repeat` button L542 |
| 队列里 30 条任务, 看到的是 `KSampler-42`, 找不到第几条是哪批 | **工作流命名 (workflow_title from Pinia store)** — 提交时从 `app.extensionManager.workflow.activeWorkflow.filename` 读取并落到数据库, 侧栏优先显示它 | `add` route L249, sidebar L561–L567 |
| 完成的任务里挑不到上次生成的图 | **预览图 (60x60 thumbnail)** — done 状态的 job 自动拉缩略图, 点击放大 | `get_job_with_outputs()` L246 → sidebar thumb L570–L582 |
| 同一个 workflow 跑 5 次都是 cache 命中, 全是同一张图 | **Cache 复用防止 (hook 默认 randomize)** — dispatch 时自动读 `control_after_generate` 模式 (`fixed`/`randomize`/`increment`/`decrement`) 并改写 `seed` / `noise_seed`, 同时把 UI format 转为 API format 让 hook 真正能看见 seed | `scheduler.py` `_apply_pre_dispatch_hooks`, `workflow_format.py` |
| 想让一批任务今晚跑, 又想立刻再插一条手动任务 | **暂停/恢复** — 一次 HTTP 调用暂停派发, in-flight 的 prompt 不打断 (我们到不了 ComfyUI 的 worker) | `/pause-all`, `/resume-all` |

---

## 安装 / Installation

完整步骤见 [docs/INSTALL.md](docs/INSTALL.md). Quick path:

```bash
git clone https://github.com/27-exe/ComfyUI-ScheduledQueue
cd ComfyUI-ScheduledQueue
ln -s "$(pwd)/src/comfyui_scheduled_queue" "$COMFYUI/custom_nodes/ComfyUI-ScheduledQueue"
ln -s "$(pwd)/scripts/comfy-schedule"       "$HOME/.local/bin/comfy-schedule"
"$VENV_PYTHON" main.py     # start ComfyUI
# expect: [ScheduledQueue] Stage 3 initialised. db=...
comfy-schedule resume     # default is paused on first boot
```

## 快速开始 / Quick start

```bash
# add a job in 10 minutes
echo '{"3": {"class_type": "KSampler", "inputs": {"seed": 42}}}' \
  | comfy-schedule add - --in 10m --note "morning batch"

# add 5 copies of the same workflow (count 5 → /add-batch)
cat workflow.json | comfy-schedule add - --in 1h --priority 200

# watch the queue
comfy-schedule watch --interval 2

# cancel / run-now
comfy-schedule cancel <job_id>
comfy-schedule run-now <job_id>
```

In the UI: click the **clock icon** in the topbar → choose preset → **Schedule**.

---

## Compatibility / 兼容性

| ComfyUI | Frontend | Status |
|---|---|---|
| 0.33.0 + 1.49.6 frontend | 1.49.6 | ✅ supported (developed here) |
| 0.33.0 + 1.48.x frontend | < 1.33.9 | ❌ frontend sidebar not registered |
| master (≥ v0.34) | ≥ 1.49.x | likely OK; run the test suite |

Backend requires **Python ≥ 3.10**. There are **no third-party Python dependencies** (uses `urllib.request` from stdlib).

## Running the test suite / 运行测试

```bash
python -m unittest discover tests -v
```

Currently **133 / 133 pass** (no `aiohttp` / no running ComfyUI required).

---

## Documentation / 文档

- [docs/INSTALL.md](docs/INSTALL.md) — install / upgrade / rollback / compatibility matrix / smoke test
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — process model, state machine, design decisions
- [docs/USER_GUIDE.md](docs/USER_GUIDE.md) — Chinese user guide with sidebar / schedule-dialog screenshots
- [CHANGELOG.md](CHANGELOG.md) — full version history

---

## Security / 安全

- **No payload over the wire from `/list`** — list endpoints strip `payload` so workflow JSON is never echoed back.
- **Whitelist-only updates** — `/update` rejects any field outside `{scheduled_at, priority, note, auto_retry, workflow_title}`.
- **Cancel only touches pending rows** — running jobs are not silently removed.
- **No shell hooks** — the scheduler uses `urllib.request`, not `subprocess`.

## Limitations / 局限 (read before filing a bug)

- **Cancel ≠ ComfyUI native interrupt.** A row with status `running` cannot be recalled once ComfyUI's worker thread has started; we mark our copy as `cancelled` but ComfyUI will still finish the actual prompt.
- **Reconciliation latency** is bounded by `RECONCILE_INTERVAL = 5 s`. A 1 s job may stay `running` for ~5–9 s before flipping to `done`.
- **No WebSocket subscription.** The reconciliation loop is HTTP only.
- **`workflow_title` reads `"Unsaved Workflow"`** until you Save As… the workflow in ComfyUI (matches ComfyUI's own display).
- **Frontend assumes v1.49.6+.** Older legacy queue menu is not addressed.

---

## License / 许可

MIT — see [LICENSE](LICENSE).

## Contributing / 贡献

Issues and PRs welcome at https://github.com/27-exe/ComfyUI-ScheduledQueue/issues.

When reporting a bug, include:
1. `comfy-schedule status` output
2. The relevant `comfyui.log` line(s) with `[ScheduledQueue]` prefix
3. Which side of the scheduler was active: CLI / HTTP / Sidebar / Schedule dialog