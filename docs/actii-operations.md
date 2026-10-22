# LeadMeLeads → ACT II operations — review candidate only

Nothing in this document authorizes execution against production. No deployment,
restart, provisioning, real import, migration or scheduler has been performed.
All procedures below require a later approved maintenance/activation window.
ACT II, Oracle and Slayer must remain unchanged.

## Planning identity and disabled templates

Verified planning values: LeadMeLeads account **1**, exact username
`scot.trollan@gmail.com`, existing ACT II workspace **Born On High**, importer
`http://127.0.0.1:8842/internal/leadme/import`. Existing signing configuration is
compatible. Production HTTPS and persistent ext4 storage were verified in the
activation audit. These are planning facts, not permission to enable imports.

The files in `docs/actii-config/` are templates only. Empty token, digest, secret
and expiry fields deliberately fail validation. Production's binding record is
also `enabled: false`; the environment template is `ACTII_CRM_MODE=disabled` with
an empty binding path. Do not load these files as live configuration. No raw
credential or existing signing secret belongs in Git or the candidate artifact.
For an initial private disabled binding file use `{"version":1,"connectors":[]}`.

## Private paths and permissions

| Host | Planned path | Owner / permissions | Backup responsibility |
| --- | --- | --- | --- |
| Production | `/var/www/leadmeleads/data/actii_import_queue/` | Actual LeadMeLeads service UID; 0700 directories, 0600 files | Deployment helper includes spool; coordinated snapshot required |
| Production | `/etc/leadmeleads/actii/connector-bindings.json` | Same service UID; 0700 private parent, 0600 file | Separate encrypted private-config backup; digest only |
| Workstation | `/home/OWNER/.config/leadme-actii/connector.json` | Actual companion UID; 0700 parent, 0600 file | Separate encrypted credential backup |
| Workstation | `/home/OWNER/.config/leadme-actii/local-signing.json` | Same companion UID; 0700 parent, 0600 file | Separate encrypted credential backup |
| Workstation | `/home/OWNER/.local/state/leadme-actii/` | Same companion UID; 0700 directory, 0600 journals/lock | Coordinated journal backup; retain all acknowledged receipts |

`OWNER` is a template placeholder, not a provisioned directory. Verify actual
service and companion UIDs before any later provisioning. The production queue
path is fixed relative to the application's working directory; binding and
workstation paths must be absolute. Deployment must retain the production working
directory `/var/www/leadmeleads`. Keep the queue on that persistent native POSIX
filesystem and the companion journal on native WSL/Linux ext4. No symlinks,
hard-linked files, network storage or Windows-mounted private storage. The runtime
fails closed on unsafe ownership or permission bits; it does not repair them.
Use `umask 077` during approved provisioning and same-directory atomic replacement
for private JSON files. Do not chmod the repository or copy private files into it.

## Credentials and exact workspace binding

After separate approval, the operator's secure process supplies a random token of
at least 256 bits. Production receives only its SHA-256 digest; the workstation
receives the raw token. Both configs use the same connector ID, account, exact
username, binding version and UTC expiry. Use at most a 90-day credential lifetime;
there is no automatic refresh. Verify synchronized clocks before activation.
The workstation signing file references the already compatible secret and maps
`scot.trollan@gmail.com` exactly to `Born On High`. Never copy that secret to
production. No ACT II change is needed or authorized. Recheck the already-running
listener and mapping immediately before an approved controlled import; its response
does not attest its workspace. Contact links open on the viewer's own workstation.

Pure token rotation: suspend explicit companion invocations and browser enqueueing,
reconcile all in-flight jobs, preserve journals, atomically replace production
digest/expiry and workstation token/expiry, then validate during an approved
window. Keep connector ID and binding version unchanged so old receipts can be
acknowledged. There is no overlap period. To revoke, stop new invocations and
atomically set the production binding `enabled: false`. Disable global mode too
if stopping the whole integration. Revocation cannot undo an already-sent request.
Identity/workspace changes require a new binding version and review of every old
job and receipt; do not reuse a fresh journal to bypass old records.

## Consistent backup / snapshot procedure

The deployment helper's file copy alone is **not** a consistent distributed
snapshot and does not suspend writers. Its manifest includes the default spool,
SQLite online backups and LeadMeLeads exports/ownership data, but it does not
include the external binding or workstation journal/credentials. Do not label an
uncoordinated helper backup sufficient for connector recovery.

During a separately approved maintenance window:

1. Stop future companion invocations and wait for any current invocation to finish.
   Record unresolved dispatches; do not delete or replace lock files. Exclude a
   second operator from invoking the companion during backup.
2. Quiesce all LeadMeLeads requests that can enqueue, claim, acknowledge or reconcile
   status (status reads may write expiry/retirement events), and any export/account
   writers. Use the approved maintenance mechanism and drain in-flight requests.
   Changing mode alone does not drain requests already executing. Any service stop
   or restart is a separate approved operation; none is executed by this candidate.
3. While both sides stay quiescent, capture the full production queue including
   receipts, terminal tombstones and audit records, related auth/ownership/exports,
   and the full workstation journal. Use coordinated filesystem snapshots or
   quiesced copies. Snapshotting only one host while the other writes is insufficient.
   Use SQLite's online backup API or quiesce and capture complete SQLite state;
   never copy just a live main `.db` file while ignoring its WAL.
4. Back up external production bindings and workstation credentials in an encrypted,
   access-controlled store. Record code commit, UTC time, UIDs/modes, binding version,
   queue job IDs/states and journal phases/receipt IDs in a protected manifest. Hash
   files and verify copies before declaring success. Keep all outstanding receipts
   and tombstones across backup rotations; the helper retains only ten snapshots.
5. Test restoration to a disconnected native POSIX directory with imports disabled.
   Verify file hashes, JSON readability, SQLite integrity and modes. Do not invoke
   the companion as a restore test. Resume only under the original approved window.

Preserve original evidence and choose a matched backup set by its protected
manifest. `.write-*` atomic-write leftovers are not authoritative records. Keep
`.lock` files in the evidence; restoring one does not restore a live kernel lock.
Do not clear acknowledged journals or tombstones to free capacity. Protect backup
content as prospect/account data and secrets, even when manifests omit payloads.

## Disabled restoration and receipt reconciliation

Restore in isolation first. Set `ACTII_CRM_MODE=disabled`, leave the binding path
unset and ensure every restored binding is disabled before any restored service
can serve connector requests. Hold all manual companion invocations. Restoring an
old environment or binding backup must never silently re-enable the integration.
Retain a forensic copy of current queue/journal state before replacing anything.
Restore the matched queue, auth/ownership/export state and complete journal with
original service UIDs and private modes. Do not replace the journal with an empty
directory, reset Processing to Queued or recreate failed jobs.

Compare each queue job against retained journals by job ID, connector ID, account,
username, binding version, payload hash and lease ID:

- A confirmed journal receipt for a matching lease is evidence to resend that
  receipt; it never authorizes another local POST. A matching production terminal
  receipt is historical, including if the ACT II contact has since been deleted.
- `dispatching` without a durable receipt is uncertain. An expired lease becomes
  Needs Review. Inspect the existing ACT II contact/source reference in Born On
  High during a later approved review; never automatically replay it.
- A stale queued copy with a retained prior dispatch/receipt must not be dispatched.
  If queue and journal lease IDs differ, or a job is missing from one restored side,
  hold activation for manual reconciliation. A restored acknowledged journal cannot
  automatically repair an older production copy: the companion skips acknowledged
  records and rejects conflicting receipts. Do not edit phase/lease fields to force it.
- Only a durable `not_sent` result is eligible for the bounded retry path. All other
  missing, conflicting or unprovable outcomes stay held for operator review.

Use the explicit recovery-only operation for saved receipts, after backup
comparison and separate authorization of receipt reconciliation:

```sh
PYTHONDONTWRITEBYTECODE=1 /path/to/venv/bin/python scripts/actii_pull_connector.py --config /absolute/private/connector.json --recover-only
```

`--recover-only` takes the singleton journal lock, validates credential expiry and
journal identity, and sends only existing receipts to the production result
endpoint. It never calls the claim endpoint, loads the ACT II signing file,
contacts ACT II, or starts polling. An interrupted `dispatching` record is first
persisted as an uncertain receipt. After acknowledging that uncertainty it exits
with a review-required error, and future recovery/normal invocations remain held
by the same journal record. A failed acknowledgment leaves the durable receipt
available for an idempotent retry; a crash after remote acknowledgment is also
safe to retry. Neither success nor failure transitions into normal importing.
Do not delete or edit journals to bypass a review-required outcome.

Production disabled mode rejects acknowledgments as well as claims. The helper
does not enable production: during disabled restoration leave it disabled and
perform the offline evidence comparison first. Network receipt reconciliation
requires a separately approved recovery window with a valid existing binding and
production queue access; hold all other invocations and enqueueing. Run only
`--recover-only` in that window, then return production to disabled mode and verify
it. Do not enable the queue just to experiment with recovery. Conflicting leases,
missing journals or already-acknowledged records against an older production copy
still need manual review; this command does not repair them or force replay.

Normal operation requires the mutually exclusive `--once` option. It explicitly
allows one new claim after successful receipt reconciliation, and at most one
local submission. Any retained uncertain/review receipt blocks that claim.
Neither option is selected by default; no daemon, scheduler or automatic refresh
is provided. Existing `not_sent` retries still need a later explicit `--once`.

## Exact code-only rollback

The candidate helper now provides a local `--rollback-code` operation. It never
uses SSH, fetches, pushes, restarts, restores a database, changes configuration,
or cleans untracked files. Preview is the default; `--apply` is required to alter
code. Do not use the installed user-local wrapper if it resolves to another
checkout. Run the reviewed release's `scripts/leadme-deploy` at the production
repository only during a separately approved rollback window. Its implementation
must be available in the deployed release; preserve that reviewed helper outside
the live code tree as protected evidence if the rollout can fail partway.

Prerequisites and preflight:

1. Record the exact deployed release SHA and the verified pre-deploy SHA. For this
   release the baseline is `0dce6486aa5ccb48e7359088a6fc3f45e87a5b49`. Recheck the
   actual current SHA; never substitute moving branch names. Retain both commit
   objects and an off-host code artifact/manifest through rollback retention.
2. Hold companions, disable imports in the effective service/application
   configuration, drain in-flight application requests, and arrange exclusive
   maintenance with no other Git operators, code writers or requests until health
   checks complete. Any service stop/restart is separately authorized. The helper
   does not quiesce services or set environment variables for you.
3. Preserve and verify the current protected backup set described below. Record
   hashes, modes and owners for live configuration and runtime paths before code
   rollback; do not restore old data to roll back code.
4. Preview with the exact SHAs (placeholders below are not runnable revisions):

```sh
scripts/leadme-deploy --rollback-code --expected-release RELEASE_FULL_SHA --previous-commit PREVIOUS_FULL_SHA
```

The helper verifies repository root, exact current HEAD, commit ancestry, no
tracked/staged changes or unfinished Git operation, and an enumerated code delta
containing no protected data/configuration paths. It checks actual code bytes and
executable modes even when Git flags hide modifications. Symlinked/hard-linked
code, unsupported tree entries and untracked/ignored collisions cause an abort.
An unrelated new production commit or dirty file also causes an abort; preserve
it and obtain a newly reviewed rollback plan rather than overriding the guard.
Inspect every preview path against the approved release manifest.

Only after reviewing the preview and separately authorizing rollback:

```sh
scripts/leadme-deploy --rollback-code --expected-release RELEASE_FULL_SHA --previous-commit PREVIOUS_FULL_SHA --apply
```

The helper restores only enumerated release paths using Git restore with
`--no-overlay`, which removes release-only connector files and restores any files
deleted by the release. It verifies the complete index tree equals the previous
revision, then compare-and-swap updates HEAD from the exact release SHA to the
previous SHA. It verifies clean tracked state and exact HEAD afterward. There is
no blanket cleanup or hard reset. Ignored databases, exports, settings, private
configuration, queue records, receipts and journals are neither read nor replaced.
A code rollback cannot undo an ACT II import.

Failure handling and post-rollback checks:

- A guard failure before restore leaves code and refs unchanged. A failure after
  restore begins may leave a partial code/index state; keep traffic and companions
  held, preserve evidence and obtain operator review. Do not restart, retry blindly,
  clean, reset, or restore databases. Exclusive maintenance is essential: this is
  not an atomic transaction against concurrent Git operators or code writers.
- Confirm `git rev-parse HEAD` equals the recorded previous SHA, `git write-tree`
  equals that commit's tree, and tracked/index diffs are empty. Confirm all
  release-only paths in the preview are absent. Compare protected runtime/config
  hashes, modes and owners against the pre-rollback manifest.
- Confirm the effective mode remains disabled, no binding was enabled, and no
  companion or scheduler is running. Restart LeadMeLeads only under separate
  approval after code/config verification. Require an active service, no new
  startup/import errors in its journal, and successful GET health checks for `/`,
  `/login`, and authenticated Lead Finder; anonymous `/history` must redirect to
  `/login`. The original baseline has no connector routes; verify those remain
  absent. No import or paid scan is a health check.
- Reopen traffic only after these checks pass. Keep all connector evidence for
  future reconciliation. If data recovery is independently required, use the
  matched disabled-restore procedure; do not roll back ACT II, Oracle or Slayer.

## Protected backup and restoration inventory

These are documentation requirements, not backups created by this mission. The
ordinary deployment helper is a data snapshot tool, not complete machine/config
recovery. Its manifest does not back up the external/private items below. A code
pull normally leaves them in place, but preserve them separately before any
maintenance that could replace configuration. Backups and manifests containing
secrets must be encrypted off-host with tightly restricted operator access.
Never attach them to a PR, logs or this Git repository.

| Protected item | Exact source / inventory rule | Restoration requirement |
| --- | --- | --- |
| Production application configuration | `/var/www/leadmeleads/.env` plus every additional application config file actually used | Restore contents and original UID/GID/mode from protected evidence, then explicitly force CRM disabled and binding unset before startup; never let an old enabled value return silently |
| Service environment and definition | `/etc/systemd/system/leadmeleads.service`, all effective systemd drop-ins, and every `EnvironmentFile` named by the effective unit, including paths outside the app; record unit enablement and service UID/working directory | Restore only during approved maintenance; review inline environment as secret-bearing; verify effective environment, service UID and working directory before an approved daemon reload/restart |
| Production connector bindings | Actual `ACTII_CONNECTOR_BINDINGS_FILE`; planned `/etc/leadmeleads/actii/connector-bindings.json` | Preserve digest/identity/version/expiry; restore 0600 under the service UID and a 0700 parent; disable every binding and leave path unset until reconciliation is approved |
| Production queue | Entire `/var/www/leadmeleads/data/actii_import_queue/`, including receipts, tombstones, audit records and lock evidence | Restore matched queue/auth/export/ownership snapshot with original UID/GID and 0700/0600 modes; exclude nonauthoritative `.write-*` leftovers; never reset Processing or delete terminal tombstones |
| Workstation connector configuration | Actual `--config` file, planned `/home/OWNER/.config/leadme-actii/connector.json`, and referenced `actii_secret_file` | Keep raw token and existing signing material encrypted and separate from production; restore original UID/GID, 0700 parent and 0600 files; expiry/revocation must still be checked; never change ACT II's own files |
| Dispatch journals and receipts | Entire configured `state_dir`, planned `/home/OWNER/.local/state/leadme-actii/`, including acknowledged receipts and dispatching records | Restore matching full history and private modes before any invocation; compare binding/payload/lease identities offline; use only explicit recovery-only reconciliation after separate approval |

Include related application auth, ownership metadata, exports and settings in the
same quiesced backup set using the existing helper inventory. Verify the encrypted
copies by protected hashes and restore them to a disconnected POSIX staging area
before declaring readiness. Directory backup manifest counts alone are not a
per-file integrity proof; keep an additional protected file-hash/mode/owner
manifest. Record missing optional paths explicitly. Retain receipt/tombstone
history beyond the helper's ten-snapshot rotation. Configuration restoration is a
reviewed operation, not an automatic step in code rollback; never copy restored
credentials into code or create new credentials as a recovery shortcut.

## Remaining activation gates

- Review and approve the exact connector candidate and later deployment/restart.
- Approve the private paths and actual UIDs; exercise the coordinated backup and
  disabled restore procedure, including mismatched or acknowledged receipt cases.
- Provision the separate connector token/digest/UTC expiry and private files through
  the existing secure process. No real configuration has been created here.
- Reverify clocks, HTTPS/secure sessions, persistent paths and the already-running
  ACT II listener/mapping immediately before activation; verified planning facts do
  not constitute activation authorization.
- Reconcile any queue/journal evidence; approve enabling the exact account/workspace
  binding and one controlled import. Until then, production mode stays disabled.
- Background polling or scheduling is outside this candidate and requires separate
  review. The browser checks status on load/submission; later status uses refresh.
