---
[EN]

- **Description:** End-to-end installation, upgrade, and rollback procedures for the ComfyUI-ScheduledQueue plugin — symlink vs. copy install, schema migrations, where plugin state is written, and a compatibility matrix for ComfyUI server / frontend / Python / OS versions.
- **Audience:** Operator installing or maintaining the plugin on a host running ComfyUI. Assumes familiarity with running ComfyUI from a terminal but not with this plugin.
- **Contents:** §1 Requirements (software, hardware, network, disk) · §2 Install (symlink / copy) · §3 Upgrade · §4 Rollback / Uninstall · §5 Where state lives · §6 Compatibility matrix · §7 Common install problems · §8 Upgrading from a pre-release snapshot.

> Paths are written generically below. Replace the placeholders before
> running the commands:
>
> | placeholder | meaning |
> |---|---|
> | `$COMFYUI_ROOT` | Directory that contains `main.py` for your ComfyUI checkout (the parent of `custom_nodes/` and `user/`). |
> | `$VENV_PYTHON` | Python interpreter inside the ComfyUI virtualenv, usually `$COMFYUI_ROOT/.venv/bin/python`. |
> | `$PATH_BIN` | Directory on your `PATH` where you'd like the CLI wrapper to live, usually `$HOME/.local/bin`. |
>
> Example values used while testing this plugin:
> `COMFYUI_ROOT=/path/to/ComfyUI`,
> `VENV_PYTHON=$COMFYUI_ROOT/.venv/bin/python`,
> `PATH_BIN=$HOME/.local/bin`.

---

## 1. Requirements

### 1.1 Software

| What | Version | Notes |
|---|---|---|
| ComfyUI server | ≥ 0.33.0 | uses `/prompt`, `/history/{prompt_id}` |
| ComfyUI frontend | ≥ 1.49.6 | `registerExtension` + `extensionManager.registerSidebarTab` required for full sidebar UI |
| Frontend fallback | ≥ 1.33.9, < 1.49.6 | topbar Schedule button only; sidebar tab will not register |
| Python | 3.10 – 3.13 | uses PEP 604 `X \| None` syntax; `<3.10` not supported |
| OS | Linux (primary); macOS / Windows should work but are not the test target | uses POSIX-only paths in INSTALL |
| Stdlib only | – | no `pip install` required; the plugin uses `urllib.request` |

### 1.2 Hardware

There are no plugin-imposed hardware requirements. Disk usage is roughly
`len(prompt_json) × job_count`; in practice a few thousand queued jobs fit
in <10 MB. The plugin performs:

- one `urllib` POST per dispatched job (every 1 s tick, when due jobs exist)
- one `urllib` GET `/history/{prompt_id}` per *running* job every 5 s
- one SQLite read + write per tick

These are dwarfed by the ComfyUI worker itself.

### 1.3 Network

- Plugin only talks to **the configured ComfyUI HTTP endpoint**
  (default `http://127.0.0.1:8188`).
- It does not call out to any third-party host.
- It does not bind any port itself.

### 1.4 Disk / filesystem

- Writes only to `$COMFYUI_ROOT/user/scheduled_queue.sqlite3` (+ `-wal` /
  `-shm` siblings) and `$COMFYUI_ROOT/logs/`.
- It does not modify any other file under `$COMFYUI_ROOT`.

---

## 2. Install

### 2.1 Quick install (recommended — symlink)

```bash
git clone https://github.com/27-exe/ComfyUI-ScheduledQueue
cd ComfyUI-ScheduledQueue

# Option A: symlink (recommended — `git pull` updates it in place)
ln -s "$(pwd)/src/comfyui_scheduled_queue" \
       "$COMFYUI_ROOT/custom_nodes/ComfyUI-ScheduledQueue"

# CLI wrapper
ln -s "$(pwd)/scripts/comfy-schedule" \
       "$PATH_BIN/comfy-schedule"
```

### 2.2 Frozen install (copy)

```bash
git clone https://github.com/27-exe/ComfyUI-ScheduledQueue
cd ComfyUI-ScheduledQueue
cp -r src/comfyui_scheduled_queue \
      "$COMFYUI_ROOT/custom_nodes/"
ln -s "$(pwd)/scripts/comfy-schedule" \
       "$PATH_BIN/comfy-schedule"
```

### 2.3 Start ComfyUI

```bash
"$VENV_PYTHON" main.py
```

You should see (within ~1 s of the `Starting server` line):

```
[ScheduledQueue] Stage 3 initialised. db=$COMFYUI_ROOT/user/scheduled_queue.sqlite3
```

If instead you see `aiohttp unavailable` or `PromptServer.instance is None`,
that is **benign** — the plugin retries once ComfyUI finishes bootstrapping.

### 2.4 Verify

```bash
comfy-schedule status
# expect: {"paused": true, "counts": {"scheduled": 0, "running": 0, ...}}

echo '{"3": {"class_type": "KSampler", "inputs": {"seed": 42}}}' | \
  comfy-schedule add - --in 1m --note "smoke"

comfy-schedule resume
sleep 5
comfy-schedule list
```

If `list` shows your job, the plugin is wired end-to-end.

---

## 3. Upgrade

```bash
cd ComfyUI-ScheduledQueue
git pull
# restart ComfyUI
```

Schema migrations happen **automatically** inside
`ScheduledQueueDB.__init__()` (`database.py`). Old rows survive every
upgrade; the `queue_order` and `workflow_title` columns are added via
idempotent `ALTER TABLE … ADD COLUMN`. No manual SQLite work required.

---

## 4. Rollback / Uninstall

```bash
# 1. stop ComfyUI
# 2. remove the symlink or copy
rm "$COMFYUI_ROOT/custom_nodes/ComfyUI-ScheduledQueue"

# 3. (optional) wipe the database — the plugin is the only writer:
rm "$COMFYUI_ROOT/user/scheduled_queue.sqlite3"*

# 4. remove the CLI wrapper
rm "$PATH_BIN/comfy-schedule"

# 5. restart ComfyUI; it should start cleanly without the plugin.
```

The plugin keeps **no background daemon**. Removal is immediate on restart.

---

## 5. Where state lives

- **Database:** `$COMFYUI_ROOT/user/scheduled_queue.sqlite3` — auto-created,
  WAL mode. Three tables: `scheduled_jobs`, `job_history`, `scheduler_state`.
- **WebExtension JS:** served by ComfyUI at
  `/extensions/ComfyUI-ScheduledQueue/sidebar_tab.js` — no manual copy
  needed; ComfyUI's static router serves `WEB_DIRECTORY` automatically.
- **Logs:** standard ComfyUI log file; look for the `[ScheduledQueue]`
  prefix to filter.

---

## 6. Compatibility matrix

| ComfyUI frontend | Works? | Notes |
|---|---|---|
| ≥ 1.49.6 | ✅ | Full feature set (sidebar panel + topbar Schedule button) |
| ≥ 1.33.9, < 1.49.6 | ⚠️ | Topbar button works; sidebar tab render is 1.49-only |
| < 1.33.9 | ❌ | `actionBarButtons` API missing — topbar button won't appear |

| ComfyUI server | Works? | Notes |
|---|---|---|
| ≥ 0.33.0 | ✅ | Uses `registerExtension` and `/history/{prompt_id}` |
| older | ⚠️ | API surface may differ |

| Python | Works? |
|---|---|
| 3.10 – 3.13 | ✅ |
| < 3.10 | ❌ (uses PEP 604 `X \| None` syntax) |

| OS | Works? |
|---|---|
| Linux | ✅ (primary test target) |
| macOS | ✅ (not regularly tested; uses POSIX paths) |
| Windows | ⚠️ should work via WSL; symlink the package into `custom_nodes\` |

---

## 7. Common install problems

### 7.1 `comfy-schedule: command not found`

Cause: `$PATH_BIN` is not on your `PATH`. Either:

```bash
# option 1: extend PATH for the current shell
export PATH="$HOME/.local/bin:$PATH"

# option 2: put it where your shell already looks
ln -sf "$(pwd)/scripts/comfy-schedule" /usr/local/bin/comfy-schedule   # needs sudo
```

Or invoke the wrapper directly:

```bash
"$REPO/scripts/comfy-schedule" status
```

### 7.2 `[ScheduledQueue] Stage 3 initialised` is **missing** from logs

Causes / remedies:

1. **Wrong directory copied.** The plugin directory *must* be named
   `comfyui_scheduled_queue` (underscore, lowercase) — that's the Python
   package name and the loader imports it by that name. If you copied
   `src/ComfyUI-ScheduledQueue/` instead of `src/comfyui_scheduled_queue/`,
   rename it:
   ```bash
   mv "$COMFYUI_ROOT/custom_nodes/ComfyUI-ScheduledQueue/src/ComfyUI-ScheduledQueue" \
      "$COMFYUI_ROOT/custom_nodes/ComfyUI-ScheduledQueue"
   ```
2. **Stale `__pycache__`** in the custom_nodes folder from a previous
   layout:
   ```bash
   rm -rf "$COMFYUI_ROOT/custom_nodes/comfyui_scheduled_queue/__pycache__"
   ```
3. **Frontend didn't reach Stage 3.** Check that ComfyUI itself started
   (look for `Starting server\nTo see the GUI go to:`). The plugin
   registers its routes after `PromptServer.instance` exists.

### 7.3 Sidebar tab does not show up

Cause: frontend < 1.49.6. The plugin registers
`app.extensionManager.registerSidebarTab(...)`, which is a v1.49+ API.
Either upgrade ComfyUI frontend or use the CLI / HTTP API exclusively
(both fully functional on older frontends).

### 7.4 Topbar Schedule button missing

Cause: frontend < 1.33.9. The plugin uses `actionBarButtons` which was
introduced in 1.33.9.

### 7.5 `database is locked` errors in the log

Cause: another process has the SQLite file open. SQLite allows multiple
readers + one writer; if ComfyUI itself or a stray `sqlite3` CLI is
holding a write lock, the scheduler will retry on the next 1 s tick.
If persistent, check:

```bash
lsof "$COMFYUI_ROOT/user/scheduled_queue.sqlite3"
```

…and kill any unexpected holders.

### 7.6 Plugin loads but `comfy-schedule status` returns connection refused

Cause: ComfyUI bound to a non-default host/port. Set the env var:

```bash
export COMFYUI_HOST=http://127.0.0.1:8188   # default
# or whatever your ComfyUI logs printed as "Starting server"
```

The CLI reads `COMFYUI_HOST` on every invocation.

### 7.7 Tests fail with `ModuleNotFoundError: src`

You are running from the wrong directory:

```bash
cd /path/to/ComfyUI-ScheduledQueue
python -m unittest discover tests -v
```

The repo has no `pip install` step; tests use `sys.path.insert(0, ...)`
to find the package.

---

## 8. Upgrading from a pre-release snapshot

If you installed an early development copy that lives directly inside
`custom_nodes/` (no `src/comfyui_scheduled_queue` wrapper), remove it
first:

```bash
rm -rf "$COMFYUI_ROOT/custom_nodes/ComfyUI-ScheduledQueue"
```

Then follow [§2 Install](#2-install). The new layout uses a Python
package directory name that starts with `comfyui_scheduled_queue`,
which is required for the custom-node loader to import sibling modules
reliably.