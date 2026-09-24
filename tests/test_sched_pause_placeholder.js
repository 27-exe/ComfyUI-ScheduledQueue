/* The datetime-local placeholder must follow the UI language, never the
 * browser locale.
 *
 * <input type="datetime-local"> renders its placeholder from the BROWSER's
 * locale, so a Chinese browser shows 年/月/日 even with the sidebar in
 * English. Setting `placeholder` in buildPanel() fixes newly built panels,
 * but a DOM left over from an earlier build (tab never switched, page never
 * reloaded) keeps the browser-native text. A self-heal call inside the 5s
 * poll converges regardless of how the DOM came to exist.
 */
"use strict";

const fs = require("fs");
const path = require("path");

const source = fs.readFileSync(
    path.join(__dirname, "..", "src", "comfyui_scheduled_queue", "web",
        "sidebar_tab.js"),
    "utf8",
);

let failures = 0;
function check(name, cond, detail) {
    if (cond) {
        console.log(`  PASS  ${name}`);
    } else {
        failures++;
        console.log(`  FAIL  ${name}${detail ? " -> " + detail : ""}`);
    }
}

// ---- 1. the healing helper exists and sets the attribute -----------------
check(
    "a self-heal helper is defined",
    /function _healSchedPausePlaceholders\(\)/.test(source),
);
check(
    "it writes the placeholder attribute, not just the value",
    /setAttribute\("placeholder", want\)/.test(source),
);
check(
    "it covers BOTH pickers, not just one",
    /for \(const el of \[pauseAtInput, resumeAtInput\]\)/.test(source),
);
check(
    "it reads the localised text instead of hardcoding English",
    /t\("sched_pause\.placeholder"/.test(source),
);

// ---- 2. it runs on a path the user cannot avoid --------------------------
// The 5s poll calls loadSchedPause() unconditionally, so anything inside it
// eventually reaches every mounted panel.
const pollBody = source.slice(
    source.indexOf("async function loadSchedPause()"),
    source.indexOf("async function saveSchedPause()"),
);
check(
    "the heal runs inside the 5s poll body",
    /_healSchedPausePlaceholders\(\);/.test(pollBody),
);

// ---- 3. the built-in template still sets it (first paint) ----------------
const built = source.split('placeholder="${escapeHtml(t("sched_pause.placeholder"').length - 1;
check(
    "both pickers also carry a placeholder in the template",
    built === 2,
    "found " + built,
);

// ---- 4. behavioural: really run the helper against a stubbed DOM ---------
const marker = source.indexOf(
    "function _healSchedPausePlaceholders()",
);
const start = source.indexOf("{", marker);
let depth = 0, end = start;
for (let i = start; i < source.length; i++) {
    if (source[i] === "{") depth++;
    else if (source[i] === "}") { depth--; if (depth === 0) { end = i + 1; break; } }
}
const fnSrc = source.slice(start, end);

const elA = { attrs: { type: "datetime-local" },
              getAttribute(k) { return this.attrs[k] ?? null; },
              setAttribute(k, v) { this.attrs[k] = String(v); },
              removeAttribute(k) { delete this.attrs[k]; },
              hasAttribute(k) { return k in this.attrs; } };
const elB = { attrs: { type: "datetime-local" },
              getAttribute(k) { return this.attrs[k] ?? null; },
              setAttribute(k, v) { this.attrs[k] = String(v); },
              removeAttribute(k) { delete this.attrs[k]; },
              hasAttribute(k) { return k in this.attrs; } };

let healed = null;
// Re-wrap the extracted body as a standalone function: `fnSrc` is the inner
// { ... } of the original, so it is already a valid block body.
const fn = new Function(
    "pauseAtInput", "resumeAtInput", "t",
    "return function () " + fnSrc + ";",
);
try {
    healed = fn(elA, elB, (k) => k === "sched_pause.placeholder" ? "YYYY-MM-DD HH:MM" : k);
} catch (e) {
    failures++;
    console.log("  FAIL  helper is executable -> " + e.message);
}

if (healed) {
    healed();
    check(
        "an element with the browser-native text gets overwritten",
        elA.getAttribute("placeholder") === "YYYY-MM-DD HH:MM",
        "got " + JSON.stringify(elA.getAttribute("placeholder")),
    );
    check(
        "the second picker is healed too",
        elB.getAttribute("placeholder") === "YYYY-MM-DD HH:MM",
    );
    // Idempotence: the poll runs every 5s, it must not churn the attribute.
    const before = elA.getAttribute("placeholder");
    healed();
    check(
        "re-running is idempotent (no needless DOM churn on every poll)",
        elA.getAttribute("placeholder") === before,
    );
}

console.log(failures === 0 ? "\nALL PASS" : `\n${failures} FAILURE(S)`);
process.exit(failures === 0 ? 0 : 1);
