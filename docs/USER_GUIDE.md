# ComfyUI-ScheduledQueue 用户指南

> 目标读者: 已经装好 ComfyUI + ComfyUI-ScheduledQueue, 想搞清楚这个插件到底能干什么、什么时候该用、踩坑了怎么排查的人。
>
> 文档版本: v0.3.10 / v0.3.11

---

## 目录

1. [30 秒看懂插件](#1-30-秒看懂插件)
2. [界面长什么样](#2-界面长什么样)
3. [添加单个任务 — Schedule 对话框](#3-添加单个任务--schedule-对话框)
4. [批量添加同一个工作流](#4-批量添加同一个工作流)
5. [队列管理 — Clear / Repeat / 分页 / 过滤](#5-队列管理--clear--repeat--分页--过滤)
6. [编辑已排队的任务](#6-编辑已排队的任务)
7. [Cache 复用场景解释](#7-cache-复用场景解释)
8. [CLI 用法 (可选)](#8-cli-用法-可选)
9. [故障排查](#9-故障排查)

---

## 1. 30 秒看懂插件

ComfyUI 自带的队列是「按下 Run 立刻占用 GPU、ComfyUI 关了就没了」。这个插件在它旁边开了一个**持久化的并行队列**:

- **持久化**: 任务进 SQLite, 关机 / 崩溃后还在
- **任意时间投递**: 现在 / 5 分钟后 / 明早 9 点 / 任意 ISO 时间都可以
- **暂停 / 恢复**: 一键让 scheduler 停止派发, in-flight 的 prompt 不打断
- **批量**: 一个 workflow 跑 1–50 份, 不用一次次按 Run
- **每张图不同**: 自动改写 seed, 不再 cache 命中

ComfyUI 原生的 Run 按钮**完全没改**。插件只是在你 *Schedule* 一个 workflow 时, 把它的 JSON 复制一份, 等到时间到了再 `POST /prompt` 给 ComfyUI, 等于「定时 Run」。

---

## 2. 界面长什么样

### 2.1 Sidebar 主界面

```
┌────────────────────────────────────────────────────────────────────┐
│ Scheduled Queue                                                    │
│ Managed by ScheduledQueue (not ComfyUI native queue).           │
│ Workflow title = current                                          │
│ app.extensionManager.workflow.activeWorkflow.filename             │
│ from ComfyUI Pinia store.                                          │
│                                                                    │
│ [All*] [Scheduled ] [Running ] [Done ] [Failed ] [Cancelled ]   │
│                                              [ Clear... ]          │  ← 状态过滤 + 清空面板
│                                                                    │
│ ┌──────────────────────────────────────────────────────────────┐  │
│ │ ☐ Done (12)  ☐ Failed (3)  ☐ Cancelled (5)                   │  │   默认收起, 点
│ │ ☐ Running    ☐ Scheduled ☐ Interrupted                       │  │   Clear... 展开
│ │                              [ Clear selected ]                │  │
│ └──────────────────────────────────────────────────────────────┘  │
│                                                                    │
│ [ Refresh ]  [ Pause ]                                             │  ← 顶部操作栏
│                                                                    │
│ ┌──────────────────────────────────────────────────────────────┐  │
│ │ paused · scheduled=0  running=0  done=0  failed=0  cancelled=0│  │   实时状态条
│ └──────────────────────────────────────────────────────────────┘  │
│                                                                    │
│ ┌──────────────────────────────────────────────────────────────┐  │
│ │ My morning batch (a1b2c3d4)                  [↑] [↓] [Run]   │   ① 任务标题 = workflow_title
│ │ [scheduled] in 9h 14m                                          │   ② 状态徽章 (彩色)
│ │ scheduled 2026-08-23 22:30:00                                  │   ③ 相对时间 (还有多久到)
│ │                                                [↻ Repeat] [⬇]  │   ④ 复制 / 导出
│ ├──────────────────────────────────────────────────────────────┤  │
│ │ Style variant 3 (9f8e7d6c)                       [× Cancel]    │
│ │ [running] running                                                │
│ │ started 22:31:05 · 12.4s                                         │
│ ├──────────────────────────────────────────────────────────────┤  │
│ │ Seed sweep pass 2 (5a4b3c2d)                    [↻ Repeat] [⬇]  │
│ │ [done] done yesterday · 41.0s                                    │
│ │ [thumb 60x60]  ← 点击放大                                       │   ⑤ 60x60 缩略图 (done 才有)
│ └──────────────────────────────────────────────────────────────┘  │
│                                                                    │
│ [ ‹ Prev ]  Page 1 (1–3 of 17)  [ Next › ]                       │  ← 分页
│                                                                    │
│ Use the clock icon in the topbar to add a new scheduled task.     │
└────────────────────────────────────────────────────────────────────┘
```

### 2.2 元素说明

| 区域 | 作用 |
|---|---|
| **① 任务标题** | 默认是 ComfyUI 里 *Save As…* 时的文件名 (`activeWorkflow.filename`)。未保存的 workflow 会显示 `Unsaved Workflow`。 |
| **② 状态徽章** | `scheduled` 灰 / `dispatched` 蓝 / `running` 黄 / `done` 绿 / `failed` 红 / `cancelled` 暗灰 / `interrupted` 橙。 |
| **③ 相对时间** | `in 9h 14m` 是离 `scheduled_at` 还有多久; `running` 显示 `running`; `done` 显示耗时 `41.0s`。 |
| **④ 复制 / 导出** | `↻ Repeat` 把当前 job 复制一份 (新 id, 新 scheduled_at=now+5s); `⬇` 导出原始 JSON 文件。 |
| **⑤ 缩略图** | 只对 `done` 状态显示, 点击放大到全屏模态框。 |
| **顶部状态过滤** | 点击切换只显示某类任务; 切换时自动回到 Page 1。 |
| **Clear 面板** | 勾选要清空的类别, 显示每个状态的当前数量, 点 `Clear selected` 一次性删除。 |
| **Pause / Resume** | 暂停时按钮变绿显示 `Resume`, 状态条显示 `paused · ...`。 |
| **分页** | `limit=50` 默认; 显示当前 offset/limit/total, 末页自动 disable `Next ›`。 |

---

## 3. 添加单个任务 — Schedule 对话框

点顶栏的 **🕒 时钟图标** (Run 按钮左侧), 弹出 Schedule 对话框:

```
┌────────────────────────────────────────────────────────────┐
│ Schedule current workflow                                │
│                                                          │
│ When (local time)                                          │
│ [in 30s] [in 5 min] [in 30 min] [in 2 hours] [tomorrow 9am]│  ← 快捷预设
│                                                            │
│ ┌──────────────────────────────────┐  ┌───────────────┐    │
│ │ 2026-08-23 22:30:00              │  │-1h -10m -1m -10s -5s│ ← 微调按钮
│ └──────────────────────────────────┘  └───────────────┘    │
│                              ┌───────────────┐              │
│                              │+5s +10s +1m +10m +1h│        │
│                              └───────────────┘              │
│                                                            │
│ Priority (0-1000, higher runs first)                         │
│ [ 100 ]                                                    │
│                                                            │
│ Note (optional)                                              │
│ [ e.g. morning batch / variant 3                          ]│
│                                                            │
│ Count: [ 1 ] (1-50, repeat same workflow)                  │
│                                                            │
│                              [ Cancel ]  [ Schedule ]       │
└────────────────────────────────────────────────────────────┘
```

### 3.1 时间输入

时间输入框接受三种本地时间格式:

- `2026-08-23 22:30:00`
- `2026-08-23T22:30:00`
- `2026/08/23 22:30:00`

不允许 ISO `Z` 后缀 — 全部按**本地时间**解释。解析失败时输入框会变红, 弹出 `Invalid time` 警告, 不会静默用旧值提交。

### 3.2 微调按钮

- `-1h` / `-10m` / `-1m` / `-10s` / `-5s` 各自减去对应秒数
- `+5s` / `+10s` / `+1m` / `+10m` / `+1h` 各自加上
- 每次按下:
  - **减法**时输入框边框**闪蓝色**
  - **加法**时输入框边框**闪绿色**
  用来区分两种方向, 视觉反馈在深色背景下很明显。

### 3.3 Priority

- 范围 `0..1000`
- 默认 `100`
- 同一时刻到期的多个任务里, **高 priority 优先派发**; 同 priority 时按 `queue_order` 排序
- 实用建议:
  - 想让某条任务「插队到现有队列前面」 → 调高它的 priority 到 500
  - 想跑后台大量任务 → 默认 100 即可

### 3.4 Note

纯文本备注, 显示在 sidebar 任务标题下方。可以写「morning batch / variant 3」之类的标记。如果 `workflow_title` 为空, sidebar 会用 note 当标题显示。

### 3.5 Count

`Count: N` 是核心的批量入口:

- `1` (默认): 加一条任务
- `2..50`: 加 N 条**完全相同的 workflow**, 每条一个新 id, scheduled_at 都等于同一时刻, priority 相同

实际上是一次性 POST `/api/schedule/add-batch`, 后端在数据库里原子写入 N 行, 不会出现「加了 50 个只进了 30 个」的中间态。

### 3.6 Schedule 按钮

按下后:

1. 通过 `app.graphToPrompt()` 拿到当前画布的 API 格式 JSON
2. 从 Pinia store 读 `activeWorkflow.filename` → `workflow_title`
3. POST `/api/schedule/add` (或 `/add-batch`, 当 Count > 1)
4. 后端返回新 job id, sidebar 自动刷新一次 (5s 轮询前的「立即拉」)

---

## 4. 批量添加同一个工作流

如果你的目的是「同一个 prompt 跑 30 张不同 seed」, 两种方式:

### 4.1 对话框方式

Schedule 对话框的 `Count: 30` → 一次性提交。30 个 job 都进 `scheduled`, priority 相同 → scheduler 按 `queue_order` 顺序逐条派发。

### 4.2 Sidebar 重复 (推荐用于「复跑已经满意的工作流」)

对 `done` / `cancelled` 的 job, sidebar 行的 `↻` (Repeat) 按钮会:
1. 读取原 job 的 `payload` + `priority` + `note`
2. 新建一个 `scheduled` 行, scheduled_at = now + 5s
3. 返回新 id, sidebar 立刻刷新

适合「这组参数不错, 再跑一组」的场景。

---

## 5. 队列管理 — Clear / Repeat / 分页 / 过滤

### 5.1 过滤

顶部的 `[All] [Scheduled] [Running] [Done] [Failed] [Cancelled]` 单选按钮:

- 点击切换 → offset 重置为 0 (回到 Page 1)
- 默认 `All` 显示所有 status (兼容老行为, 显示 pending + running)

### 5.2 分页

- 默认每页 50 条 (sidebar 会显示 `Page N (start–end of total)`)
- 末页时 `Next ›` 自动 disable; `‹ Prev` 在第一页时 disable
- 后端 `GET /list?limit=50&offset=N`, 默认 order by `queue_order` ASC, `priority` DESC

### 5.3 Clear

`Clear...` 按钮展开一个面板:

```
┌──────────────────────────────────────────┐
│ ☐ Done (12)                              │
│ ☐ Failed (3)                              │
│ ☐ Cancelled (5)                           │
│ ☐ Running                                 │   ← 一般不要选这个
│ ☐ Scheduled                              │   ← 一般不要选这个
│ ☐ Interrupted                             │
│            [ Clear selected ]             │
└──────────────────────────────────────────┘
```

- 每个 checkbox 旁边实时显示当前数量 (从最近一次 `/status` 拉取)
- `Clear selected` 一次性 POST `/clear`, body 是 `{statuses: ["done", "failed"]}`
- 后端只接受白名单内的 status 字符串
- `Running` / `Scheduled` 通常不建议勾 — 删了 running 会让对应 prompt 在 ComfyUI 里**继续跑完**, 但我们的记录没了, 下次 reconcile 时 ComfyUI 会把 prompt_id 当成孤儿处理

### 5.4 Repeat (↻)

对任意状态的 job 都可点:

- 对 `done` / `failed` / `cancelled` → 复制 payload, 新建一条 `scheduled` (scheduled_at = now + 5s)
- 对 `scheduled` / `running` → 复制 payload + 当前 `scheduled_at`, 新建一条 `scheduled`
- 对 `dispatched` → 同上 (但复制的是已派发的 payload, 不是原始时刻)

### 5.5 Export (⬇)

下载该 job 的完整 JSON:

- 浏览器导航到 `/api/schedule/job/{id}/export`, 触发文件下载
- 包含 `payload`, `note`, `priority`, `workflow_title` 等全部字段
- 可以 `cat` 看, 或 `comfy-schedule add exported.json --in 1h` 重新加入

---

## 6. 编辑已排队的任务

Sidebar 中每个 `scheduled` / `running` 的 job 行右侧有:

| 按钮 | 作用 | 说明 |
|---|---|---|
| `↑` | 上移一位 | `POST /reorder/{id}?direction=up` — 改 `queue_order`, 顶部时灰掉 |
| `↓` | 下移一位 | 同上, direction=down |
| `Run` | 立即跑 | `POST /run-now/{id}` — 把 `scheduled_at` 改成 now, 下一个 tick 就派发 |
| `×` | 取消 | `POST /cancel/{id}` — running 状态返回 409 (truthful) |

其它字段 (priority / note / workflow_title / auto_retry / scheduled_at) 走 `POST /api/schedule/update/{id}` (whitelist-only), 通常用 CLI:

```bash
comfy-schedule update <job_id> --priority 500 --note "high-prio batch"
comfy-schedule update <job_id> --in 30m
```

---

## 7. Cache 复用场景解释

### 7.1 现象

按 `Run` 三次同样的 workflow, 结果拿到的是同一张图三次 (除了文件名, 内容像素完全一样)。

### 7.2 原因

ComfyUI 在底层按 `(model_hash, seed, sampler_params)` 做 cache 命中; `KSampler` 节点的 `seed` widget 默认绑 `control_after_generate = "fixed"`, 所以下一次提交时 seed 没变, cache 直接命中, 不重跑 sampler。

### 7.3 插件怎么解决

`scheduler.py` 的 `_apply_pre_dispatch_hooks()` 在每次 POST `/prompt` 之前:

1. 读每个 KSampler / KSamplerAdvanced 的 `widgets_values`, 找到 `control_after_generate` 槽位
2. 按以下规则改写 seed:

   | mode | 新 seed |
   |---|---|
   | `fixed` | 不变 (用户明确要可复现) |
   | `increment` | seed + 1 |
   | `decrement` | seed - 1 |
   | `randomize` | random.randint(0, 2³²-1) |
   | 未设置 | 视为 `randomize` (与 ComfyUI 默认行为一致) |

3. 同时把 UI 格式的 `payload` 转成 API 格式 (`workflow_format.py`), 这样 seed 才能写到正确位置

### 7.4 用户应该做什么

- **想要每张图不同**: 保留 KSampler 默认 `control_after_generate = randomize` 即可。插件每次随机化。
- **想要可复现**: 显式选 `fixed`, 但**同一个工作流不要 Count > 1**, 否则还是同样的图。
- **想要序列变化** (cover grid): 用 `increment` + Count = 50, 拿到 seed 42, 43, 44, ..., 91 的渐变。

### 7.5 已知细节

- 插件只对 `KSampler` / `KSamplerAdvanced` 做处理。其它自定义 sampler 节点需要自己确保 seed 是 input edge 而不是 fixed value。
- 如果你在 KSampler 之前手动覆盖了 seed (用 `PrimitiveNode` 或 `SetNode`), 插件会改写 KSampler 自己的 seed, 不影响上游。

---

## 8. CLI 用法 (可选)

CLI 完全等价于 HTTP API。装好后默认在 `~/.local/bin/comfy-schedule`:

```bash
# 加任务
echo '{"3":{"class_type":"KSampler","inputs":{"seed":42}}}' \
  | comfy-schedule add - --in 10m --note "morning batch"

# 加 5 份
cat workflow.json | comfy-schedule add - --in 1h --count 5

# 列表
comfy-schedule list
comfy-schedule list --status scheduled

# 状态
comfy-schedule status
# { "paused": false, "counts": {...}, "version": "0.3.10" }

# 暂停 / 恢复
comfy-schedule pause-all
comfy-schedule resume-all

# 立即跑 / 取消 / 复制
comfy-schedule run-now <job_id>
comfy-schedule cancel <job_id>
comfy-schedule repeat <job_id>

# 编辑 (whitelist)
comfy-schedule update <job_id> --priority 500
comfy-schedule update <job_id> --in 30m --note "delay"

# 持续观察
comfy-schedule watch --interval 2
```

`comfy-schedule` 默认连 `http://127.0.0.1:8188`, 通过环境变量覆盖:

```bash
export COMFYUI_HOST=http://127.0.0.1:8188
comfy-schedule status
```

---

## 9. 故障排查

### 9.1 Sidebar 没出现

**症状**: 左边没有 Scheduled Queue 标签。

**原因 / 解决**:

1. ComfyUI frontend < 1.49.6 → 升级 frontend, 或只用 CLI / HTTP
2. 浏览器缓存 → 强制刷新 (Ctrl/Cmd+Shift+R)
3. JS 报错 → 打开浏览器 DevTools Console, 看是否有红色报错, 截图反馈

### 9.2 顶栏时钟按钮缺失

**原因**: ComfyUI frontend < 1.33.9, 没有 `actionBarButtons` API。升级 frontend。

### 9.3 Schedule 对话框点了 Schedule 没反应

**原因 / 解决**:

1. 时间格式错 (输入框会变红 + 弹 `Invalid time`)
2. 浏览器没接 `app.graphToPrompt()` — 多发生在用 SaveImage / LoadImage 等不支持 API export 的旧 custom node; 看 console 错误
3. 后端报错 → 看 `comfyui.log` 里 `[ScheduledQueue]` 前缀

### 9.4 任务永远 `running`

**原因**: 5s 之后 reconcile 还没看到 `/history` 里出现该 prompt_id。

**排查**:

```bash
curl -s http://127.0.0.1:8188/history/<prompt_id> | jq
```

- 如果返回 `{}`: ComfyUI 的 history 里没有这条 prompt → 它在 worker 里卡了, 看 ComfyUI 自己的日志
- 如果返回 `{"<prompt_id>": {"status": "success" | "error"}}`: 说明 ComfyUI 已经完成, 但我们这边 reconcile 还没拿到 → 等下一个 5s tick
- 如果完全没回应: ComfyUI 卡死 / OOM

### 9.5 同一张图出现 N 次 (cache 命中)

**原因**: KSampler 的 `control_after_generate` = `fixed` + 同一个 workflow 跑了 N 次。

**解决**:

- 改成 `randomize` (推荐, 跟 ComfyUI 默认行为一致)
- 或者用 `increment` (拿相邻 seed 序列)

详见 §7。

### 9.6 关掉 ComfyUI 后任务没了

**不应该发生**。任务都在 `$COMFYUI_ROOT/user/scheduled_queue.sqlite3` 里。如果真的没了:

1. 检查 DB 文件还在不在: `ls -lh "$COMFYUI_ROOT/user/scheduled_queue.sqlite3"`
2. 看 ComfyUI 启动日志里有没有 `[ScheduledQueue] Stage 3 initialised`
3. 看日志有没有 `database is locked` 错误 (一般是别的进程占着 DB)

### 9.7 Pause / Resume 没生效

**症状**: 按了 Pause, 任务还在派发。

**排查**:

1. 等下一次 5s 轮询, sidebar 上方状态条会更新成 `paused · ...`
2. 看日志里 `[ScheduledQueue] tick skipped: paused`
3. `comfy-schedule status` 应当返回 `"paused": true`

### 9.8 改 priority 后顺序没变

**原因**: priority 只在「同一时刻到期的多个任务」之间生效。如果你的 30 个任务都是 now + 1h, 改某个任务的 priority 到 500 也不会让它提前一小时跑。

**解决**: 用 `↑` / `↓` 按钮改 `queue_order` (它是在 ready bucket 里的实际派发顺序); 或者改 `scheduled_at` (Run now)。

### 9.9 Export 出来的 JSON 没法重新 add

**原因**: Export 出的 JSON 包含 `id` / `created_at` 等不应回传的字段。

**解决**: 用 `payload` 字段喂回去:

```bash
comfy-schedule export <job_id> > job.json
jq '.payload' job.json | comfy-schedule add - --in 1h
```

---

## 10. 提 issue 之前

请附上:

1. `comfy-schedule status` 输出
2. ComfyUI 日志中含 `[ScheduledQueue]` 的行 (前后各 20 行)
3. 是哪条路径触发的: CLI / HTTP curl / sidebar / Schedule 对话框
4. ComfyUI 版本 + frontend 版本 (`Settings → About` 或 `comfy --version`)

CLI 输出可以直接贴, sidebar 截图请用 DevTools → Elements 选中对应区域截图, 包含属性面板。

---

## 11. 下一步可以读的

- [INSTALL.md](INSTALL.md) — 安装 / 升级 / 回滚
- [ARCHITECTURE.md](ARCHITECTURE.md) — 想改 scheduler / 数据库 schema 的人必读
- [README.md](../README.md) — 项目总览
- [CHANGELOG.md](../CHANGELOG.md) — 完整版本历史