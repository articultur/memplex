-- Durable v1 sync catalogue.
--
-- This resource deliberately contains only the database contract.  Repository
-- transactions are introduced by G004 Task 3; direct callers must not treat
-- these tables as an HTTP protocol or an eventually-consistent sidecar.

CREATE TABLE memplex_sync_outbox (
    tenant_id TEXT NOT NULL,
    stream_seq BIGINT GENERATED ALWAYS AS IDENTITY,
    event_id TEXT NOT NULL,
    origin_node_id TEXT NOT NULL,
    node_type TEXT NOT NULL CHECK (node_type IN ('function','fact','preference','observation','edge')),
    entity_key TEXT NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('upsert','tombstone')),
    version_key TEXT NOT NULL,
    payload JSONB,
    visibility TEXT NOT NULL CHECK (visibility IN ('user','workspace','session')),
    owner_subject_id TEXT NOT NULL,
    workspace_id TEXT,
    agent_id TEXT,
    session_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, stream_seq),
    UNIQUE (tenant_id, origin_node_id, event_id),
    CHECK ((operation = 'upsert' AND payload IS NOT NULL)
        OR (operation = 'tombstone' AND payload IS NULL))
);

CREATE TABLE memplex_sync_entity_versions (
    tenant_id TEXT NOT NULL,
    node_type TEXT NOT NULL CHECK (node_type IN ('function','fact','preference','observation','edge')),
    entity_key TEXT NOT NULL,
    version_key TEXT NOT NULL,
    deleted BOOLEAN NOT NULL,
    event_id TEXT NOT NULL,
    last_stream_seq BIGINT NOT NULL,
    PRIMARY KEY (tenant_id, node_type, entity_key)
);

CREATE TABLE memplex_sync_inbox (
    tenant_id TEXT NOT NULL,
    origin_node_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('accepted','duplicate','rejected_conflict')),
    applied_stream_seq BIGINT,
    received_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, origin_node_id, event_id)
);

CREATE TABLE memplex_sync_batches (
    tenant_id TEXT NOT NULL,
    origin_node_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
    response JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, origin_node_id, batch_id)
);

CREATE TABLE memplex_sync_targets (
    tenant_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    remote_node_id TEXT NOT NULL,
    bootstrap_seq BIGINT NOT NULL CHECK (bootstrap_seq >= 0),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY (tenant_id, target_id),
    UNIQUE (tenant_id, remote_node_id)
);

CREATE TABLE memplex_sync_deliveries (
    tenant_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    stream_seq BIGINT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending','leased','delivered','dead_letter')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    lease_owner TEXT,
    lease_until TIMESTAMPTZ,
    last_error_code TEXT,
    PRIMARY KEY (tenant_id, target_id, stream_seq),
    FOREIGN KEY (tenant_id, target_id)
      REFERENCES memplex_sync_targets (tenant_id, target_id) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_id, stream_seq)
      REFERENCES memplex_sync_outbox (tenant_id, stream_seq) ON DELETE CASCADE,
    CHECK ((state = 'leased') = (lease_owner IS NOT NULL AND lease_until IS NOT NULL))
);

CREATE TABLE memplex_sync_cursors (
    tenant_id TEXT NOT NULL,
    remote_id TEXT NOT NULL,
    consumer_id TEXT NOT NULL,
    after_seq BIGINT NOT NULL CHECK (after_seq >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, remote_id, consumer_id)
);

CREATE TABLE memplex_sync_stream_state (
    tenant_id TEXT PRIMARY KEY,
    retention_floor BIGINT NOT NULL DEFAULT 0 CHECK (retention_floor >= 0),
    compacted_through BIGINT NOT NULL DEFAULT 0 CHECK (compacted_through >= retention_floor)
);

-- This deployment singleton is deliberately outside the application ACL.  A
-- locally supplied custom GUC is not an authority for origin identity.
CREATE TABLE memplex_sync_local_identity (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    node_id TEXT NOT NULL UNIQUE CHECK (node_id <> '')
);

CREATE TABLE memplex_sync_ingress_principals (
    role_name NAME NOT NULL,
    remote_node_id TEXT NOT NULL CHECK (remote_node_id <> ''),
    enabled BOOLEAN NOT NULL DEFAULT TRUE
    ,PRIMARY KEY (role_name, remote_node_id)
);

CREATE TABLE memplex_sync_snapshots (
    tenant_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    remote_id TEXT NOT NULL,
    consumer_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    resume_seq BIGINT NOT NULL CHECK (resume_seq >= 0),
    expires_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (tenant_id, snapshot_id),
    UNIQUE (tenant_id, remote_id, consumer_id, request_id)
);

CREATE TABLE memplex_sync_snapshot_items (
    tenant_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    node_type TEXT NOT NULL CHECK (node_type IN ('function','fact','preference','observation','edge')),
    entity_key TEXT NOT NULL,
    event JSONB NOT NULL,
    PRIMARY KEY (tenant_id, snapshot_id, node_type, entity_key),
    FOREIGN KEY (tenant_id, snapshot_id)
      REFERENCES memplex_sync_snapshots (tenant_id, snapshot_id) ON DELETE CASCADE
);

CREATE INDEX memplex_sync_outbox_tenant_stream_idx
ON memplex_sync_outbox (tenant_id, stream_seq);
CREATE INDEX memplex_sync_deliveries_claim_idx
ON memplex_sync_deliveries (tenant_id, target_id, next_attempt_at, stream_seq)
WHERE state IN ('pending', 'leased');
CREATE INDEX memplex_sync_deliveries_retention_idx
ON memplex_sync_deliveries (tenant_id, stream_seq, state);
CREATE INDEX memplex_sync_cursors_tenant_after_idx
ON memplex_sync_cursors (tenant_id, after_seq);
CREATE INDEX memplex_sync_snapshots_expiry_idx
ON memplex_sync_snapshots (tenant_id, expires_at);

-- Keep an outbox event under the same subject/workspace/session visibility as
-- the business row that caused it.  Tombstones retain this scope even after
-- the business row itself is gone.
ALTER TABLE memplex_sync_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE memplex_sync_outbox FORCE ROW LEVEL SECURITY;
CREATE POLICY memplex_sync_outbox_scope ON memplex_sync_outbox
USING (
    current_user = (
        SELECT pg_catalog.pg_get_userbyid(relation.relowner)
        FROM pg_catalog.pg_class AS relation
        WHERE relation.oid = 'memplex_sync_outbox'::pg_catalog.regclass
    )
    OR (tenant_id = current_setting('memplex.tenant_id', true)
        AND ((visibility = 'user' AND owner_subject_id = current_setting('memplex.subject_id', true))
          OR (visibility = 'workspace' AND workspace_id = current_setting('memplex.workspace_id', true))
          OR (visibility = 'session'
              AND owner_subject_id = current_setting('memplex.subject_id', true)
              AND workspace_id = current_setting('memplex.workspace_id', true)
              AND agent_id = current_setting('memplex.agent_id', true)
              AND session_id = current_setting('memplex.session_id', true))))
)
WITH CHECK (
    tenant_id = current_setting('memplex.tenant_id', true)
    AND owner_subject_id = current_setting('memplex.subject_id', true)
    AND (workspace_id IS NULL OR workspace_id = current_setting('memplex.workspace_id', true))
);

ALTER TABLE memplex_sync_entity_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE memplex_sync_entity_versions FORCE ROW LEVEL SECURITY;
CREATE POLICY memplex_sync_entity_versions_scope ON memplex_sync_entity_versions
USING (tenant_id = current_setting('memplex.tenant_id', true))
WITH CHECK (tenant_id = current_setting('memplex.tenant_id', true));

ALTER TABLE memplex_sync_inbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE memplex_sync_inbox FORCE ROW LEVEL SECURITY;
CREATE POLICY memplex_sync_inbox_scope ON memplex_sync_inbox
USING (tenant_id = current_setting('memplex.tenant_id', true)
       AND origin_node_id = current_setting('memplex.verified_remote_node_id', true))
WITH CHECK (tenant_id = current_setting('memplex.tenant_id', true)
            AND origin_node_id = current_setting('memplex.verified_remote_node_id', true));

ALTER TABLE memplex_sync_batches ENABLE ROW LEVEL SECURITY;
ALTER TABLE memplex_sync_batches FORCE ROW LEVEL SECURITY;
CREATE POLICY memplex_sync_batches_scope ON memplex_sync_batches
USING (tenant_id = current_setting('memplex.tenant_id', true)
       AND origin_node_id = current_setting('memplex.verified_remote_node_id', true))
WITH CHECK (tenant_id = current_setting('memplex.tenant_id', true)
            AND origin_node_id = current_setting('memplex.verified_remote_node_id', true));

ALTER TABLE memplex_sync_targets ENABLE ROW LEVEL SECURITY;
ALTER TABLE memplex_sync_targets FORCE ROW LEVEL SECURITY;
CREATE POLICY memplex_sync_targets_scope ON memplex_sync_targets
USING (tenant_id = current_setting('memplex.tenant_id', true))
WITH CHECK (tenant_id = current_setting('memplex.tenant_id', true));

ALTER TABLE memplex_sync_deliveries ENABLE ROW LEVEL SECURITY;
ALTER TABLE memplex_sync_deliveries FORCE ROW LEVEL SECURITY;
CREATE POLICY memplex_sync_deliveries_scope ON memplex_sync_deliveries
USING (tenant_id = current_setting('memplex.tenant_id', true))
WITH CHECK (tenant_id = current_setting('memplex.tenant_id', true));

ALTER TABLE memplex_sync_cursors ENABLE ROW LEVEL SECURITY;
ALTER TABLE memplex_sync_cursors FORCE ROW LEVEL SECURITY;
CREATE POLICY memplex_sync_cursors_scope ON memplex_sync_cursors
USING (
    current_user = (
        SELECT pg_catalog.pg_get_userbyid(relation.relowner)
        FROM pg_catalog.pg_class AS relation
        WHERE relation.oid = 'memplex_sync_cursors'::pg_catalog.regclass
    )
    OR (tenant_id = current_setting('memplex.tenant_id', true)
        AND remote_id = current_setting('memplex.verified_remote_node_id', true)
        AND consumer_id = current_setting('memplex.consumer_id', true))
)
WITH CHECK (tenant_id = current_setting('memplex.tenant_id', true)
            AND remote_id = current_setting('memplex.verified_remote_node_id', true)
            AND consumer_id = current_setting('memplex.consumer_id', true));

ALTER TABLE memplex_sync_stream_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE memplex_sync_stream_state FORCE ROW LEVEL SECURITY;
CREATE POLICY memplex_sync_stream_state_scope ON memplex_sync_stream_state
USING (tenant_id = current_setting('memplex.tenant_id', true))
WITH CHECK (tenant_id = current_setting('memplex.tenant_id', true));

ALTER TABLE memplex_sync_local_identity ENABLE ROW LEVEL SECURITY;
ALTER TABLE memplex_sync_local_identity FORCE ROW LEVEL SECURITY;
CREATE POLICY memplex_sync_local_identity_scope ON memplex_sync_local_identity
USING (
    current_user = (
        SELECT pg_catalog.pg_get_userbyid(relation.relowner)
        FROM pg_catalog.pg_class AS relation
        WHERE relation.oid = 'memplex_sync_local_identity'::pg_catalog.regclass
    )
)
WITH CHECK (
    current_user = (
        SELECT pg_catalog.pg_get_userbyid(relation.relowner)
        FROM pg_catalog.pg_class AS relation
        WHERE relation.oid = 'memplex_sync_local_identity'::pg_catalog.regclass
    )
);

ALTER TABLE memplex_sync_ingress_principals ENABLE ROW LEVEL SECURITY;
ALTER TABLE memplex_sync_ingress_principals FORCE ROW LEVEL SECURITY;
CREATE POLICY memplex_sync_ingress_principals_scope ON memplex_sync_ingress_principals
USING (
    current_user = (
        SELECT pg_catalog.pg_get_userbyid(relation.relowner)
        FROM pg_catalog.pg_class AS relation
        WHERE relation.oid = 'memplex_sync_ingress_principals'::pg_catalog.regclass
    )
)
WITH CHECK (
    current_user = (
        SELECT pg_catalog.pg_get_userbyid(relation.relowner)
        FROM pg_catalog.pg_class AS relation
        WHERE relation.oid = 'memplex_sync_ingress_principals'::pg_catalog.regclass
    )
);

ALTER TABLE memplex_sync_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE memplex_sync_snapshots FORCE ROW LEVEL SECURITY;
CREATE POLICY memplex_sync_snapshots_scope ON memplex_sync_snapshots
USING (
    current_user = (
        SELECT pg_catalog.pg_get_userbyid(relation.relowner)
        FROM pg_catalog.pg_class AS relation
        WHERE relation.oid = 'memplex_sync_snapshots'::pg_catalog.regclass
    )
    OR (tenant_id = current_setting('memplex.tenant_id', true)
        AND remote_id = current_setting('memplex.verified_remote_node_id', true)
        AND consumer_id = current_setting('memplex.consumer_id', true))
)
WITH CHECK (tenant_id = current_setting('memplex.tenant_id', true)
            AND remote_id = current_setting('memplex.verified_remote_node_id', true)
            AND consumer_id = current_setting('memplex.consumer_id', true));

ALTER TABLE memplex_sync_snapshot_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE memplex_sync_snapshot_items FORCE ROW LEVEL SECURITY;
CREATE POLICY memplex_sync_snapshot_items_scope ON memplex_sync_snapshot_items
USING (tenant_id = current_setting('memplex.tenant_id', true)
       AND EXISTS (
           SELECT 1 FROM memplex_sync_snapshots snapshot
           WHERE snapshot.tenant_id = memplex_sync_snapshot_items.tenant_id
             AND snapshot.snapshot_id = memplex_sync_snapshot_items.snapshot_id
             AND snapshot.remote_id = current_setting('memplex.verified_remote_node_id', true)
             AND snapshot.consumer_id = current_setting('memplex.consumer_id', true)))
WITH CHECK (tenant_id = current_setting('memplex.tenant_id', true));

-- Trigger functions are security-definer and have a fixed catalog-only path.
-- The normal application path leaves sync_capture unset, preserving the
-- sync-disabled deployment contract.  Once an active store marks capture
-- required, it must have bound identity, verified local node and apply mode.
CREATE FUNCTION memplex_configure_sync_local_identity(configured_node_id TEXT)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    owner_name TEXT;
BEGIN
    SELECT pg_catalog.pg_get_userbyid(relation.relowner)
    INTO owner_name
    FROM pg_catalog.pg_class AS relation
    WHERE relation.oid = 'memplex_sync_local_identity'::pg_catalog.regclass;
    IF session_user IS DISTINCT FROM owner_name
       OR NULLIF(configured_node_id, '') IS NULL THEN
        RAISE EXCEPTION 'memplex local identity configuration is owner-only' USING ERRCODE = '42501';
    END IF;
    INSERT INTO memplex_sync_local_identity (singleton, node_id)
    VALUES (TRUE, configured_node_id)
    ON CONFLICT (singleton) DO UPDATE SET node_id = EXCLUDED.node_id;
END;
$$;

-- Task 3 must call this same transaction-scoped gate before target registration
-- and inbound delivery creation.  The local trigger supplies its enabled-target
-- fanout as ``additional_deliveries`` before any business or outbox write.
CREATE FUNCTION memplex_sync_assert_delivery_quota(
    quota_tenant_id TEXT,
    additional_deliveries BIGINT
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    existing_deliveries BIGINT;
BEGIN
    IF NULLIF(quota_tenant_id, '') IS NULL OR additional_deliveries < 0 THEN
        RAISE EXCEPTION 'memplex sync delivery quota input is invalid' USING ERRCODE = '42501';
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended('memplex-sync-delivery-quota:' || quota_tenant_id, 0)
    );
    PERFORM 1 FROM memplex_sync_stream_state
    WHERE tenant_id = quota_tenant_id
    FOR UPDATE;
    SELECT count(*) INTO existing_deliveries
    FROM memplex_sync_deliveries AS delivery
    JOIN memplex_sync_targets AS target
      ON target.tenant_id = delivery.tenant_id
     AND target.target_id = delivery.target_id
    WHERE delivery.tenant_id = quota_tenant_id
      AND target.enabled
      AND delivery.state IN ('pending', 'leased', 'dead_letter');
    IF existing_deliveries + additional_deliveries > 100000 THEN
        RAISE EXCEPTION 'memplex sync pending delivery quota exceeded' USING ERRCODE = '54000';
    END IF;
END;
$$;

-- Snapshot admission is a tenant-wide invariant, while ordinary snapshot RLS
-- intentionally exposes only the caller's exact remote/consumer pair.  This
-- owner-executed helper serializes all admissions for one tenant and returns
-- counts across those hidden consumers without exposing their identities.
CREATE FUNCTION memplex_sync_snapshot_admission_counts()
RETURNS TABLE(remote_count BIGINT, tenant_count BIGINT)
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    scope_tenant_id TEXT := NULLIF(current_setting('memplex.tenant_id', true), '');
    scope_remote_id TEXT := NULLIF(current_setting('memplex.verified_remote_node_id', true), '');
BEGIN
    IF scope_tenant_id IS NULL OR scope_remote_id IS NULL THEN
        RAISE EXCEPTION 'memplex snapshot admission scope is incomplete' USING ERRCODE = '42501';
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended('memplex-sync-snapshot-admission:' || scope_tenant_id, 0)
    );
    RETURN QUERY
    SELECT
        count(*) FILTER (WHERE snapshot.remote_id = scope_remote_id)::BIGINT,
        count(*)::BIGINT
    FROM memplex_sync_snapshots AS snapshot
    WHERE snapshot.tenant_id = scope_tenant_id
      AND snapshot.expires_at > clock_timestamp();
END;
$$;

-- Compact only one safe, old, continuously consumable tenant prefix.  The
-- caller supplies policy durations as absolute cutoffs; identities and pins
-- are read under the owner while only the deleted-row count is disclosed.
CREATE FUNCTION memplex_sync_compact(
    retention_before TIMESTAMPTZ,
    consumer_cutoff TIMESTAMPTZ,
    max_rows INTEGER
)
RETURNS BIGINT
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    scope_tenant_id TEXT := NULLIF(current_setting('memplex.tenant_id', true), '');
    current_floor BIGINT;
    cursor_floor BIGINT;
    snapshot_floor BIGINT;
    compact_through BIGINT;
    deleted_rows BIGINT := 0;
BEGIN
    IF scope_tenant_id IS NULL
       OR retention_before IS NULL
       OR consumer_cutoff IS NULL
       OR max_rows IS NULL
       OR max_rows < 1 THEN
        RAISE EXCEPTION 'memplex compaction input is invalid' USING ERRCODE = '22023';
    END IF;
    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended('memplex-sync-retention:' || scope_tenant_id, 0)
    );
    INSERT INTO memplex_sync_stream_state (tenant_id, retention_floor, compacted_through)
    VALUES (scope_tenant_id, 0, 0)
    ON CONFLICT (tenant_id) DO NOTHING;
    SELECT state.retention_floor
    INTO current_floor
    FROM memplex_sync_stream_state AS state
    WHERE state.tenant_id = scope_tenant_id
    FOR UPDATE;
    SELECT min(cursor.after_seq)
    INTO cursor_floor
    FROM memplex_sync_cursors AS cursor
    WHERE cursor.tenant_id = scope_tenant_id
      AND cursor.updated_at >= consumer_cutoff;
    SELECT min(snapshot.resume_seq)
    INTO snapshot_floor
    FROM memplex_sync_snapshots AS snapshot
    WHERE snapshot.tenant_id = scope_tenant_id
      AND snapshot.expires_at > clock_timestamp();

    WITH candidate AS (
        SELECT outbox.stream_seq,
               outbox.created_at <= retention_before
               AND (cursor_floor IS NULL OR outbox.stream_seq <= cursor_floor)
               AND (snapshot_floor IS NULL OR outbox.stream_seq <= snapshot_floor)
               AND NOT EXISTS (
                   SELECT 1
                   FROM memplex_sync_targets AS target
                   WHERE target.tenant_id = scope_tenant_id
                     AND target.enabled
                     AND outbox.stream_seq > target.bootstrap_seq
                     AND NOT EXISTS (
                         SELECT 1
                         FROM memplex_sync_deliveries AS delivery
                         WHERE delivery.tenant_id = scope_tenant_id
                           AND delivery.target_id = target.target_id
                           AND delivery.stream_seq = outbox.stream_seq
                           AND delivery.state = 'delivered'
                     )
               ) AS safe
        FROM memplex_sync_outbox AS outbox
        WHERE outbox.tenant_id = scope_tenant_id
          AND outbox.stream_seq > current_floor
        ORDER BY outbox.stream_seq
        LIMIT max_rows
    ), prefix AS (
        SELECT stream_seq,
               bool_and(safe) OVER (
                   ORDER BY stream_seq ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
               ) AS prefix_safe
        FROM candidate
    )
    SELECT max(stream_seq) FILTER (WHERE prefix_safe)
    INTO compact_through
    FROM prefix;

    IF compact_through IS NULL THEN
        RETURN 0;
    END IF;
    UPDATE memplex_sync_stream_state
    SET retention_floor = GREATEST(retention_floor, compact_through),
        compacted_through = GREATEST(compacted_through, compact_through)
    WHERE tenant_id = scope_tenant_id;
    DELETE FROM memplex_sync_outbox
    WHERE tenant_id = scope_tenant_id
      AND stream_seq <= compact_through;
    GET DIAGNOSTICS deleted_rows = ROW_COUNT;
    RETURN deleted_rows;
END;
$$;

CREATE FUNCTION memplex_configure_sync_ingress_principal(
    configured_role NAME,
    configured_remote_node_id TEXT
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE owner_name TEXT;
BEGIN
    SELECT pg_catalog.pg_get_userbyid(relation.relowner) INTO owner_name
    FROM pg_catalog.pg_class AS relation
    WHERE relation.oid = 'memplex_sync_ingress_principals'::pg_catalog.regclass;
    IF session_user IS DISTINCT FROM owner_name
       OR NULLIF(configured_role::TEXT, '') IS NULL
       OR NULLIF(configured_remote_node_id, '') IS NULL THEN
        RAISE EXCEPTION 'memplex ingress configuration is owner-only' USING ERRCODE = '42501';
    END IF;
    INSERT INTO memplex_sync_ingress_principals (role_name, remote_node_id, enabled)
    VALUES (configured_role, configured_remote_node_id, TRUE)
    ON CONFLICT (role_name, remote_node_id) DO UPDATE SET enabled = TRUE;
END;
$$;

-- Task 1 JCS is a byte-level protocol, not merely JSONB-shaped data.  These
-- helpers are owner-only implementation details: the ingress entrypoint alone
-- invokes them before any receipt/quota/identity effect.  Object keys sort by
-- UTF-16BE code units, matching RFC 8785 / ECMAScript rather than PostgreSQL's
-- JSONB key ordering.
CREATE FUNCTION memplex_sync_jcs_number(value NUMERIC)
RETURNS TEXT
LANGUAGE plpgsql
IMMUTABLE
SECURITY DEFINER
AS $$
DECLARE
    as_float DOUBLE PRECISION;
    rendered TEXT;
    unsigned TEXT;
    digits TEXT;
    exponent INTEGER;
BEGIN
    as_float := value::DOUBLE PRECISION;
    IF as_float IN ('Infinity'::DOUBLE PRECISION, '-Infinity'::DOUBLE PRECISION) OR as_float <> as_float THEN
        RAISE EXCEPTION 'memplex JCS number is non-finite' USING ERRCODE = '22023';
    END IF;
    IF as_float = 0 THEN RETURN '0'; END IF;
    IF abs(as_float) < 0.000001 OR abs(as_float) >= 1e21 THEN
        rendered := value::TEXT;
        IF rendered LIKE '-%' THEN
            unsigned := substr(rendered, 2);
        ELSE
            unsigned := rendered;
        END IF;
        IF abs(as_float) < 0.000001 THEN
            digits := ltrim(substr(unsigned, 3), '0');
            digits := regexp_replace(digits, '0+$', '');
            exponent := length(unsigned) - length(digits) - 1;
            rendered := CASE WHEN rendered LIKE '-%' THEN '-' ELSE '' END
                || substr(digits, 1, 1)
                || CASE WHEN length(digits) > 1 THEN '.' || substr(digits, 2) ELSE '' END
                || 'e-' || exponent::TEXT;
        ELSE
            digits := regexp_replace(unsigned, '\\.', '', 'g');
            exponent := length(digits) - 1;
            digits := regexp_replace(digits, '0+$', '');
            rendered := CASE WHEN rendered LIKE '-%' THEN '-' ELSE '' END
                || substr(digits, 1, 1)
                || CASE WHEN length(digits) > 1 THEN '.' || substr(digits, 2) ELSE '' END
                || 'e+' || exponent::TEXT;
        END IF;
    ELSE
        rendered := pg_catalog.to_jsonb(as_float)::TEXT;
        IF position('.' IN rendered) > 0 AND position('e' IN lower(rendered)) = 0 THEN
            rendered := regexp_replace(rendered, '0+$', '');
            rendered := regexp_replace(rendered, '\\.$', '');
        END IF;
    END IF;
    rendered := regexp_replace(rendered, 'e([+-])0+([0-9]+)$', 'e\\1\\2');
    RETURN rendered;
END;
$$;

CREATE FUNCTION memplex_sync_jcs_key_sort_key(value TEXT)
RETURNS TEXT
LANGUAGE plpgsql
IMMUTABLE
SECURITY DEFINER
AS $$
DECLARE
    position_index INTEGER;
    codepoint INTEGER;
    bytes BYTEA;
    first_byte INTEGER;
    result TEXT := '';
BEGIN
    FOR position_index IN 1..char_length(value) LOOP
        bytes := convert_to(substr(value, position_index, 1), 'UTF8');
        first_byte := get_byte(bytes, 0);
        codepoint := CASE
            WHEN first_byte < 128 THEN first_byte
            WHEN first_byte < 224 THEN ((first_byte & 31) << 6) + (get_byte(bytes, 1) & 63)
            WHEN first_byte < 240 THEN ((first_byte & 15) << 12) + ((get_byte(bytes, 1) & 63) << 6) + (get_byte(bytes, 2) & 63)
            ELSE ((first_byte & 7) << 18) + ((get_byte(bytes, 1) & 63) << 12) + ((get_byte(bytes, 2) & 63) << 6) + (get_byte(bytes, 3) & 63)
        END;
        IF codepoint <= 65535 THEN
            result := result || lpad(to_hex(codepoint), 4, '0');
        ELSE
            codepoint := codepoint - 65536;
            result := result || lpad(to_hex(55296 + (codepoint >> 10)), 4, '0');
            result := result || lpad(to_hex(56320 + (codepoint & 1023)), 4, '0');
        END IF;
    END LOOP;
    RETURN result;
END;
$$;

CREATE FUNCTION memplex_sync_jcs_encode(value JSONB)
RETURNS TEXT
LANGUAGE plpgsql
IMMUTABLE
SECURITY DEFINER
AS $$
DECLARE
    item JSONB;
    key TEXT;
    pairs TEXT[] := ARRAY[]::TEXT[];
BEGIN
    CASE jsonb_typeof(value)
    WHEN 'null' THEN RETURN 'null';
    WHEN 'boolean' THEN RETURN CASE WHEN value = 'true'::jsonb THEN 'true' ELSE 'false' END;
    WHEN 'number' THEN RETURN memplex_sync_jcs_number(value::TEXT::NUMERIC);
    WHEN 'string' THEN RETURN pg_catalog.to_jsonb(value #>> '{}')::TEXT;
    WHEN 'array' THEN
        FOR item IN SELECT element FROM jsonb_array_elements(value) AS entry(element) LOOP
            pairs := array_append(pairs, memplex_sync_jcs_encode(item));
        END LOOP;
        RETURN '[' || array_to_string(pairs, ',') || ']';
    WHEN 'object' THEN
        FOR key, item IN
            SELECT entry.key, entry.value
            FROM jsonb_each(value) AS entry(key, value)
            ORDER BY memplex_sync_jcs_key_sort_key(entry.key)
        LOOP
            pairs := array_append(pairs, pg_catalog.to_jsonb(key)::TEXT || ':' || memplex_sync_jcs_encode(item));
        END LOOP;
        RETURN '{' || array_to_string(pairs, ',') || '}';
    ELSE RAISE EXCEPTION 'memplex JCS value is invalid' USING ERRCODE = '22023';
    END CASE;
END;
$$;

CREATE FUNCTION memplex_sync_encode_string_array(value JSONB)
RETURNS TEXT LANGUAGE plpgsql IMMUTABLE SECURITY DEFINER AS $$
BEGIN
    IF jsonb_typeof(value) <> 'array' OR jsonb_array_length(value) <> 3
       OR jsonb_typeof(value->0) <> 'string' OR jsonb_typeof(value->1) <> 'string'
       OR jsonb_typeof(value->2) <> 'string' THEN
        RAISE EXCEPTION 'memplex canonical string array is invalid' USING ERRCODE = '22023';
    END IF;
    RETURN '[' || pg_catalog.to_jsonb(value->>0)::TEXT || ',' || pg_catalog.to_jsonb(value->>1)::TEXT || ',' || pg_catalog.to_jsonb(value->>2)::TEXT || ']';
END;
$$;

CREATE FUNCTION memplex_sync_require_canonical_entity_key(entity_key TEXT, expected_kind TEXT)
RETURNS void
LANGUAGE plpgsql
IMMUTABLE
SECURITY DEFINER
AS $$
DECLARE
    encoded TEXT;
    raw BYTEA;
    decoded TEXT;
    edge_parts JSONB;
    canonical TEXT;
BEGIN
    IF expected_kind NOT IN ('node', 'edge') OR octet_length(entity_key) > 1200
       OR entity_key !~ ('^' || expected_kind || ':v1:[A-Za-z0-9_-]+$') THEN
        RAISE EXCEPTION 'memplex sync entity key is not canonical' USING ERRCODE = '22023';
    END IF;
    encoded := split_part(entity_key, ':', 3);
    BEGIN
        raw := decode(replace(replace(encoded, '-', '+'), '_', '/')
                      || repeat('=', (4 - length(encoded) % 4) % 4), 'base64');
        canonical := translate(trim(trailing '=' FROM replace(encode(raw, 'base64'), E'\n', '')), '+/', '-_');
    EXCEPTION WHEN others THEN
        RAISE EXCEPTION 'memplex sync entity key is not canonical' USING ERRCODE = '22023';
    END;
    IF canonical IS DISTINCT FROM encoded THEN
        RAISE EXCEPTION 'memplex sync entity key is not canonical' USING ERRCODE = '22023';
    END IF;
    BEGIN
        decoded := convert_from(raw, 'UTF8');
    EXCEPTION WHEN others THEN
        RAISE EXCEPTION 'memplex sync entity key is not canonical' USING ERRCODE = '22023';
    END;
    IF expected_kind = 'node' THEN
        IF decoded = '' OR octet_length(raw) > 256 THEN
            RAISE EXCEPTION 'memplex sync entity key is not canonical' USING ERRCODE = '22023';
        END IF;
    ELSE
        BEGIN edge_parts := decoded::jsonb; EXCEPTION WHEN others THEN
            RAISE EXCEPTION 'memplex sync entity key is not canonical' USING ERRCODE = '22023';
        END;
        IF jsonb_typeof(edge_parts) <> 'array' OR jsonb_array_length(edge_parts) <> 3
           OR jsonb_typeof(edge_parts->0) <> 'string' OR jsonb_typeof(edge_parts->1) <> 'string'
           OR jsonb_typeof(edge_parts->2) <> 'string'
           OR edge_parts->>0 = '' OR edge_parts->>1 = '' OR edge_parts->>2 = ''
           OR octet_length(convert_to(edge_parts->>0, 'UTF8')) > 256
           OR octet_length(convert_to(edge_parts->>1, 'UTF8')) > 256
           OR octet_length(convert_to(edge_parts->>2, 'UTF8')) > 256
           OR memplex_sync_encode_string_array(edge_parts) IS DISTINCT FROM decoded
           THEN
            RAISE EXCEPTION 'memplex sync entity key is not canonical' USING ERRCODE = '22023';
        END IF;
    END IF;
END;
$$;

CREATE FUNCTION memplex_sync_require_canonical_version(version_key TEXT, expected_origin TEXT, expected_event_id TEXT)
RETURNS void
LANGUAGE plpgsql
IMMUTABLE
SECURITY DEFINER
AS $$
DECLARE encoded TEXT; raw BYTEA; decoded TEXT; parts JSONB; canonical TEXT;
BEGIN
    IF version_key !~ '^v1:[A-Za-z0-9_-]+$' THEN
        RAISE EXCEPTION 'memplex sync version is not canonical' USING ERRCODE = '22023';
    END IF;
    encoded := split_part(version_key, ':', 2);
    BEGIN
        raw := decode(replace(replace(encoded, '-', '+'), '_', '/') || repeat('=', (4 - length(encoded) % 4) % 4), 'base64');
        canonical := translate(trim(trailing '=' FROM replace(encode(raw, 'base64'), E'\n', '')), '+/', '-_');
        decoded := convert_from(raw, 'UTF8'); parts := decoded::jsonb;
    EXCEPTION WHEN others THEN
        RAISE EXCEPTION 'memplex sync version is not canonical' USING ERRCODE = '22023';
    END;
    IF parts->>0 !~ '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$' THEN
        RAISE EXCEPTION 'memplex sync version is not canonical' USING ERRCODE = '22023';
    END IF;
    BEGIN
        PERFORM (parts->>0)::TIMESTAMP;
    EXCEPTION WHEN others THEN
        RAISE EXCEPTION 'memplex sync version is not canonical' USING ERRCODE = '22023';
    END;
    IF pg_catalog.to_char(
        (parts->>0)::TIMESTAMP, 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
    ) IS DISTINCT FROM parts->>0 THEN
        RAISE EXCEPTION 'memplex sync version is not canonical' USING ERRCODE = '22023';
    END IF;
    IF canonical IS DISTINCT FROM encoded OR jsonb_typeof(parts) <> 'array' OR jsonb_array_length(parts) <> 3
       OR jsonb_typeof(parts->0) <> 'string' OR jsonb_typeof(parts->1) <> 'string' OR jsonb_typeof(parts->2) <> 'string'
       OR parts->>0 !~ '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$'
       OR parts->>1 IS DISTINCT FROM expected_origin OR parts->>2 IS DISTINCT FROM expected_event_id
       OR memplex_sync_encode_string_array(parts) IS DISTINCT FROM decoded THEN
        RAISE EXCEPTION 'memplex sync version is not canonical' USING ERRCODE = '22023';
    END IF;
END;
$$;

CREATE FUNCTION memplex_sync_capture_before()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    local_node_id TEXT;
    source_tenant_id TEXT;
    enabled_target_fanout BIGINT;
BEGIN
    IF current_setting('memplex.sync_capture', true) IS DISTINCT FROM 'required' THEN
        RETURN COALESCE(NEW, OLD);
    END IF;
    IF current_setting('memplex.sync_apply_mode', true) = 'inbound' THEN
        -- An application session can manufacture arbitrary custom GUCs.  The
        -- only inbound authority is the LOGIN role binding below; that role
        -- has no relation/sequence privileges, so its writes can only arrive
        -- through memplex_sync_apply_inbound.
        IF NOT EXISTS (
            SELECT 1 FROM memplex_sync_ingress_principals
            WHERE role_name = session_user AND enabled
        ) THEN
            RAISE EXCEPTION 'memplex sync inbound is ingress-only' USING ERRCODE = '42501';
        END IF;
        RETURN COALESCE(NEW, OLD);
    END IF;
    IF current_setting('memplex.sync_apply_mode', true) IS DISTINCT FROM 'local' THEN
        RAISE EXCEPTION 'memplex sync context is incomplete' USING ERRCODE = '42501';
    END IF;
    IF current_setting('memplex.sync_apply_mode', true) = 'local' THEN
        source_tenant_id := to_jsonb(COALESCE(NEW, OLD))->>'tenant_id';
        SELECT node_id INTO local_node_id
        FROM memplex_sync_local_identity
        WHERE singleton;
    END IF;
    IF current_setting('memplex.sync_apply_mode', true) = 'local' AND (
        NULLIF(current_setting('memplex.tenant_id', true), '') IS NULL
        OR NULLIF(current_setting('memplex.subject_id', true), '') IS NULL
        OR NULLIF(current_setting('memplex.sync_origin_node_id', true), '') IS NULL
        OR current_setting('memplex.sync_origin_node_id', true) IS DISTINCT FROM
           local_node_id
        OR source_tenant_id IS DISTINCT FROM current_setting('memplex.tenant_id', true)
        OR current_setting('memplex.sync_event_id', true) IS NULL
        OR current_setting('memplex.sync_event_id', true) !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
        OR NULLIF(current_setting('memplex.sync_version_key', true), '') IS NULL
        OR current_setting('memplex.sync_version_key', true) !~ '^v1:[A-Za-z0-9_-]+$'
        OR NULLIF(current_setting('memplex.sync_entity_key', true), '') IS NULL
        OR (TG_ARGV[0] = 'edge' AND current_setting('memplex.sync_entity_key', true) !~ '^edge:v1:[A-Za-z0-9_-]+$')
        OR (TG_ARGV[0] <> 'edge' AND current_setting('memplex.sync_entity_key', true) !~ '^node:v1:[A-Za-z0-9_-]+$')
        OR (TG_OP = 'DELETE' AND NULLIF(current_setting('memplex.sync_payload', true), '') IS NOT NULL)
        OR (TG_OP <> 'DELETE' AND (
            NULLIF(current_setting('memplex.sync_payload', true), '') IS NULL
            OR jsonb_typeof(current_setting('memplex.sync_payload', true)::jsonb) IS DISTINCT FROM 'object'
        ))
    ) THEN
        RAISE EXCEPTION 'memplex sync context is incomplete' USING ERRCODE = '42501';
    END IF;
    IF current_setting('memplex.sync_apply_mode', true) = 'local' THEN
        SELECT count(*) INTO enabled_target_fanout
        FROM memplex_sync_targets AS target
        WHERE target.tenant_id = source_tenant_id AND target.enabled;
        PERFORM memplex_sync_assert_delivery_quota(source_tenant_id, enabled_target_fanout);
    END IF;
    RETURN COALESCE(NEW, OLD);
END;
$$;

-- This is intentionally the *only* inbound write boundary.  It receives the
-- exact Task-1 JCS bytes (not a decoded/re-serialised body), verifies their
-- SHA-256 itself, authenticates session_user against the owner-managed ingress
-- binding, then records all durable state in the caller transaction.  The
-- Python repository remains responsible for producing JCS and for the richer
-- domain payload projection introduced in Task 3; this SQL boundary never
-- derives scope from payload fields.
CREATE FUNCTION memplex_sync_apply_inbound(batch_jcs BYTEA, request_sha256 TEXT)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    batch JSONB;
    event JSONB;
    scope JSONB;
    event_count INTEGER;
    inbound_origin TEXT;
    tenant TEXT;
    event_tenant TEXT;
    event_uuid TEXT;
    node_kind TEXT;
    entity TEXT;
    operation TEXT;
    version_key TEXT;
    payload JSONB;
    stream BIGINT;
    prior_version TEXT;
    incoming_version JSONB;
    stored_version JSONB;
    fanout BIGINT;
    accepted INTEGER := 0;
    duplicate INTEGER := 0;
    conflict INTEGER := 0;
    receipts JSONB := '[]'::jsonb;
    node_id TEXT;
    edge_parts JSONB;
    edge_weight REAL;
    edge_evidence JSONB;
    edge_created_at TIMESTAMPTZ;
    relation_name TEXT;
BEGIN
    IF batch_jcs IS NULL OR octet_length(batch_jcs) = 0 OR octet_length(batch_jcs) > 4194304
       OR request_sha256 IS NULL OR request_sha256 !~ '^[0-9a-f]{64}$'
       OR encode(pg_catalog.sha256(batch_jcs), 'hex') IS DISTINCT FROM request_sha256 THEN
        RAISE EXCEPTION 'memplex sync inbound batch is invalid' USING ERRCODE = '22023';
    END IF;
    BEGIN
        batch := convert_from(batch_jcs, 'UTF8')::jsonb;
    EXCEPTION WHEN others THEN
        RAISE EXCEPTION 'memplex sync inbound batch is invalid' USING ERRCODE = '22023';
    END;
    IF jsonb_typeof(batch) <> 'object'
       OR (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(batch) key)
          IS DISTINCT FROM ARRAY['batch_id','events','origin_node_id','protocol_version']
       OR batch->>'protocol_version' <> '1'
       OR batch->>'batch_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
       OR NULLIF(batch->>'origin_node_id', '') IS NULL
       OR jsonb_typeof(batch->'events') <> 'array' THEN
        RAISE EXCEPTION 'memplex sync inbound batch is invalid' USING ERRCODE = '22023';
    END IF;
    event_count := jsonb_array_length(batch->'events');
    IF event_count < 1 OR event_count > 1000 THEN
        RAISE EXCEPTION 'memplex sync inbound batch is invalid' USING ERRCODE = '22023';
    END IF;
    inbound_origin := batch->>'origin_node_id';
    IF NOT EXISTS (
        SELECT 1 FROM memplex_sync_ingress_principals
        WHERE role_name = session_user AND remote_node_id = inbound_origin AND enabled
    ) THEN
        RAISE EXCEPTION 'memplex sync ingress origin is not authorised' USING ERRCODE = '42501';
    END IF;
    SELECT value->'scope'->>'tenant_id' INTO tenant
    FROM jsonb_array_elements(batch->'events') value LIMIT 1;
    IF NULLIF(tenant, '') IS NULL THEN
        RAISE EXCEPTION 'memplex sync inbound batch is invalid' USING ERRCODE = '22023';
    END IF;
    -- Validate every protocol object before quota admission or any identity
    -- allocation.  PostgreSQL sequences are intentionally non-transactional,
    -- so discovering a malformed later event after accepting an earlier one
    -- would violate the no-leak atomic ingress contract.
    FOR event IN SELECT value FROM jsonb_array_elements(batch->'events') value LOOP
        IF jsonb_typeof(event) <> 'object'
           OR (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(event) key)
              IS DISTINCT FROM ARRAY['entity_key','event_id','node_type','operation','origin_node_id','payload','protocol_version','scope','version']
           OR event->>'protocol_version' <> '1'
           OR event->>'origin_node_id' IS DISTINCT FROM inbound_origin
           OR event->>'event_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
           OR event->>'node_type' NOT IN ('function','fact','preference','observation','edge')
           OR event->>'operation' NOT IN ('upsert','tombstone')
           OR NULLIF(event->>'version','') IS NULL
           OR jsonb_typeof(event->'scope') <> 'object' THEN
            RAISE EXCEPTION 'memplex sync inbound event is invalid' USING ERRCODE = '22023';
        END IF;
        scope := event->'scope'; event_tenant := scope->>'tenant_id';
        IF event_tenant IS DISTINCT FROM tenant
           OR (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(scope) key)
              IS DISTINCT FROM ARRAY['agent_id','owner_subject_id','session_id','tenant_id','visibility','workspace_id']
           OR NULLIF(scope->>'owner_subject_id','') IS NULL
           OR scope->>'visibility' NOT IN ('user','workspace','session')
           OR (scope->>'visibility' IN ('workspace','session') AND NULLIF(scope->>'workspace_id','') IS NULL)
           OR (scope->>'visibility' = 'session' AND (NULLIF(scope->>'agent_id','') IS NULL OR NULLIF(scope->>'session_id','') IS NULL))
           OR (event->>'node_type' = 'edge' AND event->>'entity_key' !~ '^edge:v1:[A-Za-z0-9_-]+$')
           OR (event->>'node_type' <> 'edge' AND event->>'entity_key' !~ '^node:v1:[A-Za-z0-9_-]+$')
           OR (event->>'operation' = 'upsert' AND jsonb_typeof(event->'payload') <> 'object')
           OR (event->>'operation' = 'tombstone' AND event->'payload' <> 'null'::jsonb) THEN
            RAISE EXCEPTION 'memplex sync inbound event is invalid' USING ERRCODE = '22023';
        END IF;
        -- Edge data is a fixed, independently projected storage payload.  This
        -- is deliberately checked in the all-event preflight, before quota or
        -- identity allocation, so a malformed later edge cannot leak a stream
        -- sequence from an earlier event in the same batch.
        IF event->>'node_type' = 'edge' AND event->>'operation' = 'upsert' THEN
            payload := event->'payload';
            IF (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(payload) key)
                   IS DISTINCT FROM ARRAY['created_at','evidence','weight']
               OR jsonb_typeof(payload->'weight') <> 'number'
               OR jsonb_typeof(payload->'evidence') <> 'array'
               OR EXISTS (
                   SELECT 1 FROM jsonb_array_elements(payload->'evidence') AS evidence_item
                   WHERE jsonb_typeof(evidence_item) <> 'string'
               )
               OR jsonb_typeof(payload->'created_at') <> 'string'
               OR payload->>'created_at' !~ '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$' THEN
                RAISE EXCEPTION 'memplex sync inbound edge payload is invalid' USING ERRCODE = '22023';
            END IF;
            BEGIN
                edge_weight := (payload->>'weight')::DOUBLE PRECISION::REAL;
                edge_created_at := (payload->>'created_at')::TIMESTAMP AT TIME ZONE 'UTC';
            EXCEPTION WHEN others THEN
                RAISE EXCEPTION 'memplex sync inbound edge payload is invalid' USING ERRCODE = '22023';
            END;
            IF pg_catalog.to_char(edge_created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
                    IS DISTINCT FROM payload->>'created_at' THEN
                RAISE EXCEPTION 'memplex sync inbound edge payload is invalid' USING ERRCODE = '22023';
            END IF;
        END IF;
        PERFORM memplex_sync_require_canonical_entity_key(
            event->>'entity_key', CASE WHEN event->>'node_type' = 'edge' THEN 'edge' ELSE 'node' END
        );
        PERFORM memplex_sync_require_canonical_version(event->>'version', inbound_origin, event->>'event_id');
        BEGIN
            incoming_version := convert_from(
                decode(replace(replace(split_part(event->>'version', ':', 2), '-', '+'), '_', '/')
                       || repeat('=', (4 - length(split_part(event->>'version', ':', 2)) % 4) % 4), 'base64'),
                'UTF8'
            )::jsonb;
        EXCEPTION WHEN others THEN
            RAISE EXCEPTION 'memplex sync inbound event is invalid' USING ERRCODE = '22023';
        END;
        IF event->>'version' !~ '^v1:[A-Za-z0-9_-]+$'
           OR jsonb_typeof(incoming_version) <> 'array'
           OR jsonb_array_length(incoming_version) <> 3
           OR jsonb_typeof(incoming_version->0) <> 'string'
           OR jsonb_typeof(incoming_version->1) <> 'string'
           OR jsonb_typeof(incoming_version->2) <> 'string'
           OR incoming_version->>0 !~ '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$'
           OR incoming_version->>1 IS DISTINCT FROM inbound_origin
           OR incoming_version->>2 IS DISTINCT FROM event->>'event_id' THEN
            RAISE EXCEPTION 'memplex sync inbound event is invalid' USING ERRCODE = '22023';
        END IF;
    END LOOP;
    IF EXISTS (
        SELECT 1 FROM memplex_sync_batches AS batch_row
        WHERE batch_row.tenant_id=tenant AND batch_row.origin_node_id=inbound_origin AND batch_row.batch_id=batch->>'batch_id'
          AND batch_row.request_sha256 IS DISTINCT FROM memplex_sync_apply_inbound.request_sha256
    ) THEN
        RAISE EXCEPTION 'memplex sync batch digest conflict' USING ERRCODE = '23505';
    END IF;
    IF EXISTS (
        SELECT 1 FROM memplex_sync_batches AS batch_row
        WHERE batch_row.tenant_id=tenant AND batch_row.origin_node_id=inbound_origin AND batch_row.batch_id=batch->>'batch_id'
          AND batch_row.request_sha256 = memplex_sync_apply_inbound.request_sha256
    ) THEN
        RETURN (SELECT batch_row.response FROM memplex_sync_batches AS batch_row
                WHERE batch_row.tenant_id=tenant AND batch_row.origin_node_id=inbound_origin AND batch_row.batch_id=batch->>'batch_id');
    END IF;
    -- Serialize quota admission before business/outbox identities are used.
    SELECT count(*) INTO fanout FROM memplex_sync_targets AS quota_target
    WHERE quota_target.tenant_id = tenant AND quota_target.enabled;
    PERFORM memplex_sync_assert_delivery_quota(tenant, fanout * event_count);
    PERFORM set_config('memplex.sync_capture', 'required', true);
    PERFORM set_config('memplex.sync_apply_mode', 'inbound', true);
    FOR event IN SELECT value FROM jsonb_array_elements(batch->'events') value LOOP
        IF jsonb_typeof(event) <> 'object'
           OR (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(event) key)
              IS DISTINCT FROM ARRAY['entity_key','event_id','node_type','operation','origin_node_id','payload','protocol_version','scope','version']
           OR event->>'protocol_version' <> '1'
           OR event->>'origin_node_id' IS DISTINCT FROM inbound_origin
           OR event->>'event_id' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
           OR event->>'node_type' NOT IN ('function','fact','preference','observation','edge')
           OR event->>'operation' NOT IN ('upsert','tombstone')
           OR NULLIF(event->>'version','') IS NULL
           OR jsonb_typeof(event->'scope') <> 'object' THEN
            RAISE EXCEPTION 'memplex sync inbound event is invalid' USING ERRCODE = '22023';
        END IF;
        scope := event->'scope'; event_tenant := scope->>'tenant_id';
        IF event_tenant IS DISTINCT FROM tenant
           OR NULLIF(scope->>'owner_subject_id','') IS NULL
           OR (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(scope) key)
              IS DISTINCT FROM ARRAY['agent_id','owner_subject_id','session_id','tenant_id','visibility','workspace_id']
           OR scope->>'visibility' NOT IN ('user','workspace','session')
           OR (scope->>'visibility' IN ('workspace','session') AND NULLIF(scope->>'workspace_id','') IS NULL)
           OR (scope->>'visibility' = 'session' AND (NULLIF(scope->>'agent_id','') IS NULL OR NULLIF(scope->>'session_id','') IS NULL))
           OR (event->>'node_type' = 'edge' AND event->>'entity_key' !~ '^edge:v1:[A-Za-z0-9_-]+$')
           OR (event->>'node_type' <> 'edge' AND event->>'entity_key' !~ '^node:v1:[A-Za-z0-9_-]+$')
           OR (event->>'operation' = 'upsert' AND jsonb_typeof(event->'payload') <> 'object')
           OR (event->>'operation' = 'tombstone' AND event->'payload' <> 'null'::jsonb) THEN
           RAISE EXCEPTION 'memplex sync inbound event is invalid' USING ERRCODE = '22023';
        END IF;
        event_uuid := event->>'event_id'; node_kind := event->>'node_type'; entity := event->>'entity_key';
        operation := event->>'operation'; version_key := event->>'version'; payload := event->'payload';
        BEGIN
            incoming_version := convert_from(
                decode(replace(replace(split_part(version_key, ':', 2), '-', '+'), '_', '/')
                       || repeat('=', (4 - length(split_part(version_key, ':', 2)) % 4) % 4), 'base64'),
                'UTF8'
            )::jsonb;
        EXCEPTION WHEN others THEN
            RAISE EXCEPTION 'memplex sync inbound event is invalid' USING ERRCODE = '22023';
        END;
        IF version_key !~ '^v1:[A-Za-z0-9_-]+$'
           OR jsonb_typeof(incoming_version) <> 'array'
           OR jsonb_array_length(incoming_version) <> 3
           OR jsonb_typeof(incoming_version->0) <> 'string'
           OR jsonb_typeof(incoming_version->1) <> 'string'
           OR jsonb_typeof(incoming_version->2) <> 'string'
           OR incoming_version->>0 !~ '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$'
           OR incoming_version->>1 IS DISTINCT FROM inbound_origin
           OR incoming_version->>2 IS DISTINCT FROM event_uuid THEN
            RAISE EXCEPTION 'memplex sync inbound event is invalid' USING ERRCODE = '22023';
        END IF;
        PERFORM set_config('memplex.tenant_id', tenant, true);
        PERFORM set_config('memplex.subject_id', scope->>'owner_subject_id', true);
        PERFORM set_config('memplex.workspace_id', COALESCE(scope->>'workspace_id', ''), true);
        PERFORM set_config('memplex.agent_id', COALESCE(scope->>'agent_id', ''), true);
        PERFORM set_config('memplex.session_id', COALESCE(scope->>'session_id', ''), true);
        SELECT version_row.version_key INTO prior_version FROM memplex_sync_entity_versions AS version_row
        WHERE version_row.tenant_id=tenant AND version_row.node_type=node_kind AND version_row.entity_key=entity FOR UPDATE;
        IF EXISTS (SELECT 1 FROM memplex_sync_inbox AS inbox_row WHERE inbox_row.tenant_id=tenant AND inbox_row.origin_node_id=inbound_origin AND inbox_row.event_id=event_uuid) THEN
            duplicate := duplicate + 1;
            receipts := receipts || jsonb_build_array(jsonb_build_object('event_id',event_uuid,'outcome','duplicate'));
            CONTINUE;
        END IF;
        IF prior_version IS NOT NULL THEN
            BEGIN
                stored_version := convert_from(
                    decode(replace(replace(split_part(prior_version, ':', 2), '-', '+'), '_', '/')
                           || repeat('=', (4 - length(split_part(prior_version, ':', 2)) % 4) % 4), 'base64'),
                    'UTF8'
                )::jsonb;
            EXCEPTION WHEN others THEN
                RAISE EXCEPTION 'memplex stored sync version is invalid' USING ERRCODE = 'XX000';
            END;
        END IF;
        IF prior_version IS NOT NULL AND (
            (stored_version->>0)::timestamptz,
            stored_version->>1,
            stored_version->>2
        ) >= (
            (incoming_version->>0)::timestamptz,
            incoming_version->>1,
            incoming_version->>2
        ) THEN
            INSERT INTO memplex_sync_inbox (tenant_id,origin_node_id,event_id,outcome) VALUES (tenant,inbound_origin,event_uuid,'rejected_conflict');
            conflict := conflict + 1;
            receipts := receipts || jsonb_build_array(jsonb_build_object('event_id',event_uuid,'outcome','rejected_conflict'));
            CONTINUE;
        END IF;
        IF node_kind = 'edge' THEN
            BEGIN
                edge_parts := convert_from(decode(replace(replace(split_part(entity, ':', 3), '-', '+'), '_', '/') || repeat('=', (4 - length(split_part(entity, ':', 3)) % 4) % 4), 'base64'), 'UTF8')::jsonb;
            EXCEPTION WHEN others THEN
                RAISE EXCEPTION 'memplex sync inbound event is invalid' USING ERRCODE = '22023';
            END;
            IF jsonb_typeof(edge_parts) <> 'array' OR jsonb_array_length(edge_parts) <> 3
               OR jsonb_typeof(edge_parts->0) <> 'string' OR jsonb_typeof(edge_parts->1) <> 'string' OR jsonb_typeof(edge_parts->2) <> 'string' THEN
                RAISE EXCEPTION 'memplex sync inbound event is invalid' USING ERRCODE = '22023';
            END IF;
            IF operation = 'tombstone' THEN
                DELETE FROM memplex_edges WHERE tenant_id=tenant AND source=edge_parts->>0 AND target=edge_parts->>1 AND edge_type=edge_parts->>2;
            ELSE
                edge_weight := (payload->>'weight')::DOUBLE PRECISION::REAL;
                edge_evidence := payload->'evidence';
                edge_created_at := (payload->>'created_at')::TIMESTAMP AT TIME ZONE 'UTC';
                INSERT INTO memplex_edges (tenant_id,source,target,edge_type,weight,evidence,created_at,owner_subject,workspace,visibility,source_agent,source_session)
                VALUES (tenant,edge_parts->>0,edge_parts->>1,edge_parts->>2,edge_weight,edge_evidence,edge_created_at,scope->>'owner_subject_id',COALESCE(scope->>'workspace_id',''),scope->>'visibility',COALESCE(scope->>'agent_id',''),COALESCE(scope->>'session_id',''))
                ON CONFLICT (tenant_id,source,target,edge_type) DO UPDATE SET weight=EXCLUDED.weight,evidence=EXCLUDED.evidence,created_at=EXCLUDED.created_at,owner_subject=EXCLUDED.owner_subject,workspace=EXCLUDED.workspace,visibility=EXCLUDED.visibility,source_agent=EXCLUDED.source_agent,source_session=EXCLUDED.source_session;
            END IF;
        ELSE
            node_id := convert_from(decode(replace(replace(split_part(entity, ':', 3), '-', '+'), '_', '/') || repeat('=', (4 - length(split_part(entity, ':', 3)) % 4) % 4), 'base64'), 'UTF8');
            relation_name := 'memplex_' || CASE node_kind WHEN 'function' THEN 'functions' WHEN 'fact' THEN 'facts' WHEN 'preference' THEN 'preferences' WHEN 'observation' THEN 'observations' END;
            IF operation = 'tombstone' THEN
                IF node_kind = 'function' AND EXISTS (
                    SELECT 1 FROM memplex_edges AS dependent_edge
                    WHERE dependent_edge.tenant_id=tenant
                      AND (dependent_edge.source=node_id OR dependent_edge.target=node_id)
                ) THEN
                    RAISE EXCEPTION 'memplex function tombstone requires explicit edge tombstones' USING ERRCODE = '23503';
                END IF;
                EXECUTE format('DELETE FROM %I WHERE tenant_id=$1 AND id=$2', relation_name) USING tenant, node_id;
            ELSE
                EXECUTE format('INSERT INTO %I (tenant_id,id,data,owner_subject,workspace,visibility,source_agent,source_session) VALUES ($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT (tenant_id,id) DO UPDATE SET data=EXCLUDED.data,owner_subject=EXCLUDED.owner_subject,workspace=EXCLUDED.workspace,visibility=EXCLUDED.visibility,source_agent=EXCLUDED.source_agent,source_session=EXCLUDED.source_session', relation_name)
                USING tenant,node_id,payload,scope->>'owner_subject_id',COALESCE(scope->>'workspace_id',''),scope->>'visibility',COALESCE(scope->>'agent_id',''),COALESCE(scope->>'session_id','');
            END IF;
        END IF;
        INSERT INTO memplex_sync_outbox (tenant_id,event_id,origin_node_id,node_type,entity_key,operation,version_key,payload,visibility,owner_subject_id,workspace_id,agent_id,session_id)
        VALUES (tenant,event_uuid,inbound_origin,node_kind,entity,operation,version_key,CASE WHEN operation='upsert' THEN payload ELSE NULL END,scope->>'visibility',scope->>'owner_subject_id',scope->>'workspace_id',scope->>'agent_id',scope->>'session_id') RETURNING stream_seq INTO stream;
        INSERT INTO memplex_sync_entity_versions (tenant_id,node_type,entity_key,version_key,deleted,event_id,last_stream_seq)
        VALUES (tenant,node_kind,entity,version_key,operation='tombstone',event_uuid,stream)
        ON CONFLICT (tenant_id,node_type,entity_key) DO UPDATE SET version_key=EXCLUDED.version_key,deleted=EXCLUDED.deleted,event_id=EXCLUDED.event_id,last_stream_seq=EXCLUDED.last_stream_seq;
        INSERT INTO memplex_sync_inbox (tenant_id,origin_node_id,event_id,outcome,applied_stream_seq) VALUES (tenant,inbound_origin,event_uuid,'accepted',stream);
        INSERT INTO memplex_sync_deliveries (tenant_id,target_id,stream_seq,state)
        SELECT tenant,target.target_id,stream,'pending' FROM memplex_sync_targets AS target
        WHERE target.tenant_id=tenant AND target.enabled AND target.remote_node_id IS DISTINCT FROM inbound_origin;
        accepted := accepted + 1;
        receipts := receipts || jsonb_build_array(jsonb_build_object('event_id',event_uuid,'outcome','accepted','stream_seq',stream));
    END LOOP;
    INSERT INTO memplex_sync_batches (tenant_id,origin_node_id,batch_id,request_sha256,response)
    VALUES (tenant,inbound_origin,batch->>'batch_id',request_sha256,jsonb_build_object('accepted',accepted,'duplicate',duplicate,'conflict',conflict,'receipts',receipts))
    ON CONFLICT (tenant_id,origin_node_id,batch_id) DO NOTHING;
    RETURN (SELECT batch_row.response FROM memplex_sync_batches AS batch_row WHERE batch_row.tenant_id=tenant AND batch_row.origin_node_id=inbound_origin AND batch_row.batch_id=batch->>'batch_id');
END;
$$;

CREATE FUNCTION memplex_sync_capture_local_change()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    row_data JSONB;
    row_tenant TEXT;
    row_owner TEXT;
    row_workspace TEXT;
    row_visibility TEXT;
    row_agent TEXT;
    row_session TEXT;
    context_payload JSONB;
    inserted_seq BIGINT;
BEGIN
    IF current_setting('memplex.sync_capture', true) IS DISTINCT FROM 'required'
       OR current_setting('memplex.sync_apply_mode', true) IS DISTINCT FROM 'local' THEN
        RETURN COALESCE(NEW, OLD);
    END IF;
    row_data := to_jsonb(COALESCE(NEW, OLD));
    row_tenant := row_data->>'tenant_id';
    row_owner := row_data->>'owner_subject';
    row_workspace := row_data->>'workspace';
    row_visibility := row_data->>'visibility';
    row_agent := row_data->>'source_agent';
    row_session := row_data->>'source_session';
    IF row_tenant IS NULL OR row_owner IS NULL OR row_workspace IS NULL OR row_visibility IS NULL THEN
        RAISE EXCEPTION 'memplex sync source row lacks durable scope' USING ERRCODE = '23514';
    END IF;
    context_payload := CASE WHEN TG_OP = 'DELETE' THEN NULL ELSE current_setting('memplex.sync_payload', true)::jsonb END;
    INSERT INTO memplex_sync_outbox (
        tenant_id, event_id, origin_node_id, node_type, entity_key, operation, version_key,
        payload, visibility, owner_subject_id, workspace_id, agent_id, session_id
    ) VALUES (
        row_tenant, current_setting('memplex.sync_event_id', true), current_setting('memplex.sync_origin_node_id', true), TG_ARGV[0], current_setting('memplex.sync_entity_key', true),
        CASE WHEN TG_OP = 'DELETE' THEN 'tombstone' ELSE 'upsert' END,
        current_setting('memplex.sync_version_key', true), context_payload,
        row_visibility, row_owner, row_workspace, row_agent, row_session
    ) RETURNING stream_seq INTO inserted_seq;
    INSERT INTO memplex_sync_entity_versions
        (tenant_id, node_type, entity_key, version_key, deleted, event_id, last_stream_seq)
    VALUES (row_tenant, TG_ARGV[0], current_setting('memplex.sync_entity_key', true),
            current_setting('memplex.sync_version_key', true),
            TG_OP = 'DELETE', current_setting('memplex.sync_event_id', true), inserted_seq)
    ON CONFLICT (tenant_id, node_type, entity_key) DO UPDATE
      SET version_key = EXCLUDED.version_key, deleted = EXCLUDED.deleted,
          event_id = EXCLUDED.event_id, last_stream_seq = EXCLUDED.last_stream_seq;
    INSERT INTO memplex_sync_deliveries (tenant_id, target_id, stream_seq, state)
    SELECT row_tenant, target.target_id, inserted_seq, 'pending'
    FROM memplex_sync_targets target
    WHERE target.tenant_id = row_tenant AND target.enabled;
    RETURN COALESCE(NEW, OLD);
END;
$$;

REVOKE ALL ON FUNCTION memplex_sync_capture_before() FROM PUBLIC;
REVOKE ALL ON FUNCTION memplex_sync_capture_local_change() FROM PUBLIC;
REVOKE ALL ON FUNCTION memplex_configure_sync_local_identity(TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION memplex_sync_assert_delivery_quota(TEXT, BIGINT) FROM PUBLIC;
REVOKE ALL ON FUNCTION memplex_sync_snapshot_admission_counts() FROM PUBLIC;
REVOKE ALL ON FUNCTION memplex_sync_compact(TIMESTAMPTZ, TIMESTAMPTZ, INTEGER) FROM PUBLIC;
REVOKE ALL ON FUNCTION memplex_configure_sync_ingress_principal(NAME, TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION memplex_sync_apply_inbound(BYTEA, TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION memplex_sync_require_canonical_entity_key(TEXT, TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION memplex_sync_require_canonical_version(TEXT, TEXT, TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION memplex_sync_encode_string_array(JSONB) FROM PUBLIC;

DO $$
DECLARE current_schema_name TEXT := current_schema();
BEGIN
    EXECUTE format(
        'ALTER FUNCTION %I.memplex_sync_capture_before() SET search_path TO pg_catalog, %I',
        current_schema_name, current_schema_name
    );
    EXECUTE format(
        'ALTER FUNCTION %I.memplex_sync_capture_local_change() SET search_path TO pg_catalog, %I',
        current_schema_name, current_schema_name
    );
    EXECUTE format(
        'ALTER FUNCTION %I.memplex_configure_sync_local_identity(TEXT) SET search_path TO pg_catalog, %I',
        current_schema_name, current_schema_name
    );
    EXECUTE format(
        'ALTER FUNCTION %I.memplex_sync_assert_delivery_quota(TEXT, BIGINT) SET search_path TO pg_catalog, %I',
        current_schema_name, current_schema_name
    );
    EXECUTE format(
        'ALTER FUNCTION %I.memplex_sync_snapshot_admission_counts() SET search_path TO pg_catalog, %I',
        current_schema_name, current_schema_name
    );
    EXECUTE format(
        'ALTER FUNCTION %I.memplex_sync_compact(TIMESTAMPTZ, TIMESTAMPTZ, INTEGER) SET search_path TO pg_catalog, %I',
        current_schema_name, current_schema_name
    );
    EXECUTE format(
        'ALTER FUNCTION %I.memplex_configure_sync_ingress_principal(NAME, TEXT) SET search_path TO pg_catalog, %I',
        current_schema_name, current_schema_name
    );
    EXECUTE format(
        'ALTER FUNCTION %I.memplex_sync_apply_inbound(BYTEA, TEXT) SET search_path TO pg_catalog, %I',
        current_schema_name, current_schema_name
    );
    EXECUTE format('ALTER FUNCTION %I.memplex_sync_require_canonical_entity_key(TEXT, TEXT) SET search_path TO pg_catalog, %I', current_schema_name, current_schema_name);
    EXECUTE format('ALTER FUNCTION %I.memplex_sync_require_canonical_version(TEXT, TEXT, TEXT) SET search_path TO pg_catalog, %I', current_schema_name, current_schema_name);
    EXECUTE format('ALTER FUNCTION %I.memplex_sync_encode_string_array(JSONB) SET search_path TO pg_catalog, %I', current_schema_name, current_schema_name);
END;
$$;

-- Protocol JCS proof belongs to the trusted Python ingress gateway, which
-- uses the frozen Task 1 implementation before opening this DB credential.
-- PostgreSQL retains only independently useful codec/shape defenses.
DROP FUNCTION memplex_sync_jcs_encode(JSONB);
DROP FUNCTION memplex_sync_jcs_key_sort_key(TEXT);
DROP FUNCTION memplex_sync_jcs_number(NUMERIC);

CREATE TRIGGER memplex_sync_functions_before
BEFORE INSERT OR UPDATE OR DELETE ON memplex_functions
FOR EACH ROW EXECUTE FUNCTION memplex_sync_capture_before('function');
CREATE TRIGGER memplex_sync_functions_after
AFTER INSERT OR UPDATE OR DELETE ON memplex_functions
FOR EACH ROW EXECUTE FUNCTION memplex_sync_capture_local_change('function');
CREATE TRIGGER memplex_sync_edges_before
BEFORE INSERT OR UPDATE OR DELETE ON memplex_edges
FOR EACH ROW EXECUTE FUNCTION memplex_sync_capture_before('edge');
CREATE TRIGGER memplex_sync_edges_after
AFTER INSERT OR UPDATE OR DELETE ON memplex_edges
FOR EACH ROW EXECUTE FUNCTION memplex_sync_capture_local_change('edge');
CREATE TRIGGER memplex_sync_observations_before
BEFORE INSERT OR UPDATE OR DELETE ON memplex_observations
FOR EACH ROW EXECUTE FUNCTION memplex_sync_capture_before('observation');
CREATE TRIGGER memplex_sync_observations_after
AFTER INSERT OR UPDATE OR DELETE ON memplex_observations
FOR EACH ROW EXECUTE FUNCTION memplex_sync_capture_local_change('observation');
CREATE TRIGGER memplex_sync_facts_before
BEFORE INSERT OR UPDATE OR DELETE ON memplex_facts
FOR EACH ROW EXECUTE FUNCTION memplex_sync_capture_before('fact');
CREATE TRIGGER memplex_sync_facts_after
AFTER INSERT OR UPDATE OR DELETE ON memplex_facts
FOR EACH ROW EXECUTE FUNCTION memplex_sync_capture_local_change('fact');
CREATE TRIGGER memplex_sync_preferences_before
BEFORE INSERT OR UPDATE OR DELETE ON memplex_preferences
FOR EACH ROW EXECUTE FUNCTION memplex_sync_capture_before('preference');
CREATE TRIGGER memplex_sync_preferences_after
AFTER INSERT OR UPDATE OR DELETE ON memplex_preferences
FOR EACH ROW EXECUTE FUNCTION memplex_sync_capture_local_change('preference');
