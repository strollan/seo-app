# ACT II outbound connector — implementation awaiting activation review

This implementation is disabled by default. Nothing installs or starts a service,
poller, scheduled task, tunnel, or listener. ACT II, Oracle and Slayer are unchanged.
No schema changes are required. Production credentials and real imports are not
part of implementation or testing.

## Trust and request flow

The existing review form posts to `/lead-bot/add-to-crm`. In `queue` mode the route
requires an active session, session CSRF, same-origin requests when Origin is
present, an explicit account/connector binding, and exact export ownership even
for an admin. The server reconstructs provenance and search metadata from the
owned CSV and freezes the reviewed contact fields in a durable job. A website
must keep the verified source hostname (www normalization is permitted).

`202` means saved in the queue, not imported. Owner-scoped status routes expose
state and contact links, never the prospect snapshot. A bearer token cannot
enqueue jobs or access browser status APIs. Public signup never creates bindings.

The manually invoked companion calls only the configured HTTPS production origin
and `http://127.0.0.1:8842/internal/leadme/import`. TLS certificate verification is
enabled, redirects and environment proxies are disabled, and response sizes are
bounded. No browser sends requests to ACT II. Port 8842 must stay bound to loopback.

The companion uses ACT II's existing envelope and timestamp/HMAC-SHA256 headers.
Production does not receive or store ACT II's signing secret. The unchanged source
reference is `leadmeleads:<export filename>:<normalized original domain>`.

## Production configuration — later activation only

Defaults in `.env.example`:

```dotenv
ACTII_CRM_MODE=disabled
ACTII_CONNECTOR_BINDINGS_FILE=
```

`local` explicitly opts into the prior synchronous loopback bridge; use it only on
the workstation. It also requires the explicit account binding and exact export
ownership, so it cannot bypass the queue's account authorization. `queue` selects
production enqueueing. An unknown/unset mode is disabled. Setting queue mode does
not start a companion.

After activation approval, an operator must create a private JSON file outside
Git, owned by the application service user with mode 0600. Start with an empty
disabled setup: `{"version":1,"connectors":[]}`. The following is a schema example;
placeholders are deliberately invalid and are not credentials:

```json
{
  "version": 1,
  "connectors": [{
    "connector_id": "owner-workstation",
    "user_id": 123,
    "username": "EXACT_VERIFIED_LEADME_USERNAME",
    "binding_version": "v1",
    "enabled": false,
    "token_sha256": "REPLACE_WITH_SHA256_OF_SEPARATELY_PROVISIONED_TOKEN",
    "expires_at": "REPLACE_WITH_UTC_EXPIRY"
  }]
}
```

The account ID and username must identify the authorized owner; neither is
inferred from a CRM workspace name. One connector per account is supported.
Binding files are reread per operation, allowing revocation/rotation without
restarting the application. Replace them atomically. Malformed records,
duplicates, unsafe permissions, missing files and expired credentials fail closed.

The fixed spool is `data/actii_import_queue/` relative to the application's working
directory. It requires a persistent local POSIX filesystem, ownership by the
service user, directory mode 0700 and file mode 0600. Symlinks and hard-linked files
are rejected. File locks coordinate processes on ONE host; do not share this queue
across independent hosts or use a network filesystem. The current bound is 1000
total retained jobs and 20 new jobs per owner per minute. Near capacity requires
operator review; do not delete tombstones just to free space.

## Workstation configuration — later activation only

Use a native WSL/Linux directory with owner-only permissions, not a Windows mount
that cannot enforce those permissions. The companion configuration is a 0600 JSON
file outside Git:

```json
{
  "production_origin": "https://leadmeleads.com",
  "connector_id": "owner-workstation",
  "user_id": 123,
  "username": "EXACT_VERIFIED_LEADME_USERNAME",
  "binding_version": "v1",
  "workspace": "EXACT_EXISTING_ACTII_WORKSPACE",
  "token": "REPLACE_WITH_SEPARATELY_PROVISIONED_CONNECTOR_TOKEN",
  "expires_at": "REPLACE_WITH_UTC_EXPIRY",
  "state_dir": "/home/OWNER/.local/state/leadme-actii",
  "actii_secret_file": "/home/OWNER/.config/leadme-actii/local-signing.json"
}
```

The separate owner-only signing file has this schema:

```json
{
  "integration_secret": "EXISTING_ACTII_INTEGRATION_SECRET",
  "owner_map": {"EXACT_VERIFIED_LEADME_USERNAME": "EXACT_EXISTING_ACTII_WORKSPACE"}
}
```

An operator must verify these settings match the ALREADY RUNNING ACT II process,
including exact case. ACT II's endpoint does not attest its destination workspace;
the companion validates the operator-verified configuration, not ACT II runtime
state. If ACT II uses an unmapped username workspace, an empty owner_map is valid
only when workspace equals username. Mapping drift requires suspension and review.
Never guess a mapping or change ACT II automatically. If its running configuration
is missing/wrong, stop: this task does not authorize fixing it or restarting it.

Once separately approved, a single explicit invocation is:

```sh
PYTHONDONTWRITEBYTECODE=1 /path/to/venv/bin/python scripts/actii_pull_connector.py --config /absolute/private/connector.json --once
```

Do not run this against production or install a scheduler during implementation.
The CLI requires either `--recover-only` (saved receipts only, no claim/import)
or `--once` (recover, then explicitly claim at most one job). Neither is selected
by default; no continuous mode exists. Uncertain/review journals block normal claims. A singleton file lock rejects overlapping invocations.
The existing workstation environment needs Python, requests and access to ACT II
in the same loopback network namespace. This implementation targets POSIX/WSL.

## States, attempts and crash recovery

Public states are Queued, Processing, Added, Already Present, Needs Review and
Failed. Pending jobs expire after seven days. Expiration is recorded on the next
queue access; no cleanup service is started. Account activity, exact export
ownership and the source domain's continued presence are checked before dispatch.

Every claim has a 120-second lease. A missing receipt after expiry moves to Needs
Review, NEVER back to Queued. A late matching confirmed receipt can resolve it.
The companion records `dispatching` with fsync BEFORE the local POST. It records
the validated result with fsync BEFORE acknowledging production. A recovered
dispatch with no receipt reports uncertainty and never repeats the import.

Only a positively not-sent result (currently Requests ConnectTimeout) can retry,
with exponential backoff and jitter up to five minutes and at most ten claims. Ordinary
connection errors, read timeouts, malformed responses and local 5xx ambiguity
require review. ACT II's explicit authentication/validation/mapping rejections
become Failed; its 409 conflicts become Needs Review. Production outages retain
the journal for the next explicit invocation; no automatic loop is enabled.

Acknowledgments are idempotent for the same job, lease and normalized receipt.
Conflicting receipts are rejected. Successful acknowledgment retries never POST
to ACT II again. Repeated browser clicks return the original job and snapshot,
including after terminal failure; they do not silently authorize a second attempt.

Recovery procedure:

1. Stop future companion invocations; preserve production queue and workstation
   journal backups. Do not remove `.lock`, clear journals or reset Processing.
2. For a saved receipt, repair the production connection/configuration and invoke
   `--recover-only` after approval. It sends saved results only, never claims a
   new job or contacts ACT II. Uncertain outcomes remain held for operator review.
   Production disabled mode also rejects acknowledgments; see the operations
   runbook for the separately authorized receipt-reconciliation window.
3. For dispatch uncertainty or 409, inspect ACT II in the verified workspace using
   the original source reference/domain. Review the actual contact and its import
   note. No automatic merge, workspace reassignment or ownership repair exists.
4. A recorded successful import remains historical even if the contact was later
   deleted. ACT II deletion removes its import reference; do not replay old jobs
   to "repair" that state. Creating it again needs a deliberate new approval.
5. Expired/failed jobs remain terminal. Any deliberate requeue, manual resolution,
   remapping or replacement job requires a separately reviewed operator procedure;
   this implementation intentionally offers no force/replay command.

This is conservative delivery with review for ambiguous commits, not an
exactly-once distributed transaction. The unchanged importer serially protects
source references and checks domains but has no unique domain constraint. Other
concurrent CRM writers can still race its domain check. Make this companion the
sole automated writer for this connection.

## Credentials, audit and backups

No provisioning or credential generator is included. After approval, provision a
separate high-entropy connector token (at least 256 random bits) through the
operator's existing secure process. Store the raw value only on the workstation;
production stores its SHA-256 digest and uses constant-time comparison. Use a
90-day expiry policy. No token is sent in a URL, HTML, browser storage or logs.

For rotation: finish/reconcile in-flight work, suspend invocations, atomically
replace the production digest/expiry and workstation token/expiry, then test an
explicit invocation after approval. Keep the binding version unchanged for pure
credential rotation so receipts can recover. There is no automatic refresh or
overlap. Disable a binding immediately to revoke it. Revocation cannot undo an
already submitted local request. Identity/workspace changes need a new binding
version and review of old journals/jobs, not credential rotation.

Each job atomically stores its state and audit events: enqueue, claim, receipt,
expiration, authorization/binding changes and payload retirement. Events include
UTC epoch timestamps, attempt numbers and sanitized outcome codes. Job metadata
includes account, connector, binding version, source reference and payload hash.
Operational warnings contain only rejection status or a fixed integrity-error
code; they never dump exceptions, raw responses, snapshots or credentials. Keep
application security logs for at least 90 days under existing server operations.

Full prospect snapshots are retired seven days after a terminal outcome on the
next queue access. Minimal job/audit tombstones and companion journals must remain
to prevent replay. Application logs/audit files are not tamper-proof against a
compromised host. A stolen connector token can retrieve its queued data and fake
receipts; a stolen local signing secret retains ACT II's existing broad trust.

`scripts/leadme_deploy.py` includes the default spool in its protected backup
manifest. Back up external binding configuration separately (digest only), protect
raw workstation credentials separately, and include the local journal directory.
For a coherent operational restore, suspend ALL queue writers/companions while
copying the queue and journal, or use a filesystem snapshot. Per-job atomicity
does not make a multi-file backup a global transaction. Atomic-write temporary
files are not authoritative; ignore them on recovery.

After any rollback/restore, start in disabled mode. Compare queue receipts against
the retained workstation journal before permitting dispatch. A stale queued copy
must not be dispatched from a fresh/empty journal. Queue files are not database
tables, and no auth/ACT II schema migration is performed.

## Validation and activation gate

Focused tests use synthetic accounts/exports, mocked HTTP transport and disposable
ACT II SQLite databases. They never call the running CRM or production. Read ACT
II's source via ACTII_TEST_ROOT; do not edit it. Run tests from `/tmp` with
PYTHONDONTWRITEBYTECODE=1 so imported application defaults remain disposable.

Candidate verification results and exact baseline are recorded in the separate
candidate review report; do not reuse historical working-tree test counts.
The browser checks job status once on page load and once after submission.
Refresh the page for later status. No recurring browser timer or companion
polling is included.

Test entrypoints under `scripts/` are `test_actii_outbound_connector.py`,
`test_leadbot_crm_import.py`, `check_leadbot_crm_buttons.py`, `test_actii_crm_ui.py`,
and `test_leadme_deploy.py`. The first two need ACTII_TEST_ROOT to include the real
contract checks; browser tests need the existing Playwright/Chromium test setup.
This environment's sandbox stalls TestClient's event-loop wakeup, so the HTTP
tests were run outside that sandbox with intercepted transport and temporary data.
Existing ACT II code emits SQLite connection ResourceWarnings under Python 3.14;
it was left unchanged. An unrelated existing dashboard escape SyntaxWarning also
remains unchanged.

Before activation approve the exact owner/binding, verify the already-running
ACT II secret/mapping and loopback listener, verify production TLS/secure cookies,
persistent POSIX storage, synchronized server/workstation clocks and backup/restore
handling, provision credentials, deploy
the reviewed code separately, then authorize one controlled import. Background
polling, production deployment and any service restart require separate approval.

## Deployment candidate operations

Use [the operations runbook](actii-operations.md) and the nonfunctional templates
in [actii-config/](actii-config/). Verified planning identity is account 1,
`scot.trollan@gmail.com`, workspace `Born On High`, loopback port 8842.
These verified values do not authorize activation. No credentials are supplied.
