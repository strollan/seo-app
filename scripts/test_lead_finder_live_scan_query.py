"""
Regression tests for two Lead Finder live-scan console fixes:

1. SCAN line copy. app.main.leadbot_live_page's <span id="liveConsoleLine1">
   (the "scan" console line) used to be driven straight from job.message.
   That's fine for ordinary progress text ("Found a lead: ...",
   "Searching local results for batch N of M..."), but job.message is also
   where agents.lead_live_job_agent.INVALID_MARKET_LOCATION_MESSAGE ("Enter
   a City, State or ZIP Code, such as Albany, NY or 12207.") lands once a
   scan errors out on an unresolvable market -- and that form-validation
   copy has no business appearing in the live scan console. The fix makes
   liveConsoleLine1 always describe the job's actual submitted
   keyword/market (server-rendered initially from job.params, then kept in
   sync client-side by poll()'s new formatScanQuery() helper) instead of
   ever echoing job.message, so that text can no longer leak in there.

2. Cancelling-state helper text. cancelScan()'s non-"cancelled" response
   branch used to set #cancelNote to `data.message || "Cancelling
   scan..."`. If Cancel is clicked on a job that had already reached a
   terminal "error" status (e.g. the same invalid-market case above) before
   the click, cancel_job() returns that job unchanged -- so data.status is
   "error", not "cancelled", data.message is that stale error text, and it
   would show up right beside the "Cancelling..." button. The fix drops
   the data.message fallback entirely: that branch always shows a plain
   "Cancelling scan..." now.

Two test styles, matching existing conventions in this directory:

- LiveScanQueryRenderingTests: a real FastAPI TestClient hitting
  GET /lead-bot/live/{job_id} and asserting on the rendered HTML (same
  pattern as test_contact_lookup_wording.py's LiveScanPageWordingTests).
- LiveScanQueryJsTests / CancelHelperTextJsTests: extract the exact
  <script> block from leadbot_live_page's source (via inspect.getsource,
  so there is no template drift) and execute it for real under Node with a
  minimal DOM/fetch/setTimeout shim -- same harness pattern and fakeDocument
  fixture as scripts/test_lead_finder_cancel_ui.py.
"""

import inspect
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("OPENAI_API_KEY", "test-placeholder-not-a-real-key")

from fastapi.testclient import TestClient

import app.main as appmain
import agents.auth_agent as auth_agent
import agents.lead_live_job_agent as job_agent
from agents.lead_live_job_agent import INVALID_MARKET_LOCATION_MESSAGE

VALID_PASSWORD = "correct-horse-battery-staple"

# The retired copy: must never appear anywhere in the live scan console,
# regardless of job status.
RETIRED_ZIP_HELP_TEXT = "Enter a City, State or ZIP Code"


class LiveScanQueryRenderingTests(unittest.TestCase):
    """Rendered /lead-bot/live/{job_id} (app.main.leadbot_live_page)."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

        self.auth_db_path = Path(self.tmpdir.name) / "test_auth.db"
        db_patch = mock.patch.object(auth_agent, "AUTH_DB", self.auth_db_path)
        db_patch.start()
        self.addCleanup(db_patch.stop)
        auth_agent.init_auth_db()

        auth_agent.create_user("user1", VALID_PASSWORD, role="standard", email="user1@example.com")
        self.client = TestClient(appmain.app)

        self._job_files_to_remove = []

    def tearDown(self):
        for path in self._job_files_to_remove:
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def login(self, client, username):
        user = auth_agent.get_user_by_username(username)
        token = auth_agent.create_session(user)
        client.cookies.set(appmain.AUTH_COOKIE_NAME, token)
        return token

    def write_fake_job(self, params, status="running", message="Working"):
        job_id = uuid.uuid4().hex[:16]
        job = {
            "job_id": job_id,
            "status": status,
            "message": message,
            "params": {
                "owner_email": "user1@example.com",
                "owner_username": "user1",
                **params,
            },
            "leads": [],
            "counts": {"found": 0, "cached": 0, "enriched": 0, "needs_research": 0},
            "export_file": "",
        }
        job_agent.write_job(job)
        self._job_files_to_remove.append(job_agent.job_path(job_id))
        return job_id

    def _get(self, job_id):
        self.login(self.client, "user1")
        resp = self.client.get(f"/lead-bot/live/{job_id}")
        self.assertEqual(resp.status_code, 200)
        return resp.text

    def test_scan_line_shows_actual_keyword_and_market(self):
        job_id = self.write_fake_job({"keyword": "plumber", "market": "Albany, NY"})
        html = self._get(job_id)

        self.assertIn(
            '<span id="liveConsoleLine1">Searching for &quot;plumber&quot; in Albany, NY.</span>',
            html,
        )

    def test_retired_zip_help_text_never_appears(self):
        job_id = self.write_fake_job({"keyword": "plumber", "market": "Albany, NY"})
        html = self._get(job_id)
        self.assertNotIn(RETIRED_ZIP_HELP_TEXT, html)

    def test_retired_zip_help_text_absent_even_on_invalid_market_error_job(self):
        # The scenario that produced the original bug: the job already
        # errored out because the market couldn't be resolved, and
        # job.message is literally the ZIP-code helper copy. The SCAN line
        # must not echo it.
        job_id = self.write_fake_job(
            {"keyword": "plumber", "market": "not a real place"},
            status="error",
            message=INVALID_MARKET_LOCATION_MESSAGE,
        )
        html = self._get(job_id)
        self.assertNotIn(RETIRED_ZIP_HELP_TEXT, html)
        self.assertNotIn(INVALID_MARKET_LOCATION_MESSAGE, html)

    def test_degrades_gracefully_with_only_keyword(self):
        job_id = self.write_fake_job({"keyword": "plumber", "market": ""})
        html = self._get(job_id)
        self.assertIn(
            '<span id="liveConsoleLine1">Searching for &quot;plumber&quot;.</span>',
            html,
        )
        self.assertNotIn(RETIRED_ZIP_HELP_TEXT, html)

    def test_degrades_gracefully_with_only_market(self):
        job_id = self.write_fake_job({"keyword": "", "market": "Albany, NY"})
        html = self._get(job_id)
        self.assertIn(
            '<span id="liveConsoleLine1">Searching in Albany, NY.</span>',
            html,
        )
        self.assertNotIn(RETIRED_ZIP_HELP_TEXT, html)

    def test_degrades_gracefully_with_neither_value(self):
        job_id = self.write_fake_job({"keyword": "", "market": ""})
        html = self._get(job_id)
        # Falls back to the original generic placeholder rather than
        # rendering blank/broken text.
        self.assertIn(
            '<span id="liveConsoleLine1">Initializing Lead Finder crawler...</span>',
            html,
        )
        self.assertNotIn(RETIRED_ZIP_HELP_TEXT, html)

    def test_keyword_and_market_are_html_escaped(self):
        job_id = self.write_fake_job({"keyword": '<b>"evil"</b>', "market": "<i>NY</i>"})
        html = self._get(job_id)
        self.assertNotIn("<b>evil", html)
        self.assertIn("&lt;b&gt;", html)
        self.assertIn("&lt;i&gt;NY&lt;/i&gt;", html)


SCRIPT_RE = re.compile(r"<script>\n(.*?)\n</script>", re.DOTALL)


def _extract_live_page_js():
    source = inspect.getsource(appmain.leadbot_live_page)
    match = SCRIPT_RE.search(source)
    assert match, "could not find <script> block in leadbot_live_page source"
    js = match.group(1)

    js = js.replace("{{", "\x00").replace("}}", "\x01")
    js = js.replace("\x00", "{").replace("\x01", "}")
    js = js.replace('"{job_id}"', '"test-job-id"')
    js = js.replace("{is_guest_js}", "false")
    js = js.replace("{json.dumps(live_csrf_token)}", '"test-csrf-token"')

    js = re.sub(r"\npoll\(\);\s*$", "", js)

    return js


HARNESS_PREAMBLE = r"""
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
const bodyEl = makeElement("__body__");
elements.set("__body__", bodyEl);

const fakeDocument = {
    body: bodyEl,
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
"""


SCAN_LINE_HARNESS = (
    HARNESS_PREAMBLE
    + r"""
async function main() {
    const results = {};

    // Actively running scan: SCAN line must show the real query, not
    // job.message.
    queueFetch({
        status: "running", message: "Searching local results for batch 2 of 5...",
        leads: [], counts: {}, params: { keyword: "apples", market: "Shirley, NY" },
    });
    await sandbox.poll();
    results.runningLine1 = elementFor("liveConsoleLine1").textContent;

    // Keyword only.
    queueFetch({
        status: "running", message: "Working",
        leads: [], counts: {}, params: { keyword: "apples", market: "" },
    });
    await sandbox.poll();
    results.keywordOnlyLine1 = elementFor("liveConsoleLine1").textContent;

    // Market only.
    queueFetch({
        status: "running", message: "Working",
        leads: [], counts: {}, params: { keyword: "", market: "Shirley, NY" },
    });
    await sandbox.poll();
    results.marketOnlyLine1 = elementFor("liveConsoleLine1").textContent;

    // Neither value available: degrade gracefully, no blank/broken text.
    queueFetch({
        status: "running", message: "Working",
        leads: [], counts: {}, params: {},
    });
    await sandbox.poll();
    results.neitherLine1 = elementFor("liveConsoleLine1").textContent;

    // The original bug scenario: job errored out on an invalid market, so
    // job.message is literally the ZIP-code helper copy. Must never reach
    // the console.
    queueFetch({
        status: "error", error_code: "invalid_market_location",
        message: %(invalid_market_message)s,
        leads: [], counts: {}, params: { keyword: "plumber", market: "not a real place" },
    });
    await sandbox.poll();
    results.invalidMarketErrorLine1 = elementFor("liveConsoleLine1").textContent;

    // Terminal cancelled: still finalizes to "Scan cancelled." (existing
    // behavior, unaffected by the SCAN-line change).
    queueFetch({
        status: "cancelled", message: "Scan cancelled.",
        leads: [], counts: {}, params: { keyword: "apples", market: "Shirley, NY" },
    });
    await sandbox.poll();
    results.cancelledLine1 = elementFor("liveConsoleLine1").textContent;
    results.cancelledBodyClass = bodyEl.classListItems.has("leadbot-live-final");
    results.cancelledStatusBoxClass = statusBox.classListItems.has("leadbot-cancelled-state");
    results.cancelledBtnDisabled = elementFor("cancelScanBtn").disabled;

    console.log(JSON.stringify(results));
}

main().catch((e) => {
    console.error(e.stack || String(e));
    process.exit(1);
});
"""
)


CANCEL_HELPER_TEXT_HARNESS = (
    HARNESS_PREAMBLE
    + r"""
async function main() {
    const results = {};

    // Cancel clicked on a job that had already errored out on an invalid
    // market: cancel_job() returns the job unchanged (status "error"),
    // and its message is the ZIP-code helper copy. That must never show
    // up next to "Cancelling...".
    queueFetch({ status: "error", message: %(invalid_market_message)s });
    const pending = sandbox.cancelScan();

    // Assert the state immediately after the synchronous portion of the
    // click handler runs, before the queued fetch response is awaited --
    // this is the "REMOVE/HIDE ... immediately" requirement.
    results.immediatelyAfterClickNote = elementFor("cancelNote").textContent;
    results.immediatelyAfterClickBtnText = elementFor("cancelScanBtn").textContent;

    await pending;
    results.afterResponseNote = elementFor("cancelNote").textContent;
    results.afterResponseBtnText = elementFor("cancelScanBtn").textContent;

    console.log(JSON.stringify(results));
}

main().catch((e) => {
    console.error(e.stack || String(e));
    process.exit(1);
});
"""
)


class _NodeHarnessTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.live_js = _extract_live_page_js()

    def _run_node(self, template):
        script = template % {
            "live_js": json.dumps(self.live_js),
            "invalid_market_message": json.dumps(INVALID_MARKET_LOCATION_MESSAGE),
        }
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


class LiveScanQueryJsTests(_NodeHarnessTestCase):
    def test_running_scan_shows_actual_query(self):
        r = self._run_node(SCAN_LINE_HARNESS)
        self.assertEqual(r["runningLine1"], 'Searching for "apples" in Shirley, NY.')

    def test_keyword_only_degrades_gracefully(self):
        r = self._run_node(SCAN_LINE_HARNESS)
        self.assertEqual(r["keywordOnlyLine1"], 'Searching for "apples".')

    def test_market_only_degrades_gracefully(self):
        r = self._run_node(SCAN_LINE_HARNESS)
        self.assertEqual(r["marketOnlyLine1"], "Searching in Shirley, NY.")

    def test_neither_value_falls_back_without_blank_or_broken_text(self):
        r = self._run_node(SCAN_LINE_HARNESS)
        self.assertEqual(r["neitherLine1"], "Lead Finder is scanning...")
        self.assertNotIn(RETIRED_ZIP_HELP_TEXT, r["neitherLine1"])

    def test_invalid_market_error_never_leaks_zip_help_text_into_console(self):
        r = self._run_node(SCAN_LINE_HARNESS)
        self.assertNotIn(RETIRED_ZIP_HELP_TEXT, r["invalidMarketErrorLine1"])
        self.assertEqual(r["invalidMarketErrorLine1"], 'Searching for "plumber" in not a real place.')

    def test_cancelled_terminal_state_still_finalizes(self):
        r = self._run_node(SCAN_LINE_HARNESS)
        self.assertEqual(r["cancelledLine1"], "Scan cancelled.")
        self.assertTrue(r["cancelledBodyClass"])
        self.assertTrue(r["cancelledStatusBoxClass"])
        self.assertTrue(r["cancelledBtnDisabled"])


class CancelHelperTextJsTests(_NodeHarnessTestCase):
    def test_helper_text_hidden_immediately_on_click(self):
        r = self._run_node(CANCEL_HELPER_TEXT_HARNESS)
        self.assertEqual(r["immediatelyAfterClickNote"], "Cancelling scan...")
        self.assertNotIn(RETIRED_ZIP_HELP_TEXT, r["immediatelyAfterClickNote"])
        self.assertEqual(r["immediatelyAfterClickBtnText"], "Cancelling...")

    def test_stray_backend_message_never_shown_beside_cancelling_button(self):
        r = self._run_node(CANCEL_HELPER_TEXT_HARNESS)
        self.assertEqual(r["afterResponseNote"], "Cancelling scan...")
        self.assertNotIn(RETIRED_ZIP_HELP_TEXT, r["afterResponseNote"])
        self.assertEqual(r["afterResponseBtnText"], "Cancelling...")


if __name__ == "__main__":
    unittest.main()
