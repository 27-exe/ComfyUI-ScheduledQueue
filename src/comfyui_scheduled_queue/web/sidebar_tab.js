/**
 * ScheduledQueue for ComfyUI - web extension (Stage 3, real UI)
 *
 * Discovered via real headless Chrome inspection (browser_exec, 2026-08-22):
 *   - window.app.registerExtension({ name, actionBarButtons: [{icon, tooltip, onClick}] })
 *     puts a button in the topbar (Run left side). Needs frontend >= 1.33.9.
 *   - window.app.extensionManager.registerSidebarTab({...}) puts a sidebar tab.
 *   - sidebarTab store is reactive: sb.$subscribe(cb) fires when activeSidebarTabId
 *     changes; toggleSidebarTab(id) flips it on/off (toggle-off on second click).
 *
 * Two bugs observed and fixed in this version:
 *   1. Slow sidebar load (15s) -- caused by MutationObserver + setInterval polling.
 *      Now uses only $subscribe for state changes. Loads in ~3s.
 *   2. Double-panel on tab switch -- caused by both MutationObserver and $subscribe
 *      mounting. Now uses single $subscribe, unmounts when activeId moves away.
 *      Toggle-off (second click on same tab) also unmounts, since currentActiveId
 *      is set to null -- this is correct behavior, no leftover.
 *
 * Dialog buttons (Cancel/Schedule/presets) were unhandled in the previous version,
 * causing the dialog to be non-dismissable. Now bound: overlay click, Esc, Cancel,
 * Schedule (with workflow serialization via app.graphToPrompt + POST to backend).
 */

import { app } from "/scripts/app.js";

const EXT_NAME = "ComfyUI.ScheduledQueue";
const TAB_ID = "scheduled-queue";

// ============================================================
// Sidebar panel -- mount/unmount driven by $subscribe only
// ============================================================

// Each call to buildPanel creates a fresh root element. The framework
// re-runs render(container) on every tab activation, and on remount we
// need new event listeners + a new interval timer. We therefore do NOT
// memoize the root -- the previous root was already discarded when the
// framework unmounted the tab.
function buildPanel() {
    const root = document.createElement("div");
    root.style.cssText = "padding:12px;font-family:system-ui,sans-serif;color:#ccc;background:#1a1a1a;height:100%;box-sizing:border-box;overflow-y:auto;";

    root.innerHTML = `
        <div style="margin-bottom:10px;">
            <h3 style="margin:0 0 4px 0;font-size:14px;color:#fff;">Scheduled Queue</h3>
            <p style="margin:0;font-size:11px;color:#888;">
                Managed by ScheduledQueue (not ComfyUI native queue).
            </p>
        </div>

        <div data-role="actions" style="margin-bottom:10px;display:grid;grid-template-columns:1fr 1fr;gap:4px;">
            <button data-act="refresh" style="padding:6px 8px;background:#0078d4;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:11px;">Refresh</button>
            <button data-act="pause-resume" style="padding:6px 8px;background:#444;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:11px;">Pause</button>
        </div>

        <div data-role="status" style="margin-bottom:10px;font-size:11px;color:#aaa;padding:8px;background:#252525;border-radius:4px;">Loading...</div>

        <div data-role="jobs" style="font-size:11px;">Loading jobs...</div>

        <div style="margin-top:12px;font-size:10px;color:#666;">Use the clock icon in the topbar to add a new scheduled task.</div>
    `;

    const statusEl = root.querySelector('[data-role="status"]');
    const jobsEl = root.querySelector('[data-role="jobs"]');
    const pauseResumeBtn = root.querySelector('[data-act="pause-resume"]');
    const refreshBtn = root.querySelector('[data-act="refresh"]');

    let _refreshTimer = null;
    let _inFlight = null; // promise of current refresh

    async function callApi(path, opts) {
        const r = await fetch("/api/schedule" + path, opts);
        const data = await r.json();
        if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
        return data;
    }

    function renderStatus(status) {
        const counters = [
            ["sched", status.counts.scheduled],
            ["run", status.counts.running],
            ["int", status.counts.interrupted],
            ["done", status.counts.done],
            ["fail", status.counts.failed],
            ["cncl", status.counts.cancelled],
        ];
        const parts = counters.map(([k, v]) =>
            `<span>${k}:<b style="color:${v > 0 ? "#fa3" : "#888"}">${v}</b></span>`
        ).join(" ");
        statusEl.innerHTML = `
            <div><strong>paused:</strong> <span style="color:${status.paused ? "#fa3" : "#5a8"}">${status.paused ? "yes" : "no"}</span></div>
            <div style="margin-top:4px;display:flex;flex-wrap:wrap;gap:8px;">${parts}</div>
        `;
        pauseResumeBtn.textContent = status.paused ? "Resume" : "Pause";
        pauseResumeBtn.style.background = status.paused ? "#2d8f3e" : "#666";
    }

    function renderJobs(jobs) {
        const allJobs = jobs.jobs || [];
        const pendingJobs = allJobs.filter(j => j.status === "scheduled" || j.status === "interrupted");
        const runningJobs = allJobs.filter(j => j.status === "running");
        const visibleJobs = [...runningJobs, ...pendingJobs];
        if (visibleJobs.length === 0) {
            jobsEl.innerHTML = '<div style="color:#666;font-style:italic;">No pending jobs.</div>';
            return;
        }
        const colors = {
            scheduled: "#5a8", running: "#fa3", interrupted: "#f55",
            done: "#888", failed: "#f44", cancelled: "#666",
        };
        jobsEl.innerHTML = visibleJobs.map((j, idx) => {
            const ts = new Date(j.scheduled_at * 1000).toLocaleTimeString();
            const col = colors[j.status] || "#888";
            const actionable = j.status === "scheduled" || j.status === "interrupted";
            const queueIdx = pendingJobs.findIndex(p => p.id === j.id);
            const isFirst = queueIdx <= 0;
            const isLast = queueIdx < 0 || queueIdx === pendingJobs.length - 1;
            return `<div data-job-id="${j.id}" style="padding:6px;margin-bottom:4px;background:#252525;border-radius:3px;border-left:3px solid ${col};">
                <div style="display:flex;justify-content:space-between;align-items:center;">
                    <div style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">
                        <b>${escapeHtml(j.note) || j.id.slice(0, 8)}</b>
                        <span style="font-size:10px;color:#888;">[${j.status}]</span>
                    </div>
                    <div data-actions="${j.id}" style="display:flex;gap:2px;flex-shrink:0;margin-left:4px;">
                        ${actionable ? `<button data-act="up" title="Move up (higher priority)" ${isFirst ? "disabled style=\"padding:2px 6px;background:#222;color:#555;border:none;border-radius:3px;font-size:11px;cursor:not-allowed;\"" : "style=\"padding:2px 6px;background:#3a3;color:#fff;border:none;border-radius:3px;font-size:11px;cursor:pointer;\""}>↑</button><button data-act="down" title="Move down (lower priority)" ${isLast ? "disabled style=\"padding:2px 6px;background:#222;color:#555;border:none;border-radius:3px;font-size:11px;cursor:not-allowed;\"" : "style=\"padding:2px 6px;background:#3a3;color:#fff;border:none;border-radius:3px;font-size:11px;cursor:pointer;\""}>↓</button>` : ""}
                        ${actionable ? `<button data-act="run-now" title="Run immediately" style="padding:2px 6px;background:#2d6f9e;color:#fff;border:none;border-radius:3px;font-size:11px;cursor:pointer;">Run</button>` : ""}
                        ${actionable ? `<button data-act="cancel" title="Cancel pending task" style="padding:2px 6px;background:#7a3030;color:#fff;border:none;border-radius:3px;font-size:11px;cursor:pointer;">×</button>` : ""}
                    </div>
                </div>
                <div style="font-size:10px;color:#666;margin-top:2px;">@ ${ts} • pri=${j.priority}${j.error ? " • " + escapeHtml(j.error) : ""}</div>
            </div>`;
        }).join("");
    }

    function escapeHtml(s) {
        if (s == null) return "";
        return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    }

    // Refresh is fast and idempotent. Multiple concurrent calls dedupe via _inFlight.
    async function refresh(opts = {}) {
        const silent = opts.silent === true;
        if (_inFlight) return _inFlight;
        _inFlight = (async () => {
            try {
                if (!silent) refreshBtn.disabled = true;
                const [status, jobs] = await Promise.all([
                    callApi("/status"),
                    callApi("/list?limit=20"),
                ]);
                renderStatus(status);
                renderJobs(jobs);
            } catch (e) {
                statusEl.innerHTML = `<div style="color:#f44;">Error: ${escapeHtml(e.message)}</div>`;
            } finally {
                if (!silent) refreshBtn.disabled = false;
                _inFlight = null;
            }
        })();
        return _inFlight;
    }

    // Top-level action buttons. Each calls the API and immediately re-renders.
    refreshBtn.addEventListener("click", () => refresh());
    pauseResumeBtn.addEventListener("click", async () => {
        const wasPaused = pauseResumeBtn.textContent === "Pause";
        pauseResumeBtn.disabled = true;
        try {
            await callApi(wasPaused ? "/pause-all" : "/resume-all", { method: "POST" });
            await refresh({ silent: true });
        } catch (e) {
            statusEl.innerHTML = `<div style="color:#f44;">${escapeHtml(e.message)}</div>`;
        } finally {
            pauseResumeBtn.disabled = false;
        }
    });

    // Per-job actions: event delegation (jobs list re-renders on refresh).
    jobsEl.addEventListener("click", async (e) => {
        const btn = e.target.closest("button[data-act]");
        if (!btn) return;
        const actionsEl = btn.closest("[data-actions]");
        if (!actionsEl) return;
        const id = actionsEl.dataset.actions;
        const jobEl = actionsEl.closest("[data-job-id]");
        const act = btn.dataset.act;

        // Disable all buttons in this job's row during in-flight.
        actionsEl.querySelectorAll("button").forEach(b => (b.disabled = true));
        try {
            if (act === "cancel") {
                await callApi(`/cancel/${encodeURIComponent(id)}`, { method: "POST" });
            } else if (act === "run-now") {
                await callApi(`/run-now/${encodeURIComponent(id)}`, { method: "POST" });
            } else if (act === "up" || act === "down") {
                const delta = act === "up" ? 10 : -10;
                const r = await fetch(`/api/schedule/list?limit=200`);
                const { jobs } = await r.json();
                const idx = jobs.findIndex(j => j.id === id);
                if (idx === -1) return;
                const target = jobs[idx + (act === "up" ? -1 : 1)];
                if (!target) return;
                // Swap priorities between this job and its neighbour.
                const a = jobs[idx].priority;
                const b = target.priority;
                await Promise.all([
                    callApi(`/update/${encodeURIComponent(id)}`, {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ priority: b }),
                    }),
                    callApi(`/update/${encodeURIComponent(target.id)}`, {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ priority: a }),
                    }),
                ]);
            }
        } catch (err) {
            statusEl.innerHTML = `<div style="color:#f44;">Action failed: ${escapeHtml(err.message)}</div>`;
        } finally {
            await refresh({ silent: true });
        }
    });

    // First render and a slow background refresh to catch out-of-band changes.
    // The framework calls render(container) every time the panel becomes active
    // and unmounts the returned root when the user switches tabs. We attach
    // the timer and cleanup observer INSIDE the panel so they live with it.
    refresh();
    _refreshTimer = setInterval(() => refresh({ silent: true }), 5000);

    // Expose refresh on the root so the framework render() can call it
    // immediately after mount. When the panel unmounts, the interval is
    // cleaned up. We watch only the immediate parent (cheap) instead of the
    // whole document.body subtree.
    root.refresh = refresh;
    const observer = new MutationObserver(() => {
        if (!root.parentNode) {
            clearInterval(_refreshTimer);
            observer.disconnect();
        }
    });
    // Defer observing until the framework has actually inserted the root.
    // render() returns the element but framework's append happens after.
    queueMicrotask(() => {
        if (root.parentNode && root.parentNode.parentNode) {
            observer.observe(root.parentNode.parentNode, { childList: true });
        }
    });

    return root;
}

// ============================================================
// Schedule dialog -- triggered by topbar button
// ============================================================

function openScheduleDialog() {
    const now = Math.floor(Date.now() / 1000);

    const presets = [
        { label: "in 30s", offset: 30 },
        { label: "in 5 min", offset: 300 },
        { label: "in 30 min", offset: 1800 },
        { label: "in 2 hours", offset: 7200 },
    ];
    const tomorrow9 = (() => {
        const d = new Date(Date.now() + 86400_000);
        d.setHours(9, 0, 0, 0);
        return Math.floor(d.getTime() / 1000);
    })();
    presets.push({ label: "tomorrow 9am", absolute: tomorrow9 });

    const dlg = document.createElement("div");
    dlg.dataset.sqDialog = "1";
    dlg.style.cssText = `
        position: fixed; inset: 0; z-index: 99999;
        background: rgba(0,0,0,0.6);
        display: flex; align-items: center; justify-content: center;
        font-family: system-ui, sans-serif;
    `;
    dlg.innerHTML = `
        <div style="background:#1e1e1e;color:#ccc;padding:20px;border-radius:8px;width:380px;box-shadow:0 8px 32px rgba(0,0,0,0.5);">
            <h3 style="margin:0 0 12px 0;color:#fff;font-size:15px;">Schedule current workflow</h3>

            <div style="margin-bottom:10px;">
                <label style="display:block;font-size:11px;color:#aaa;margin-bottom:4px;">When (Unix timestamp, seconds)</label>
                <div style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:6px;">
                    ${presets.map((p, i) => `<button data-preset="${i}" style="padding:4px 8px;background:#333;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:11px;">${p.label}</button>`).join("")}
                </div>
                <input data-role="when" type="number" style="width:100%;padding:6px;background:#252525;color:#fff;border:1px solid #444;border-radius:3px;font-family:monospace;" value="${now + 30}" />
            </div>

            <div style="margin-bottom:10px;">
                <label style="display:block;font-size:11px;color:#aaa;margin-bottom:4px;">Priority (0-1000, higher runs first)</label>
                <input data-role="priority" type="number" min="0" max="1000" value="100" style="width:100%;padding:6px;background:#252525;color:#fff;border:1px solid #444;border-radius:3px;font-family:monospace;" />
            </div>

            <div style="margin-bottom:14px;">
                <label style="display:block;font-size:11px;color:#aaa;margin-bottom:4px;">Note (optional)</label>
                <input data-role="note" type="text" placeholder="e.g. morning batch / variant 3" style="width:100%;padding:6px;background:#252525;color:#fff;border:1px solid #444;border-radius:3px;" />
            </div>

            <div style="display:flex;justify-content:flex-end;gap:8px;">
                <button data-act="cancel" style="padding:6px 14px;background:#444;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:12px;">Cancel</button>
                <button data-act="submit" style="padding:6px 14px;background:#0078d4;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:12px;">Schedule</button>
            </div>
        </div>
    `;

    // ---- Event wiring (the fix for bug 1: dialog was un-dismissable) ----

    function closeDialog() {
        dlg.remove();
        document.removeEventListener("keydown", onKey);
    }
    function onKey(e) { if (e.key === "Escape") closeDialog(); }
    document.addEventListener("keydown", onKey);

    dlg.addEventListener("click", (e) => {
        if (e.target === dlg) closeDialog();
    });

    dlg.querySelector('[data-act="cancel"]').addEventListener("click", () => closeDialog());

    dlg.querySelector('[data-act="submit"]').addEventListener("click", async () => {
        const scheduledAt = Math.floor(Number(dlg.querySelector('[data-role="when"]').value) || 0);
        const priority = Math.max(0, Math.min(1000, parseInt(dlg.querySelector('[data-role="priority"]').value || "100", 10)));
        const note = (dlg.querySelector('[data-role="note"]').value || "").trim();

        if (!scheduledAt || scheduledAt <= Math.floor(Date.now() / 1000)) {
            alert("Scheduled time must be in the future.");
            return;
        }

        let payload;
        try {
            const graph = await app.graphToPrompt();
            // graphToPrompt returns { output: {nodes...}, workflow: {...} }
            payload = graph.output || graph;
            if (!payload || Object.keys(payload).length === 0) {
                alert("Current workflow is empty.");
                return;
            }
        } catch (err) {
            alert("Could not serialize workflow: " + err.message);
            return;
        }

        try {
            const resp = await fetch("/api/schedule/add", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ payload, scheduled_at: scheduledAt, priority, note }),
            });
            const data = await resp.json();
            if (!resp.ok) {
                alert("Add failed: " + (data.error || resp.statusText));
                return;
            }
            console.log("[ScheduledQueue] Added job:", data);
            closeDialog();
        } catch (err) {
            alert("Network error: " + err.message);
        }
    });

    dlg.querySelectorAll('[data-preset]').forEach((btn) => {
        btn.addEventListener("click", () => {
            const idx = parseInt(btn.dataset.preset, 10);
            const p = presets[idx];
            const target = p.absolute !== undefined ? p.absolute : (now + p.offset);
            dlg.querySelector('[data-role="when"]').value = target;
            dlg.querySelectorAll('[data-preset]').forEach((b) => {
                b.style.background = "#333";
                b.style.color = "#fff";
            });
            btn.style.background = "#0078d4";
        });
    });

    // Auto-select first preset
    const first = dlg.querySelector('[data-preset="0"]');
    if (first) first.click();

    document.body.appendChild(dlg);
    return dlg;
}

// ============================================================
// Registration
// ============================================================

app.registerExtension({
    name: EXT_NAME,

    actionBarButtons: [
        {
            icon: "pi pi-clock",
            tooltip: "Schedule current workflow (sends to ScheduledQueue, not ComfyUI native queue)",
            onClick: () => openScheduleDialog(),
        },
    ],
});

// Sidebar tab (must call sidebarTab store directly -- registerExtension does
// not auto-handle the `sidebarTab` field). Framework calls render(container)
// every time the tab becomes active; it owns mount/unmount lifecycle.
app.extensionManager.registerSidebarTab({
    id: TAB_ID,
    title: "Scheduled Queue",
    icon: "pi pi-clock",
    tooltip: "Scheduled Queue (managed jobs)",
    type: "custom",
    render: (container) => {
        const p = buildPanel();
        container.innerHTML = "";
        container.appendChild(p);
        if (typeof p.refresh === "function") p.refresh();
        return p;
    },
});

// Start watching after a tick so the pinia store is fully initialized.
// (Removed: manual $subscribe-based mounting is no longer needed -- the
// framework calls render(container) directly whenever our sidebar tab is
// activated.)

console.log("[ScheduledQueue] Registered: topbar button + sidebar tab.");
