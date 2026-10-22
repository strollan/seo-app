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

The current CLI has only `--once`: it recovers pending receipts **and may claim a
new job**. It is not a receipt-only/offline recovery command. Do not run it while
performing disabled restoration or while any stale/uncertain queue copy remains.
Only after reconciliation proves the whole queue safe and activation is separately
approved may an explicit invocation resend pending receipts and claim at most one
job. Cases requiring manual resolution, requeue or receipt-only transport need a
separately reviewed procedure; no force/replay tool is provided. Disabling the
mode also disables acknowledgments, so re-enabling solely to try recovery is unsafe
without that review.

## Rollback

Before any later deployment, preserve the verified pre-deploy code SHA and a
coherent backup set. For this candidate the observed production baseline is
`0dce6486aa5ccb48e7359088a6fc3f45e87a5b49`; reverify it at deploy time.

For a code-only rollback, first hold invocations and disable imports through the
approved maintenance mechanism, preserve the current spool/journal, and restore
the pre-deploy code artifact using the normal reviewed deployment procedure.
Retain all queue/journal files outside code replacement. No schema migration or
reverse migration is involved. A code rollback does not undo an ACT II import.
Do not restore old data merely to roll back code.

If data recovery is also required, follow the matched disabled-restore procedure
above, then reconcile every receipt before any activation. Never roll back ACT II,
Oracle or Slayer as part of this operation. Any later production restart/deployment
needs its own authorization.

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
