/* Regression checks for status-specific row actions in sidebar_tab.js. */

"use strict";

const fs = require("fs");
const path = require("path");

const source = fs.readFileSync(
    path.join(__dirname, "..", "src", "comfyui_scheduled_queue", "web", "sidebar_tab.js"),
    "utf8",
);

if (!source.includes('j.status === "dispatched" ? `<button data-act="row-pause"')) {
    throw new Error("dispatched rows must render the global pause action");
}
if (source.includes('j.status === "scheduled" ? `<button data-act="row-pause"')) {
    throw new Error("scheduled rows must not render a row-level pause action");
}