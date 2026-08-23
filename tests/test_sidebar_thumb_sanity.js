/*
 * Sanity tests for the nested-outputs thumbnail resolver in
 * src/comfyui_scheduled_queue/web/sidebar_tab.js.
 *
 * sidebar_tab.js is an ES module that imports /scripts/app.js, so we can't
 * import it directly from Node. Instead, we reproduce the two helper
 * functions verbatim and assert the expected shape. If the helpers in
 * sidebar_tab.js ever drift from these, the contract is broken — update
 * both in lockstep.
 */

"use strict";

function buildViewUrl(img) {
    if (!img || typeof img !== "object") return null;
    const filename = encodeURIComponent(img.filename || "");
    if (!filename) return null;
    const subfolder = encodeURIComponent(img.subfolder || "");
    const type = encodeURIComponent(img.type || "output");
    return `/view?filename=${filename}&subfolder=${subfolder}&type=${type}`;
}

function findFirstImageUrl(outputsDict) {
    if (!outputsDict || typeof outputsDict !== "object") return null;
    for (const nodeId of Object.keys(outputsDict)) {
        const node = outputsDict[nodeId];
        if (node && Array.isArray(node.images) && node.images[0]) {
            return buildViewUrl(node.images[0]);
        }
    }
    return null;
}

// ---------- assertions ----------
let passed = 0;
let failed = 0;
function assertEq(label, actual, expected) {
    const ok = actual === expected;
    if (ok) {
        passed++;
        console.log(`  PASS  ${label}`);
    } else {
        failed++;
        console.log(`  FAIL  ${label}`);
        console.log(`        expected: ${JSON.stringify(expected)}`);
        console.log(`        actual:   ${JSON.stringify(actual)}`);
    }
}

// 1. Happy path: nested dict with one node carrying one image.
//    Note: buildViewUrl defaults type to "output" when absent (existing
//    behavior of the helper in sidebar_tab.js, intentionally preserved).
//    → should return "/view?filename=x.png&subfolder=&type=output".
assertEq(
    "single node, single image",
    findFirstImageUrl({ "80": { images: [{ filename: "x.png" }] } }),
    "/view?filename=x.png&subfolder=&type=output"
);

// 2. Empty node (no images array) — should return null.
assertEq(
    "node without images",
    findFirstImageUrl({ "80": {} }),
    null
);

// 3. Multi-node: first node empty, second has the image — should find the
//    second one (preserves "first non-empty node wins" semantics).
assertEq(
    "skip empty node, take second",
    findFirstImageUrl({
        "80": {},
        "45": { images: [{ filename: "y.png", subfolder: "sub", type: "temp" }] },
    }),
    "/view?filename=y.png&subfolder=sub&type=temp"
);

// 4. Defensive: undefined / null / non-object inputs.
assertEq("undefined input", findFirstImageUrl(undefined), null);
assertEq("null input", findFirstImageUrl(null), null);
assertEq("string input", findFirstImageUrl("nope"), null);
assertEq("array input", findFirstImageUrl([]), null);

// 5. Empty images array inside a node — should fall through.
assertEq(
    "node with empty images array",
    findFirstImageUrl({ "80": { images: [] } }),
    null
);

// 6. URL-encoding safety: filename with spaces must be encoded.
//    Note type defaults to "output".
assertEq(
    "filename with space encodes correctly",
    findFirstImageUrl({ "1": { images: [{ filename: "my file.png" }] } }),
    "/view?filename=my%20file.png&subfolder=&type=output"
);

console.log("");
console.log(`Results: ${passed} passed, ${failed} failed`);
process.exit(failed === 0 ? 0 : 1);