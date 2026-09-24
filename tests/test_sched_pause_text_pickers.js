"use strict";
const fs = require("fs");
const path = require("path");
const source = fs.readFileSync(
    path.join(__dirname, "..", "src", "comfyui_scheduled_queue", "web", "sidebar_tab.js"),
    "utf8");
let failures = 0;
function check(name, cond, detail) {
    if (cond) { console.log("  PASS  " + name); }
    else { failures++; console.log("  FAIL  " + name + (detail ? "  [" + detail + "]" : "")); }
}
function whole(name) {
    const at = source.indexOf("function " + name + "(");
    if (at < 0) throw new Error("not found: " + name);
    const open = source.indexOf("{", source.indexOf(")", at));
    let depth = 0;
    for (let i = open; i < source.length; i++) {
        if (source[i] === "{") depth++;
        else if (source[i] === "}") { depth--; if (depth === 0) return source.slice(at, i + 1); }
    }
    throw new Error("unbalanced: " + name);
}
const H = Function(
    ["_spPad2", "_spFormat", "_spParse", "_spNextWholeMinute", "_spNormalise"]
        .map(whole).join("\n") +
    "\nreturn { _spPad2, _spFormat, _spParse, _spNextWholeMinute, _spNormalise };"
)();
const pad2 = H._spPad2, fmt = H._spFormat, parse = H._spParse, norm = H._spNormalise;

// ---- 1. shape --------------------------------------------------------
// Strip // comments before deciding: our own comments legitimately spell out
// <input type="datetime-local"> to explain WHY we do not use it.
const codeOnly = source.replace(/^\s*\/\/.*$/gm, "");
check("no datetime-local INPUT remains (comments stripped)",
    !/<input[^>]*type="datetime-local"/.test(codeOnly),
    "an actual control would ignore the placeholder attribute in Chrome");
check("both pickers are type=text",
    (source.match(/type="text"/g) || []).length >= 2);
check("pause picker has nudge buttons",
    /data-sched-delta="pause-at:/.test(source));
check("resume picker has nudge buttons",
    /data-sched-delta="resume-at:/.test(source));
check("placeholders are localised, not hardcoded",
    /placeholder="\$\{escapeHtml\(t\("sched_pause\.placeholder/.test(source));

// ---- 2. behaviour: the extracted helpers really run -------------------
const parsed = parse("2026-09-24 23:00");
check("parse accepts a space-separated entry", parsed !== null);
check("parse accepts the T form", parse("2026-09-24T23:00") !== null);
check("parse accepts trailing seconds", parse("2026-09-24 23:00:30") !== null);
check("parse rejects garbage", parse("tomorrow") === null);
check("parse rejects an impossible date", parse("2026-13-40 25:99") === null);
check("format(parse(x)) round-trips",
    fmt(parsed) === "2026-09-24 23:00", "got " + fmt(parsed));
check("parse(format(x)) round-trips", parse(fmt(parsed)) === parsed);

const sent = norm("2026-09-24 23:00");
check("normalise emits the backend's T form",
    /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(sent), "got " + sent);
check("normalise preserves the WALL-CLOCK hour (no UTC skew)",
    sent.endsWith("T23:00"),
    "got " + sent + " -- toISOString() would have shifted it by the UTC offset");
check("normalise maps an unparseable entry to empty", norm("junk") === "");
check("next-whole-minute is in the future",
    H._spNextWholeMinute() > Math.floor(Date.now() / 1000));
check("pad2 pads", pad2(7) === "07");

// ---- 3. self-heal covers a node mounted by an older version ----------
// Re-materialise with a real fake element so we can observe the writes.
const wrote = [];
function mkEl() {
    return { _a: { type: "datetime-local" },
        getAttribute(k) { return this._a[k] ?? null; },
        setAttribute(k, v) { this._a[k] = String(v); wrote.push([k, String(v)]); },
        removeAttribute(k) { delete this._a[k]; wrote.push(["remove", k]); },
        hasAttribute(k) { return k in this._a; } };
}
const pA = mkEl(), pR = mkEl();
const heal2 = Function(["t", "decline", "pauseAtInput", "resumeAtInput"],
    whole("_healSchedPausePlaceholders") +
    "\nreturn _healSchedPausePlaceholders;")(() => "YYYY-MM-DD HH:MM", null, pA, pR);
heal2();
check("heal flips a stale datetime-local to text",
    pA._a.type === "text" && pR._a.type === "text",
    JSON.stringify(pA._a) + " / " + JSON.stringify(pR._a));
check("heal drops the now-meaningless step attr",
    !("step" in pA._a));
check("heal writes the placeholder",
    pA._a.placeholder === "YYYY-MM-DD HH:MM");
check("heal leaves an already-correct node alone",
    (function () { const b = mkEl(); b._a = { type: "text", placeholder: "YYYY-MM-DD HH:MM" };
        const before = JSON.stringify(b._a);
        Function(["t", "decline", "pauseAtInput", "resumeAtInput"],
            whole("_healSchedPausePlaceholders") +
            "\nreturn _healSchedPausePlaceholders;")(() => "YYYY-MM-DD HH:MM", null, b, null)();
        return JSON.stringify(b._a) === before; })());

// ---- 4. nudges and saves must refuse a past timestamp ----------------
check("the nudge handler clamps to the next whole minute",
    /Math\.max\(raw, floor\)/.test(source),
    "a -1h on a near-future value would otherwise round-trip into the past");
check("the nudge handler computes a floor",
    /const floor = _spNextWholeMinute\(\);/.test(source));
check("saveSchedPause pre-checks for a past value",
    /ts < floor/.test(source));
check("the in-past message key exists in both locales",
    (function () {
        ["zh", "en"].forEach(function (l) {
            var d = JSON.parse(fs.readFileSync(
                path.join(__dirname, "..", "src", "comfyui_scheduled_queue",
                    "web", "locales", l + ".json"), "utf8"));
            if (!d["sched_pause.in_past"]) throw new Error("missing in_past in " + l);
        });
        return true;
    })());

// ---- 5. the flat-load trap must not reappear -------------------------
var schedSrc = fs.readFileSync(
    path.join(__dirname, "..", "src", "comfyui_scheduled_queue", "scheduler.py"),
    "utf8");
check("scheduler loads routes through a loader helper",
    /def _load_routes_module\(/.test(schedSrc) &&
    /_routes = _load_routes_module\(\)/.test(schedSrc),
    "a bare absolute import raises ModuleNotFoundError under ComfyUI's flat load");
check("the loader falls back to spec_from_file_location",
    /spec_from_file_location/.test(schedSrc));
check("no bare absolute routes import remains in scheduler",
    !/from comfyui_scheduled_queue import routes as _routes/.test(schedSrc));


if (failures) { console.log("\n" + failures + " FAILURE(S)"); process.exit(1); }
console.log("\nALL PASS");
