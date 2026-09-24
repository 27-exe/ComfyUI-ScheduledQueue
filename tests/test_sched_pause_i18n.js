/* The scheduled-pause panel's own strings must exist in EVERY locale file.
 *
 * A missing key does not fail loudly: t() falls back to the zh dict, then to
 * the inline default, then to the key — so an untranslated key silently
 * renders Chinese in an English UI. That is exactly the class of bug the
 * user reported ("still Chinese under en"), so assert the coverage here.
 */
"use strict";

const fs = require("fs");
const path = require("path");

const LOCALES = path.join(
    __dirname, "..", "src", "comfyui_scheduled_queue", "web", "locales",
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

const REQUIRED = {
    "sched_pause.title": true,
    "sched_pause.paause_at": false, // deliberate typo guard below
    "sched_pause.pause_at": true,
    "sched_pause.resume_at": true,
    "sched_pause.save": true,
    "sched_pause.clear": true,
    "sched_pause.armed": true,
    "sched_pause.not_armed": true,
    "sched_pause.saved": true,
    "sched_pause.cleared": true,
    "sched_pause.preview": true,
    "sched_pause.placeholder": true,
};

const langs = fs.readdirSync(LOCALES).filter((f) => f.endsWith(".json"));
check("both locales present", langs.length === 2, "found " + langs.join(","));

for (const file of langs) {
    const dict = JSON.parse(fs.readFileSync(path.join(LOCALES, file), "utf8"));
    const missing = Object.keys(REQUIRED).filter(
        (k) => REQUIRED[k] && !(k in dict),
    );
    check(`${file} defines every sched_pause key`, missing.length === 0,
          "missing: " + missing.join(", "));

    // The values must also be non-empty strings — an empty value renders as
    // nothing at all, which reads as a broken panel rather than a translation.
    const blank = Object.keys(REQUIRED).filter(
        (k) => REQUIRED[k] &&
               (typeof dict[k] !== "string" || !dict[k].trim()),
    );
    check(`${file} values are non-empty strings`, blank.length === 0,
          "blank: " + blank.join(", "));
}

// en must not be a copy of zh: a real translation, or the switch is cosmetic.
const en = JSON.parse(fs.readFileSync(path.join(LOCALES, "en.json"), "utf8"));
const zh = JSON.parse(fs.readFileSync(path.join(LOCALES, "zh.json"), "utf8"));
const sameValue = Object.keys(REQUIRED).filter(
    (k) => REQUIRED[k] && en[k] === zh[k],
);
check(
    "en values differ from zh (actually translated)",
    sameValue.length === 0,
    "identical: " + sameValue.join(", "),
);

console.log(failures === 0 ? "\nALL PASS" : `\n${failures} FAILURE(S)`);
process.exit(failures === 0 ? 0 : 1);
