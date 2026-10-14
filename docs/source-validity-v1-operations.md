# Source validity V1: admission and controlled restore

`source_contract.mode` defaults to `off`. Legacy banks retain their ordinary
behavior. An existing V1 bank cannot be served in off mode. V1 requires an exact
`authority_id`, `tenant_id`, and `workspace_id`, an audited native JSON Lite or
PostgreSQL resource, and its sealed capability. Configuration alone cannot
create an authority. SQLite authority, delegated wrappers, enabled sync and
live sync state are unsupported. Backend aliases do not imply admission.

Provisioning uses the existing trusted resource factories and owner connection.
It requires a new dedicated globally empty domain. PostgreSQL serializes this
decision with a target advisory lock and table locks. Temporary owner-created
CHECK constraints validate every row, including rows hidden by FORCE RLS; all
constraints are dropped in the same transaction. No BYPASSRLS, policy change or
additional grant is needed. An owner who cannot perform this proof cannot
provision the domain. The scope is bound before the authority insert.

An owner-protected PostgreSQL schema comment and a path-bound Lite admission
file record admission outside backup application bytes. Cold startup validates
the marker against the actual authority. Missing markers never reconstruct or
reinitialize V1: unmarked PostgreSQL off startup additionally proves all source
ledger tables and V1 carriers empty through the existing owner route. A failed
or interrupted Lite initialization stays unavailable without its final durable
live receipt.

## Unadapted writers and synchronization

Only the source mutation/publication routes may write an admitted V1 bank.
The restrictions below also apply to a legacy resource opened before admission.

| Producer | V1 boundary |
| --- | --- |
| Ordinary store add/update/delete and direct typed input | Native source write scope required |
| Consolidation promotion, eviction and stamps | Native deferred commit rejects; report records the failure |
| Entity hub generation | Native add rejects the unregistered derivation |
| Compressed or tool Observation capture | Service Observation entry point refuses unadapted publication |
| Background extraction, wiki compilation, vector refresh, compaction | Worker refuses before side effects |
| Sleep maintenance | Pass refuses before maintenance work |
| Service archive compaction | Refuses before archive output |
| Old SyncableStore wrappers | Cannot delegate a capability or operate an admitted native store |
| Sync egress, snapshots and old peer exchange | V1 refuses the legacy protocol |
| Typed inbound events without raw evidence | Rejected at the V1 native boundary; never relabeled direct input |
| Reserved V1 IDs or carrier metadata received by legacy ingress | Item rejected before a sealed legacy batch |

Other unadapted maintenance has no V1 write authority. This release introduces
no raw-evidence synchronization protocol. Scores, ordering, budgets and ordinary
legacy ACL rules continue to use their existing paths.

## Controlled restore is quarantined, not serving

`RestoreTarget` carries backend, scope and storage identity, never a DSN.
`begin_v1_restore` requires a sealed authority bound to an isolated empty target.
It commits and reads back quarantine before importing any backup bytes. Restore
preserves source heads, deletion tombstones and publication records. Backup
live flags cannot clear target quarantine, and success means imported bytes,
not readiness to serve. This release has no quarantine promotion operation.

Lite uses the existing durable pair/journal protocol. A populated V2-to-V3
restore is permitted only from a truly empty generation-zero target under the
exact external quarantine marker. Recovery derives this scope under the writer
lock; no serialized bypass flag or caller-provided recovery scope exists.
Recovery may finish a durable decision while the target remains unavailable.
Ordinary V3 monotonicity and full ledger/body reconciliation still apply.

PostgreSQL accepts only signed first-party archives from the existing trusted
owner backup boundary. TOC selection is not a sandbox for arbitrary SQL from a
hostile source superuser. Before reservation it rejects malformed or unknown
objects. It imports only the audited managed objects, omits source schema
creation/comments/default ACLs and ownership changes, and withholds schema ACL
entries. Existing audited object grants and PUBLIC revokes remain intact.
Before import, actual target ownership, effective schema access and owner
defaults must be safe; no permission is changed to make the check pass.
Explicit V1 imports undergo quarantined structural/ACL verification without
claiming normal application readiness. Object grants alone do not give an old
reader schema USAGE. The trusted owner and server superusers remain outside
this application-role isolation boundary.

The ordinary legacy restore entry point also reserves quarantine first because
an old-format archive can contain V1 data. Only a successful global proof of no
V1 bytes may release its own `legacy_candidate` marker. It first proves that
condition while schema access is withheld, commits the proof transaction,
replays schema ACLs, verifies normal catalogue/ACL readiness, then repeats the
proof and clears its exact marker in one transaction. A target session lock
spans these phases; table locks are released before the separate ACL process.
All subprocess and database phases share the original command deadline.

This changes interrupted fresh-restore availability: invalid TOC input still
leaves the schema absent, while failure after reservation leaves quarantine.
A retry is allowed only for the same backup, target, owner, restore ID and kind
when the original attempt is globally empty. A nonempty or unknown target stays
quarantined and needs a new isolated target or separately authorized maintenance.
An explicit V1 quarantine is never cleared by the generic legacy entry point.
Serving targets are never overwritten. Preserve failure evidence and quarantine;
do not delete markers or tombstones to make a retry work.

Normal crashes use the existing durable recovery protocol. Manual file rollback,
whole-machine rollback and external owner-level database rollback are unsupported.
Restored metadata cannot prove currentness or substitute for missing latest
authoritative state. Admission is a dedicated-deployment contract, not a new
security boundary against the trusted filesystem/database owner.
