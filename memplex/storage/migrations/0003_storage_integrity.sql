-- Feedback catalog and integrity constraints owned by the migration package.
CREATE TABLE IF NOT EXISTS feedback (
    memory_id TEXT NOT NULL,
    field_role TEXT NOT NULL,
    value_index INTEGER DEFAULT 0,
    verdict TEXT NOT NULL,
    reason TEXT,
    source TEXT DEFAULT 'user',
    timestamp TIMESTAMPTZ,
    owner TEXT,
    feedback_type TEXT DEFAULT 'field_value',
    old_value TEXT,
    new_value TEXT,
    needs_review BOOLEAN DEFAULT TRUE,
    needs_review_until TIMESTAMPTZ,
    resolved_at TIMESTAMPTZ,
    resolution TEXT,
    tenant_id TEXT NOT NULL,
    owner_subject_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    visibility TEXT NOT NULL DEFAULT 'workspace',
    provenance JSONB NOT NULL DEFAULT '{}'::jsonb
);

ALTER TABLE feedback ADD COLUMN IF NOT EXISTS tenant_id TEXT;
ALTER TABLE feedback ADD COLUMN IF NOT EXISTS owner_subject_id TEXT;
ALTER TABLE feedback ADD COLUMN IF NOT EXISTS workspace_id TEXT;
ALTER TABLE feedback ADD COLUMN IF NOT EXISTS visibility TEXT DEFAULT 'workspace';
ALTER TABLE feedback ADD COLUMN IF NOT EXISTS provenance JSONB DEFAULT '{}'::jsonb;
UPDATE feedback
SET tenant_id = COALESCE(tenant_id, '__memplex_legacy__'),
    owner_subject_id = COALESCE(owner_subject_id, '__memplex_legacy__'),
    workspace_id = COALESCE(workspace_id, '__memplex_legacy__'),
    visibility = COALESCE(visibility, 'workspace'),
    provenance = COALESCE(provenance, '{}'::jsonb);
ALTER TABLE feedback ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE feedback ALTER COLUMN owner_subject_id SET NOT NULL;
ALTER TABLE feedback ALTER COLUMN workspace_id SET NOT NULL;
ALTER TABLE feedback ALTER COLUMN visibility SET NOT NULL;
ALTER TABLE feedback ALTER COLUMN provenance SET NOT NULL;

CREATE INDEX IF NOT EXISTS feedback_tenant_memory_idx
ON feedback (tenant_id, memory_id, timestamp DESC);
ALTER TABLE feedback ENABLE ROW LEVEL SECURITY;
ALTER TABLE feedback FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS feedback_tenant_scope ON feedback;
CREATE POLICY feedback_tenant_scope ON feedback
USING (tenant_id <> '__memplex_legacy__'
       AND tenant_id = current_setting('memplex.tenant_id', true)
       AND ((visibility = 'user'
             AND owner_subject_id = current_setting('memplex.subject_id', true))
            OR (visibility = 'workspace'
                AND workspace_id = current_setting('memplex.workspace_id', true))
            OR (visibility = 'session'
                AND workspace_id = current_setting('memplex.workspace_id', true)
                AND owner_subject_id = current_setting('memplex.subject_id', true)
                AND NULLIF(current_setting('memplex.agent_id', true), '') IS NOT NULL
                AND NULLIF(current_setting('memplex.session_id', true), '') IS NOT NULL
                AND NULLIF(provenance->>'agent_id', '') IS NOT NULL
                AND NULLIF(provenance->>'session_id', '') IS NOT NULL
                AND provenance->>'agent_id' = current_setting('memplex.agent_id', true)
                AND provenance->>'session_id' = current_setting('memplex.session_id', true))))
WITH CHECK (tenant_id <> '__memplex_legacy__'
            AND tenant_id = current_setting('memplex.tenant_id', true)
            AND owner_subject_id = current_setting('memplex.subject_id', true)
            AND workspace_id = current_setting('memplex.workspace_id', true)
            AND visibility IN ('user', 'workspace', 'session')
            AND (visibility <> 'session'
                 OR (NULLIF(current_setting('memplex.agent_id', true), '') IS NOT NULL
                     AND NULLIF(current_setting('memplex.session_id', true), '') IS NOT NULL
                     AND provenance->>'agent_id' = current_setting('memplex.agent_id', true)
                     AND provenance->>'session_id' = current_setting('memplex.session_id', true))));

CREATE UNIQUE INDEX IF NOT EXISTS memplex_functions_workspace_normalized_name_key
ON memplex_functions (tenant_id, workspace, lower(btrim(coalesce(data->>'name_normalized', data->>'name', ''))))
WHERE visibility = 'workspace' AND btrim(coalesce(data->>'name_normalized', data->>'name', '')) <> '';
CREATE UNIQUE INDEX IF NOT EXISTS memplex_functions_user_normalized_name_key
ON memplex_functions (tenant_id, owner_subject, lower(btrim(coalesce(data->>'name_normalized', data->>'name', ''))))
WHERE visibility = 'user' AND btrim(coalesce(data->>'name_normalized', data->>'name', '')) <> '';
CREATE UNIQUE INDEX IF NOT EXISTS memplex_functions_session_normalized_name_key
ON memplex_functions (tenant_id, workspace, owner_subject, source_agent, source_session, lower(btrim(coalesce(data->>'name_normalized', data->>'name', ''))))
WHERE visibility = 'session' AND btrim(coalesce(data->>'name_normalized', data->>'name', '')) <> '';

CREATE TABLE IF NOT EXISTS memplex_schema_capabilities (
    capability_name TEXT PRIMARY KEY,
    parameter_digest TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL
);
