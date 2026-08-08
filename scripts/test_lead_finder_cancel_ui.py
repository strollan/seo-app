"""
Regression test for the "Cancelling scan..." UI getting stuck after a
successful backend cancel.

Root cause (app/main.py, leadbot_live_page's poll()): once /lead-bot/live-
status/{job_id} reports the terminal "cancelled" status, poll() finalized
the Cancel button's text but never touched the separate #cancelNote
element (the "Cancelling scan..." text next to the button). That element
is only ever set by the cancelScan() click handler itself, and that
handler's own POST response almost always still says "cancelling" (the
backend subprocess is still exiting at that point) -- so the note was
left showing "Cancelling scan..." forever, even though the job file, the
button, and #message all correctly reflected the finished cancellation.

This test extracts the actual <script> block embedded in
leadbot_live_page() (the same source string served to real browsers,
verified via inspect.getsource so no template drift is possible) and
executes it for real under Node with a minimal DOM/fetch/setTimeout shim
-- not a re-implementation of the polling logic -- so it exercises the
exact code that runs in production.
"""

import inspect
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("OPENAI_API_KEY", "test-placeholder-not-a-real-key")

import app.main as appmain

SCRIPT_RE = re.compile(r"<script>\n(.*?)\n</script>", re.DOTALL)


def _extract_live_page_js():
    source = inspect.getsource(appmain.leadbot_live_page)
    match = SCRIPT_RE.search(source)
    assert match, "could not find <script> block in leadbot_live_page source"
    js = match.group(1)

    # Undo f-string brace-doubling. The three real (single-brace) Python
    # interpolations left in the extracted block are replaced with fixed
    # test values.
    js = js.replace("{{", "\x00").replace("}}", "\x01")
    js = js.replace("\x00", "{").replace("\x01", "}")
    js = js.replace('"{job_id}"', '"test-job-id"')
    js = js.replace("{is_guest_js}", "false")
    js = js.replace("{json.dumps(live_csrf_token)}", '"test-csrf-token"')

    # Auto-invoked in the browser on page load; the harness drives poll()
    # explicitly instead so each call can be observed and controlled.
    js = re.sub(r"\npoll\(\);\s*$", "", js)

    return js


HARNESS_TEMPLATE = r"""
const vm = require("vm");
const assert = require("assert");

const LIVE_JS = %(live_js)s;

function makeElement(id) {
    return {
        id,
        textContent: "",
        innerHTML: "",
        className: "",
        style: { display: "" },
        disabled: false,
        classListItems: new Set(),
        classList: {
            add(c) { elementFor(id).classListItems.add(c); },
            contains(c) { return elementFor(id).classListItems.has(c); },
        },
        children: [],
        appendChild(child) {
            this.children.push(child);
            if (child && child.id) {
                elements.set(child.id, child);
            }
        },
        addEventListener() {},
        querySelector() { return null; },
    };
}

const KNOWN_IDS = [
    "cancelScanBtn", "cancelNote", "guestContinueBtn", "message",
    "found", "cached", "enriched", "needs", "liveConsoleLine1",
    "liveConsoleLine2", "liveConsoleLine3", "leads", "exportWrap",
    "exportLink", "exportWrapBottom", "exportLinkBottom",
];

const elements = new Map();
for (const id of KNOWN_IDS) elements.set(id, makeElement(id));

function elementFor(id) { return elements.get(id); }

const statusBox = makeElement("__statusBox__");
elements.set("__statusBox__", statusBox);

const fakeDocument = {
    getElementById(id) {
        return elements.has(id) ? elements.get(id) : null;
    },
    querySelector(sel) {
        return sel === ".status" ? statusBox : null;
    },
    createElement(tag) {
        return makeElement("__created_" + Math.random());
    },
};

let fetchQueue = [];
function queueFetch(body) {
    fetchQueue.push(body);
}
const fetchCalls = [];
async function fakeFetch(url, opts) {
    fetchCalls.push(url);
    const body = fetchQueue.shift();
    if (body === undefined) {
        throw new Error("no queued fetch response for " + url);
    }
    return { json: async () => body };
}

let scheduledPolls = 0;
function fakeSetTimeout(fn, ms) {
    scheduledPolls += 1;
    return 0;
}

const sandbox = {
    document: fakeDocument,
    fetch: fakeFetch,
    setTimeout: fakeSetTimeout,
    console,
    URLSearchParams,
    encodeURIComponent,
};
vm.createContext(sandbox);
vm.runInContext(LIVE_JS, sandbox, { filename: "leadbot_live_page_script.js" });

async function main() {
    const results = {};

    // --- Sequence reproducing the production incident ---
    // 1) user clicks Cancel; POST responds while the worker is still
    //    exiting, so status is still "cancelling".
    queueFetch({ status: "cancelling", message: "Cancelling scan..." });
    await sandbox.cancelScan();
    results.afterClickNote = elementFor("cancelNote").textContent;
    results.afterClickBtnDisabled = elementFor("cancelScanBtn").disabled;

    // 2) a status poll lands while still cancelling: must keep polling
    //    and must NOT prematurely show a final state.
    scheduledPolls = 0;
    queueFetch({
        status: "cancelling", message: "Cancelling scan...",
        leads: [], counts: {}, params: {},
    });
    await sandbox.poll();
    results.stillCancellingNote = elementFor("cancelNote").textContent;
    results.stillCancellingScheduledNextPoll = scheduledPolls > 0;

    // 3) the backend finalizes -- status is now "cancelled". The note,
    //    the button, and the status box must all reflect this, and no
    //    further poll may be scheduled.
    scheduledPolls = 0;
    queueFetch({
        status: "cancelled", message: "Scan cancelled.",
        leads: [], counts: {}, params: {},
    });
    await sandbox.poll();
    results.finalNote = elementFor("cancelNote").textContent;
    results.finalBtnText = elementFor("cancelScanBtn").textContent;
    results.finalBtnDisabled = elementFor("cancelScanBtn").disabled;
    results.finalStatusBoxClass = statusBox.classListItems.has("leadbot-cancelled-state");
    results.finalScheduledNextPoll = scheduledPolls > 0;

    console.log(JSON.stringify(results));
}

main().catch((e) => {
    console.error(e.stack || String(e));
    process.exit(1);
});
"""


DONE_HARNESS_TEMPLATE = r"""
const vm = require("vm");

const LIVE_JS = %(live_js)s;

function makeElement(id) {
    return {
        id, textContent: "", innerHTML: "", className: "",
        style: { display: "" }, disabled: false,
        classListItems: new Set(),
        classList: {
            add(c) { elementFor(id).classListItems.add(c); },
            contains(c) { return elementFor(id).classListItems.has(c); },
        },
        children: [],
        appendChild(child) {
            this.children.push(child);
            if (child && child.id) elements.set(child.id, child);
        },
        addEventListener() {},
    };
}

const KNOWN_IDS = [
    "cancelScanBtn", "cancelNote", "guestContinueBtn", "message",
    "found", "cached", "enriched", "needs", "liveConsoleLine1",
    "liveConsoleLine2", "liveConsoleLine3", "leads", "exportWrap",
    "exportLink", "exportWrapBottom", "exportLinkBottom",
];
const elements = new Map();
for (const id of KNOWN_IDS) elements.set(id, makeElement(id));
function elementFor(id) { return elements.get(id); }
const statusBox = makeElement("__statusBox__");
elements.set("__statusBox__", statusBox);

const fakeDocument = {
    getElementById(id) { return elements.has(id) ? elements.get(id) : null; },
    querySelector(sel) { return sel === ".status" ? statusBox : null; },
    createElement(tag) { return makeElement("__created_" + Math.random()); },
};

let fetchQueue = [];
function queueFetch(body) { fetchQueue.push(body); }
async function fakeFetch(url, opts) {
    const body = fetchQueue.shift();
    if (body === undefined) throw new Error("no queued fetch response for " + url);
    return { json: async () => body };
}
let scheduledPolls = 0;
function fakeSetTimeout(fn, ms) { scheduledPolls += 1; return 0; }

const sandbox = {
    document: fakeDocument, fetch: fakeFetch, setTimeout: fakeSetTimeout,
    console, URLSearchParams, encodeURIComponent,
};
vm.createContext(sandbox);
vm.runInContext(LIVE_JS, sandbox, { filename: "leadbot_live_page_script.js" });

async function main() {
    const results = {};

    queueFetch({
        status: "%(status)s", message: "%(message)s",
        error_code: "%(error_code)s",
        leads: [], counts: {}, params: {},
    });
    scheduledPolls = 0;
    await sandbox.poll();
    results.messageText = elementFor("message").innerHTML;
    results.scheduledNextPoll = scheduledPolls > 0;
    results.statusBoxClasses = Array.from(statusBox.classListItems);

    console.log(JSON.stringify(results));
}

main().catch((e) => {
    console.error(e.stack || String(e));
    process.exit(1);
});
"""


class LeadFinderCancelUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.live_js = _extract_live_page_js()

    def _run_node(self, template, **extra):
        script = template % {"live_js": json.dumps(self.live_js), **extra}
        proc = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(
            proc.returncode, 0,
            f"node harness failed:\nstdout={proc.stdout}\nstderr={proc.stderr}",
        )
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_cancel_sequence_finalizes_note_and_stops_polling(self):
        r = self._run_node(HARNESS_TEMPLATE)

        # Right after the click, the note shows the temporary message.
        self.assertEqual(r["afterClickNote"], "Cancelling scan...")
        self.assertTrue(r["afterClickBtnDisabled"])

        # While still "cancelling", polling must continue and the note
        # must not be finalized early.
        self.assertEqual(r["stillCancellingNote"], "Cancelling scan...")
        self.assertTrue(r["stillCancellingScheduledNextPoll"])

        # Once the backend reports "cancelled", the note, button, and
        # status box must all reach a final state, and polling must stop.
        self.assertEqual(r["finalNote"], "Scan cancelled.")
        self.assertEqual(r["finalBtnText"], "Scan Cancelled")
        self.assertTrue(r["finalBtnDisabled"])
        self.assertTrue(r["finalStatusBoxClass"])
        self.assertFalse(r["finalScheduledNextPoll"])

    def test_done_terminal_handling_unchanged(self):
        r = self._run_node(
            DONE_HARNESS_TEMPLATE,
            status="done", message="Scan complete.", error_code="",
        )
        self.assertIn("Scan complete", r["messageText"])
        self.assertFalse(r["scheduledNextPoll"])
        self.assertIn("leadbot-done-state", r["statusBoxClasses"])

    def test_error_terminal_handling_unchanged(self):
        r = self._run_node(
            DONE_HARNESS_TEMPLATE,
            status="error", message="Lead search temporarily unavailable",
            error_code="search_provider_unavailable",
        )
        self.assertFalse(r["scheduledNextPoll"])


if __name__ == "__main__":
    unittest.main()
