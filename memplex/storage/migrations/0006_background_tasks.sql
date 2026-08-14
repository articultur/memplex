-- Durable service-global background task catalogue.
--
-- Claims are at-least-once.  ``lease_id`` is a fencing token for repository
-- state transitions; it does not make arbitrary handler side effects exactly
-- once.  All scheduling timestamps are assigned or compared by PostgreSQL.

CREATE TABLE memplex_background_tasks (
    task_id TEXT PRIMARY KEY,
    task_type TEXT NOT NULL CHECK (
        task_type IN (
            'extract_document',
            'build_index',
            'compile_wiki',
            'refresh_vector',
            'compaction'
        )
    ),
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'running', 'completed', 'failed', 'cancelled')
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    completed_at TIMESTAMPTZ,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(payload) = 'object'),
    result JSONB,
    error TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    max_retries INTEGER NOT NULL DEFAULT 3 CHECK (max_retries >= 0),
    next_attempt_at TIMESTAMPTZ,
    lease_until TIMESTAMPTZ,
    lease_id TEXT,
    last_error_code TEXT,
    CHECK ((status = 'running') = (lease_id IS NOT NULL AND lease_until IS NOT NULL)),
    CHECK ((status = 'pending') = (next_attempt_at IS NOT NULL)),
    CHECK ((status = 'completed') = (completed_at IS NOT NULL))
);

CREATE INDEX memplex_background_tasks_due_idx
ON memplex_background_tasks (next_attempt_at, created_at, task_id)
WHERE status = 'pending';

CREATE INDEX memplex_background_tasks_lease_idx
ON memplex_background_tasks (lease_until, created_at, task_id)
WHERE status = 'running';
