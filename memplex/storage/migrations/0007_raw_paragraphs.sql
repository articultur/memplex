-- ADR-013 Stage 2 / Phase-B B3: the verbatim raw-paragraph authoritative
-- layer. Same typed-node shape as memplex_facts/memplex_preferences
-- (id + JSONB data + row identity), so the upsert/changelog machinery is
-- reused; the data payload carries raw_text, trust_tier, and the
-- extractor source reference. Rows never participate in prune. Sync
-- (SyncNodeType.PARAGRAPH) and retrieval integration follow in Stage 3.

CREATE TABLE memplex_paragraphs (
    id TEXT NOT NULL,
    data JSONB NOT NULL,
    updated_at TIMESTAMPTZ,
    tenant_id TEXT NOT NULL,
    owner_subject TEXT NOT NULL,
    workspace TEXT NOT NULL,
    visibility TEXT NOT NULL,
    source_agent TEXT NOT NULL,
    source_session TEXT NOT NULL,
    PRIMARY KEY (tenant_id, id)
);

CREATE INDEX IF NOT EXISTS memplex_paragraphs_tenant_idx ON memplex_paragraphs (tenant_id);

-- Same RLS safety contract as the other typed-node tables (policy named
-- <table>_scope so schema inspection proves the uniform boundary).
ALTER TABLE memplex_paragraphs ENABLE ROW LEVEL SECURITY;
ALTER TABLE memplex_paragraphs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS memplex_paragraphs_scope ON memplex_paragraphs;
CREATE POLICY memplex_paragraphs_scope ON memplex_paragraphs
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
