# Install / Upgrade / Rollback

> Paths are written generically below. Replace the placeholders before
> running the commands:
>
> | placeholder | meaning |
> |---|---|
> | `$COMFYUI_ROOT` | Directory that contains `main.py` for your ComfyUI checkout (the parent of `custom_nodes/` and `user/`). |
> | `$VENV_PYTHON` | Python interpreter inside the ComfyUI virtualenv, usually `$COMFYUI_ROOT/.venv/bin/python`. |
> | `$PATH_BIN` | Directory on your `PATH` where you'd like the CLI wrapper to live, usually `$HOME/.local/bin`. |
>
> Example values used while testing this plugin: `COMFYUI_ROOT=/path/to/ComfyUI`,
> `VENV_PYTHON=$COMFYUI_ROOT/.venv/bin/python`, `PATH_BIN=$HOME/.local/bin`.

## Quick install (recommended)

The plugin ships as a normal Python package under `src/comfyui_scheduled_queue`.
Either copy it into `custom_nodes/` or symlink so `git pull` updates the live
files without re-installing.

```bash
git clone https://github.com/27-exe/ComfyUI-ScheduledQueue

# Option A: symlink (recommended — `git pull` updates it in place)
ln -s "$(pwd)/ComfyUI-ScheduledQueue/src/comfyui_scheduled_queue" \
       "$COMFYUI_ROOT/custom_nodes/ComfyUI-ScheduledQueue"

# Option B: copy (use when you want a frozen install)
cp -r ComfyUI-ScheduledQueue/src/comfyui_scheduled_queue \
      "$COMFYUI_ROOT/custom_nodes/"

# CLI wrapper
ln -s "$(pwd)/ComfyUI-ScheduledQueue/scripts/comfy-schedule" \
       "$PATH_BIN/comfy-schedule"
```

Start ComfyUI:

```bash
"$VENV_PYTHON" main.py
```

The log should contain:

```
[ScheduledQueue] Stage 3 initialised. db=$COMFYUI_ROOT/user/scheduled_queue.sqlite3
```

If it does, the plugin is loaded. If `routes` warning shows
`aiohttp unavailable` or `PromptServer.instance is None`, that is also
benign — the plugin retries once ComfyUI finishes bootstrapping.

## Quick upgrade

```bash
cd ComfyUI-ScheduledQueue
git pull
# restart ComfyUI
```

Schema migrations happen automatically inside `ScheduledQueueDB.__init__`.
Old rows survive; the `queue_order` column is backfilled on first start.

## Quick rollback

```bash
# 1. stop ComfyUI
# 2. remove the symlink or copy
rm "$COMFYUI_ROOT/custom_nodes/ComfyUI-ScheduledQueue"
# 3. (optional) wipe the database — the plugin is the only writer:
#      rm "$COMFYUI_ROOT/user/scheduled_queue.sqlite3"*
# 4. restart ComfyUI
```

The plugin keeps no background daemon. Removal is immediate on restart.

## Compatibility matrix

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

## Smoke test after install

```bash
comfy-schedule status
# expect: {"paused": true, "counts": {"scheduled": 0, ...}}

echo '{"3": {"class_type": "KSampler"}}' | \
  comfy-schedule add - --in 1m --note "smoke"

comfy-schedule resume
sleep 5
comfy-schedule list
```

If `/api/schedule/list` returns your job after a few seconds, the plugin
is wired correctly.

## Where the state lives

- **Database:** `$COMFYUI_ROOT/user/scheduled_queue.sqlite3` — auto-created,
  WAL mode.
- **WebExtension JS:** served by ComfyUI at
  `/extensions/ComfyUI-ScheduledQueue/sidebar_tab.js` (no copy needed;
  ComfyUI's static router serves `WEB_DIRECTORY`).

## Permissions

- The plugin writes only to `$COMFYUI_ROOT/user/` and `$COMFYUI_ROOT/logs/`.
- It does not modify any other file under `$COMFYUI_ROOT`.
- It does not call out to the network; it talks only to the configured
  ComfyUI HTTP endpoint (default `http://127.0.0.1:8188`).

## Uninstallation checklist

1. Stop ComfyUI.
2. Remove the symlink or copy from `custom_nodes/`.
3. Decide whether to keep the database:
   - Keep it if you plan to reinstall later — your queue survives.
   - Delete it for a clean slate: `rm "$COMFYUI_ROOT/user/scheduled_queue.sqlite3"`.
4. Remove the CLI wrapper: `rm "$PATH_BIN/comfy-schedule"`.
5. Restart ComfyUI; it should start cleanly without the plugin.

## Upgrading from a pre-release snapshot

If you installed an early development copy that lives directly inside
`custom_nodes/` (no `src/comfyui_scheduled_queue` wrapper), remove it first:

```bash
rm -rf "$COMFYUI_ROOT/custom_nodes/ComfyUI-ScheduledQueue"
```

Then follow the [Quick install](#quick-install-recommended) steps above.
The new layout uses a Python package directory name that starts with
`comfyui_scheduled_queue`, which is required for the custom-node loader
to import sibling modules reliably.
