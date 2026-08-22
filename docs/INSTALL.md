# Install / Upgrade / Rollback

## Quick install (recommended)

```bash
git clone https://github.com/27-exe/ComfyUI-ScheduledQueue
ln -s "$(pwd)/ComfyUI-ScheduledQueue/src/comfyui_scheduled_queue" \
       "$COMFYUI_ROOT/custom_nodes/ComfyUI-ScheduledQueue"
ln -s "$(pwd)/ComfyUI-ScheduledQueue/scripts/comfy-schedule" \
       "$HOME/.local/bin/comfy-schedule"
"$COMFYUI_ROOT/.venv/bin/python" main.py
# log should include:
#   [ScheduledQueue] Stage 3 initialised. db=/…/user/scheduled_queue.sqlite3
```

`$COMFYUI_ROOT` defaults to `/mnt/data/ai-art/ComfyUI` on this machine.

## Quick upgrade

```bash
cd ComfyUI-ScheduledQueue
git pull            # pulls latest from main
# restart ComfyUI
```

Schema migrations happen automatically inside `ScheduledQueueDB.__init__`.
Old rows survive; the `queue_order` column is backfilled on first start.

## Quick rollback

```bash
rm "$COMFYUI_ROOT/custom_nodes/ComfyUI-ScheduledQueue"
# optionally wipe the database (the plugin is the only writer):
#   rm "$COMFYUI_ROOT/user/scheduled_queue.sqlite3"*
# restart ComfyUI
```

The plugin keeps no background daemon. Removal is immediate on restart.

## Compatibility matrix

| ComfyUI frontend | works? | notes |
|---|---|---|
| 1.49.6 (current) | ✅ | tested on this machine |
| ≥ 1.33.9, < 1.49 | ⚠️ | toolbar button should work; sidebar render is 1.49-only |
| < 1.33.9 | ❌ | `actionBarButtons` API missing |

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

If `/api/schedule/list` returns your job after a few seconds, the
plugin is wired correctly.

## Where the state lives

- **Database:** `$COMFYUI_ROOT/user/scheduled_queue.sqlite3`
  (auto-created, `WAL` mode).
- **WebExtension JS:** served by ComfyUI at
  `/extensions/ComfyUI-ScheduledQueue/sidebar_tab.js` (no copy needed;
  ComfyUI's static router serves `WEB_DIRECTORY`).

## Permissions

- The plugin writes to `$COMFYUI_ROOT/user/` and `$COMFYUI_ROOT/logs/`.
- It does NOT modify any other file under `$COMFYUI_ROOT`.
- It does NOT call out to the network; it talks only to
  `http://127.0.0.1:8188` (ComfyUI's own HTTP server).
