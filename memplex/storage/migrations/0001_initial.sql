-- Pre-ACL core schema.  0002 adds authoritative scope metadata and RLS.
CREATE TABLE memplex_functions (
    id TEXT PRIMARY KEY,
    data JSONB NOT NULL,
    updated_at TIMESTAMPTZ,
    search_tsv TSVECTOR GENERATED ALWAYS AS (
        to_tsvector('simple',
            coalesce(data->>'name', '') || ' ' ||
            coalesce(data->>'domain', '') || ' ' ||
            coalesce(data->>'trigger_text', '') || ' ' ||
            coalesce(data->>'action_text', '')
        )
    ) STORED
);
CREATE INDEX fts_functions_idx ON memplex_functions USING GIN (search_tsv);

CREATE TABLE memplex_edges (
    source TEXT NOT NULL,
    target TEXT NOT NULL,
    edge_type TEXT NOT NULL,
    weight REAL,
    evidence JSONB,
    created_at TIMESTAMPTZ,
    PRIMARY KEY (source, target, edge_type)
);

CREATE TABLE memplex_observations (
    id TEXT PRIMARY KEY,
    data JSONB NOT NULL,
    created_at TIMESTAMPTZ
);

CREATE TABLE memplex_facts (
    id TEXT PRIMARY KEY,
    data JSONB NOT NULL,
    updated_at TIMESTAMPTZ
);

CREATE TABLE memplex_preferences (
    id TEXT PRIMARY KEY,
    data JSONB NOT NULL,
    updated_at TIMESTAMPTZ
);

CREATE TABLE memplex_changelog (
    id BIGSERIAL PRIMARY KEY,
    func_id TEXT,
    ts TIMESTAMPTZ,
    event_type TEXT,
    description TEXT,
    source TEXT,
    actor TEXT
);
