# Real-value CLI workflows

This guide records the command forms exercised by the G004 real-value tests. Every Memplex
example places the global `--output json` option before the command. The guide describes what
to check in prose and intentionally does not predict or copy JSON responses.

## Evidence boundaries

- **Lite local:** the local workflow proves persistence between independent CLI processes that
  share one Lite storage path. It is not PostgreSQL or multi-host evidence.
- **Agent CLI:** capture and recall exercise the portable agent CLI adapter. They are
  not real-host G008 evidence for installation, hooks, MCP registration, or host readiness.
- **Loopback sync:** the sync workflow uses two Lite stores and an HTTP peer on `127.0.0.1`. It is
  not WAN or HA evidence and does not test failover, load balancing, or network partitions.
- **Temporary PostgreSQL backup drill:** the backup lifecycle was exercised against disposable
  PostgreSQL infrastructure with pgvector and PostgreSQL client tools. It is
  not deployment RPO/RTO evidence for a long-lived production service.

## Environment, identity, and configuration

Run these workflows from a source checkout with Memplex installed in the active Python
environment. Lite needs a writable, isolated `MEMPLEX_STORAGE_PATH`. Agent capture and recall
must use the same `--agent`, `--user-id`, `--session-id`, and `--project-path` identity tuple.

The sync and PostgreSQL sections below list every required environment/configuration input. Treat
signing keys, principal tokens, DSNs, and backup keys as secrets; keep them out of logs and version
control.

## Local Lite workflow

Choose a disposable path before running the commands:

```bash
export MEMPLEX_STORAGE_BACKEND=lite
export MEMPLEX_STORAGE_PATH="$PWD/.tmp/g004-guide-memory.json"
memplex --output json write --text "Remember that release canary alpha is durable."
memplex --output json recall "release canary alpha"
memplex --output json query "release canary alpha"
memplex --output json scope list
```

Confirm that recall and query contain the unique phrase written by the earlier process and that
scope listing reports the existing global visibility. Do not infer undocumented field names.

## Agent CLI workflow

Use the same Lite configuration and identity tuple for both processes:

```bash
memplex --output json agent capture --agent codex --user-id g004-guide-user --session-id g004-guide-session --project-path "$PWD" --user-message "For durable release lookup, remember unique canary alpha-7f9c." --assistant-message "Recorded."
memplex --output json agent recall --agent codex --user-id g004-guide-user --session-id g004-guide-session --project-path "$PWD" "durable release lookup"
```

The lookup phrase is `durable release lookup`; the canary is `alpha-7f9c`. Confirm that recall
contains the canary even though the recall argv contains only the lookup phrase and never the
canary. This proves the CLI round trip only; use the separate G008 host matrix for claims about a
real Codex, Claude Code, OpenClaw, or Hermes installation.

## Loopback sync workflow

Prepare two isolated Lite configurations with this complete prerequisite set:

| Input | Exact contract |
| --- | --- |
| `MEMPLEX_STORAGE_BACKEND` | `lite` on both peers |
| `MEMPLEX_STORAGE_PATH` | Distinct writable source and destination paths |
| `MEMPLEX_LLM_QUERY_ENHANCEMENT` | `false` on both peers for the tested deterministic path |
| `MEMPLEX_SYNC_ENABLED` | `true` on both peers |
| `MEMPLEX_SYNC_NODE_ID` | Distinct source and destination node IDs |
| `MEMPLEX_SYNC_CURSOR_SIGNING_KEY_ID` | Same non-empty active key ID on both peers |
| `MEMPLEX_SYNC_CURSOR_SIGNING_SECRET` | Same secret on both peers; at least 32 bytes |
| `MEMPLEX_SYNC_TARGETS_JSON` | Maps `central-node` to the ready loopback service URL |
| `MEMPLEX_PRINCIPALS_JSON` | Both principals, one tenant/workspace, SHA-256 token hashes only |
| `MEMPLEX_PRINCIPAL_TOKEN` | Distinct plaintext source/destination token injected per process |
| `MEMPLEX_SESSION_ID` | Distinct source and destination session IDs |
| Server override: `MEMPLEX_SYNC_NODE_ID` | `central-node` |
| Server override: `MEMPLEX_SYNC_TARGETS_JSON` | `{}` |
| Real loopback service | Real uvicorn child bound to `127.0.0.1` using `memplex.adapters.http_api:create_app --factory` |
| Readiness | Successful HTTP check of `/health/ready` before sync commands |

The tested harness derives the server environment from the source peer, then overrides the node ID
to `central-node` and the target map to `{}`. It reserves the loopback listener before handing its
file descriptor to the real uvicorn child. It starts no fake server and does not begin sync
commands until HTTP readiness succeeds.

With the correct environment selected for each node, the tested public command forms are:

```bash
memplex --output json sync status
memplex --output json sync pull --target central-node
memplex --output json sync drain --timeout 5
memplex --output json sync dlq list --limit 7
export EVENT_ID=00000000-0000-4000-8000-000000000004
memplex --output json sync dlq replay --target central-node --event-id "$EVENT_ID"
```

Run `sync status` for both peers before transfer. Write a unique canary on the source with the
local Lite command, pull from the destination, drain the source, then recall the canary from the
destination. On an empty dead-letter queue, replaying an absent event is expected to return a
nonzero status; it is an honest failure-path check, not a successful replay claim.

## Temporary PostgreSQL backup drill

Prepare the externally gated temporary PostgreSQL environment:

| Input | Exact contract |
| --- | --- |
| `MEMPLEX_STORAGE_BACKEND` | `postgres` |
| `MEMPLEX_STORAGE_PATH` | Application-role PostgreSQL DSN |
| `MEMPLEX_STORAGE_MIGRATION_DSN` | Migration-role PostgreSQL DSN for the same disposable schema |
| `MEMPLEX_SYNC_ENABLED` | `false` for the isolated backup lifecycle |
| `MEMPLEX_BACKUP_KEY_ID` | Non-secret active backup signing key identifier |
| `MEMPLEX_BACKUP_HMAC_KEY` | Canonical Base64 encoding of exactly 32 secret bytes |
| PostgreSQL tools | `pg_dump` and `pg_restore` available on `PATH` |
| PostgreSQL capability | pgvector extension and vector operator probe succeed |
| Python capability | `psycopg2` is importable in the Python environment running Memplex |
| Drill paths | Disposable backup directory, artifact directory, and restore schema |

Set `BACKUP_DIR` to the disposable backup directory, `ARTIFACT` to the artifact directory returned
by create, and `RESTORE_SCHEMA` to the disposable schema. Never reuse a live production schema.

```bash
python -m memplex --output json write --text "For backup drill lookup, preserve release canary alpha."
python -m memplex --output json storage backup create --destination "$BACKUP_DIR"
python -m memplex --output json storage backup verify "$ARTIFACT"
python -m memplex --output json storage backup restore "$ARTIFACT" --target-schema "$RESTORE_SCHEMA"
python -m memplex --output json recall "backup drill lookup"
python -m memplex --output json storage backup drill --artifact "$ARTIFACT" --target-schema "$RESTORE_SCHEMA"
```

Verify the artifact before restore, remove only the disposable target schema before restore or
drill, and confirm the canary through public recall afterward. Any observed drill timings apply
only to that temporary run; they are not a production recovery objective or service guarantee.
