-- Memplex PostgreSQL tenant-first authorization contract.
--
-- This migration is deliberately immutable.  The versioned migration runner
-- and advisory locking are introduced separately; do not execute this file
-- ad hoc against a production database.  Existing unscoped rows are kept in
-- an inaccessible reserved tenant rather than assigned to an arbitrary user.

BEGIN;

-- The same identity columns are present on every memory-bearing table.  Row
-- metadata, not JSON payload claims, is authoritative for PostgreSQL ACL.
ALTER TABLE memplex_functions ADD COLUMN IF NOT EXISTS tenant_id TEXT;
ALTER TABLE memplex_functions ADD COLUMN IF NOT EXISTS owner_subject TEXT;
ALTER TABLE memplex_functions ADD COLUMN IF NOT EXISTS workspace TEXT;
ALTER TABLE memplex_functions ADD COLUMN IF NOT EXISTS visibility TEXT;
ALTER TABLE memplex_functions ADD COLUMN IF NOT EXISTS source_agent TEXT;
ALTER TABLE memplex_functions ADD COLUMN IF NOT EXISTS source_session TEXT;

ALTER TABLE memplex_edges ADD COLUMN IF NOT EXISTS tenant_id TEXT;
ALTER TABLE memplex_edges ADD COLUMN IF NOT EXISTS owner_subject TEXT;
ALTER TABLE memplex_edges ADD COLUMN IF NOT EXISTS workspace TEXT;
ALTER TABLE memplex_edges ADD COLUMN IF NOT EXISTS visibility TEXT;
ALTER TABLE memplex_edges ADD COLUMN IF NOT EXISTS source_agent TEXT;
ALTER TABLE memplex_edges ADD COLUMN IF NOT EXISTS source_session TEXT;

ALTER TABLE memplex_observations ADD COLUMN IF NOT EXISTS tenant_id TEXT;
ALTER TABLE memplex_observations ADD COLUMN IF NOT EXISTS owner_subject TEXT;
ALTER TABLE memplex_observations ADD COLUMN IF NOT EXISTS workspace TEXT;
ALTER TABLE memplex_observations ADD COLUMN IF NOT EXISTS visibility TEXT;
ALTER TABLE memplex_observations ADD COLUMN IF NOT EXISTS source_agent TEXT;
ALTER TABLE memplex_observations ADD COLUMN IF NOT EXISTS source_session TEXT;

ALTER TABLE memplex_facts ADD COLUMN IF NOT EXISTS tenant_id TEXT;
ALTER TABLE memplex_facts ADD COLUMN IF NOT EXISTS owner_subject TEXT;
ALTER TABLE memplex_facts ADD COLUMN IF NOT EXISTS workspace TEXT;
ALTER TABLE memplex_facts ADD COLUMN IF NOT EXISTS visibility TEXT;
ALTER TABLE memplex_facts ADD COLUMN IF NOT EXISTS source_agent TEXT;
ALTER TABLE memplex_facts ADD COLUMN IF NOT EXISTS source_session TEXT;

ALTER TABLE memplex_preferences ADD COLUMN IF NOT EXISTS tenant_id TEXT;
ALTER TABLE memplex_preferences ADD COLUMN IF NOT EXISTS owner_subject TEXT;
ALTER TABLE memplex_preferences ADD COLUMN IF NOT EXISTS workspace TEXT;
ALTER TABLE memplex_preferences ADD COLUMN IF NOT EXISTS visibility TEXT;
ALTER TABLE memplex_preferences ADD COLUMN IF NOT EXISTS source_agent TEXT;
ALTER TABLE memplex_preferences ADD COLUMN IF NOT EXISTS source_session TEXT;

ALTER TABLE memplex_changelog ADD COLUMN IF NOT EXISTS tenant_id TEXT;
ALTER TABLE memplex_changelog ADD COLUMN IF NOT EXISTS owner_subject TEXT;
ALTER TABLE memplex_changelog ADD COLUMN IF NOT EXISTS workspace TEXT;
ALTER TABLE memplex_changelog ADD COLUMN IF NOT EXISTS visibility TEXT;
ALTER TABLE memplex_changelog ADD COLUMN IF NOT EXISTS source_agent TEXT;
ALTER TABLE memplex_changelog ADD COLUMN IF NOT EXISTS source_session TEXT;

-- No historical row is attributed to a live tenant.  The RLS policies below
-- reject this reserved tenant even if a client presents that literal value.
UPDATE memplex_functions
SET tenant_id = COALESCE(tenant_id, '__memplex_legacy__'),
    owner_subject = COALESCE(owner_subject, '__memplex_legacy__'),
    workspace = COALESCE(workspace, '__memplex_legacy__'),
    visibility = COALESCE(visibility, 'private'),
    source_agent = COALESCE(source_agent, 'legacy'),
    source_session = COALESCE(source_session, 'legacy');
UPDATE memplex_edges
SET tenant_id = COALESCE(tenant_id, '__memplex_legacy__'),
    owner_subject = COALESCE(owner_subject, '__memplex_legacy__'),
    workspace = COALESCE(workspace, '__memplex_legacy__'),
    visibility = COALESCE(visibility, 'private'),
    source_agent = COALESCE(source_agent, 'legacy'),
    source_session = COALESCE(source_session, 'legacy');
UPDATE memplex_observations
SET tenant_id = COALESCE(tenant_id, '__memplex_legacy__'),
    owner_subject = COALESCE(owner_subject, '__memplex_legacy__'),
    workspace = COALESCE(workspace, '__memplex_legacy__'),
    visibility = COALESCE(visibility, 'private'),
    source_agent = COALESCE(source_agent, 'legacy'),
    source_session = COALESCE(source_session, 'legacy');
UPDATE memplex_facts
SET tenant_id = COALESCE(tenant_id, '__memplex_legacy__'),
    owner_subject = COALESCE(owner_subject, '__memplex_legacy__'),
    workspace = COALESCE(workspace, '__memplex_legacy__'),
    visibility = COALESCE(visibility, 'private'),
    source_agent = COALESCE(source_agent, 'legacy'),
    source_session = COALESCE(source_session, 'legacy');
UPDATE memplex_preferences
SET tenant_id = COALESCE(tenant_id, '__memplex_legacy__'),
    owner_subject = COALESCE(owner_subject, '__memplex_legacy__'),
    workspace = COALESCE(workspace, '__memplex_legacy__'),
    visibility = COALESCE(visibility, 'private'),
    source_agent = COALESCE(source_agent, 'legacy'),
    source_session = COALESCE(source_session, 'legacy');
UPDATE memplex_changelog
SET tenant_id = COALESCE(tenant_id, '__memplex_legacy__'),
    owner_subject = COALESCE(owner_subject, '__memplex_legacy__'),
    workspace = COALESCE(workspace, '__memplex_legacy__'),
    visibility = COALESCE(visibility, 'private'),
    source_agent = COALESCE(source_agent, 'legacy'),
    source_session = COALESCE(source_session, 'legacy');

ALTER TABLE memplex_functions ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE memplex_functions ALTER COLUMN owner_subject SET NOT NULL;
ALTER TABLE memplex_functions ALTER COLUMN workspace SET NOT NULL;
ALTER TABLE memplex_functions ALTER COLUMN visibility SET NOT NULL;
ALTER TABLE memplex_functions ALTER COLUMN source_agent SET NOT NULL;
ALTER TABLE memplex_functions ALTER COLUMN source_session SET NOT NULL;
ALTER TABLE memplex_edges ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE memplex_edges ALTER COLUMN owner_subject SET NOT NULL;
ALTER TABLE memplex_edges ALTER COLUMN workspace SET NOT NULL;
ALTER TABLE memplex_edges ALTER COLUMN visibility SET NOT NULL;
ALTER TABLE memplex_edges ALTER COLUMN source_agent SET NOT NULL;
ALTER TABLE memplex_edges ALTER COLUMN source_session SET NOT NULL;
ALTER TABLE memplex_observations ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE memplex_observations ALTER COLUMN owner_subject SET NOT NULL;
ALTER TABLE memplex_observations ALTER COLUMN workspace SET NOT NULL;
ALTER TABLE memplex_observations ALTER COLUMN visibility SET NOT NULL;
ALTER TABLE memplex_observations ALTER COLUMN source_agent SET NOT NULL;
ALTER TABLE memplex_observations ALTER COLUMN source_session SET NOT NULL;
ALTER TABLE memplex_facts ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE memplex_facts ALTER COLUMN owner_subject SET NOT NULL;
ALTER TABLE memplex_facts ALTER COLUMN workspace SET NOT NULL;
ALTER TABLE memplex_facts ALTER COLUMN visibility SET NOT NULL;
ALTER TABLE memplex_facts ALTER COLUMN source_agent SET NOT NULL;
ALTER TABLE memplex_facts ALTER COLUMN source_session SET NOT NULL;
ALTER TABLE memplex_preferences ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE memplex_preferences ALTER COLUMN owner_subject SET NOT NULL;
ALTER TABLE memplex_preferences ALTER COLUMN workspace SET NOT NULL;
ALTER TABLE memplex_preferences ALTER COLUMN visibility SET NOT NULL;
ALTER TABLE memplex_preferences ALTER COLUMN source_agent SET NOT NULL;
ALTER TABLE memplex_preferences ALTER COLUMN source_session SET NOT NULL;
ALTER TABLE memplex_changelog ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE memplex_changelog ALTER COLUMN owner_subject SET NOT NULL;
ALTER TABLE memplex_changelog ALTER COLUMN workspace SET NOT NULL;
ALTER TABLE memplex_changelog ALTER COLUMN visibility SET NOT NULL;
ALTER TABLE memplex_changelog ALTER COLUMN source_agent SET NOT NULL;
ALTER TABLE memplex_changelog ALTER COLUMN source_session SET NOT NULL;

ALTER TABLE memplex_functions DROP CONSTRAINT IF EXISTS memplex_functions_pkey;
ALTER TABLE memplex_functions ADD PRIMARY KEY (tenant_id, id);
ALTER TABLE memplex_edges DROP CONSTRAINT IF EXISTS memplex_edges_pkey;
ALTER TABLE memplex_edges ADD PRIMARY KEY (tenant_id, source, target, edge_type);
ALTER TABLE memplex_observations DROP CONSTRAINT IF EXISTS memplex_observations_pkey;
ALTER TABLE memplex_observations ADD PRIMARY KEY (tenant_id, id);
ALTER TABLE memplex_facts DROP CONSTRAINT IF EXISTS memplex_facts_pkey;
ALTER TABLE memplex_facts ADD PRIMARY KEY (tenant_id, id);
ALTER TABLE memplex_preferences DROP CONSTRAINT IF EXISTS memplex_preferences_pkey;
ALTER TABLE memplex_preferences ADD PRIMARY KEY (tenant_id, id);
ALTER TABLE memplex_changelog DROP CONSTRAINT IF EXISTS memplex_changelog_pkey;
ALTER TABLE memplex_changelog ADD PRIMARY KEY (tenant_id, id);

CREATE INDEX IF NOT EXISTS memplex_functions_tenant_updated_idx ON memplex_functions (tenant_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS memplex_functions_tenant_idx ON memplex_functions (tenant_id);
CREATE INDEX IF NOT EXISTS memplex_edges_tenant_source_type_target_idx ON memplex_edges (tenant_id, source, edge_type, target);
CREATE INDEX IF NOT EXISTS memplex_edges_tenant_target_type_source_idx ON memplex_edges (tenant_id, target, edge_type, source);
CREATE INDEX IF NOT EXISTS memplex_edges_tenant_idx ON memplex_edges (tenant_id);
CREATE INDEX IF NOT EXISTS memplex_observations_tenant_idx ON memplex_observations (tenant_id);
CREATE INDEX IF NOT EXISTS memplex_facts_tenant_idx ON memplex_facts (tenant_id);
CREATE INDEX IF NOT EXISTS memplex_preferences_tenant_idx ON memplex_preferences (tenant_id);
CREATE INDEX IF NOT EXISTS memplex_changelog_tenant_idx ON memplex_changelog (tenant_id);

-- Every policy is intentionally named <table>_scope so schema inspection can
-- prove the uniform safety contract.
ALTER TABLE memplex_functions ENABLE ROW LEVEL SECURITY;
ALTER TABLE memplex_functions FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS memplex_functions_scope ON memplex_functions;
CREATE POLICY memplex_functions_scope ON memplex_functions
USING (tenant_id <> '__memplex_legacy__' AND tenant_id = current_setting('memplex.tenant_id', true)
       AND ((visibility = 'user' AND owner_subject = current_setting('memplex.subject_id', true))
            OR (visibility = 'workspace' AND workspace = current_setting('memplex.workspace_id', true))
            OR (visibility = 'session'
                AND workspace = current_setting('memplex.workspace_id', true)
                AND owner_subject = current_setting('memplex.subject_id', true)
                AND NULLIF(current_setting('memplex.agent_id', true), '') IS NOT NULL
                AND NULLIF(current_setting('memplex.session_id', true), '') IS NOT NULL
                AND NULLIF(source_agent, '') IS NOT NULL
                AND NULLIF(source_session, '') IS NOT NULL
                AND source_agent = current_setting('memplex.agent_id', true)
                AND source_session = current_setting('memplex.session_id', true))))
WITH CHECK (tenant_id <> '__memplex_legacy__' AND tenant_id = current_setting('memplex.tenant_id', true)
            AND ((visibility = 'user' AND owner_subject = current_setting('memplex.subject_id', true))
                 OR (visibility = 'workspace' AND workspace = current_setting('memplex.workspace_id', true))
                 OR (visibility = 'session'
                     AND workspace = current_setting('memplex.workspace_id', true)
                     AND owner_subject = current_setting('memplex.subject_id', true)
                     AND NULLIF(current_setting('memplex.agent_id', true), '') IS NOT NULL
                     AND NULLIF(current_setting('memplex.session_id', true), '') IS NOT NULL
                     AND NULLIF(source_agent, '') IS NOT NULL
                     AND NULLIF(source_session, '') IS NOT NULL
                     AND source_agent = current_setting('memplex.agent_id', true)
                     AND source_session = current_setting('memplex.session_id', true)))
            AND owner_subject = current_setting('memplex.subject_id', true)
            AND workspace = current_setting('memplex.workspace_id', true)
            AND source_agent = current_setting('memplex.agent_id', true)
            AND source_session = current_setting('memplex.session_id', true));

ALTER TABLE memplex_edges ENABLE ROW LEVEL SECURITY;
ALTER TABLE memplex_edges FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS memplex_edges_scope ON memplex_edges;
CREATE POLICY memplex_edges_scope ON memplex_edges
USING (tenant_id <> '__memplex_legacy__' AND tenant_id = current_setting('memplex.tenant_id', true)
       AND ((visibility = 'user' AND owner_subject = current_setting('memplex.subject_id', true))
            OR (visibility = 'workspace' AND workspace = current_setting('memplex.workspace_id', true))
            OR (visibility = 'session'
                AND workspace = current_setting('memplex.workspace_id', true)
                AND owner_subject = current_setting('memplex.subject_id', true)
                AND NULLIF(current_setting('memplex.agent_id', true), '') IS NOT NULL
                AND NULLIF(current_setting('memplex.session_id', true), '') IS NOT NULL
                AND NULLIF(source_agent, '') IS NOT NULL
                AND NULLIF(source_session, '') IS NOT NULL
                AND source_agent = current_setting('memplex.agent_id', true)
                AND source_session = current_setting('memplex.session_id', true))))
WITH CHECK (tenant_id <> '__memplex_legacy__' AND tenant_id = current_setting('memplex.tenant_id', true)
            AND ((visibility = 'user' AND owner_subject = current_setting('memplex.subject_id', true))
                 OR (visibility = 'workspace' AND workspace = current_setting('memplex.workspace_id', true))
                 OR (visibility = 'session'
                     AND workspace = current_setting('memplex.workspace_id', true)
                     AND owner_subject = current_setting('memplex.subject_id', true)
                     AND NULLIF(current_setting('memplex.agent_id', true), '') IS NOT NULL
                     AND NULLIF(current_setting('memplex.session_id', true), '') IS NOT NULL
                     AND NULLIF(source_agent, '') IS NOT NULL
                     AND NULLIF(source_session, '') IS NOT NULL
                     AND source_agent = current_setting('memplex.agent_id', true)
                     AND source_session = current_setting('memplex.session_id', true)))
            AND owner_subject = current_setting('memplex.subject_id', true)
            AND workspace = current_setting('memplex.workspace_id', true)
            AND source_agent = current_setting('memplex.agent_id', true)
            AND source_session = current_setting('memplex.session_id', true));

ALTER TABLE memplex_observations ENABLE ROW LEVEL SECURITY;
ALTER TABLE memplex_observations FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS memplex_observations_scope ON memplex_observations;
CREATE POLICY memplex_observations_scope ON memplex_observations
USING (tenant_id <> '__memplex_legacy__' AND tenant_id = current_setting('memplex.tenant_id', true)
       AND ((visibility = 'user' AND owner_subject = current_setting('memplex.subject_id', true))
            OR (visibility = 'workspace' AND workspace = current_setting('memplex.workspace_id', true))
            OR (visibility = 'session'
                AND workspace = current_setting('memplex.workspace_id', true)
                AND owner_subject = current_setting('memplex.subject_id', true)
                AND NULLIF(current_setting('memplex.agent_id', true), '') IS NOT NULL
                AND NULLIF(current_setting('memplex.session_id', true), '') IS NOT NULL
                AND NULLIF(source_agent, '') IS NOT NULL
                AND NULLIF(source_session, '') IS NOT NULL
                AND source_agent = current_setting('memplex.agent_id', true)
                AND source_session = current_setting('memplex.session_id', true))))
WITH CHECK (tenant_id <> '__memplex_legacy__' AND tenant_id = current_setting('memplex.tenant_id', true)
            AND ((visibility = 'user' AND owner_subject = current_setting('memplex.subject_id', true))
                 OR (visibility = 'workspace' AND workspace = current_setting('memplex.workspace_id', true))
                 OR (visibility = 'session'
                     AND workspace = current_setting('memplex.workspace_id', true)
                     AND owner_subject = current_setting('memplex.subject_id', true)
                     AND NULLIF(current_setting('memplex.agent_id', true), '') IS NOT NULL
                     AND NULLIF(current_setting('memplex.session_id', true), '') IS NOT NULL
                     AND NULLIF(source_agent, '') IS NOT NULL
                     AND NULLIF(source_session, '') IS NOT NULL
                     AND source_agent = current_setting('memplex.agent_id', true)
                     AND source_session = current_setting('memplex.session_id', true)))
            AND owner_subject = current_setting('memplex.subject_id', true)
            AND workspace = current_setting('memplex.workspace_id', true)
            AND source_agent = current_setting('memplex.agent_id', true)
            AND source_session = current_setting('memplex.session_id', true));

ALTER TABLE memplex_facts ENABLE ROW LEVEL SECURITY;
ALTER TABLE memplex_facts FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS memplex_facts_scope ON memplex_facts;
CREATE POLICY memplex_facts_scope ON memplex_facts
USING (tenant_id <> '__memplex_legacy__' AND tenant_id = current_setting('memplex.tenant_id', true)
       AND ((visibility = 'user' AND owner_subject = current_setting('memplex.subject_id', true))
            OR (visibility = 'workspace' AND workspace = current_setting('memplex.workspace_id', true))
            OR (visibility = 'session'
                AND workspace = current_setting('memplex.workspace_id', true)
                AND owner_subject = current_setting('memplex.subject_id', true)
                AND NULLIF(current_setting('memplex.agent_id', true), '') IS NOT NULL
                AND NULLIF(current_setting('memplex.session_id', true), '') IS NOT NULL
                AND NULLIF(source_agent, '') IS NOT NULL
                AND NULLIF(source_session, '') IS NOT NULL
                AND source_agent = current_setting('memplex.agent_id', true)
                AND source_session = current_setting('memplex.session_id', true))))
WITH CHECK (tenant_id <> '__memplex_legacy__' AND tenant_id = current_setting('memplex.tenant_id', true)
            AND ((visibility = 'user' AND owner_subject = current_setting('memplex.subject_id', true))
                 OR (visibility = 'workspace' AND workspace = current_setting('memplex.workspace_id', true))
                 OR (visibility = 'session'
                     AND workspace = current_setting('memplex.workspace_id', true)
                     AND owner_subject = current_setting('memplex.subject_id', true)
                     AND NULLIF(current_setting('memplex.agent_id', true), '') IS NOT NULL
                     AND NULLIF(current_setting('memplex.session_id', true), '') IS NOT NULL
                     AND NULLIF(source_agent, '') IS NOT NULL
                     AND NULLIF(source_session, '') IS NOT NULL
                     AND source_agent = current_setting('memplex.agent_id', true)
                     AND source_session = current_setting('memplex.session_id', true)))
            AND owner_subject = current_setting('memplex.subject_id', true)
            AND workspace = current_setting('memplex.workspace_id', true)
            AND source_agent = current_setting('memplex.agent_id', true)
            AND source_session = current_setting('memplex.session_id', true));

ALTER TABLE memplex_preferences ENABLE ROW LEVEL SECURITY;
ALTER TABLE memplex_preferences FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS memplex_preferences_scope ON memplex_preferences;
CREATE POLICY memplex_preferences_scope ON memplex_preferences
USING (tenant_id <> '__memplex_legacy__' AND tenant_id = current_setting('memplex.tenant_id', true)
       AND ((visibility = 'user' AND owner_subject = current_setting('memplex.subject_id', true))
            OR (visibility = 'workspace' AND workspace = current_setting('memplex.workspace_id', true))
            OR (visibility = 'session'
                AND workspace = current_setting('memplex.workspace_id', true)
                AND owner_subject = current_setting('memplex.subject_id', true)
                AND NULLIF(current_setting('memplex.agent_id', true), '') IS NOT NULL
                AND NULLIF(current_setting('memplex.session_id', true), '') IS NOT NULL
                AND NULLIF(source_agent, '') IS NOT NULL
                AND NULLIF(source_session, '') IS NOT NULL
                AND source_agent = current_setting('memplex.agent_id', true)
                AND source_session = current_setting('memplex.session_id', true))))
WITH CHECK (tenant_id <> '__memplex_legacy__' AND tenant_id = current_setting('memplex.tenant_id', true)
            AND ((visibility = 'user' AND owner_subject = current_setting('memplex.subject_id', true))
                 OR (visibility = 'workspace' AND workspace = current_setting('memplex.workspace_id', true))
                 OR (visibility = 'session'
                     AND workspace = current_setting('memplex.workspace_id', true)
                     AND owner_subject = current_setting('memplex.subject_id', true)
                     AND NULLIF(current_setting('memplex.agent_id', true), '') IS NOT NULL
                     AND NULLIF(current_setting('memplex.session_id', true), '') IS NOT NULL
                     AND NULLIF(source_agent, '') IS NOT NULL
                     AND NULLIF(source_session, '') IS NOT NULL
                     AND source_agent = current_setting('memplex.agent_id', true)
                     AND source_session = current_setting('memplex.session_id', true)))
            AND owner_subject = current_setting('memplex.subject_id', true)
            AND workspace = current_setting('memplex.workspace_id', true)
            AND source_agent = current_setting('memplex.agent_id', true)
            AND source_session = current_setting('memplex.session_id', true));

ALTER TABLE memplex_changelog ENABLE ROW LEVEL SECURITY;
ALTER TABLE memplex_changelog FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS memplex_changelog_scope ON memplex_changelog;
CREATE POLICY memplex_changelog_scope ON memplex_changelog
USING (tenant_id <> '__memplex_legacy__' AND tenant_id = current_setting('memplex.tenant_id', true)
       AND ((visibility = 'user' AND owner_subject = current_setting('memplex.subject_id', true))
            OR (visibility = 'workspace' AND workspace = current_setting('memplex.workspace_id', true))
            OR (visibility = 'session'
                AND workspace = current_setting('memplex.workspace_id', true)
                AND owner_subject = current_setting('memplex.subject_id', true)
                AND NULLIF(current_setting('memplex.agent_id', true), '') IS NOT NULL
                AND NULLIF(current_setting('memplex.session_id', true), '') IS NOT NULL
                AND NULLIF(source_agent, '') IS NOT NULL
                AND NULLIF(source_session, '') IS NOT NULL
                AND source_agent = current_setting('memplex.agent_id', true)
                AND source_session = current_setting('memplex.session_id', true))))
WITH CHECK (tenant_id <> '__memplex_legacy__' AND tenant_id = current_setting('memplex.tenant_id', true)
            AND ((visibility = 'user' AND owner_subject = current_setting('memplex.subject_id', true))
                 OR (visibility = 'workspace' AND workspace = current_setting('memplex.workspace_id', true))
                 OR (visibility = 'session'
                     AND workspace = current_setting('memplex.workspace_id', true)
                     AND owner_subject = current_setting('memplex.subject_id', true)
                     AND NULLIF(current_setting('memplex.agent_id', true), '') IS NOT NULL
                     AND NULLIF(current_setting('memplex.session_id', true), '') IS NOT NULL
                     AND NULLIF(source_agent, '') IS NOT NULL
                     AND NULLIF(source_session, '') IS NOT NULL
                     AND source_agent = current_setting('memplex.agent_id', true)
                     AND source_session = current_setting('memplex.session_id', true)))
            AND owner_subject = current_setting('memplex.subject_id', true)
            AND workspace = current_setting('memplex.workspace_id', true)
            AND source_agent = current_setting('memplex.agent_id', true)
            AND source_session = current_setting('memplex.session_id', true));

COMMIT;
