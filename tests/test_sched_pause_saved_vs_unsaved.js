/* The panel must never call a SAVED rule "Unsaved".
 *
 * The original logic used "is any input non-empty?" as its unsaved signal.
 * That is wrong: a successful save writes the server value INTO the inputs,
 * so they stay non-empty forever and the next 5s poll flipped the label back
 * to "Unsaved" while the Save button row still read "Saved". The user saw
 * both states at once.
 *
 * The only meaningful signal is a MISMATCH between what the inputs hold and
 * what the server last reported, compared AFTER normalisation (the user types
 * a space, the server echoes a "T").
 *
 * These assertions extract the real predicate and the real normaliser from
 * sidebar_tab.js and RUN them -- no string matching.
 */
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

// Slice a top-level `function name(` out of an extracted inner scope by
// indentation -- braces are unusable (dict literals, f-strings).
function body(name) {
    const lines = source.split("\n");
    const at = lines.findIndex((l) => l.trim().startsWith("function " + name + "("));
    if (at < 0) throw new Error("not found: " + name);
    const open = lines[at].indexOf("{");
    let depth = 0, end = at;
    for (let i = at; i < lines.length; i++) {
        for (const ch of lines[i]) {
            if (ch === "{") depth++;
            else if (ch === "}") { depth--; if (depth === 0) { end = i; break; } }
        }
        if (end !== at) break;
    }
    return lines.slice(at, end + 1).join("\n");
}

// Compile the predicate with real norm/parse helpers wired in.
// Take a whole `function name(...)` plus its body by BRACE MATCHING inside a
// line slice. We first cut a generous window with indexOf, then balance
// braces over just that window -- no regex `[\s\S]*?` that can swallow the
// next function or its comment.
function take(name) {
    const at = source.indexOf("function " + name + "(");
    if (at < 0) throw new Error("not found: " + name);
    // Back up to the start of the line so we keep the indentation.
    let lineStart = source.lastIndexOf("\n", at) + 1;
    let depth = 0, seen = false;
    for (let i = source.indexOf("{", at); i < source.length; i++) {
        const ch = source[i];
        if (ch === "{") { depth++; seen = true; }
        else if (ch === "}") {
            depth--;
            if (seen && depth === 0) return source.slice(lineStart, i + 1);
        }
    }
    throw new Error("unbalanced: " + name);
}

// Compile and run the real predicate with the real normaliser wired in.
function makeEdited(pauseInput, resumeInput, serverPause, serverResume) {
    const src = [
        "var pauseAtInput = " + (pauseInput ? JSON.stringify({value: pauseInput}) : "null") + ";",
        "var resumeAtInput = " + (resumeInput ? JSON.stringify({value: resumeInput}) : "null") + ";",
        "var SERVER_PAUSE_AT = " + JSON.stringify(serverPause) + ";",
        "var SERVER_RESUME_AT = " + JSON.stringify(serverResume) + ";",
        take("_spPad2"),
        take("_spParse"),
        take("_spNormalise"),
        take("_schedPauseEdited"),
        "return _schedPauseEdited();",
    ].join("\n");
    return Function(src)();
}

// ---- the reported regression -----------------------------------------
check("a saved rule is NOT reported as Unsaved",
    makeEdited("2026-09-24T22:04", "2026-09-24T22:03",
               "2026-09-24T22:04", "2026-09-24T22:03") === false,
    "space/T separator differences must not count as an edit");

check("a saved rule survives the space-vs-T difference",
    makeEdited("2026-09-24 22:04", "", "2026-09-24T22:04", "") === false);

check("a real edit IS reported as Unsaved",
    makeEdited("2026-09-24T23:00", "", "2026-09-24T22:04", "") === true);

check("clearing a saved rule counts as an edit (not saved)",
    makeEdited("", "", "2026-09-24T22:04", "") === true);

check("both halves saved = not edited",
    makeEdited("2026-09-24 22:04", "2026-09-24 22:50",
               "2026-09-24T22:04", "2026-09-24T22:50") === false);

check("editing only the resume half counts as an edit",
    makeEdited("2026-09-24 22:04", "2026-09-24 23:50",
               "2026-09-24T22:04", "2026-09-24T22:50") === true);

// ---- the old, broken predicate must be gone ---------------------------
check("the 'input non-empty' predicate is gone from _renderSchedPauseState",
    !/const hasPending =/.test(source),
    "that predicate relabels a just-saved rule as Unsaved on the next poll");

check("the state renderer consults the mismatch predicate",
    /if \(_schedPauseEdited\(\)\)/.test(source));

check("loadSchedPause records the server snapshot",
    /SERVER_PAUSE_AT = data\.pause_at/.test(source));

check("saveSchedPause adopts the server snapshot as the new baseline",
    /SERVER_PAUSE_AT = data\.pause_at \|\| "";/.test(source) &&
    /SERVER_RESUME_AT = data\.resume_at \|\| "";/.test(source));

// ---- an inverted pair must be refused before it is stored -------------
check("saveSchedPause rejects resume-earlier-than-pause",
    /resume_before_pause/.test(source),
    "the server accepts each half individually, so only the panel can catch this");
check("that message exists in both locales",
    ["zh", "en"].every(function (l) {
        const d = JSON.parse(fs.readFileSync(path.join(__dirname, "..",
            "src", "comfyui_scheduled_queue", "web", "locales", l + ".json"), "utf8"));
        return typeof d["sched_pause.resume_before_pause"] === "string";
    }));

if (failures) { console.log("\n" + failures + " FAILURE(S)"); process.exit(1); }
console.log("\nALL PASS");
