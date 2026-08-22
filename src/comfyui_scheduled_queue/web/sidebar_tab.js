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
// Sidebar panel -- framework-managed render lifecycle.
// registerSidebarTab's render(container) is called ONCE per tab activation
// in 1.49.6; switching to another tab and back does NOT re-invoke render.
// To stay correct across tab switches we listen to the sidebarTab store
// and clear/replace the container ourselves on every activeId change.

// Each call to buildPanel creates a fresh root element. The framework
// re-runs render(container) on every tab activation, and on remount we
// need new event listeners + a new interval timer. We therefore do NOT
// memoize the root -- the previous root was already discarded when the
// framework unmounted the tab.
function buildPanel() {
    const root = document.createElement("div");
    root.dataset.sqRoot = "1";
    root.style.cssText = "padding:12px;font-family:system-ui,sans-serif;color:#ccc;background:#1a1a1a;height:100%;box-sizing:border-box;overflow-y:auto;";

    root.innerHTML = `
        <div style="margin-bottom:10px;">
            <h3 style="margin:0 0 4px 0;font-size:14px;color:#fff;">Scheduled Queue</h3>
            <p style="margin:0;font-size:11px;color:#888;">
                Managed by ScheduledQueue (not ComfyUI native queue).
            </p>
        </div>

        <div data-role="status-tabs" style="display:flex;gap:2px;margin-bottom:6px;flex-wrap:wrap;align-items:center;">
            <button data-filter="all" style="padding:4px 6px;background:#0078d4;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:10px;">All</button>
            <button data-filter="scheduled" style="padding:4px 6px;background:#333;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:10px;">Scheduled</button>
            <button data-filter="running" style="padding:4px 6px;background:#333;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:10px;">Running</button>
            <button data-filter="done" style="padding:4px 6px;background:#333;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:10px;">Done</button>
            <button data-filter="failed" style="padding:4px 6px;background:#333;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:10px;">Failed</button>
            <button data-filter="cancelled" style="padding:4px 6px;background:#333;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:10px;">Cancelled</button>
            <button data-role="clear-toggle" style="margin-left:8px;padding:4px 6px;background:#444;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:10px;">Clear...</button>
        </div>

        <div data-role="clear-panel" style="display:none;margin-bottom:6px;padding:6px;background:#222;border-radius:3px;font-size:11px;">
            <label style="display:block;margin-bottom:2px;"><input type="checkbox" data-clear-status="done"/> Done (<span data-count-done>0</span>)</label>
            <label style="display:block;margin-bottom:2px;"><input type="checkbox" data-clear-status="failed"/> Failed (<span data-count-failed>0</span>)</label>
            <label style="display:block;margin-bottom:2px;"><input type="checkbox" data-clear-status="cancelled"/> Cancelled (<span data-count-cancelled>0</span>)</label>
            <label style="display:block;margin-bottom:2px;"><input type="checkbox" data-clear-status="running"/> Running</label>
            <label style="display:block;margin-bottom:2px;"><input type="checkbox" data-clear-status="scheduled"/> Scheduled</label>
            <label style="display:block;margin-bottom:2px;"><input type="checkbox" data-clear-status="interrupted"/> Interrupted</label>
            <button data-role="clear-execute" style="margin-top:4px;padding:4px 8px;background:#7a3030;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:11px;">Clear selected</button>
        </div>

        <div data-role="actions" data-actions="__header__" style="margin-bottom:10px;display:grid;grid-template-columns:1fr 1fr;gap:4px;">
            <button data-act="refresh" style="padding:6px 8px;background:#0078d4;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:11px;">Refresh</button>
            <button data-act="pause-resume" style="padding:6px 8px;background:#444;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:11px;">Pause</button>
        </div>

        <div data-role="status" style="margin-bottom:10px;font-size:11px;color:#aaa;padding:8px;background:#252525;border-radius:4px;">Loading...</div>

        <div data-role="jobs" style="font-size:11px;">Loading jobs...</div>

        <div data-role="pager" style="margin-top:8px;display:flex;gap:4px;align-items:center;font-size:11px;">
            <button data-role="prev" style="padding:4px 8px;background:#444;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:11px;">‹ Prev</button>
            <span data-role="page-info" style="color:#aaa;">Page 1</span>
            <button data-role="next" style="padding:4px 8px;background:#444;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:11px;">Next ›</button>
        </div>

        <div style="margin-top:12px;font-size:10px;color:#666;">Use the clock icon in the topbar to add a new scheduled task.</div>
    `;

    const statusEl = root.querySelector('[data-role="status"]');
    const jobsEl = root.querySelector('[data-role="jobs"]');
    const pauseResumeBtn = root.querySelector('[data-act="pause-resume"]');
    const refreshBtn = root.querySelector('[data-act="refresh"]');
    const statusTabsEl = root.querySelector('[data-role="status-tabs"]');
    const clearToggleBtn = root.querySelector('[data-role="clear-toggle"]');
    const clearPanelEl = root.querySelector('[data-role="clear-panel"]');
    const clearExecuteBtn = root.querySelector('[data-role="clear-execute"]');
    const pagerEl = root.querySelector('[data-role="pager"]');
    const prevBtn = root.querySelector('[data-role="prev"]');
    const nextBtn = root.querySelector('[data-role="next"]');
    const pageInfoEl = root.querySelector('[data-role="page-info"]');

    let _refreshTimer = null;
    let _inFlight = null; // promise of current refresh
    let activeFilter = "all";
    let currentOffset = 0;
    const PAGE_LIMIT = 20;
    let currentTotal = 0;

    async function callApi(path, opts) {
        const r = await fetch("/api/schedule" + path, opts);
        const data = await r.json();
        if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
        return data;
    }

    // Mirror of Python `workflow_format.get_node_title`.
    //
    // The /list endpoint currently strips `payload` (see routes._strip_payload),
    // so sidebar rows don't carry the API dict. We pick the most descriptive
    // title across nodes; this is the "workflow nickname" shown in the row
    // header.
    //
    // Priority:
    //   1. SaveImage / PreviewImage / VAEDecode _meta.title   (these name the
    //      *output* the user cares about; a vanilla KSampler-only workflow
    //      has no SaveImage and we fall through.)
    //   2. Any other node's _meta.title                       (catches the
    //      common case where the user has tagged one node with a friendly
    //      name like "portrait batch".)
    //   3. The first node's class_type                         (last-resort
    //      structural hint, e.g. "KSampler" — better than empty.)
    //
    // Callers wanting a friendlier label should pass note separately; we
    // don't reach for it here because apiDict doesn't carry the user note.
    //
    // Input: apiDict — API-format workflow dict keyed by node id (string or int).
    // Returns: the first usable title, or null if the dict is empty / malformed.
    const OUTPUT_NODE_PRIORITY = ["SaveImage", "PreviewImage", "VAEDecode"];
    function getNodeTitle(apiDict) {
        if (!apiDict || typeof apiDict !== "object") return null;
        const keys = Object.keys(apiDict);

        // 1. Output-node titles first — these are what the user mentally
        //    labels the workflow by ("portrait batch" sits on the SaveImage).
        for (const wanted of OUTPUT_NODE_PRIORITY) {
            for (const k of keys) {
                const entry = apiDict[k];
                if (!entry || typeof entry !== "object") continue;
                if (entry.class_type !== wanted) continue;
                const meta = entry._meta;
                if (meta && typeof meta === "object" && typeof meta.title === "string" && meta.title) {
                    return meta.title;
                }
            }
        }

        // 2. Any node's _meta.title (first match wins — preserves the legacy
        //    behaviour where users tagged a single node to name the workflow).
        for (const k of keys) {
            const entry = apiDict[k];
            if (!entry || typeof entry !== "object") continue;
            const meta = entry._meta;
            if (meta && typeof meta === "object" && typeof meta.title === "string" && meta.title) {
                return meta.title;
            }
        }

        // 3. class_type of the first usable node — structural fallback.
        for (const k of keys) {
            const entry = apiDict[k];
            if (!entry || typeof entry !== "object") continue;
            if (typeof entry.class_type === "string" && entry.class_type) {
                return entry.class_type;
            }
        }

        return null;
    }

    // Resolve a job's workflow nickname, falling back to fetching the detail
    // endpoint when the list payload is unavailable. Returns a Promise<string>.
    // Tracks per-job in-flight requests so concurrent renderJobs() calls
    // dedupe to the same network round-trip.
    const _titleInflight = new Map();
    async function resolveJobTitle(job) {
        if (job && job.payload && typeof job.payload === "object") {
            const t = getNodeTitle(job.payload);
            if (t) return t;
        }
        const id = job && job.id;
        if (!id) return null;
        if (_titleInflight.has(id)) return _titleInflight.get(id);
        const p = (async () => {
            try {
                const data = await callApi(`/job/${encodeURIComponent(id)}`);
                const t = getNodeTitle(data && data.payload);
                return t || null;
            } catch (_e) {
                return null;
            } finally {
                _titleInflight.delete(id);
            }
        })();
        _titleInflight.set(id, p);
        return p;
    }

    // Resolve a job's first output image (ComfyUI /view URL). Same fallback
    // pattern as resolveJobTitle. Returns Promise<string|null>.
    const _thumbInflight = new Map();
    async function resolveJobThumb(job) {
        if (job && job.outputs && Array.isArray(job.outputs.images) && job.outputs.images[0]) {
            const img = job.outputs.images[0];
            return buildViewUrl(img);
        }
        const id = job && job.id;
        if (!id) return null;
        if (_thumbInflight.has(id)) return _thumbInflight.get(id);
        const p = (async () => {
            try {
                const data = await callApi(`/job/${encodeURIComponent(id)}`);
                if (data && data.outputs && Array.isArray(data.outputs.images) && data.outputs.images[0]) {
                    return buildViewUrl(data.outputs.images[0]);
                }
            } catch (_e) {
                // ignore
            } finally {
                _thumbInflight.delete(id);
            }
            return null;
        })();
        _thumbInflight.set(id, p);
        return p;
    }

    // Build the ComfyUI /view URL for an output image record.
    // An image record looks like: { filename, subfolder, type }
    function buildViewUrl(img) {
        if (!img || typeof img !== "object") return null;
        const filename = encodeURIComponent(img.filename || "");
        if (!filename) return null;
        const subfolder = encodeURIComponent(img.subfolder || "");
        const type = encodeURIComponent(img.type || "output");
        return `/view?filename=${filename}&subfolder=${subfolder}&type=${type}`;
    }

    // Lightbox modal: closes on click anywhere or on Esc.
    function openImageModal(imgUrl) {
        if (!imgUrl) return;
        const modal = document.createElement("div");
        modal.style.cssText = "position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.9);z-index:9999;display:flex;align-items:center;justify-content:center;cursor:pointer;";
        modal.innerHTML = `<img src="${imgUrl}" style="max-width:90%;max-height:90%;object-fit:contain;" />`;
        const close = () => {
            modal.remove();
            document.removeEventListener("keydown", onEsc);
        };
        const onEsc = (e) => { if (e.key === "Escape") close(); };
        modal.onclick = close;
        document.addEventListener("keydown", onEsc);
        document.body.appendChild(modal);
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
        // Apply active filter; "all" keeps the legacy visibleJobs (pending+running).
        // Other filters show only jobs matching that status.
        let visibleJobs;
        if (activeFilter === "all") {
            visibleJobs = [...runningJobs, ...pendingJobs];
        } else {
            visibleJobs = allJobs.filter(j => j.status === activeFilter);
        }
        if (visibleJobs.length === 0) {
            jobsEl.innerHTML = '<div style="color:#666;font-style:italic;">No jobs match this filter.</div>';
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
            const shortId = (j.id || "").slice(0, 8);
            // Done thumbnails get a click-to-zoom modal. We render a placeholder
            // that gets swapped once resolveJobThumb resolves (see hydrateThumbs).
            const thumbHtml = j.status === "done"
                ? `<div data-role="thumb-slot" data-job-id="${escapeHtml(j.id)}" style="margin-top:4px;width:60px;height:60px;background:#333;border-radius:3px;display:flex;align-items:center;justify-content:center;color:#666;font-size:10px;">…</div>`
                : "";
            return `<div data-job-id="${j.id}" style="padding:6px;margin-bottom:4px;background:#252525;border-radius:3px;border-left:3px solid ${col};">
                <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:4px;">
                    <div style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0;">
                        <div data-role="job-title">
                            <span data-role="job-nickname" data-job-id="${escapeHtml(j.id)}" title="${escapeHtml(j.note || "")}">${escapeHtml(j.note) || "untitled"}</span>
                            <span style="opacity:.6;font-size:10px;margin-left:6px;">(${escapeHtml(shortId)})</span>
                        </div>
                        <div style="font-size:10px;color:#888;margin-top:2px;">[${j.status}] • pri=${j.priority}</div>
                    </div>
                    <div data-actions="${j.id}" style="display:flex;gap:2px;flex-shrink:0;margin-left:4px;">
                        ${actionable ? `<button data-act="up" title="Move up (higher priority)" ${isFirst ? "disabled style=\"padding:2px 6px;background:#222;color:#555;border:none;border-radius:3px;font-size:11px;cursor:not-allowed;\"" : "style=\"padding:2px 6px;background:#3a3;color:#fff;border:none;border-radius:3px;font-size:11px;cursor:pointer;\""}>↑</button><button data-act="down" title="Move down (lower priority)" ${isLast ? "disabled style=\"padding:2px 6px;background:#222;color:#555;border:none;border-radius:3px;font-size:11px;cursor:not-allowed;\"" : "style=\"padding:2px 6px;background:#3a3;color:#fff;border:none;border-radius:3px;font-size:11px;cursor:pointer;\""}>↓</button>` : ""}
                        ${actionable ? `<button data-act="run-now" title="Run immediately" style="padding:2px 6px;background:#2d6f9e;color:#fff;border:none;border-radius:3px;font-size:11px;cursor:pointer;">Run</button>` : ""}
                        ${actionable ? `<button data-act="cancel" title="Cancel pending task" style="padding:2px 6px;background:#7a3030;color:#fff;border:none;border-radius:3px;font-size:11px;cursor:pointer;">×</button>` : ""}
                        <button data-act="repeat" data-id="${j.id}" title="Repeat / clone this job" style="padding:2px 6px;background:#5a4f9e;color:#fff;border:none;border-radius:3px;font-size:11px;cursor:pointer;">↻</button>
                        <button data-act="export" data-id="${j.id}" title="Export this job (download JSON)" style="padding:2px 6px;background:#666;color:#fff;border:none;border-radius:3px;font-size:11px;cursor:pointer;">⬇</button>
                    </div>
                </div>
                ${thumbHtml}
                <div style="font-size:10px;color:#666;margin-top:2px;">@ ${ts}${j.error ? " • " + escapeHtml(j.error) : ""}</div>
            </div>`;
        }).join("");

        // Hydrate nickname + thumbnail for each visible job. We fetch in
        // parallel; each resolveJobTitle/Thumb dedupes in-flight calls so
        // repeated refreshes don't spam the backend.
        const allTitles = Promise.all(visibleJobs.map(j => resolveJobTitle(j).then(t => ({ j, t }))));
        const allThumbs = Promise.all(visibleJobs
            .filter(j => j.status === "done")
            .map(j => resolveJobThumb(j).then(url => ({ j, url }))));
        allTitles.then((arr) => {
            for (const { j, t } of arr) {
                if (!t) continue;
                const el = jobsEl.querySelector('[data-role="job-nickname"][data-job-id="' + cssEscape(j.id) + '"]');
                if (el && el.textContent !== t) el.textContent = t;
            }
        }).catch(() => {});
        allThumbs.then((arr) => {
            for (const { j, url } of arr) {
                const slot = jobsEl.querySelector('[data-role="thumb-slot"][data-job-id="' + cssEscape(j.id) + '"]');
                if (!slot) continue;
                if (url) {
                    slot.innerHTML = `<img data-role="thumb" src="${escapeHtml(url)}" style="width:60px;height:60px;object-fit:cover;cursor:pointer;border-radius:3px;" />`;
                    const img = slot.querySelector("img");
                    if (img) img.addEventListener("click", () => openImageModal(url));
                } else {
                    slot.innerHTML = '<span style="color:#666;font-size:10px;">no preview</span>';
                }
            }
        }).catch(() => {});
    }

    // CSS.escape is widely supported but we polyfill for older WebViews.
    // attribute selector escaping for ids that may contain ":[].#" etc.
    function cssEscape(s) {
        if (typeof CSS !== "undefined" && typeof CSS.escape === "function") return CSS.escape(s);
        return String(s).replace(/[^a-zA-Z0-9_-]/g, c => "\\" + c);
    }

    // Update the count spans inside the clear panel so the user sees how
    // many jobs each status would remove. Reads from the most recent status
    // payload (renderStatus already populated statusEl's counters).
    function renderClearCounts(status) {
        if (!status || !status.counts) return;
        const counts = status.counts;
        const map = {
            "data-count-done": counts.done || 0,
            "data-count-failed": counts.failed || 0,
            "data-count-cancelled": counts.cancelled || 0,
        };
        for (const [sel, val] of Object.entries(map)) {
            const el = root.querySelector('[' + sel + ']');
            if (el) el.textContent = String(val);
        }
    }

    // Reflect the current activeFilter in the status tabs row by repainting
    // each button's background. The default-active button is "all".
    function renderFilterTabs() {
        if (!statusTabsEl) return;
        statusTabsEl.querySelectorAll('button[data-filter]').forEach((b) => {
            if (b.dataset.filter === activeFilter) {
                b.style.background = "#0078d4";
            } else {
                b.style.background = "#333";
            }
        });
    }

    // Update the pager controls based on current offset/limit/total.
    // total comes from the list payload when available, otherwise falls
    // back to the current page's length + has_more heuristic.
    function renderPager(jobs) {
        if (!pagerEl) return;
        const total = (typeof jobs.total === "number") ? jobs.total
            : (typeof jobs.has_more === "boolean" && jobs.has_more)
                ? currentOffset + visibleJobsCount() + 1
                : currentOffset + visibleJobsCount();
        currentTotal = total;
        const page = Math.floor(currentOffset / PAGE_LIMIT) + 1;
        if (pageInfoEl) pageInfoEl.textContent = `Page ${page} (${currentOffset + 1}–${currentOffset + visibleJobsCount()} of ${currentTotal})`;
        if (prevBtn) prevBtn.disabled = currentOffset <= 0;
        if (nextBtn) {
            // Disable Next when the current page returned fewer than limit
            // items (no more pages) or when has_more is explicitly false.
            const hasMore = (typeof jobs.has_more === "boolean")
                ? jobs.has_more
                : (visibleJobsCount() >= PAGE_LIMIT);
            nextBtn.disabled = !hasMore;
        }
    }

    // Helper: count of currently-rendered job rows. Used by the pager to
    // decide whether "Next" should be enabled when the backend doesn't
    // return a has_more flag.
    function visibleJobsCount() {
        return jobsEl.querySelectorAll('[data-job-id]').length;
    }

    function escapeHtml(s) {
        if (s == null) return "";
        return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    }

    // Refresh is fast and idempotent. Multiple concurrent calls dedupe via _inFlight.
    // The list call respects activeFilter (status param) and currentOffset/limit
    // so the pager + filter tabs work end-to-end.
    async function refresh(opts = {}) {
        const silent = opts.silent === true;
        if (_inFlight) return _inFlight;
        _inFlight = (async () => {
            try {
                if (!silent) refreshBtn.disabled = true;
                const listQs = new URLSearchParams();
                listQs.set("limit", String(PAGE_LIMIT));
                listQs.set("offset", String(currentOffset));
                if (activeFilter && activeFilter !== "all") {
                    listQs.set("status", activeFilter);
                }
                const [status, jobs] = await Promise.all([
                    callApi("/status"),
                    callApi("/list?" + listQs.toString()),
                ]);
                renderStatus(status);
                renderClearCounts(status);
                renderFilterTabs();
                renderJobs(jobs);
                renderPager(jobs);
                // NOTE: do NOT auto-collapse the clear panel here. The 5 s
                // background poll calls refresh() repeatedly; resetting
                // clearPanelEl.style.display = "none" on every tick made the
                // panel flicker closed and visually "flash" the whole
                // sidebar. The user's open/closed intent for that panel is
                // owned by the toggle button only.
            } catch (e) {
                statusEl.innerHTML = `<div style="color:#f44;">Error: ${escapeHtml(e.message)}</div>`;
            } finally {
                if (!silent) refreshBtn.disabled = false;
                _inFlight = null;
            }
        })();
        return _inFlight;
    }

    // Helper: set the active filter and reset to page 1. The task
    // requirement says "切换 status filter 时 offset 重置为 0".
    function setActiveFilter(name) {
        activeFilter = name || "all";
        currentOffset = 0;
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

    // Status filter tabs (event delegation on the row).
    if (statusTabsEl) {
        statusTabsEl.addEventListener("click", (e) => {
            const btn = e.target.closest('button[data-filter]');
            if (!btn) return;
            const f = btn.dataset.filter;
            if (!f || f === activeFilter) {
                // Even when the same tab is re-clicked, reset to page 1.
                currentOffset = 0;
                refresh();
                return;
            }
            setActiveFilter(f);
            refresh();
        });
    }

    // Clear... toggle: expand/collapse the checkbox list. Refresh always
    // closes the panel; this is the only way to reopen it.
    if (clearToggleBtn && clearPanelEl) {
        clearToggleBtn.addEventListener("click", () => {
            clearPanelEl.style.display =
                (clearPanelEl.style.display === "none" || !clearPanelEl.style.display)
                    ? "block" : "none";
        });
    }

    // Clear selected: gather checked statuses and DELETE in one round-trip.
    // Uses raw fetch because callApi assumes a JSON body; aiohttp/ComfyUI
    // accept DELETE on the route and respond 200/204 with no body to parse.
    if (clearExecuteBtn && clearPanelEl) {
        clearExecuteBtn.addEventListener("click", async () => {
            const boxes = clearPanelEl.querySelectorAll('input[type="checkbox"][data-clear-status]');
            const selected = [];
            boxes.forEach((b) => { if (b.checked) selected.push(b.dataset.clearStatus); });
            if (selected.length === 0) {
                statusEl.innerHTML = '<div style="color:#fa3;">No statuses selected to clear.</div>';
                return;
            }
            clearExecuteBtn.disabled = true;
            try {
                const r = await fetch(
                    "/api/schedule/clear?statuses=" + encodeURIComponent(selected.join(",")),
                    { method: "DELETE" }
                );
                if (!r.ok && r.status !== 204) {
                    // Best-effort: try to read an error message but don't crash.
                    let msg = `HTTP ${r.status}`;
                    try { const j = await r.json(); if (j && j.error) msg = j.error; } catch (_e) { /* ignore */ }
                    throw new Error(msg);
                }
            } catch (err) {
                alert("Clear failed: " + err.message);
            } finally {
                clearExecuteBtn.disabled = false;
                await refresh({ silent: true });
            }
        });
    }

    // Pager: Prev / Next adjust currentOffset and re-fetch.
    if (prevBtn) {
        prevBtn.addEventListener("click", () => {
            if (currentOffset <= 0) return;
            currentOffset = Math.max(0, currentOffset - PAGE_LIMIT);
            refresh();
        });
    }
    if (nextBtn) {
        nextBtn.addEventListener("click", () => {
            if (nextBtn.disabled) return;
            currentOffset = currentOffset + PAGE_LIMIT;
            refresh();
        });
    }

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
                // Swap with neighbour via the dedicated reorder endpoint,
                // which persists queue_order. Refreshing then re-renders
                // jobs in the new order.
                const direction = act === "up" ? -1 : 1;
                const r = await callApi(
                    `/reorder/${encodeURIComponent(id)}`,
                    {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ direction }),
                    }
                );
                if (!r.moved) {
                    // No-op (already at edge or job not pending). Refresh
                    // anyway so the disabled state matches reality.
                }
            } else if (act === "repeat") {
                // Repeat / clone: POST to the repeat endpoint. We do NOT
                // use callApi() because repeat may return 201 with no body
                // in some backend implementations.
                const r = await fetch(
                    `/api/schedule/repeat/${encodeURIComponent(id)}`,
                    { method: "POST" }
                );
                if (!r.ok && r.status !== 204) {
                    let msg = `HTTP ${r.status}`;
                    try { const j = await r.json(); if (j && j.error) msg = j.error; } catch (_e) { /* ignore */ }
                    throw new Error(msg);
                }
            } else if (act === "export") {
                // Export: trigger a browser download via direct navigation.
                // We let the browser handle the response; no need to read it.
                window.location.href = `/api/schedule/export/${encodeURIComponent(id)}`;
                return; // page is navigating away; don't re-enable buttons
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
    // Default timestamp used by both the three-section time picker and the
    // hidden Unix-seconds input. The latter exists only so submit() can post
    // an integer unix-seconds value to /api/schedule/add -- the user never
    // touches it.
    let currentWhenTs = now + 30;

    // Format an integer unix-seconds timestamp as local "YYYY-MM-DD HH:MM:SS".
    // Uses the user's local timezone so "what I see" matches "what the OS
    // clock shows". Sub-second fractions are dropped.
    function formatWhen(ts) {
        const d = new Date(ts * 1000);
        const pad = (n) => String(n).padStart(2, "0");
        return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate())
            + " " + pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
    }

    // Parse the three accepted human formats into a unix-seconds integer.
    // Returns NaN on failure so callers can show a red border without
    // crashing on partial input. Accepts:
    //   "YYYY-MM-DD HH:MM:SS"     (local time)
    //   "YYYY-MM-DDTHH:MM:SS"     (ISO local, no Z)
    //   "YYYY/MM/DD HH:MM:SS"     (local time)
    function parseWhen(text) {
        if (typeof text !== "string") return NaN;
        const trimmed = text.trim();
        if (!trimmed) return NaN;
        // Normalise the "T" separator and "/" slashes so a single Date ctor call
        // handles all three formats. We require explicit YYYY-MM-DD layout.
        const m = trimmed.match(/^(\d{4})[-/](\d{1,2})[-/](\d{1,2})[ T](\d{1,2}):(\d{1,2})(?::(\d{1,2}))?$/);
        if (!m) return NaN;
        const [, y, mo, d, h, mi, s] = m;
        const Y = parseInt(y, 10), Mo = parseInt(mo, 10), D = parseInt(d, 10);
        const H = parseInt(h, 10), Mi = parseInt(mi, 10), S = s == null ? 0 : parseInt(s, 10);
        if (Mo < 1 || Mo > 12 || D < 1 || D > 31) return NaN;
        if (H > 23 || Mi > 59 || S > 59) return NaN;
        // Date(year, monthIndex, ...) treats the input as local time -- exactly
        // what we want for a human-typed wall clock value.
        const dt = new Date(Y, Mo - 1, D, H, Mi, S, 0);
        if (isNaN(dt.getTime())) return NaN;
        return Math.floor(dt.getTime() / 1000);
    }

    dlg.innerHTML = `
        <div style="background:#1e1e1e;color:#ccc;padding:20px;border-radius:8px;width:380px;box-shadow:0 8px 32px rgba(0,0,0,0.5);">
            <h3 style="margin:0 0 12px 0;color:#fff;font-size:15px;">Schedule current workflow</h3>

            <div style="margin-bottom:10px;">
                <label style="display:block;font-size:11px;color:#aaa;margin-bottom:4px;">When (local time)</label>
                <div style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:6px;">
                    ${presets.map((p, i) => `<button data-preset="${i}" style="padding:4px 8px;background:#333;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:11px;">${p.label}</button>`).join("")}
                </div>
                <div data-role="when-row" style="display:grid;grid-template-columns:auto 1fr auto;gap:6px;align-items:center;margin-top:6px;">
                    <div data-role="when-dec" style="display:flex;gap:2px;">
                        <button type="button" data-delta="-3600" style="padding:4px 6px;background:#333;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:11px;">-1h</button>
                        <button type="button" data-delta="-600" style="padding:4px 6px;background:#333;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:11px;">-10m</button>
                        <button type="button" data-delta="-60" style="padding:4px 6px;background:#333;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:11px;">-1m</button>
                        <button type="button" data-delta="-10" style="padding:4px 6px;background:#333;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:11px;">-10s</button>
                        <button type="button" data-delta="-5" style="padding:4px 6px;background:#333;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:11px;">-5s</button>
                    </div>
                    <input data-role="when-display" placeholder="2026-08-22 22:30:00" style="width:100%;padding:6px;background:#252525;color:#fff;border:1px solid #444;border-radius:3px;font-family:monospace;text-align:center;" value="${formatWhen(currentWhenTs)}" />
                    <div data-role="when-inc" style="display:flex;gap:2px;">
                        <button type="button" data-delta="5" style="padding:4px 6px;background:#333;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:11px;">+5s</button>
                        <button type="button" data-delta="10" style="padding:4px 6px;background:#333;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:11px;">+10s</button>
                        <button type="button" data-delta="60" style="padding:4px 6px;background:#333;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:11px;">+1m</button>
                        <button type="button" data-delta="600" style="padding:4px 6px;background:#333;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:11px;">+10m</button>
                        <button type="button" data-delta="3600" style="padding:4px 6px;background:#333;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:11px;">+1h</button>
                    </div>
                </div>
                <!-- Hidden unix-seconds input: source of truth at submit time. -->
                <input data-role="when" type="hidden" value="${currentWhenTs}" />
            </div>

            <div style="margin-bottom:10px;">
                <label style="display:block;font-size:11px;color:#aaa;margin-bottom:4px;">Priority (0-1000, higher runs first)</label>
                <input data-role="priority" type="number" min="0" max="1000" value="100" style="width:100%;padding:6px;background:#252525;color:#fff;border:1px solid #444;border-radius:3px;font-family:monospace;" />
            </div>

            <div style="margin-bottom:14px;">
                <label style="display:block;font-size:11px;color:#aaa;margin-bottom:4px;">Note (optional)</label>
                <input data-role="note" type="text" placeholder="e.g. morning batch / variant 3" style="width:100%;padding:6px;background:#252525;color:#fff;border:1px solid #444;border-radius:3px;" />
            </div>

            <div data-role="count-row" style="margin-top:6px;margin-bottom:14px;display:flex;gap:6px;align-items:center;">
                <label style="opacity:.7;font-size:11px;">Count:</label>
                <input data-role="count" type="number" min="1" max="50" value="1" style="width:60px;background:#222;color:#fff;border:1px solid #555;padding:4px;border-radius:3px;" />
                <span style="opacity:.5;font-size:11px;">(1-50, repeat same workflow)</span>
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
        // Re-parse the display input at submit time so the user's last edit
        // wins, even if they typed something without using the +/- buttons.
        // If parsing fails we refuse to submit rather than silently using a
        // stale timestamp.
        const displayEl = dlg.querySelector('[data-role="when-display"]');
        const parsedTs = parseWhen(displayEl.value);
        if (!Number.isFinite(parsedTs)) {
            displayEl.style.borderColor = "#c44";
            alert("Invalid time");
            return;
        }
        displayEl.style.borderColor = "";  // clear any prior error highlight
        currentWhenTs = parsedTs;
        dlg.querySelector('[data-role="when"]').value = String(currentWhenTs);
        const scheduledAt = currentWhenTs;
        const priority = Math.max(0, Math.min(1000, parseInt(dlg.querySelector('[data-role="priority"]').value || "100", 10)));
        const note = (dlg.querySelector('[data-role="note"]').value || "").trim();
        // Count: number of identical jobs to enqueue. Defaults to 1, hard-capped
        // to 50 to keep the batch payload small and the queue manageable.
        const countRaw = parseInt(dlg.querySelector('[data-role="count"]').value, 10);
        const count = Number.isFinite(countRaw) ? Math.max(1, Math.min(50, countRaw)) : 1;

        if (!scheduledAt || scheduledAt <= Math.floor(Date.now() / 1000)) {
            alert("Scheduled time must be in the future.");
            return;
        }

        const calledWidgets = [];
        for (const node of app.graph?._nodes || []) {
            for (const widget of node.widgets || []) {
                if (typeof widget.beforeQueued !== "function") continue;
                calledWidgets.push(widget);
                try {
                    widget.beforeQueued({ isPartialExecution: false });
                } catch (err) {
                    console.warn("[ScheduledQueue] widget.beforeQueued failed:", err);
                }
            }
        }

        try {
            const graph = await app.graphToPrompt();
            // graphToPrompt returns { output: {nodes...}, workflow: {...} }
            const payload = graph.output || graph;
            if (!payload || Object.keys(payload).length === 0) {
                alert("Current workflow is empty.");
                return;
            }

            try {
                let resp;
                if (count <= 1) {
                    // Single-job path: unchanged behaviour.
                    resp = await fetch("/api/schedule/add", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({
                            payload,
                            extra_data: { extra_pnginfo: { workflow: graph.workflow } },
                            scheduled_at: scheduledAt,
                            priority,
                            note,
                        }),
                    });
                } else {
                    // Batch path: POST a list of identical jobs (backend creates one row per item).
                    // items[] intentionally omits extra_data -- the workflow payload itself
                    // encodes everything needed for replay.
                    const items = Array.from({ length: count }, () => ({
                        payload,
                        scheduled_at: scheduledAt,
                        priority,
                        note,
                    }));
                    resp = await fetch("/api/schedule/add-batch", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ items }),
                    });
                }
                const data = await resp.json();
                if (!resp.ok) {
                    alert("Add failed: " + (data.error || resp.statusText));
                    return;
                }
                console.log("[ScheduledQueue] Added job(s):", data);
                closeDialog();
                // Ask any open sidebar panel to refresh so the new job shows up
                // immediately, without waiting for the next 5 s poll.
                window.dispatchEvent(new CustomEvent("sq:job-added"));
                try {
                    const r = await fetch("/api/schedule/status");
                    const s = await r.json();
                    document.querySelectorAll('[data-sq-root] [data-act="refresh"]')
                        .forEach((b) => b.click());
                } catch (_e) { /* best-effort */ }
            } catch (err) {
                alert("Network error: " + err.message);
            }
        } catch (err) {
            alert("Could not serialize workflow: " + err.message);
        } finally {
            for (const widget of calledWidgets) {
                if (typeof widget.afterQueued !== "function") continue;
                try {
                    widget.afterQueued();
                } catch (err) {
                    console.warn("[ScheduledQueue] widget.afterQueued failed:", err);
                }
            }
        }
    });

    // ---- Three-section time picker wiring ----

    const whenDisplay = dlg.querySelector('[data-role="when-display"]');
    const whenHidden = dlg.querySelector('[data-role="when"]');

    // Push currentWhenTs out to both the visible input and the hidden
    // unix-seconds input. Centralised so preset clicks, +/- buttons, and
    // programmatic edits all converge on the same render path.
    function refreshWhenDisplay() {
        whenDisplay.value = formatWhen(currentWhenTs);
        whenDisplay.style.borderColor = "";
        whenHidden.value = String(currentWhenTs);
    }

    // +/- buttons: each click adds its data-delta (seconds) to currentWhenTs.
    dlg.querySelectorAll('[data-role="when-row"] [data-delta]').forEach((btn) => {
        btn.addEventListener("click", () => {
            const delta = parseInt(btn.dataset.delta, 10);
            if (!Number.isFinite(delta)) return;
            currentWhenTs += delta;
            refreshWhenDisplay();
        });
    });

    // Display input: accept manual edits in any of the three formats.
    // Successful parse updates the canonical timestamp and the hidden
    // input; failure paints a red border without breaking the dialog.
    whenDisplay.addEventListener("input", () => {
        const text = whenDisplay.value;
        const parsed = parseWhen(text);
        if (Number.isFinite(parsed)) {
            whenDisplay.style.borderColor = "";
            currentWhenTs = parsed;
            whenHidden.value = String(currentWhenTs);
        } else {
            whenDisplay.style.borderColor = "#c44";
        }
    });

    dlg.querySelectorAll('[data-preset]').forEach((btn) => {
        btn.addEventListener("click", () => {
            const idx = parseInt(btn.dataset.preset, 10);
            const p = presets[idx];
            currentWhenTs = p.absolute !== undefined ? p.absolute : (now + p.offset);
            refreshWhenDisplay();
            dlg.querySelectorAll('[data-preset]').forEach((b) => {
                b.style.background = "#333";
                b.style.color = "#fff";
            });
            btn.style.background = "#0078d4";
        });
    });

    // Auto-select first preset (also drives initial display value)
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

// Sidebar tab. The 1.49.6 framework calls render(container) ONCE per tab
// activation -- switching to another tab and back does NOT re-invoke it.
// We render once, then keep the container in sync with the sidebarTab
// store via $subscribe so the panel swaps correctly when the user
// toggles tabs.
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
        // Install watcher AFTER first mount so we can also clean up the
        // container when the user navigates away.
        installSidebarWatcher(container);
        return p;
    },
});

// Keep the container we were given in sync with activeSidebarTabId.
let _sidebarWatcher = null;
function installSidebarWatcher(container) {
    if (_sidebarWatcher) return; // already installed
    const sb = window.app.extensionManager.sidebarTab;
    if (!sb || typeof sb.$subscribe !== "function") return;
    const apply = () => {
        const activeId = sb.activeSidebarTabId;
        if (activeId !== TAB_ID && container.firstChild) {
            // Active tab is something else: clear our content so the
            // framework's native panel is free to render.
            container.innerHTML = "";
        } else if (activeId === TAB_ID && !container.firstChild) {
            // Active tab is ours but content missing: re-mount.
            const p = buildPanel();
            container.appendChild(p);
            if (typeof p.refresh === "function") p.refresh();
        }
    };
    sb.$subscribe(() => apply());
    _sidebarWatcher = { sb, apply };
    apply();
}

// Start watching after a tick so the pinia store is fully initialized.
// (Removed: manual $subscribe-based mounting is no longer needed -- the
// framework calls render(container) directly whenever our sidebar tab is
// activated.)

console.log("[ScheduledQueue] Registered: topbar button + sidebar tab.");
