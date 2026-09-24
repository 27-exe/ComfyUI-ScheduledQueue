/* Regression: the 5s poll must not wipe a time the user just picked.
 *
 * Two user-visible bugs are covered:
 *
 *   1. "点好时间后输入框自动刷新导致时间不见了" -- loadSchedPause() ran every
 *      5s and re-assigned .value unconditionally. A datetime-local element
 *      whose picker is mid-open/close reads back "" even though the user
 *      chose a time, so the poll erased the selection with no visible cause.
 *
 *   2. No way to tell saved from unsaved -- the panel showed the server
 *      truth in the armed-state line, but an unsaved pick looked identical
 *      to a cleared one.
 *
 * The guards are real code, executed here, not asserted by string matching.
 */

"use strict";

const fs = require("fs");
const path = require("path");

const source = fs.readFileSync(
    path.join(__dirname, "..", "src", "comfyui_scheduled_queue", "web", "sidebar_tab.js"),
    "utf8",
);

let failures = 0;
function check(name, cond, detail) {
    if (cond) {
        console.log(`  PASS  ${name}`);
    } else {
        failures++;
        console.log(`  FAIL  ${name}${detail ? "  -> " + detail : ""}`);
    }
}

// ---- 1. the guards must exist -------------------------------------------
check(
    "loadSchedPause routes writes through a guarded sync helper",
    source.includes("_syncSchedPauseInput(pauseAtInput, nextPause)") &&
    source.includes("_syncSchedPauseInput(resumeAtInput, nextResume)"),
);
check(
    "the unguarded direct re-assignment is gone",
    !source.includes("if (pauseAtInput.value !== nextPause) pauseAtInput.value = nextPause;"),
);
check(
    "a focus guard is present",
    /function _syncSchedPauseInput[\s\S]{0,400}document\.activeElement/.test(source),
);
check(
    "an empty-server-value content guard is present",
    /function _syncSchedPauseInput[\s\S]{0,400}if \(!nextValue && el\.value\) return;/.test(source),
);

// ---- 2. execute the real guard against the real bug --------------------
// Extract the function body from the source so this test cannot drift from
// the shipped code.
const m = source.match(/function _syncSchedPauseInput\([\s\S]*?\n    \}/);
check("guard function extracted from source", !!m);
if (m) {
    const factory = new Function(
        "document",
        `${m[0]}\nreturn _syncSchedPauseInput;`,
    );

    function run(elValue, nextValue, activeElement) {
        const el = { value: elValue };
        const doc = { activeElement: activeElement === "self" ? el : null };
        const sync = factory(doc);
        sync(el, nextValue);
        return el.value;
    }

    // Bug 1: user picked a time, the element momentarily reads back "",
    // the server has nothing saved, and the poll must NOT clear it.
    check(
        "picked-but-unsaved value survives an empty server value",
        run("2026-09-24T23:00", "", null) === "2026-09-24T23:00",
    );
    check(
        "picked-but-unsaved value survives while the picker has focus",
        run("2026-09-24T23:00", "", "self") === "2026-09-24T23:00",
    );
    check(
        "focused element is never overwritten at all",
        run("2026-09-25T01:00", "2027-01-01T00:00", "self") === "2026-09-25T01:00",
    );
    // Bug 1 tail: picker closed and reported "" -- must not blank either.
    check(
        "transitional empty local value is not overwritten by empty server",
        run("", "", null) === "",
    );
    // Normal path: server has a rule and the user is not editing -> sync it.
    check(
        "a real server value lands in an idle input",
        run("", "2026-09-25T02:30", null) === "2026-09-25T02:30",
    );
    check(
        "a stale local value is refreshed from the server when idle",
        run("2026-09-25T02:00", "2026-09-25T02:30", null) === "2026-09-25T02:30",
    );
    check(
        "an already-correct value is left untouched",
        run("2026-09-25T02:30", "2026-09-25T02:30", null) === "2026-09-25T02:30",
    );
}

// ---- 3. the user must be able to see what is armed ----------------------
check(
    "armed state renders the concrete times, not just a count",
    /armed\.push\(/.test(source) && /\.replace\("T", " "\)/.test(source),
);
check(
    "an unarmed panel says so explicitly",
    /sched_pause\.not_armed/.test(source),
);

// ---- 4. the user can always see what will be saved --------------------
check(
    "an unsaved edit renders a preview instead of the server line",
    /function _renderSchedPausePreview/.test(source) &&
    /hasPending/.test(source),
);
check(
    "the preview is recomputed on every input event",
    /addEventListener\("input", _renderSchedPausePreview\)/.test(source),
);
check(
    "the preview is explicitly labelled Unsaved, not mistaken for a saved rule",
    /sched_pause\.preview/.test(source),
);
check(
    "both halves of the pair appear in the preview",
    (source.match(/sched_pause\.pause_at/g) || []).length >= 2,
);

console.log(failures === 0 ? "\nALL PASS" : `\n${failures} FAILURE(S)`);
process.exit(failures === 0 ? 0 : 1);
