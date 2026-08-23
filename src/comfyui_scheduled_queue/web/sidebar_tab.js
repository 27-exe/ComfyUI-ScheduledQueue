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

// ===========================================================
// i18n -- no real i18n framework in a ComfyUI JS extension, so we
// inline a tiny lookup table driven by ./locales/{zh,en}.json.
// The JSON files are fetched at module init; if a fetch fails we
// fall back to these critical strings so registration and first paint
// never expose raw i18n keys.
// ===========================================================

const LS_LANG_KEY = "sq.lang";

function detectInitialLang() {
    try {
        const stored = window.localStorage?.getItem(LS_LANG_KEY);
        if (stored === "zh" || stored === "en") return stored;
    } catch (_e) { /* localStorage may be blocked */ }
    const nav = (typeof navigator !== "undefined" && navigator.language) || "zh";
    return nav.toLowerCase().startsWith("en") ? "en" : "zh";
}

let LANG = detectInitialLang();
const I18N = { zh: {}, en: {} };
const BUILTIN_FALLBACKS = {
    "sidebar.lang.zh": "中",
    "sidebar.lang.en": "EN",
    "sidebar.lang.switch_to_zh": "Switch to Chinese",
    "sidebar.lang.switch_to_en": "Switch to English",
    "filter.all": "All",
    "filter.scheduled": "Scheduled",
    "filter.running": "Running",
    "filter.done": "Done",
    "filter.failed": "Failed",
    "filter.cancelled": "Cancelled",
    "filter.interrupted": "Interrupted",
    "filter.dispatched": "Dispatched",
    "sidebar.title": "Scheduled Queue",
    "sidebar.refresh": "Refresh",
    "sidebar.pause": "Pause",
    "sidebar.resume": "Resume",
    "status_bar.paused": "Paused",
    "status_bar.paused_yes": "Yes",
    "status_bar.paused_no": "No",
    "status_bar.label.sched": "Scheduled",
    "status_bar.label.run": "Running",
    "status_bar.label.int": "Interrupted",
    "status_bar.label.done": "Done",
    "status_bar.label.fail": "Failed",
    "status_bar.label.cncl": "Cancelled",
    "topbar.schedule_tooltip": "Schedule current workflow",
};

// Asynchronous locale bootstrap: load both JSON files in parallel
// and update I18N when ready. Until they resolve, t() will fall
// back to its `fallback` arg (or the key). UI strings render on
// tab activation -- by then the fetch is usually done, but we MUST
// guard against the race so the first paint doesn't show raw keys.
async function loadLocales() {
    const tryLoad = async (lng) => {
        try {
            const r = await fetch(`/extensions/ComfyUI-ScheduledQueue/locales/${lng}.json`, { cache: "no-cache" });
            if (!r.ok) throw new Error(`HTTP ${r.status}`);
            I18N[lng] = await r.json();
        } catch (e) {
            console.warn(`[ScheduledQueue] Failed to load ${lng} locale; using built-in fallbacks.`, e);
            throw e;
        }
    };
    await Promise.allSettled([tryLoad("zh"), tryLoad("en")]);
}

function setLang(lng) {
    if (lng !== "zh" && lng !== "en") return;
    if (LANG === lng) return;
    LANG = lng;
    try { window.localStorage?.setItem(LS_LANG_KEY, lng); } catch (_e) { /* ignore */ }
    // Notify all open panels to re-render with the new language.
    window.dispatchEvent(new CustomEvent("sq:lang-changed", { detail: { lang: lng } }));
}

// `t(key, fallback)` -- look up a translation. Resolution order:
//   1. Current language's I18N dict
//   2. Chinese (zh) fallback dict
//   3. The explicit `fallback` arg
//   4. The key string itself (last resort, makes missing keys visible)
function t(key, fallback) {
    if (I18N[LANG] && Object.prototype.hasOwnProperty.call(I18N[LANG], key)) {
        return I18N[LANG][key];
    }
    if (I18N.zh && Object.prototype.hasOwnProperty.call(I18N.zh, key)) {
        return I18N.zh[key];
    }
    if (Object.prototype.hasOwnProperty.call(BUILTIN_FALLBACKS, key)) {
        return BUILTIN_FALLBACKS[key];
    }
    return fallback != null ? fallback : key;
}

// Format a template string like "Page {0} ({1}-{2} of {3})" by
// replacing {N} placeholders with positional args. Missing indices
// are left in place so a translator sees the gap during QA.
function tfmt(key, fallback, args) {
    const tmpl = t(key, fallback);
    if (!Array.isArray(args)) return tmpl;
    return tmpl.replace(/\{(\d+)\}/g, (m, idx) => {
        const i = parseInt(idx, 10);
        return (i >= 0 && i < args.length) ? String(args[i]) : m;
    });
}

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

    // i18n: pull the current lang + the strings at render time so a
    // language switch (sq:lang-changed) can re-render with the new set.
    const langBtnOther = LANG === "zh" ? "en" : "zh";
    const langBtnLabel = t(`sidebar.lang.${langBtnOther}`);
    const langBtnTitle = t(`sidebar.lang.switch_to_${langBtnOther}`);

    root.innerHTML = `
        <div style="margin-bottom:10px;display:flex;justify-content:space-between;align-items:center;gap:6px;">
            <h3 style="margin:0 0 4px 0;font-size:14px;color:#fff;flex:1;min-width:0;">${escapeHtml(t("sidebar.title"))}</h3>
            <button data-role="lang-switch" title="${escapeHtml(langBtnTitle)}" style="flex-shrink:0;padding:2px 6px;background:#333;color:#fff;border:1px solid #555;border-radius:3px;cursor:pointer;font-size:10px;font-weight:600;">${escapeHtml(langBtnLabel)}</button>
        </div>
        <div style="margin-bottom:10px;">
            <p style="margin:0;font-size:11px;color:#888;">
                ${escapeHtml(t("sidebar.subtitle"))}
            </p>
            <p style="margin:4px 0 0 0;font-size:10px;color:#666;font-style:italic;line-height:1.4;">
                ${t("sidebar.workflow_hint")}
            </p>
        </div>

        <div data-role="status-tabs" style="display:flex;gap:2px;margin-bottom:6px;flex-wrap:wrap;align-items:center;">
            <button data-filter="all" style="padding:4px 6px;background:#0078d4;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:10px;">${escapeHtml(t("filter.all"))}</button>
            <button data-filter="scheduled" style="padding:4px 6px;background:#333;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:10px;">${escapeHtml(t("filter.scheduled"))}</button>
            <button data-filter="running" style="padding:4px 6px;background:#333;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:10px;">${escapeHtml(t("filter.running"))}</button>
            <button data-filter="done" style="padding:4px 6px;background:#333;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:10px;">${escapeHtml(t("filter.done"))}</button>
            <button data-filter="failed" style="padding:4px 6px;background:#333;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:10px;">${escapeHtml(t("filter.failed"))}</button>
            <button data-filter="cancelled" style="padding:4px 6px;background:#333;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:10px;">${escapeHtml(t("filter.cancelled"))}</button>
            <button data-role="clear-toggle" style="margin-left:8px;padding:4px 6px;background:#444;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:10px;">${escapeHtml(t("clear.toggle"))}</button>
        </div>

        <div data-role="clear-panel" style="display:none;margin-bottom:6px;padding:6px;background:#222;border-radius:3px;font-size:11px;">
            <label style="display:block;margin-bottom:2px;"><input type="checkbox" data-clear-status="done"/> ${escapeHtml(t("filter.done"))} (<span data-count-done>0</span>)</label>
            <label style="display:block;margin-bottom:2px;"><input type="checkbox" data-clear-status="failed"/> ${escapeHtml(t("filter.failed"))} (<span data-count-failed>0</span>)</label>
            <label style="display:block;margin-bottom:2px;"><input type="checkbox" data-clear-status="cancelled"/> ${escapeHtml(t("filter.cancelled"))} (<span data-count-cancelled>0</span>)</label>
            <label style="display:block;margin-bottom:2px;"><input type="checkbox" data-clear-status="running"/> ${escapeHtml(t("filter.running"))}</label>
            <label style="display:block;margin-bottom:2px;"><input type="checkbox" data-clear-status="scheduled"/> ${escapeHtml(t("filter.scheduled"))}</label>
            <label style="display:block;margin-bottom:2px;"><input type="checkbox" data-clear-status="interrupted"/> ${escapeHtml(t("filter.interrupted"))}</label>
            <button data-role="clear-execute" style="margin-top:4px;padding:4px 8px;background:#7a3030;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:11px;">${escapeHtml(t("clear.execute"))}</button>
        </div>

        <div data-role="actions" data-actions="__header__" style="margin-bottom:10px;display:grid;grid-template-columns:1fr 1fr;gap:4px;">
            <button data-act="refresh" style="padding:6px 8px;background:#0078d4;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:11px;">${escapeHtml(t("sidebar.refresh"))}</button>
            <button data-act="pause-resume" data-state="running" style="padding:6px 8px;background:#444;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:11px;">${escapeHtml(t("sidebar.pause"))}</button>
        </div>

        <div data-role="status" style="margin-bottom:10px;font-size:11px;color:#aaa;padding:8px;background:#252525;border-radius:4px;">${escapeHtml(t("sidebar.loading_jobs"))}</div>

        <div data-role="jobs" style="font-size:11px;"></div>

        <div data-role="pager" style="margin-top:8px;display:flex;gap:4px;align-items:center;font-size:11px;">
            <button data-role="prev" style="padding:4px 8px;background:#444;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:11px;">‹ ${escapeHtml(t("pager.prev"))}</button>
            <span data-role="page-info" style="color:#aaa;"></span>
            <button data-role="next" style="padding:4px 8px;background:#444;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:11px;">${escapeHtml(t("pager.next"))} ›</button>
        </div>

        <div style="margin-top:12px;font-size:10px;color:#666;">${escapeHtml(t("sidebar.use_topbar_hint"))}</div>
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
    const langBtnEl = root.querySelector('[data-role="lang-switch"]');

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
        // workflow_title is authoritative: if the user-supplied workflow
        // title is set (captured at Schedule submit time), never overwrite
        // it with the SaveImage _meta.title (nickname) below. Returning null
        // short-circuits the async hydration entirely so any subsequent
        // code path that calls resolveJobTitle cannot clobber the row.
        const wt = job && typeof job.workflow_title === "string" ? job.workflow_title.trim() : "";
        if (wt) return null;
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
    //
    // NOTE: outputs from both /list and /job/{id} is a *nested* dict keyed by
    // node id, e.g. {"80": {"images": [{filename, subfolder, type}]}, "45": {...}}.
    // It is NOT a flat {"images": [...]} shape. We walk the keys in insertion
    // order and take the first node that exposes a non-empty images array.
    const _thumbInflight = new Map();
    async function resolveJobThumb(job) {
        if (job && job.outputs && typeof job.outputs === "object") {
            const url = findFirstImageUrl(job.outputs);
            if (url) return url;
        }
        const id = job && job.id;
        if (!id) return null;
        if (_thumbInflight.has(id)) return _thumbInflight.get(id);
        const p = (async () => {
            try {
                const data = await callApi(`/job/${encodeURIComponent(id)}`);
                if (data && data.outputs && typeof data.outputs === "object") {
                    return findFirstImageUrl(data.outputs);
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

    // Walk an outputs dict (shape: {nodeId: {images: [...]}, ...}) and return
    // a /view URL for the image most likely to be the workflow's *final*
    // output, or null if no node carries an images array.
    //
    // Heuristic: scan every image record across every node, sort candidates
    // by the trailing counter embedded in the filename (ComfyUI default
    // "prefix_NNNNNN_.png" pattern, e.g. "ComfyUI_00046_.png"), and return
    // the last one. For a single-SaveImage workflow this is identical to
    // picking the first record, but for workflows that wire up several
    // SaveImage / PreviewImage nodes (e.g. pass 1 -> KSampler -> Save, then
    // pass 2 -> KSampler -> Save), file ordering maps to execution order
    // within a node, and execution order of multiple nodes is preserved by
    // object insertion order. The last record is therefore the newest /
    // last-written image, which is what the user wants to see in the row.
    //
    // The regex /_(\d+)_/ matches an underscore-delimited integer run; on a
    // filename without that pattern (rare — ComfyUI always adds the
    // counter) we fall back to lexicographic comparison of the raw name so
    // we never return null when an image exists.
    function findFirstImageUrl(outputsDict) {
        if (!outputsDict || typeof outputsDict !== "object") return null;
        let best = null;
        let bestKey = null;
        for (const nodeId of Object.keys(outputsDict)) {
            const node = outputsDict[nodeId];
            if (!node || !Array.isArray(node.images)) continue;
            for (const img of node.images) {
                if (!img || typeof img !== "object" || !img.filename) continue;
                const key = imageSortKey(img.filename);
                if (best === null || compareImageKeys(key, bestKey) > 0) {
                    best = img;
                    bestKey = key;
                }
            }
        }
        return best ? buildViewUrl(best) : null;
    }

    // Build a sort key [numericRun, rawFilename] from a filename. The numeric
    // run is the last occurrence of /_(\d+)_/ in the name (ComfyUI files
    // look like "ComfyUI_00046_.png"). When no such run exists we use
    // +Infinity so the filename sorts AFTER any counter-bearing file when
    // it is the newest write; otherwise we still need a stable tiebreak so
    // we keep the raw filename as a secondary key. Returning an array lets
    // compareImageKeys lexicographically compare [n, raw] in one call.
    function imageSortKey(filename) {
        if (typeof filename !== "string" || !filename) return [Infinity, ""];
        // Find ALL underscore-delimited integer runs and take the LAST one
        // (matches ComfyUI's own ordering: the per-batch counter is the
        // last _N_ token before the trailing format marker).
        const matches = filename.match(/_(\d+)_/g);
        if (matches && matches.length) {
            const last = matches[matches.length - 1];
            const n = parseInt(last.slice(1, -1), 10);
            if (Number.isFinite(n)) return [n, filename];
        }
        return [Infinity, filename];
    }

    // Lexicographic comparator over [n, raw] sort keys. Coerces the numeric
    // component to Number so "10" > "9" (string compare would invert that).
    // The raw filename breaks ties so two files with the same counter
    // (unusual but possible across nodes) still pick a deterministic one.
    function compareImageKeys(a, b) {
        const an = a[0] === Infinity ? -Infinity : a[0];
        const bn = b[0] === Infinity ? -Infinity : b[0];
        if (an !== bn) return an - bn;
        if (a[1] < b[1]) return -1;
        if (a[1] > b[1]) return 1;
        return 0;
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
            [t("status_bar.label.sched"), status.counts.scheduled],
            [t("status_bar.label.run"), status.counts.running],
            [t("status_bar.label.int"), status.counts.interrupted],
            [t("status_bar.label.done"), status.counts.done],
            [t("status_bar.label.fail"), status.counts.failed],
            [t("status_bar.label.cncl"), status.counts.cancelled],
        ];
        const parts = counters.map(([k, v]) =>
            `<span>${escapeHtml(k)}:<b style="color:${v > 0 ? "#fa3" : "#888"}">${v}</b></span>`
        ).join(" ");
        statusEl.innerHTML = `
            <div><strong>${escapeHtml(t("status_bar.paused"))}:</strong> <span style="color:${status.paused ? "#fa3" : "#5a8"}">${escapeHtml(status.paused ? t("status_bar.paused_yes") : t("status_bar.paused_no"))}</span></div>
            <div style="margin-top:4px;display:flex;flex-wrap:wrap;gap:8px;">${parts}</div>
        `;
        // Use data-state on the button (set in buildPanel: data-state="running")
        // rather than comparing localized text against "Pause", which is
        // fragile after i18n. The data-state mirrors the scheduler state and
        // is language-independent.
        pauseResumeBtn.textContent = status.paused ? t("sidebar.resume") : t("sidebar.pause");
        pauseResumeBtn.dataset.state = status.paused ? "paused" : "running";
        pauseResumeBtn.style.background = status.paused ? "#2d8f3e" : "#666";
    }

    // Format a duration in seconds as a short human-readable string.
    //   < 60s  -> "Ns"
    //   < 1h   -> "Nm Ks" (omits K when K is 0)
    //   >= 1h  -> "Nh Mm" (omits M when M is 0)
    // Returns "—" for null/undefined/NaN/negative inputs.
    function formatDuration(secs) {
        if (secs == null || !Number.isFinite(secs) || secs < 0) return "—";
        const s = Math.floor(secs);
        if (s < 60) return `${s}s`;
        const m = Math.floor(s / 60);
        const rs = s % 60;
        if (m < 60) return rs > 0 ? `${m}m ${rs}s` : `${m}m`;
        const h = Math.floor(m / 60);
        const rm = m % 60;
        return rm > 0 ? `${h}h ${rm}m` : `${h}h`;
    }

    // Format a unix-seconds timestamp as a delta from now, e.g.
    // "in 0:54:00" (h:mm:ss under a day) or "in 12h 5m".
    // Returns "—" for invalid inputs and "now" if delta is <2s.
    function formatTimeUntil(ts) {
        if (ts == null || !Number.isFinite(ts)) return "—";
        const delta = ts - Math.floor(Date.now() / 1000);
        if (Math.abs(delta) < 2) return "now";
        if (delta >= 0) {
            // Future: h:mm:ss for sub-day, otherwise "Nh Mm"
            const s = delta;
            if (s < 86400) {
                const h = Math.floor(s / 3600);
                const m = Math.floor((s % 3600) / 60);
                const ss = s % 60;
                return `in ${h}:${String(m).padStart(2, "0")}:${String(ss).padStart(2, "0")}`;
            }
            const h = Math.floor(s / 3600);
            const m = Math.floor((s % 3600) / 60);
            return m > 0 ? `in ${h}h ${m}m` : `in ${h}h`;
        }
        // Past: same shape but "ago"
        const s = -delta;
        if (s < 86400) {
            const h = Math.floor(s / 3600);
            const m = Math.floor((s % 3600) / 60);
            const ss = s % 60;
            return `${h}:${String(m).padStart(2, "0")}:${String(ss).padStart(2, "0")} ago`;
        }
        const h = Math.floor(s / 3600);
        const m = Math.floor((s % 3600) / 60);
        return m > 0 ? `${h}h ${m}m ago` : `${h}h ago`;
    }

    // Format a unix-seconds timestamp as local "HH:MM:SS" (same shape as
    // formatWhen in openScheduleDialog so the sidebar matches the dialog).
    // Returns "—" for invalid inputs so missing timestamps don't break the
    // row layout.
    function formatAbsTime(ts) {
        if (ts == null || !Number.isFinite(ts)) return "—";
        const d = new Date(ts * 1000);
        if (isNaN(d.getTime())) return "—";
        const pad = (n) => String(n).padStart(2, "0");
        return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
    }

    // Resolve the row title with the documented precedence:
    //   workflow_title (sent at Schedule submit time, captured from
    //     app.extensionManager.workflow.activeWorkflow.filename) →
    //   nickname (computed from the saved SaveImage _meta.title via
    //     resolveJobTitle, applied async below) →
    //   note (free-form user note from the dialog) →
    //   "untitled" (last-resort).
    // Note: the async hydration below writes the nickname into this same
    // slot once it resolves, so we always start with whichever fallback is
    // known synchronously.
    function pickRowTitle(j) {
        if (j && typeof j.workflow_title === "string" && j.workflow_title.trim()) {
            return { text: j.workflow_title.trim(), tooltip: j.workflow_title.trim() };
        }
        if (j && typeof j.note === "string" && j.note.trim()) {
            return { text: j.note.trim(), tooltip: j.note.trim() };
        }
        return { text: t("job.untitled"), tooltip: "" };
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
            jobsEl.innerHTML = `<div style="color:#666;font-style:italic;">${escapeHtml(t("sidebar.empty_filter"))}</div>`;
            return;
        }
        const colors = {
            scheduled: "#5a8", running: "#fa3", interrupted: "#f55",
            done: "#888", failed: "#f44", cancelled: "#666",
            // 'paused' is reserved for the Improvement 3 backend work; the
            // scheduler doesn't yet emit it per-job, but rendering it keeps
            // the sidebar forward-compatible without a UI rework.
            paused: "#aa8",
            // 'dispatched' = POSTed to ComfyUI but not yet picked up.
            dispatched: "#7ad",
        };
        jobsEl.innerHTML = visibleJobs.map((j, idx) => {
            const col = colors[j.status] || "#888";
            // Per-row capability flags. We split actionability into three
            // orthogonal dimensions because the buttons have different
            // backend requirements:
            //   actionable : can be moved in the queue + run-now + cancel
            //                (only makes sense before the job is dispatched)
            //   editable   : can be edited in the mini dialog (scheduled,
            //                dispatched, paused — *not* running/interrupted
            //                because changing scheduled_at mid-flight would
            //                silently desync from the live prompt_id)
            //   cancellable: can be cancelled (still allowed for dispatched
            //                so the user can pull a stuck prompt; backend
            //                now requires a confirm() step)
            const actionable = j.status === "scheduled" || j.status === "interrupted";
            const editable = j.status === "scheduled"
                || j.status === "dispatched"
                || j.status === "paused";
            const cancellable = j.status === "scheduled"
                || j.status === "interrupted"
                || j.status === "dispatched"
                || j.status === "paused";
            const queueIdx = pendingJobs.findIndex(p => p.id === j.id);
            const isFirst = queueIdx <= 0;
            const isLast = queueIdx < 0 || queueIdx === pendingJobs.length - 1;
            const shortId = (j.id || "").slice(0, 8);
            // Done thumbnails get a click-to-zoom modal. We render a placeholder
            // that gets swapped once resolveJobThumb resolves (see hydrateThumbs).
            const thumbHtml = j.status === "done"
                ? `<div data-role="thumb-slot" data-job-id="${escapeHtml(j.id)}" style="margin-top:4px;width:60px;height:60px;background:#333;border-radius:3px;display:flex;align-items:center;justify-content:center;color:#666;font-size:10px;">…</div>`
                : "";
            // Row title (workflow_title || note || "untitled"); the async
            // hydration below overwrites the span text with the computed
            // nickname when one resolves AND no workflow_title is present.
            const rowTitle = pickRowTitle(j);
            // Status badge: localized. Falls back to the raw status token
            // (e.g. an unknown future value) so the row never goes blank.
            const statusBadgeText = t(`status.${j.status}`, j.status);
            // Three-state time line (Improvement 2). Branch on status so the
            // user sees the right countdown for each phase. Strings are now
            // pulled through t() so the dialogue matches the active locale;
            // the *structure* of the line stays the same so the column
            // alignment across rows is unaffected by language.
            const now = Math.floor(Date.now() / 1000);
            let timeLineText;
            let timeLineHint;
            if (j.status === "scheduled") {
                const delta = (j.scheduled_at || 0) - now;
                // Format the in-future sub-day duration through formatDuration
                // (e.g. "5m 30s") so it pairs with the localized template.
                if (delta > 0) {
                    const dur = formatDuration(delta);
                    timeLineText = LANG === "en"
                        // English uses "in 5m 30s" prefix.
                        ? `in ${dur}`
                        // Chinese uses "5m 30s 后投递" suffix order.
                        : `${dur} ${t("time.left", "后投递")}`;
                } else {
                    timeLineText = t("job.time.now");
                }
                timeLineHint = `${formatTimeUntil(j.scheduled_at)} (${t("status_bar.label.sched")} ${formatAbsTime(j.scheduled_at)})`;
            } else if (j.status === "dispatched") {
                timeLineText = t("job.queued_in_comfyui");
                timeLineHint = j.dispatched_at
                    ? `${t("status.scheduled")} ${formatAbsTime(j.dispatched_at)}`
                    : t("job.queued_in_comfyui");
            } else if (j.status === "running") {
                const elapsed = j.dispatched_at ? (now - j.dispatched_at) : null;
                if (elapsed != null && elapsed >= 0) {
                    const dur = formatDuration(elapsed);
                    timeLineText = LANG === "en"
                        ? `running ${dur}`
                        : `${t("job.running_label")} ${dur}`;
                } else {
                    timeLineText = t("job.executing");
                }
                timeLineHint = j.dispatched_at
                    ? `${t("job.scheduled_label")} ${formatAbsTime(j.dispatched_at)}`
                    : t("job.executing");
            } else if (j.status === "paused") {
                timeLineText = t("job.paused");
                timeLineHint = j.scheduled_at
                    ? `${formatTimeUntil(j.scheduled_at)} (${t("job.paused")})`
                    : t("job.paused");
            } else {
                // done / failed / cancelled / interrupted — keep the delta
                // to scheduled_at for context.
                timeLineText = formatTimeUntil(j.scheduled_at);
                timeLineHint = j.scheduled_at
                    ? `${t("job.scheduled_label")} ${formatAbsTime(j.scheduled_at)}`
                    : "";
            }
            // Duration row: shows the absolute finished time (HH:MM:SS local) plus
            // the elapsed time between dispatched_at and finished_at. The label
            // is status-aware and localized.
            const finishedLabel = j.status === "failed" ? t("job.failed_at")
                : j.status === "done" ? t("job.completed_at")
                : j.status === "cancelled" ? t("job.cancelled_at")
                : j.status === "running" ? t("job.running_label")
                : j.status === "interrupted" ? t("job.interrupted_at")
                : j.status === "paused" ? t("job.paused")
                : t("job.scheduled_label");
            let durationText;
            if (j.finished_at && j.dispatched_at) {
                durationText = `${formatAbsTime(j.finished_at)} · ${formatDuration(j.finished_at - j.dispatched_at)}`;
            } else if (j.finished_at) {
                durationText = formatAbsTime(j.finished_at);
            } else if (j.status === "running") {
                durationText = t("job.running_label");
            } else {
                durationText = "—";
            }
            return `<div data-job-id="${j.id}" data-status="${escapeHtml(j.status)}" style="padding:6px;margin-bottom:4px;background:#252525;border-radius:3px;border-left:3px solid ${col};">
                <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:4px;">
                    <div style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0;">
                        <div data-role="job-title" style="display:flex;justify-content:space-between;align-items:baseline;gap:6px;">
                            <span data-role="job-nickname" data-job-id="${escapeHtml(j.id)}" title="${escapeHtml(rowTitle.tooltip)}" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0;">${escapeHtml(rowTitle.text)}</span>
                            <span style="opacity:.6;font-size:10px;flex-shrink:0;">(${escapeHtml(shortId)})</span>
                        </div>
                        <div style="font-size:10px;color:#aaa;margin-top:3px;display:flex;flex-wrap:wrap;gap:6px;align-items:center;">
                            <span data-role="job-status" style="padding:1px 5px;background:${col};color:#000;border-radius:2px;font-weight:600;">${escapeHtml(statusBadgeText)}</span>
                            <span data-role="job-time" title="${escapeHtml(timeLineHint || "")}" style="color:#888;">${escapeHtml(timeLineText)}</span>
                        </div>
                        <div style="font-size:10px;color:#888;margin-top:2px;">
                            ${finishedLabel} ${escapeHtml(durationText)}
                        </div>
                    </div>
                    <div data-actions="${j.id}" style="display:flex;gap:2px;flex-shrink:0;margin-left:4px;">
                        ${editable ? `<button data-act="edit" data-id="${j.id}" title="${escapeHtml(t("action.schedule"))}" style="padding:2px 6px;background:#3a6f9e;color:#fff;border:none;border-radius:3px;font-size:11px;cursor:pointer;">✎</button>` : ""}
                        ${actionable ? `<button data-act="up" title="${escapeHtml(t("action.up_title"))}" ${isFirst ? "disabled style=\"padding:2px 6px;background:#222;color:#555;border:none;border-radius:3px;font-size:11px;cursor:not-allowed;\"" : "style=\"padding:2px 6px;background:#3a3;color:#fff;border:none;border-radius:3px;font-size:11px;cursor:pointer;\""}>↑</button><button data-act="down" title="${escapeHtml(t("action.down_title"))}" ${isLast ? "disabled style=\"padding:2px 6px;background:#222;color:#555;border:none;border-radius:3px;font-size:11px;cursor:not-allowed;\"" : "style=\"padding:2px 6px;background:#3a3;color:#fff;border:none;border-radius:3px;font-size:11px;cursor:pointer;\""}>↓</button>` : ""}
                        ${actionable ? `<button data-act="run-now" title="${escapeHtml(t("action.run_now_title"))}" style="padding:2px 6px;background:#2d6f9e;color:#fff;border:none;border-radius:3px;font-size:11px;cursor:pointer;">${escapeHtml(t("action.run_now"))}</button>` : ""}
                        ${cancellable ? `<button data-act="cancel" title="${escapeHtml(t("action.cancel_title"))}" style="padding:2px 6px;background:#7a3030;color:#fff;border:none;border-radius:3px;font-size:11px;cursor:pointer;">✕</button>` : ""}
                        <button data-act="repeat" data-id="${j.id}" title="${escapeHtml(t("action.repeat_title"))}" style="padding:2px 6px;background:#5a4f9e;color:#fff;border:none;border-radius:3px;font-size:11px;cursor:pointer;">↻</button>
                        <button data-act="export" data-id="${j.id}" title="${escapeHtml(t("action.export_title"))}" style="padding:2px 6px;background:#666;color:#fff;border:none;border-radius:3px;font-size:11px;cursor:pointer;">⬇</button>
                    </div>
                </div>
                ${thumbHtml}
                ${j.error ? `<div style="font-size:10px;color:#f88;margin-top:2px;">⚠ ${escapeHtml(j.error)}</div>` : ""}
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
                // workflow_title is authoritative: skip the async overwrite
                // entirely so the SaveImage _meta.title (nickname) can never
                // clobber the user's scheduled workflow filename.
                const wt = j && typeof j.workflow_title === "string" ? j.workflow_title.trim() : "";
                if (wt) continue;
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
                    slot.innerHTML = `<span style="color:#666;font-size:10px;">${escapeHtml(t("preview.no_preview"))}</span>`;
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
        if (pageInfoEl) {
            // Localized "Page X (Y-Z of W)" with positional {N} placeholders.
            // The English template uses 1-based visibleRange; we compute the
            // start/end indices here and pass them in.
            const visible = visibleJobsCount();
            const start = total > 0 ? currentOffset + 1 : 0;
            const end = currentOffset + visible;
            pageInfoEl.textContent = tfmt("pager.page_of", `Page ${page} (${start}-${end} of ${total})`, [page, start, end, total]);
        }
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
                statusEl.innerHTML = `<div style="color:#f44;">${escapeHtml(t("error.network"))}: ${escapeHtml(e.message)}</div>`;
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
        // Use data-state (set in renderStatus) rather than localized button
        // text; this is the canonical scheduler state mirror and survives
        // any language switch.
        const wasPaused = pauseResumeBtn.dataset.state === "paused";
        pauseResumeBtn.disabled = true;
        try {
            await callApi(wasPaused ? "/resume-all" : "/pause-all", { method: "POST" });
            await refresh({ silent: true });
        } catch (e) {
            statusEl.innerHTML = `<div style="color:#f44;">${escapeHtml(t("error.network"))}: ${escapeHtml(e.message)}</div>`;
        } finally {
            pauseResumeBtn.disabled = false;
        }
    });

    // Language switch button: flip the global LANG, persist, and re-render
    // the whole panel so every t() lookup picks up the new locale. We
    // rebuild root.innerHTML from scratch (cheaper than diffing nodes and
    // catches any string we may have forgotten to mark with data-role).
    if (langBtnEl) {
        langBtnEl.addEventListener("click", () => {
            setLang(LANG === "zh" ? "en" : "zh");
        });
    }

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
                statusEl.innerHTML = `<div style="color:#fa3;">${escapeHtml(t("clear.empty"))}</div>`;
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
                alert(t("clear.alert") + ": " + err.message);
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

        // Cancel deserves a confirmation step (Improvement 1 part 2):
        // cancelling an already-dispatched job pulls a prompt out of
        // ComfyUI's queue; cancelling a running job is a no-op on the
        // backend but still ratchets user intent in the local DB. Pulling
        // a queued prompt and then regretting it requires the user to
        // re-schedule from scratch, so we ask once.
        if (act === "cancel") {
            const status = jobEl && jobEl.dataset.status ? jobEl.dataset.status : "";
            let msg;
            // Per-state confirm copy. Keys are intentionally distinct so
            // translators see them individually in the JSON file. We pass
            // fallbacks for each branch so an incomplete locale never
            // produces an empty confirm() dialog.
            if (status === "dispatched") {
                msg = t("confirm.cancel_dispatched",
                    "This job is queued in ComfyUI's queue. Cancel will pull it out. Continue?");
            } else if (status === "running") {
                msg = t("confirm.cancel_running",
                    "Cancel a running job? The prompt will continue in ComfyUI; the row will be marked cancelled locally.");
            } else {
                msg = t("confirm.cancel", "Cancel this scheduled job?");
            }
            if (!window.confirm(msg)) {
                // Re-enable the buttons we just disabled so the user can
                // click again without waiting for refresh().
                actionsEl.querySelectorAll("button").forEach(b => (b.disabled = false));
                return;
            }
        }

        if (act === "edit") {
            // The Edit dialog needs the current job record. We can pull it
            // from the rendered row's data attributes (id, shortId,
            // status) but scheduled_at / priority / note need to come from
            // the latest API snapshot. We'll re-fetch the row via /job/:id
            // and pass the full record to the dialog. If the row is no
            // longer editable (e.g. user has just clicked Run-now), the
            // dialog will refuse to submit.
            try {
                actionsEl.querySelectorAll("button").forEach(b => (b.disabled = true));
                const detail = await callApi(`/job/${encodeURIComponent(id)}`);
                const job = detail && detail.job ? detail.job : detail;
                openEditJobDialog(job || { id, status: jobEl && jobEl.dataset.status ? jobEl.dataset.status : "" }, refresh);
            } catch (err) {
                statusEl.innerHTML = `<div style="color:#f44;">${escapeHtml(t("error.action_failed"))}: ${escapeHtml(err.message)}</div>`;
            } finally {
                actionsEl.querySelectorAll("button").forEach(b => (b.disabled = false));
            }
            return;
        }

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
            statusEl.innerHTML = `<div style="color:#f44;">${escapeHtml(t("error.action_failed"))}: ${escapeHtml(err.message)}</div>`;
        } finally {
            await refresh({ silent: true });
        }
    });

    // Mini edit dialog for a single scheduled/dispatched/paused job.
    // Lets the user nudge scheduled_at via the same +/- buttons used by
    // the schedule dialog (we re-implement them locally rather than
    // cross-call across DOM trees) and bump the priority. POSTs the patch
    // to /api/schedule/update/{id}, which whitelists exactly the fields
    // we send (scheduled_at + priority). After success we call onSaved()
    // (typically `refresh`) so the row redraws with the new values.
    function openEditJobDialog(job, onSaved) {
        const editableStatuses = new Set(["scheduled", "dispatched", "paused"]);
        const id = job.id;
        const initialScheduledAt = Number.isFinite(job.scheduled_at) ? job.scheduled_at : Math.floor(Date.now() / 1000) + 60;
        const initialPriority = Number.isFinite(job.priority) ? job.priority : 100;
        const initialNote = typeof job.note === "string" ? job.note : "";
        let currentWhenTs = initialScheduledAt;
        let currentPriority = initialPriority;
        let currentNote = initialNote;
        const MIN_SCHEDULE_OFFSET = 5;
        function minWhenTs() { return Math.floor(Date.now() / 1000) + MIN_SCHEDULE_OFFSET; }
        function formatWhen(ts) {
            const d = new Date(ts * 1000);
            const pad = (n) => String(n).padStart(2, "0");
            return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate())
                + " " + pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
        }
        function parseWhen(text) {
            if (typeof text !== "string") return NaN;
            const trimmed = text.trim();
            if (!trimmed) return NaN;
            const m = trimmed.match(/^(\d{4})[-/](\d{1,2})[-/](\d{1,2})[ T](\d{1,2}):(\d{1,2})(?::(\d{1,2}))?$/);
            if (!m) return NaN;
            const [, y, mo, d, h, mi, s] = m;
            const Y = parseInt(y, 10), Mo = parseInt(mo, 10), D = parseInt(d, 10);
            const H = parseInt(h, 10), Mi = parseInt(mi, 10), S = s == null ? 0 : parseInt(s, 10);
            if (Mo < 1 || Mo > 12 || D < 1 || D > 31) return NaN;
            if (H > 23 || Mi > 59 || S > 59) return NaN;
            const dt = new Date(Y, Mo - 1, D, H, Mi, S, 0);
            if (isNaN(dt.getTime())) return NaN;
            return Math.floor(dt.getTime() / 1000);
        }

        const dlg = document.createElement("div");
        dlg.dataset.sqDialog = "1";
        dlg.dataset.sqEditDialog = "1";
        dlg.style.cssText = `
            position: fixed; inset: 0; z-index: 99999;
            background: rgba(0,0,0,0.6);
            display: flex; align-items: center; justify-content: center;
            font-family: system-ui, sans-serif;
        `;
        const status = job.status || "";
        // Localized status badge text for the edit dialog. Reuses the
        // status.* keys defined for the main sidebar so a single zh/en
        // lookup covers both UIs.
        const statusBadge = ({
            scheduled: t("status.scheduled"),
            dispatched: t("status.dispatched") || t("filter.dispatched"),
            paused: t("job.paused"),
        })[status] || t(`status.${status}`, status) || t("status.unknown", status);
        const titleText = (job.workflow_title || job.note || t("job.untitled")).trim() || t("job.untitled");
        const editable = editableStatuses.has(status);
        const disabledAttr = editable ? "" : "disabled style=\"opacity:.5;cursor:not-allowed;\"";
        dlg.innerHTML = `
            <div style="background:#1e1e1e;color:#ccc;padding:18px;border-radius:8px;width:440px;box-shadow:0 8px 32px rgba(0,0,0,0.5);">
                <h3 style="margin:0 0 4px 0;color:#fff;font-size:14px;">${escapeHtml(t("dialog.edit_title", "Edit job"))}</h3>
                <div style="font-size:11px;color:#888;margin-bottom:10px;">
                    <span>${escapeHtml(titleText)}</span>
                    <span style="margin-left:6px;padding:1px 5px;background:#555;color:#000;border-radius:2px;">${escapeHtml(statusBadge)}</span>
                    <span style="margin-left:6px;opacity:.6;">${escapeHtml((id || "").slice(0, 8))}</span>
                </div>
                ${editable ? "" : `<div style="font-size:11px;color:#fa3;margin-bottom:10px;">${escapeHtml(t("dialog.edit_not_editable", `Current status (${status}) does not allow editing time / priority — you can still save the priority change.`))}</div>`}

                <div style="margin-bottom:10px;">
                    <label style="display:block;font-size:11px;color:#aaa;margin-bottom:4px;">When (local time)</label>
                    <input data-role="edit-when-display" placeholder="2026-08-22 22:30:00"
                        style="width:100%;padding:6px;background:#252525;color:#fff;border:1px solid #444;border-radius:3px;font-family:monospace;text-align:center;"
                        value="${escapeHtml(formatWhen(currentWhenTs))}" ${disabledAttr} />
                    <div data-role="edit-when-buttons" style="display:flex;flex-wrap:wrap;gap:2px;margin-top:6px;justify-content:flex-end;">
                        <button type="button" data-edit-delta="-3600" ${disabledAttr} style="padding:4px 6px;background:#333;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:11px;">-1h</button>
                        <button type="button" data-edit-delta="-600" ${disabledAttr} style="padding:4px 6px;background:#333;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:11px;">-10m</button>
                        <button type="button" data-edit-delta="-60" ${disabledAttr} style="padding:4px 6px;background:#333;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:11px;">-1m</button>
                        <button type="button" data-edit-delta="-10" ${disabledAttr} style="padding:4px 6px;background:#333;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:11px;">-10s</button>
                        <button type="button" data-edit-delta="-5" ${disabledAttr} style="padding:4px 6px;background:#333;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:11px;">-5s</button>
                        <button type="button" data-edit-delta="5" ${disabledAttr} style="padding:4px 6px;background:#333;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:11px;">+5s</button>
                        <button type="button" data-edit-delta="10" ${disabledAttr} style="padding:4px 6px;background:#333;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:11px;">+10s</button>
                        <button type="button" data-edit-delta="60" ${disabledAttr} style="padding:4px 6px;background:#333;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:11px;">+1m</button>
                        <button type="button" data-edit-delta="600" ${disabledAttr} style="padding:4px 6px;background:#333;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:11px;">+10m</button>
                        <button type="button" data-edit-delta="3600" ${disabledAttr} style="padding:4px 6px;background:#333;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:11px;">+1h</button>
                    </div>
                    <input data-role="edit-when" type="hidden" value="${currentWhenTs}" />
                </div>

                <div style="margin-bottom:10px;">
                    <label style="display:block;font-size:11px;color:#aaa;margin-bottom:4px;">Priority (0-1000, higher runs first)</label>
                    <input data-role="edit-priority" type="number" min="0" max="1000" value="${currentPriority}"
                        style="width:100%;padding:6px;background:#252525;color:#fff;border:1px solid #444;border-radius:3px;font-family:monospace;" />
                </div>

                <div style="margin-bottom:14px;">
                    <label style="display:block;font-size:11px;color:#aaa;margin-bottom:4px;">Note</label>
                    <input data-role="edit-note" type="text" value="${escapeHtml(currentNote)}" maxlength="200"
                        style="width:100%;padding:6px;background:#252525;color:#fff;border:1px solid #444;border-radius:3px;" />
                </div>

                <div data-role="edit-error" style="font-size:11px;color:#f44;min-height:14px;margin-bottom:6px;"></div>

                <div style="display:flex;justify-content:flex-end;gap:8px;">
                    <button data-act="edit-cancel" style="padding:6px 14px;background:#444;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:12px;">Cancel</button>
                    <button data-act="edit-save" style="padding:6px 14px;background:#0078d4;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:12px;">Save</button>
                </div>
            </div>
        `;

        function closeDialog() {
            dlg.remove();
            document.removeEventListener("keydown", onKey);
        }
        function onKey(e) { if (e.key === "Escape") closeDialog(); }
        document.addEventListener("keydown", onKey);
        dlg.addEventListener("click", (e) => { if (e.target === dlg) closeDialog(); });
        dlg.querySelector('[data-act="edit-cancel"]').addEventListener("click", () => closeDialog());

        const displayEl = dlg.querySelector('[data-role="edit-when-display"]');
        const hiddenEl = dlg.querySelector('[data-role="edit-when"]');
        const priorityEl = dlg.querySelector('[data-role="edit-priority"]');
        const noteEl = dlg.querySelector('[data-role="edit-note"]');
        const errorEl = dlg.querySelector('[data-role="edit-error"]');
        const saveBtn = dlg.querySelector('[data-act="edit-save"]');

        function refreshEditDisplay() {
            displayEl.value = formatWhen(currentWhenTs);
            hiddenEl.value = String(currentWhenTs);
        }

        // +/- buttons wired the same way as openScheduleDialog; we keep
        // them disabled when the row's status forbids edits so users on a
        // running/interrupted job see a greyed-out row instead of an
        // honest-looking but silently-no-op dialog.
        dlg.querySelectorAll('[data-edit-delta]').forEach((btn) => {
            btn.addEventListener("click", () => {
                if (btn.disabled) return;
                const delta = parseInt(btn.dataset.editDelta, 10);
                if (!Number.isFinite(delta)) return;
                currentWhenTs = Math.max(currentWhenTs + delta, minWhenTs());
                displayEl.style.borderColor = "";
                refreshEditDisplay();
            });
        });
        displayEl.addEventListener("input", () => {
            if (displayEl.disabled) return;
            const parsed = parseWhen(displayEl.value);
            if (Number.isFinite(parsed)) {
                if (parsed < minWhenTs()) {
                    displayEl.style.borderColor = "#c44";
                    return;
                }
                displayEl.style.borderColor = "";
                currentWhenTs = parsed;
                hiddenEl.value = String(parsed);
            } else {
                displayEl.style.borderColor = "#c44";
            }
        });
        priorityEl.addEventListener("input", () => {
            const v = parseInt(priorityEl.value, 10);
            currentPriority = Number.isFinite(v) ? Math.max(0, Math.min(1000, v)) : currentPriority;
        });
        noteEl.addEventListener("input", () => { currentNote = noteEl.value; });

        saveBtn.addEventListener("click", async () => {
            errorEl.textContent = "";
            // Re-parse the display at submit time so the user's last
            // manual edit wins even if they never clicked a +/- button.
            // Same defensive parse + clamp as the schedule dialog.
            const parsed = parseWhen(displayEl.value);
            if (!Number.isFinite(parsed) || parsed < minWhenTs()) {
                displayEl.style.borderColor = "#c44";
                errorEl.textContent = "Invalid / past time";
                return;
            }
            currentWhenTs = parsed;
            hiddenEl.value = String(currentWhenTs);

            const pri = Math.max(0, Math.min(1000, parseInt(priorityEl.value || "100", 10)));
            const fields = { priority: pri, note: (noteEl.value || "").trim() };
            // Only send scheduled_at when the row is editable; the backend
            // whitelists it for the editable statuses anyway but skipping
            // it for a frozen row keeps the request semantically honest.
            if (editable) fields.scheduled_at = currentWhenTs;

            saveBtn.disabled = true;
            try {
                await callApi(`/update/${encodeURIComponent(id)}`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(fields),
                });
                closeDialog();
                if (typeof onSaved === "function") {
                    // The /update endpoint round-trips through the DB
                    // synchronously; refresh() picks up the new values
                    // before the next paint.
                    await onSaved({ silent: true });
                }
            } catch (err) {
                errorEl.textContent = `Save failed: ${err.message}`;
            } finally {
                saveBtn.disabled = false;
            }
        });

        document.body.appendChild(dlg);
        return dlg;
    }

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

    // i18n: when the global LANG changes (clicking the lang switch button
    // dispatches `sq:lang-changed`), tear down the current root and rebuild
    // the panel from scratch so every t() lookup re-resolves against the
    // new locale. We rebuild by re-running buildPanel() and swapping the
    // container's child -- the simplest correct approach, since the static
    // and dynamic parts of the panel are all generated from a single
    // innerHTML template + refresh() cycle.
    const langChangedHandler = () => {
        // Tear down the existing root's MutationObserver timer first.
        clearInterval(_refreshTimer);
        observer.disconnect();
        // Rebuild and swap. The framework's container is root.parentNode
        // (we only have one level because we replace the container's
        // contents on lang switch).
        const parent = root.parentNode;
        if (!parent) {
            // Already detached -- nothing to do.
            return;
        }
        const newPanel = buildPanel();
        parent.innerHTML = "";
        parent.appendChild(newPanel);
        if (typeof newPanel.refresh === "function") newPanel.refresh();
        // Notify the sidebar watcher that the container's first child
        // changed so it doesn't try to clear us on tab toggle.
    };
    window.addEventListener("sq:lang-changed", langChangedHandler);

    // Clean up the listener on unmount so we don't leak.
    const _origDisconnect = observer.disconnect.bind(observer);
    observer.disconnect = () => {
        window.removeEventListener("sq:lang-changed", langChangedHandler);
        _origDisconnect();
    };

    return root;
}

// ============================================================
// Schedule dialog -- triggered by topbar button
// ============================================================

function openScheduleDialog() {
    const now = Math.floor(Date.now() / 1000);

    // Preset labels -- use t() so the preset chips localize. The offset
    // (in seconds) is language-agnostic; the label string is not.
    const presets = [
        { label: t("dialog.preset.30s", "in 30s"), offset: 30 },
        { label: t("dialog.preset.5min", "in 5 min"), offset: 300 },
        { label: t("dialog.preset.30min", "in 30 min"), offset: 1800 },
        { label: t("dialog.preset.2hours", "in 2 hours"), offset: 7200 },
    ];
    const tomorrow9 = (() => {
        const d = new Date(Date.now() + 86400_000);
        d.setHours(9, 0, 0, 0);
        return Math.floor(d.getTime() / 1000);
    })();
    presets.push({ label: t("dialog.preset.tomorrow", "tomorrow 9am"), absolute: tomorrow9 });

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
        <div style="background:#1e1e1e;color:#ccc;padding:20px;border-radius:8px;width:480px;box-shadow:0 8px 32px rgba(0,0,0,0.5);">
            <h3 style="margin:0 0 12px 0;color:#fff;font-size:15px;">${escapeHtml(t("dialog.schedule_title", "Schedule current workflow"))}</h3>

            <div style="margin-bottom:10px;">
                <label style="display:block;font-size:11px;color:#aaa;margin-bottom:4px;">${escapeHtml(t("dialog.when_label", "When (local time)"))}</label>
                <div style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:6px;">
                    ${presets.map((p, i) => `<button data-preset="${i}" style="padding:4px 8px;background:#333;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:11px;">${escapeHtml(p.label)}</button>`).join("")}
                </div>
                <!-- Two-row layout: input fills middle width, +/- button groups sit
                     right-aligned in both rows so the eye reads a single vertical
                     button column on the right edge. -->
                <div data-role="when-row" style="display:flex;flex-direction:column;gap:4px;margin-top:6px;align-items:stretch;">
                    <div style="display:flex;gap:2px;align-items:center;">
                        <input data-role="when-display" placeholder="2026-08-22 22:30:00" style="flex:1;min-width:0;padding:6px;background:#252525;color:#fff;border:1px solid #444;border-radius:3px;font-family:monospace;text-align:center;" value="${formatWhen(currentWhenTs)}" />
                        <div data-role="when-dec" style="display:flex;gap:2px;justify-content:flex-end;">
                            <button type="button" data-delta="-3600" style="padding:4px 6px;background:#333;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:11px;">${escapeHtml(t("dialog.delta.dec_1h", "-1h"))}</button>
                            <button type="button" data-delta="-600" style="padding:4px 6px;background:#333;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:11px;">${escapeHtml(t("dialog.delta.dec_10m", "-10m"))}</button>
                            <button type="button" data-delta="-60" style="padding:4px 6px;background:#333;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:11px;">${escapeHtml(t("dialog.delta.dec_1m", "-1m"))}</button>
                            <button type="button" data-delta="-10" style="padding:4px 6px;background:#333;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:11px;">${escapeHtml(t("dialog.delta.dec_10s", "-10s"))}</button>
                            <button type="button" data-delta="-5" style="padding:4px 6px;background:#333;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:11px;">${escapeHtml(t("dialog.delta.dec_5s", "-5s"))}</button>
                        </div>
                    </div>
                    <div style="display:flex;gap:2px;justify-content:flex-end;">
                        <div data-role="when-inc" style="display:flex;gap:2px;justify-content:flex-end;">
                            <button type="button" data-delta="5" style="padding:4px 6px;background:#333;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:11px;">${escapeHtml(t("dialog.delta.inc_5s", "+5s"))}</button>
                            <button type="button" data-delta="10" style="padding:4px 6px;background:#333;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:11px;">${escapeHtml(t("dialog.delta.inc_10s", "+10s"))}</button>
                            <button type="button" data-delta="60" style="padding:4px 6px;background:#333;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:11px;">${escapeHtml(t("dialog.delta.inc_1m", "+1m"))}</button>
                            <button type="button" data-delta="600" style="padding:4px 6px;background:#333;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:11px;">${escapeHtml(t("dialog.delta.inc_10m", "+10m"))}</button>
                            <button type="button" data-delta="3600" style="padding:4px 6px;background:#333;color:#fff;border:none;border-radius:3px;cursor:pointer;font-size:11px;">${escapeHtml(t("dialog.delta.inc_1h", "+1h"))}</button>
                        </div>
                    </div>
                </div>
                <!-- Hidden unix-seconds input: source of truth at submit time. -->
                <input data-role="when" type="hidden" value="${currentWhenTs}" />
            </div>

            <div style="margin-bottom:10px;">
                <label style="display:block;font-size:11px;color:#aaa;margin-bottom:4px;">${escapeHtml(t("dialog.priority_label", "Priority (0-1000, higher runs first)"))}</label>
                <input data-role="priority" type="number" min="0" max="1000" value="100" style="width:100%;padding:6px;background:#252525;color:#fff;border:1px solid #444;border-radius:3px;font-family:monospace;" />
            </div>

            <div style="margin-bottom:14px;">
                <label style="display:block;font-size:11px;color:#aaa;margin-bottom:4px;">${escapeHtml(t("dialog.note_label", "Note (optional)"))}</label>
                <input data-role="note" type="text" placeholder="${escapeHtml(t("dialog.note_placeholder", "e.g. morning batch / variant 3"))}" style="width:100%;padding:6px;background:#252525;color:#fff;border:1px solid #444;border-radius:3px;" />
            </div>

            <div data-role="count-row" style="margin-top:6px;margin-bottom:14px;display:flex;gap:6px;align-items:center;">
                <label style="opacity:.7;font-size:11px;">${escapeHtml(t("dialog.count_label", "Count:"))}</label>
                <input data-role="count" type="number" min="1" max="50" value="1" style="width:60px;background:#222;color:#fff;border:1px solid #555;padding:4px;border-radius:3px;" />
                <span style="opacity:.5;font-size:11px;">${escapeHtml(t("dialog.count_hint", "(1-50, repeat same workflow)"))}</span>
            </div>

            <div style="display:flex;justify-content:flex-end;gap:8px;">
                <button data-act="cancel" style="padding:6px 14px;background:#444;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:12px;">${escapeHtml(t("dialog.cancel", "Cancel"))}</button>
                <button data-act="submit" style="padding:6px 14px;background:#0078d4;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:12px;">${escapeHtml(t("dialog.submit", "Schedule"))}</button>
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
            alert(t("error.invalid_time", "Invalid time"));
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

        // Capture the current workflow's filename from the ComfyUI Pinia
        // store before serializing. The backend stores this verbatim on
        // every created job (as workflow_title) so the sidebar can label
        // rows with the user-visible workflow name instead of just a UUID.
        // Guarded because the extensionManager/activeWorkflow wiring differs
        // across ComfyUI versions; any failure here must NOT block submission.
        let workflowTitle = "";
        try {
            const aw = app.extensionManager?.workflow?.activeWorkflow;
            if (aw) workflowTitle = aw.filename || aw.fullFilename || "";
        } catch (_e) { /* ignore -- empty title is fine */ }

        if (!scheduledAt || scheduledAt <= Math.floor(Date.now() / 1000)) {
            alert(t("error.scheduled_in_past", "Scheduled time must be in the future."));
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
                alert(t("error.empty_workflow", "Current workflow is empty."));
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
                            workflow_title: workflowTitle,
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
                        workflow_title: workflowTitle,
                    }));
                    resp = await fetch("/api/schedule/add-batch", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ items }),
                    });
                }
                const data = await resp.json();
                if (!resp.ok) {
                    alert(t("error.add_failed", "Add failed") + ": " + (data.error || resp.statusText));
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
                alert(t("error.network", "Network error") + ": " + err.message);
            }
        } catch (err) {
            alert(t("error.serialize_workflow", "Could not serialize workflow") + ": " + err.message);
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
    // Brief highlight (green tint) on the time input after a +/- or preset click.
    // The value changes silently otherwise, which is invisible to users; this
    // gives immediate visual confirmation. We restore the original background
    // (#252525) instead of clearing to "" so the input never flashes white.
    // Briefly tint the time-display background so the user sees that a click
    // actually took effect. Defaults to green for preset clicks; the +/- delta
    // handler passes blue when shrinking time and green when extending it,
    // so the sign of the change is communicated without text labels.
    function flashWhenDisplay(color = "#0a4d2a") {
        whenDisplay.style.background = color;
        whenDisplay.style.transition = "background 0.05s linear";
        if (flashWhenDisplay._t) clearTimeout(flashWhenDisplay._t);
        flashWhenDisplay._t = setTimeout(() => {
            whenDisplay.style.background = "#252525";
            flashWhenDisplay._t = null;
        }, 150);
    }

    function refreshWhenDisplay() {
        whenDisplay.value = formatWhen(currentWhenTs);
        whenDisplay.style.borderColor = "";
        whenHidden.value = String(currentWhenTs);
        console.log("[ScheduledQueue] refreshWhenDisplay -> currentWhenTs=" + currentWhenTs
            + " (" + whenDisplay.value + ")");
    }

    // Minimum offset (seconds) we allow the user to schedule for. Sub-5s
    // timestamps would either land in the past (after a tiny input lag) or
    // skip dispatch entirely, so we clamp every code path that mutates
    // currentWhenTs to at least floor(Date.now()/1000) + THIS.
    const MIN_SCHEDULE_OFFSET = 5;
    function minWhenTs() {
        return Math.floor(Date.now() / 1000) + MIN_SCHEDULE_OFFSET;
    }

    // +/- buttons: each click adds its data-delta (seconds) to currentWhenTs.
    console.log("[ScheduledQueue] binding delta click handlers (v" + SQ_VERSION + ")");
    dlg.querySelectorAll('[data-role="when-row"] [data-delta]').forEach((btn) => {
        btn.addEventListener("click", () => {
            const delta = parseInt(btn.dataset.delta, 10);
            console.log("[ScheduledQueue] delta click: " + delta + "s (was " + currentWhenTs + ")");
            if (!Number.isFinite(delta)) return;
            currentWhenTs += delta;
            // Clamp: never let the +/- buttons push the time into the past
            // (or so close that input lag would skip dispatch entirely).
            currentWhenTs = Math.max(currentWhenTs, minWhenTs());
            refreshWhenDisplay();
            // Green = time moves forward (button adds seconds).
            // Blue  = time moves backward (button subtracts seconds).
            flashWhenDisplay(delta > 0 ? "#0a4d2a" : "#0a4068");
        });
    });

    // Display input: accept manual edits in any of the three formats.
    // Successful parse updates the canonical timestamp and the hidden
    // input; failure paints a red border without breaking the dialog.
    // Bug 1 fix: if the user typed a parseable time that is in the past
    // (or within MIN_SCHEDULE_OFFSET seconds of now), we reject it -- red
    // border + alert -- rather than silently accepting and clamping,
    // because silently rewriting what the user typed is more confusing
    // than refusing the bad value.
    whenDisplay.addEventListener("input", () => {
        const text = whenDisplay.value;
        const parsed = parseWhen(text);
        if (Number.isFinite(parsed)) {
            if (parsed < minWhenTs()) {
                whenDisplay.style.borderColor = "#c44";
                // Note: do not alert on every keystroke -- only the first
                // time the value crosses into "too early" territory.
                if (whenDisplay.dataset.sqBadAlerted !== "1") {
                    whenDisplay.dataset.sqBadAlerted = "1";
                    alert(t("error.scheduled_in_past", "Scheduled time must be in the future."));
                }
                return;
            }
            whenDisplay.style.borderColor = "";
            whenDisplay.dataset.sqBadAlerted = "";
            // Clamp for safety (defence-in-depth: a parallel edit path
            // could race the input event).
            currentWhenTs = Math.max(parsed, minWhenTs());
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
            // Clamp: presets are computed at dialog-open time using `now`,
            // so they are guaranteed to be future-relative -- but if the
            // user leaves the dialog open for minutes before clicking a
            // preset (e.g. "tomorrow 9am" after 9:01am), clamp keeps us
            // safe.
            currentWhenTs = Math.max(currentWhenTs, minWhenTs());
            refreshWhenDisplay();
            flashWhenDisplay();
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

let _scheduledQueueExtensionRegistered = false;
function registerScheduledQueueExtension() {
    if (_scheduledQueueExtensionRegistered) return;
    _scheduledQueueExtensionRegistered = true;

    try {
        app.registerExtension({
            name: EXT_NAME,
            actionBarButtons: [
                {
                    icon: "pi pi-clock",
                    tooltip: t("topbar.schedule_tooltip", "Schedule current workflow (sends to ScheduledQueue, not ComfyUI native queue)"),
                    onClick: () => {
                        console.log("[ScheduledQueue] topbar Schedule clicked");
                        try {
                            openScheduleDialog();
                        } catch (error) {
                            console.error("[ScheduledQueue] failed to open Schedule dialog", error);
                        }
                    },
                },
            ],
        });
        console.log("[ScheduledQueue] topbar Schedule action registered");
    } catch (error) {
        console.error("[ScheduledQueue] topbar registration failed", error);
    }

    // Sidebar tab. The 1.49.6 framework calls render(container) ONCE per tab
    // activation -- switching to another tab and back does NOT re-invoke it.
    // We render once, then keep the container in sync with the sidebarTab
    // store via $subscribe so the panel swaps correctly when the user
    // toggles tabs.
    app.extensionManager.registerSidebarTab({
        id: TAB_ID,
        title: t("sidebar.title", "Scheduled Queue"),
        icon: "pi pi-clock",
        tooltip: t("sidebar.tab_tooltip", "Scheduled Queue (managed jobs)"),
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
}

// Registration must happen synchronously during extension discovery. Locale
// loading is best-effort and may refresh already-mounted panels when complete,
// but it must never gate the topbar/sidebar registration.
registerScheduledQueueExtension();
loadLocales().then(() => {
    window.dispatchEvent(new CustomEvent("sq:lang-changed", { detail: { lang: LANG } }));
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

const SQ_VERSION = "0.3.10";
console.log("[ScheduledQueue] Loaded version " + SQ_VERSION + " (file://" + (import.meta?.url || location.href) + ")");
console.log("[ScheduledQueue] Registered: topbar button + sidebar tab.");
