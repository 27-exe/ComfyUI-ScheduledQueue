# ComfyUI-ScheduledQueue

**Status:** under development (v0.3.10). CLI and HTTP API are production-ready; the bundled sidebar UI is stable against ComfyUI ≥ 1.49.6.

[English](README.md) · [简体中文](README.zh.md)

---

## 项目简介

一个 ComfyUI 队列扩展，**把每个任务持久化到 SQLite，支持暂停/恢复、免拖拽的重排序，以及任意时间的定时投递**——完全不打扰 ComfyUI 原生的 Run 按钮。

提交一个 workflow 不再等于"立刻占用 GPU"。把今晚 23:00 的批渲染、明天 9:00 的风格实验、一周后才会用到的种子复跑都加入队列，关掉浏览器，明天打开 ComfyUI，队列已经在跑了。

> **Beta / 公测提示:** 请先阅读 [Compatibility](#compatibility)。The bundled frontend assumes ComfyUI ≥ 1.49.6 with `app.registerExtension` and `app.extensionManager.registerSidebarTab`.

---

## Sidebar UI（主界面）

```
┌──────────────────────────────────────────────────────────────────────────┐
│ Scheduled Queue                                                          │
│ Managed by ScheduledQueue (not ComfyUI native queue).                    │
│ Workflow title = current app.extensionManager.workflow.activeWorkflow    │
│ .filename from ComfyUI Pinia store.                                      │
│                                                                          │
│ [All*] [Scheduled ] [Running ] [Done ] [Failed ] [Cancelled ]  Clear... │  ← ① 状态过滤标签
│ ┌────────────────────────────────────────────────────────────────────┐   │
│ │ [ ] Done (12)                                                      │   │  ② 清空面板（默认收起，
│ │ [ ] Failed (3)                                                     │   │     Clear... 按钮切换展开）
│ │ [ ] Cancelled (5)                                                  │   │
│ │ [ ] Running                                                        │   │
│ │ [ ] Scheduled                                                      │   │
│ │ [ ] Interrupted                                                    │   │
│ │             [ Clear selected ]                                     │   │
│ └────────────────────────────────────────────────────────────────────┘   │
│                                                                          │
│ [ Refresh ]   [ Pause ]                                                  │  ← ③ 暂停/恢复按钮
│                                                                          │
│ ┌────────────────────────────────────────────────────────────────────┐   │
│ │ paused · scheduled=0  running=0  done=0  failed=0  cancelled=0    │   │  ④ 实时状态条
│ └────────────────────────────────────────────────────────────────────┘   │
│                                                                          │
│ ┌────────────────────────────────────────────────────────────────────┐   │
│ │ My morning batch (a1b2c3d4)                         [↑] [↓] [Run] │   │
│ │ [scheduled] in 9h 14m                                             │   │  ⑤ 每条任务
│ │ scheduled 2026-08-23 22:30:00                                     │   │
│ │                                                  [↻ Repeat] [⬇]   │   │  ⑥ 复制/导出按钮
│ ├────────────────────────────────────────────────────────────────────┤   │
│ │ Style variant 3 (9f8e7d6c)                       [× Cancel]      │   │
│ │ [running] running                                                  │   │
│ │ started 22:31:05 · 12.4s                                           │   │
│ ├────────────────────────────────────────────────────────────────────┤   │
│ │ Seed sweep pass 2 (5a4b3c2d)                       [↻ Repeat] [⬇] │   │
│ │ [done] done yesterday · 41.0s                                      │   │
│ │ [thumb 60x60] ← 点击放大                                           │   │  ⑦ done 任务 60x60 缩略图
│ └────────────────────────────────────────────────────────────────────┘   │
│                                                                          │
│ [ ‹ Prev ]  Page 1 (1–3 of 17)  [ Next › ]                              │  ← ⑧ 分页
│                                                                          │
│ Use the clock icon in the topbar to add a new scheduled task.           │
└──────────────────────────────────────────────────────────────────────────┘
```

**Sidebar UI 元素对照表：**

| # | 元素 | 代码位置 |
|---|------|---------|
| ① | 状态过滤标签（All / Scheduled / Running / Done / Failed / Cancelled） | `data-filter` 行，`sidebar_tab.js` L57–L63 |
| ② | 清空面板 — checkbox 列表 + 各状态实时数量 | `[data-role="clear-panel"]` L66–L73 |
| ③ | 暂停/恢复按钮（切换文字 + 绿/灰背景） | `[data-act="pause-resume"]` L78，处理器 L706–L716 |
| ④ | 实时状态条（paused 标志 + 各状态计数） | `[data-role="status"]` L81 |
| ⑤ | 任务行：`workflow_title`（+ 短 id 后缀），状态徽章，时间 | L523–L549 |
| ⑥ | 每行的 Repeat / Export 按钮 | `↻` POST `/repeat/{id}`，`⬇` GET `/export/{id}` |
| ⑦ | 60×60 缩略图（仅 done 任务）；点击 → 全屏模态框 | L570–L582，模态框 L575–L577 |
| ⑧ | 分页（Prev / Page info / Next） | `[data-role="pager"]` L86–L89 |

顶栏 **Schedule** 按钮（时钟图标，位于 Run 左侧）会弹出添加新任务的对话框 —— 详见 [docs/USER_GUIDE.md §3](docs/USER_GUIDE.md#3-添加单个任务--schedule-对话框)。

### 真实界面截图

![顶栏 Schedule 按钮](docs/screenshots/01-topbar-schedule-button.png)
*ComfyUI 顶栏时钟图标 plugin 投递按钮*

![Schedule 对话框](docs/screenshots/02-schedule-dialog.png)
*Schedule 对话框：三段式时间控件 + Priority + Note + Count*

![Scheduled 队列](docs/screenshots/03-scheduled-queue.png)
*Scheduled 标签页：队列任务 + 调整顺序按钮 + 分页*

![Done 队列含缩略图](docs/screenshots/04-done-queue-thumbnails.png)
*Done 标签页：完成时间 + 时长 + 预览图缩略图*

---

## 主要功能

| 痛点 | 解决 | 代码位置 |
|---|---|---|
| 想让 prompt 在 GPU 空闲时才跑，又不想半夜开电脑 | **任务调度（时间窗）** —— 任意 ISO 或相对时间（`--in 10m`、`--in 2h`、`tomorrow 9 am`） | `scheduler.tick()` → `claim_next_due_job()` |
| ComfyUI 崩溃/重启会丢失内存中的队列 | **持久化** —— 每个任务都进 `<ComfyUI>/user/scheduled_queue.sqlite3`（WAL 模式） | `database.py` `ScheduledQueueDB` |
| 想做 50 张批量，但不想点 50 次 Run | **重复任务（Clear / Repeat / Batch add）** —— `Count: N` 一次提交 1–50 份；Repeat（↻）一键克隆已完成任务 | `/api/schedule/add-batch`，Repeat 按钮 L542 |
| 队列里 30 条任务，看到的是 `KSampler-42`，找不到第几条是哪批 | **工作流命名（`workflow_title` 来自 Pinia store）** —— 提交时从 `app.extensionManager.workflow.activeWorkflow.filename` 读取并落到数据库，侧栏优先显示它 | `add` route L249，sidebar L561–L567 |
| 完成的任务里挑不到上次生成的图 | **预览图（60x60 缩略图）** —— done 状态的 job 自动拉缩略图，点击放大 | `get_job_with_outputs()` L246 → sidebar thumb L570–L582 |
| 同一个 workflow 跑 5 次都是 cache 命中，全是同一张图 | **Cache 复用防止（hook 默认 `randomize`）** —— dispatch 时自动读 `control_after_generate` 模式（`fixed` / `randomize` / `increment` / `decrement`）并改写 `seed` / `noise_seed`，同时把 UI 格式转为 API 格式让 hook 真正能看见 seed | `scheduler.py` `_apply_pre_dispatch_hooks`，`workflow_format.py` |
| 想让一批任务今晚跑，又想立刻再插一条手动任务 | **暂停/恢复** —— 一次 HTTP 调用暂停派发，in-flight 的 prompt 不打断（我们到不了 ComfyUI 的 worker） | `/pause-all`、`/resume-all` |

---

## 安装

完整步骤见 [docs/INSTALL.md](docs/INSTALL.md)。快速路径：

```bash
git clone https://github.com/27-exe/ComfyUI-ScheduledQueue
cd ComfyUI-ScheduledQueue
ln -s "$(pwd)/src/comfyui_scheduled_queue" "$COMFYUI/custom_nodes/ComfyUI-ScheduledQueue"
ln -s "$(pwd)/scripts/comfy-schedule"       "$HOME/.local/bin/comfy-schedule"
"$VENV_PYTHON" main.py     # start ComfyUI
# expect: [ScheduledQueue] Stage 3 initialised. db=...
comfy-schedule resume     # default is paused on first boot
```

## 快速开始

```bash
# 加一条 10 分钟后跑的任务
echo '{"3": {"class_type": "KSampler", "inputs": {"seed": 42}}}' \
  | comfy-schedule add - --in 10m --note "morning batch"

# 加 5 份同一个 workflow（count 5 → /add-batch）
cat workflow.json | comfy-schedule add - --in 1h --priority 200

# 持续观察队列
comfy-schedule watch --interval 2

# 取消 / 立即跑
comfy-schedule cancel <job_id>
comfy-schedule run-now <job_id>
```

在 UI 里：点顶栏的 **时钟图标** → 选择预设 → **Schedule**。

---

## 兼容性

| ComfyUI | Frontend | 状态 |
|---|---|---|
| 0.33.0 + 1.49.6 frontend | 1.49.6 | ✅ 支持（开发版本） |
| 0.33.0 + 1.48.x frontend | < 1.33.9 | ❌ 侧栏未注册 |
| master (≥ v0.34) | ≥ 1.49.x | 大概率 OK；跑一下测试集 |

后端要求 **Python ≥ 3.10**。**没有第三方 Python 依赖**（只使用 stdlib 的 `urllib.request`）。

## 运行测试

```bash
python -m unittest discover tests -v
```

当前 **133 / 133 通过**（不需要 `aiohttp`，也不需要运行中的 ComfyUI）。

> 注：本仓库当前在 Python 3.14 下会跑 207 个测试，其中 `test_routes.TestRoutes` / `TestWorkflowTitleRoutes` 下有 33 个 error 是预先存在的 `asyncio.get_event_loop()` 弃用导致的失败，与本次 README 拆分无关。

---

## 文档

- [docs/INSTALL.md](docs/INSTALL.md) — 安装 / 升级 / 回滚 / 兼容矩阵 / 冒烟测试
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — 进程模型、状态机、设计决策
- [docs/USER_GUIDE.md](docs/USER_GUIDE.md) — 中文用户指南，含侧栏/Schedule 对话框截图
- [CHANGELOG.md](CHANGELOG.md) — 完整版本历史

---

## 安全

- **`/list` 不返回 payload** —— list 端点剥离 `payload`，workflow JSON 永远不会被回传。
- **白名单式的更新** —— `/update` 拒绝 `{scheduled_at, priority, note, auto_retry, workflow_title}` 之外的任何字段。
- **取消只触及 pending 行** —— 不会静默删除 running 任务。
- **没有 shell hook** —— scheduler 用 `urllib.request`，不用 `subprocess`。

## 局限（提 bug 前请先读）

- **Cancel ≠ ComfyUI 原生中断。** 一旦 ComfyUI 的 worker 线程已经启动，`running` 状态的行就无法被召回；我们会把副本标记为 `cancelled`，但 ComfyUI 仍会跑完那个 prompt。
- **同步延迟** 由 `RECONCILE_INTERVAL = 5 s` 限制。一个 1 秒的任务可能 `running` 5–9 秒后才翻成 `done`。
- **没有 WebSocket 订阅。** 同步循环只走 HTTP。
- **`workflow_title` 会显示 `"Unsaved Workflow"`**，直到你在 ComfyUI 里 Save As…（与 ComfyUI 自身的显示保持一致）。
- **前端假设 v1.49.6+。** 旧版 legacy queue menu 不在处理范围内。

---

## 许可

MIT —— 见 [LICENSE](LICENSE)。

## 贡献

欢迎提 Issue 和 PR：https://github.com/27-exe/ComfyUI-ScheduledQueue/issues。

报 bug 时请附上：
1. `comfy-schedule status` 输出
2. 相关的 `comfyui.log` 行（带 `[ScheduledQueue]` 前缀）
3. 当时走的是哪条路径：CLI / HTTP / Sidebar / Schedule 对话框
