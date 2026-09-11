/* Regression checks for the Schedule dialog's "remember last settings"
 * behaviour (LS_DIALOG_PREFS_KEY) and the tomorrow-7am preset.
 *
 * Two kinds of assertion, mirroring test_sidebar_actions.js:
 *   1. source shape -- the right code paths exist (and the old 9am ones do not)
 *   2. real behaviour -- nextClockTime / loadDialogPrefs / saveDialogPrefs are
 *      extracted from sidebar_tab.js and actually executed against a stubbed
 *      localStorage, so the boundary maths is verified rather than assumed.
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
        console.log(`  FAIL  ${name}${detail ? " -- " + detail : ""}`);
    }
}
function checkEq(name, got, want) {
    check(name, got === want, `got=${got} want=${want}`);
}

// ---------------------------------------------------------------------
// 1) Source shape
// ---------------------------------------------------------------------

console.log("Source shape:");

check(
    "preset is computed via nextClockTime(7, 0, now)",
    source.includes("const next7 = nextClockTime(7, 0, now);"),
);
check(
    "no leftover tomorrow7 identifier",
    !source.includes("const tomorrow7"),
);
check(
    "no leftover tomorrow9 identifier",
    !source.includes("tomorrow9"),
);
check(
    "no leftover setHours(9, 0, 0, 0)",
    !/\bsetHours\(9, 0, 0, 0\)/.test(source),
);
check(
    "no leftover setHours(7, 0, 0, 0) inline IIFE",
    // nextClockTime() does its own setHours internally; what we forbid here
    // is a hand-rolled Date constructor that hard-codes 07:00 -- those were
    // the stale-date bug in the previous shape.
    !/d\.setHours\(7, 0, 0, 0\)/.test(source),
);
check(
    "preset is pushed with the nextClockTime absolute timestamp",
    source.includes("presets.push({ label: t(\"dialog.preset.next7\", \"next 7am\"), absolute: next7 });"),
);
check(
    "prefs key declared",
    source.includes('const LS_DIALOG_PREFS_KEY = "sq.dialog-prefs";'),
);
check(
    "count input is pre-filled from the remembered count",
    source.includes('data-role="count" type="number" min="1" max="50" value="${restoredCount}"'),
);
check(
    "preset click records the chip index",
    source.includes("lastPresetIdx = idx;"),
);
check(
    "hand-editing the time clears the preset memory",
    source.includes("lastPresetIdx = null;"),
);
check(
    "prefs are saved only after a successful POST",
    // The save must sit after the !resp.ok early-return, not before it.
    source.indexOf("saveDialogPrefs({") > source.indexOf('if (!resp.ok) {\n                    alert(t("error.add_failed"'),
);
check(
    "restored preset is re-clicked on open",
    source.includes("if (restoredBtn) restoredBtn.click();"),
);
check(
    "restored clock time falls back to nextClockTime",
    source.includes("nextClockTime(restoredClock.hh, restoredClock.mm, now),"),
);

// Locale files must agree with the chip label.
for (const [lang, want] of [["en", "next 7am"], ["zh", "下一个 7 点"]]) {
    const loc = JSON.parse(fs.readFileSync(
        path.join(__dirname, "..", "src", "comfyui_scheduled_queue", "web", "locales", `${lang}.json`),
        "utf8",
    ));
    checkEq(`locale ${lang} preset label`, loc["dialog.preset.next7"], want);
    // Defensive: the old "tomorrow" key should be gone so it can't get
    // resurrected if the source ever rewrites the chip.
    checkEq(`locale ${lang} old preset key removed`, loc["dialog.preset.tomorrow"], undefined);
}

// ---------------------------------------------------------------------
// 2) Real behaviour -- extract the functions and run them
// ---------------------------------------------------------------------

/** Slice out a top-level `function name(...) { ... }` by brace matching. */
function extractFn(src, name) {
    const start = src.indexOf(`function ${name}(`);
    if (start < 0) throw new Error(`cannot find function ${name}`);
    const open = src.indexOf("{", start);
    let depth = 0;
    for (let i = open; i < src.length; i++) {
        const ch = src[i];
        if (ch === "{") depth++;
        else if (ch === "}") {
            depth--;
            if (depth === 0) return src.slice(start, i + 1);
        }
    }
    throw new Error(`unbalanced braces in ${name}`);
}

const lsStore = Object.create(null);
const fakeWindow = {
    localStorage: {
        getItem: (k) => (k in lsStore ? lsStore[k] : null),
        setItem: (k, v) => { lsStore[k] = String(v); },
    },
};

const preamble = 'const LS_DIALOG_PREFS_KEY = "sq.dialog-prefs";';
const body = ["nextClockTime", "loadDialogPrefs", "saveDialogPrefs"]
    .map((n) => extractFn(source, n))
    .join("\n");

// eslint-disable-next-line no-new-func -- deliberate: we execute the shipped source
const api = new Function(
    "window",
    `${preamble}\n${body}\nreturn { nextClockTime, loadDialogPrefs, saveDialogPrefs };`,
)(fakeWindow);

console.log("\nnextClockTime boundaries (all local time):");

// 2026-09-11 in local time.
const at = (h, mi, s = 0) => new Date(2026, 8, 11, h, mi, s, 0);
const secs = (d) => Math.floor(d.getTime() / 1000);

checkEq(
    "now 06:00, want 07:00 -> today 07:00",
    api.nextClockTime(7, 0, secs(at(6, 0))),
    secs(at(7, 0)),
);
checkEq(
    "now 06:59:59, want 07:00 -> today 07:00 (one second of headroom)",
    api.nextClockTime(7, 0, secs(at(6, 59, 59))),
    secs(at(7, 0)),
);
checkEq(
    "now 07:00:00 exactly, want 07:00 -> tomorrow (never schedule the past)",
    api.nextClockTime(7, 0, secs(at(7, 0, 0))),
    secs(new Date(2026, 8, 12, 7, 0, 0)),
);
checkEq(
    "now 08:00, want 07:00 -> tomorrow 07:00",
    api.nextClockTime(7, 0, secs(at(8, 0))),
    secs(new Date(2026, 8, 12, 7, 0, 0)),
);
checkEq(
    "now 23:30, want 07:00 -> tomorrow 07:00 (the overnight-queue case)",
    api.nextClockTime(7, 0, secs(at(23, 30))),
    secs(new Date(2026, 8, 12, 7, 0, 0)),
);
checkEq(
    "now 00:05, want 23:00 -> today 23:00",
    api.nextClockTime(23, 0, secs(at(0, 5))),
    secs(at(23, 0)),
);
checkEq(
    "minutes are honoured (now 06:00, want 06:30)",
    api.nextClockTime(6, 30, secs(at(6, 0))),
    secs(at(6, 30)),
);

// nextClockTime at exactly HH:00:00 -- the chip's stated contract:
//   00:00–06:59 → today 07:00
//   07:00–23:59 → tomorrow 07:00
// (07:00:00 itself must roll to tomorrow; we already covered that above.
// Here we check the *chip's "07:00" case specifically, since that is the
// chip the user just re-pointed at this function.)
console.log("\nnextClockTime(7, 0) chip semantics:");
checkEq(
    "now 00:00 -> today 07:00 (morning chip: today)",
    api.nextClockTime(7, 0, secs(new Date(2026, 8, 11, 0, 0, 0))),
    secs(new Date(2026, 8, 11, 7, 0, 0)),
);
checkEq(
    "now 06:30 -> today 07:00 (morning chip: today, half-hour before)",
    api.nextClockTime(7, 0, secs(new Date(2026, 8, 11, 6, 30, 0))),
    secs(new Date(2026, 8, 11, 7, 0, 0)),
);
checkEq(
    "now 08:00 -> tomorrow 07:00 (morning chip: tomorrow, one hour past)",
    api.nextClockTime(7, 0, secs(new Date(2026, 8, 11, 8, 0, 0))),
    secs(new Date(2026, 8, 12, 7, 0, 0)),
);
checkEq(
    "now 23:59:59 -> tomorrow 07:00 (morning chip: tomorrow, last second)",
    api.nextClockTime(7, 0, secs(new Date(2026, 8, 11, 23, 59, 59))),
    secs(new Date(2026, 8, 12, 7, 0, 0)),
);

console.log("\nprefs round-trip:");

api.saveDialogPrefs({ preset: 4, hh: 7, mm: 0, count: 4 });
checkEq(
    "saved prefs read back",
    JSON.stringify(api.loadDialogPrefs()),
    JSON.stringify({ preset: 4, hh: 7, mm: 0, count: 4 }),
);
checkEq("key used matches the documented one", "sq.dialog-prefs" in lsStore, true);

lsStore["sq.dialog-prefs"] = "{ this is not json";
checkEq("corrupt JSON -> null (no throw)", api.loadDialogPrefs(), null);

lsStore["sq.dialog-prefs"] = "[1,2,3]";
checkEq("array value -> null (guarded)", api.loadDialogPrefs(), null);

lsStore["sq.dialog-prefs"] = '"a string"';
checkEq("primitive value -> null (guarded)", api.loadDialogPrefs(), null);

delete lsStore["sq.dialog-prefs"];
checkEq("absent key -> null", api.loadDialogPrefs(), null);

// localStorage that throws (Safari private mode / quota) must not break the dialog.
const throwingWindow = {
    localStorage: {
        getItem: () => { throw new Error("blocked"); },
        setItem: () => { throw new Error("quota"); },
    },
};
const apiBlocked = new Function(
    "window",
    `${preamble}\n${body}\nreturn { loadDialogPrefs, saveDialogPrefs };`,
)(throwingWindow);
let blockedOk = true;
let blockedVal = "unset";
try {
    blockedVal = apiBlocked.loadDialogPrefs();
    apiBlocked.saveDialogPrefs({ count: 2 });
} catch (e) {
    blockedOk = false;
}
check("throwing localStorage is swallowed (load)", blockedOk && blockedVal === null);
check("throwing localStorage is swallowed (save)", blockedOk);

console.log("");
if (failures) {
    console.log(`Results: ${failures} FAILED`);
    process.exit(1);
}
console.log("Results: all passed");
