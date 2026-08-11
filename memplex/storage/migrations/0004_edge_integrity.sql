-- Referential integrity for persisted graph-function endpoints.
--
-- BELONGS_TO targets are virtual namespace nodes rather than functions, so
-- only ordinary targets participate in the target function foreign key.
-- domain_node_id-v1 validation is intentionally performed only by the
-- migration runner, which reads raw JSONB under a locked temporary RLS bypass.
-- Do not execute this resource outside PostgresMigrationRunner.
ALTER TABLE memplex_functions
ADD CONSTRAINT memplex_functions_reserved_domain_id_check
CHECK (NOT starts_with(id, 'domain_'));

ALTER TABLE memplex_edges
ADD COLUMN IF NOT EXISTS target_function TEXT GENERATED ALWAYS AS (
    CASE WHEN edge_type = 'BELONGS_TO' THEN NULL::text ELSE target END
) STORED;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_constraint
        WHERE conrelid = 'memplex_edges'::regclass
          AND conname = 'memplex_edges_source_function_fk'
    ) THEN
        ALTER TABLE memplex_edges
        ADD CONSTRAINT memplex_edges_source_function_fk
        FOREIGN KEY (tenant_id, source)
        REFERENCES memplex_functions (tenant_id, id)
        ON DELETE CASCADE;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_catalog.pg_constraint
        WHERE conrelid = 'memplex_edges'::regclass
          AND conname = 'memplex_edges_target_function_fk'
    ) THEN
        ALTER TABLE memplex_edges
        ADD CONSTRAINT memplex_edges_target_function_fk
        FOREIGN KEY (tenant_id, target_function)
        REFERENCES memplex_functions (tenant_id, id)
        ON DELETE CASCADE;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS memplex_edges_tenant_target_function_idx
ON memplex_edges (tenant_id, target_function)
WHERE target_function IS NOT NULL;
