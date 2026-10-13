-- Guard actual source effects before inbound transport sequence allocation.

CREATE OR REPLACE FUNCTION memplex_sync_capture_before()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    local_node_id TEXT;
    source_tenant_id TEXT;
    enabled_target_fanout BIGINT;
    descriptor JSONB;
    actual_row JSONB;
    actual_rows JSONB;
    key_parts JSONB;
    actual_node_id TEXT;
    setting_name TEXT;
    event_settings JSONB := '{}'::jsonb;
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
        descriptor := NULLIF(current_setting('memplex.sync_inbound_event', true), '')::jsonb;
        IF descriptor IS NULL OR descriptor->>'node_type' IS DISTINCT FROM TG_ARGV[0]
           OR descriptor->>'operation' IS DISTINCT FROM CASE WHEN TG_OP = 'DELETE' THEN 'tombstone' ELSE 'upsert' END
           OR TG_ARGV[0] NOT IN ('function','fact','preference','observation','edge')
           OR (TG_ARGV[0] = 'edge' AND TG_OP = 'DELETE' AND pg_trigger_depth() <> 1) THEN
            RAISE EXCEPTION 'memplex sync inbound operation is not signed' USING ERRCODE = '42501';
        END IF;
        PERFORM memplex_sync_require_canonical_entity_key(
            descriptor->>'entity_key', CASE WHEN TG_ARGV[0] = 'edge' THEN 'edge' ELSE 'node' END
        );
        IF TG_ARGV[0] = 'edge' THEN
            key_parts := convert_from(decode(replace(replace(split_part(descriptor->>'entity_key', ':', 3), '-', '+'), '_', '/')
                || repeat('=', (4 - length(split_part(descriptor->>'entity_key', ':', 3)) % 4) % 4), 'base64'), 'UTF8')::jsonb;
        ELSE
            actual_node_id := convert_from(decode(replace(replace(split_part(descriptor->>'entity_key', ':', 3), '-', '+'), '_', '/')
                || repeat('=', (4 - length(split_part(descriptor->>'entity_key', ':', 3)) % 4) % 4), 'base64'), 'UTF8');
        END IF;
        actual_rows := CASE TG_OP WHEN 'INSERT' THEN jsonb_build_array(to_jsonb(NEW))
            WHEN 'UPDATE' THEN jsonb_build_array(to_jsonb(OLD), to_jsonb(NEW))
            ELSE jsonb_build_array(to_jsonb(OLD)) END;
        FOREACH setting_name IN ARRAY ARRAY['memplex.tenant_id','memplex.subject_id','memplex.workspace_id','memplex.agent_id','memplex.session_id'] LOOP
            event_settings := event_settings || jsonb_build_object(setting_name, COALESCE(current_setting(setting_name, true), ''));
        END LOOP;
        FOR actual_row IN SELECT value FROM jsonb_array_elements(actual_rows) LOOP
            IF actual_row->>'tenant_id' IS DISTINCT FROM descriptor->>'tenant_id'
               OR (TG_ARGV[0] = 'edge' AND jsonb_build_array(actual_row->>'source',actual_row->>'target',actual_row->>'edge_type') IS DISTINCT FROM key_parts)
               OR (TG_ARGV[0] <> 'edge' AND actual_row->>'id' IS DISTINCT FROM actual_node_id) THEN
                RAISE EXCEPTION 'memplex sync inbound operation is not signed' USING ERRCODE = '42501';
            END IF;
            IF TG_ARGV[0] <> 'edge' THEN
                PERFORM set_config('memplex.tenant_id', actual_row->>'tenant_id', true);
                PERFORM set_config('memplex.workspace_id', actual_row->>'workspace', true);
                IF current_setting('memplex.tenant_id', true) IS DISTINCT FROM actual_row->>'tenant_id'
                   OR current_setting('memplex.workspace_id', true) IS DISTINCT FROM actual_row->>'workspace' THEN
                    RAISE EXCEPTION 'memplex sync source scope is not bound' USING ERRCODE = '42501';
                END IF;
                IF EXISTS (SELECT 1 FROM memplex_source_authorities AS authority
                           WHERE authority.tenant_id = actual_row->>'tenant_id'
                             AND authority.workspace = actual_row->>'workspace') THEN
                    RAISE EXCEPTION 'memplex source ingress is unsupported' USING ERRCODE = '42501';
                END IF;
                FOREACH setting_name IN ARRAY ARRAY['memplex.tenant_id','memplex.subject_id','memplex.workspace_id','memplex.agent_id','memplex.session_id'] LOOP
                    PERFORM set_config(setting_name, event_settings->>setting_name, true);
                END LOOP;
            END IF;
        END LOOP;
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

CREATE OR REPLACE FUNCTION memplex_sync_apply_inbound(batch_jcs BYTEA, request_sha256 TEXT)
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
    setting_name TEXT;
    saved_settings JSONB := '{}'::jsonb;
    response JSONB;
    event_ordinal INTEGER := -1;
    planned JSONB;
    plan JSONB := '[]'::jsonb;
    version_cache JSONB := '{}'::jsonb;
    seen_cache JSONB := '{}'::jsonb;
    version_cache_key TEXT;
    inbox_cache_key TEXT;
BEGIN
    IF current_setting('transaction_isolation') IS DISTINCT FROM 'read committed' THEN
        RAISE EXCEPTION 'memplex sync ingress requires read committed' USING ERRCODE = '25001';
    END IF;
    FOREACH setting_name IN ARRAY ARRAY['memplex.tenant_id','memplex.subject_id','memplex.workspace_id','memplex.agent_id','memplex.session_id','memplex.verified_remote_node_id','memplex.sync_capture','memplex.sync_apply_mode','memplex.sync_inbound_event'] LOOP
        saved_settings := saved_settings || jsonb_build_object(setting_name, COALESCE(current_setting(setting_name, true), ''));
    END LOOP;
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
    -- Nonwaiting gates precede every business effect and sequence allocation.
    -- Acquire the existing quota key first; never wait on a source advisory key.
    IF NOT pg_try_advisory_xact_lock(hashtextextended('memplex-sync-delivery-quota:' || tenant, 0)) THEN
        RAISE EXCEPTION 'memplex sync ingress is busy' USING ERRCODE = '55P03';
    END IF;
    LOCK TABLE memplex_source_authorities IN SHARE MODE NOWAIT;
    LOCK TABLE memplex_sync_ingress_principals IN SHARE MODE NOWAIT;
    FOREACH relation_name IN ARRAY ARRAY['memplex_edges','memplex_facts','memplex_functions','memplex_observations','memplex_preferences','memplex_sync_batches','memplex_sync_deliveries','memplex_sync_entity_versions','memplex_sync_inbox','memplex_sync_outbox','memplex_sync_stream_state','memplex_sync_targets'] LOOP
        EXECUTE format('LOCK TABLE %I IN EXCLUSIVE MODE NOWAIT', relation_name);
    END LOOP;
    IF NOT EXISTS (SELECT 1 FROM memplex_sync_ingress_principals
                   WHERE role_name = session_user AND remote_node_id = inbound_origin AND enabled) THEN
        RAISE EXCEPTION 'memplex sync ingress origin is not authorised' USING ERRCODE = '42501';
    END IF;
    PERFORM set_config('memplex.tenant_id', tenant, true);
    PERFORM set_config('memplex.verified_remote_node_id', inbound_origin, true);
    IF current_setting('memplex.tenant_id', true) IS DISTINCT FROM tenant
       OR current_setting('memplex.verified_remote_node_id', true) IS DISTINCT FROM inbound_origin THEN
        RAISE EXCEPTION 'memplex sync inbound scope is not bound' USING ERRCODE = '42501';
    END IF;
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
        SELECT batch_row.response INTO response FROM memplex_sync_batches AS batch_row
        WHERE batch_row.tenant_id=tenant AND batch_row.origin_node_id=inbound_origin AND batch_row.batch_id=batch->>'batch_id';
        FOREACH setting_name IN ARRAY ARRAY['memplex.tenant_id','memplex.subject_id','memplex.workspace_id','memplex.agent_id','memplex.session_id','memplex.verified_remote_node_id','memplex.sync_capture','memplex.sync_apply_mode','memplex.sync_inbound_event'] LOOP
            PERFORM set_config(setting_name, saved_settings->>setting_name, true);
        END LOOP;
        RETURN response;
    END IF;
    -- Serialize quota admission before business/outbox identities are used.
    SELECT count(*) INTO fanout FROM memplex_sync_targets AS quota_target
    WHERE quota_target.tenant_id = tenant AND quota_target.enabled;
    PERFORM memplex_sync_assert_delivery_quota(tenant, fanout * event_count);
    PERFORM set_config('memplex.sync_capture', 'required', true);
    PERFORM set_config('memplex.sync_apply_mode', 'inbound', true);
    -- Phase A: ordered business effects only. Local caches simulate the old
    -- loop's inbox/version progression without allocating transport identities.
    FOR event IN SELECT value FROM jsonb_array_elements(batch->'events') value LOOP
        event_ordinal := event_ordinal + 1;
        scope := event->'scope';
        event_uuid := event->>'event_id'; node_kind := event->>'node_type'; entity := event->>'entity_key';
        operation := event->>'operation'; version_key := event->>'version'; payload := event->'payload';
        PERFORM set_config('memplex.tenant_id', tenant, true);
        PERFORM set_config('memplex.subject_id', scope->>'owner_subject_id', true);
        PERFORM set_config('memplex.workspace_id', COALESCE(scope->>'workspace_id', ''), true);
        PERFORM set_config('memplex.agent_id', COALESCE(scope->>'agent_id', ''), true);
        PERFORM set_config('memplex.session_id', COALESCE(scope->>'session_id', ''), true);
        IF current_setting('memplex.tenant_id', true) IS DISTINCT FROM tenant
           OR current_setting('memplex.subject_id', true) IS DISTINCT FROM scope->>'owner_subject_id'
           OR current_setting('memplex.workspace_id', true) IS DISTINCT FROM COALESCE(scope->>'workspace_id', '')
           OR current_setting('memplex.agent_id', true) IS DISTINCT FROM COALESCE(scope->>'agent_id', '')
           OR current_setting('memplex.session_id', true) IS DISTINCT FROM COALESCE(scope->>'session_id', '') THEN
            RAISE EXCEPTION 'memplex sync inbound scope is not bound' USING ERRCODE = '42501';
        END IF;
        version_cache_key := jsonb_build_array(tenant,node_kind,entity)::text;
        inbox_cache_key := jsonb_build_array(tenant,inbound_origin,event_uuid)::text;
        IF version_cache ? version_cache_key THEN
            prior_version := version_cache->>version_cache_key;
        ELSE
            SELECT version_row.version_key INTO prior_version FROM memplex_sync_entity_versions AS version_row
            WHERE version_row.tenant_id=tenant AND version_row.node_type=node_kind AND version_row.entity_key=entity FOR UPDATE;
            version_cache := version_cache || jsonb_build_object(version_cache_key, prior_version);
        END IF;
        IF seen_cache ? inbox_cache_key OR EXISTS (
            SELECT 1 FROM memplex_sync_inbox AS inbox_row
            WHERE inbox_row.tenant_id=tenant AND inbox_row.origin_node_id=inbound_origin AND inbox_row.event_id=event_uuid
        ) THEN
            plan := plan || jsonb_build_array(jsonb_build_object('ordinal',event_ordinal,'outcome','duplicate'));
            CONTINUE;
        END IF;
        BEGIN
            incoming_version := convert_from(
                decode(replace(replace(split_part(version_key, ':', 2), '-', '+'), '_', '/')
                       || repeat('=', (4 - length(split_part(version_key, ':', 2)) % 4) % 4), 'base64'),
                'UTF8'
            )::jsonb;
        EXCEPTION WHEN others THEN
            RAISE EXCEPTION 'memplex sync inbound event is invalid' USING ERRCODE = '22023';
        END;
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
            plan := plan || jsonb_build_array(jsonb_build_object('ordinal',event_ordinal,'outcome','rejected_conflict'));
            seen_cache := seen_cache || jsonb_build_object(inbox_cache_key, true);
            CONTINUE;
        END IF;
        PERFORM set_config('memplex.sync_inbound_event', jsonb_build_object(
            'tenant_id',tenant,'node_type',node_kind,'operation',operation,'entity_key',entity,'event_id',event_uuid
        )::text, true);
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
        plan := plan || jsonb_build_array(jsonb_build_object('ordinal',event_ordinal,'outcome','accepted'));
        version_cache := version_cache || jsonb_build_object(version_cache_key, version_key);
        seen_cache := seen_cache || jsonb_build_object(inbox_cache_key, true);
    END LOOP;
    -- Phase B cannot run business/source admission after nextval. It records
    -- the already successful plan in exactly the original receipt order.
    FOR planned IN SELECT value FROM jsonb_array_elements(plan) LOOP
        event := batch->'events'->(planned->>'ordinal')::integer;
        scope := event->'scope';
        event_uuid := event->>'event_id'; node_kind := event->>'node_type'; entity := event->>'entity_key';
        operation := event->>'operation'; version_key := event->>'version'; payload := event->'payload';
        PERFORM set_config('memplex.tenant_id', tenant, true);
        PERFORM set_config('memplex.subject_id', scope->>'owner_subject_id', true);
        PERFORM set_config('memplex.workspace_id', COALESCE(scope->>'workspace_id', ''), true);
        PERFORM set_config('memplex.agent_id', COALESCE(scope->>'agent_id', ''), true);
        PERFORM set_config('memplex.session_id', COALESCE(scope->>'session_id', ''), true);
        IF current_setting('memplex.tenant_id', true) IS DISTINCT FROM tenant
           OR current_setting('memplex.subject_id', true) IS DISTINCT FROM scope->>'owner_subject_id'
           OR current_setting('memplex.workspace_id', true) IS DISTINCT FROM COALESCE(scope->>'workspace_id', '')
           OR current_setting('memplex.agent_id', true) IS DISTINCT FROM COALESCE(scope->>'agent_id', '')
           OR current_setting('memplex.session_id', true) IS DISTINCT FROM COALESCE(scope->>'session_id', '') THEN
            RAISE EXCEPTION 'memplex sync inbound scope is not bound' USING ERRCODE = '42501';
        END IF;
        IF planned->>'outcome' = 'duplicate' THEN
            duplicate := duplicate + 1;
            receipts := receipts || jsonb_build_array(jsonb_build_object('event_id',event_uuid,'outcome','duplicate'));
            CONTINUE;
        ELSIF planned->>'outcome' = 'rejected_conflict' THEN
            INSERT INTO memplex_sync_inbox (tenant_id,origin_node_id,event_id,outcome) VALUES (tenant,inbound_origin,event_uuid,'rejected_conflict');
            conflict := conflict + 1;
            receipts := receipts || jsonb_build_array(jsonb_build_object('event_id',event_uuid,'outcome','rejected_conflict'));
            CONTINUE;
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
    SELECT batch_row.response INTO response FROM memplex_sync_batches AS batch_row WHERE batch_row.tenant_id=tenant AND batch_row.origin_node_id=inbound_origin AND batch_row.batch_id=batch->>'batch_id';
    FOREACH setting_name IN ARRAY ARRAY['memplex.tenant_id','memplex.subject_id','memplex.workspace_id','memplex.agent_id','memplex.session_id','memplex.verified_remote_node_id','memplex.sync_capture','memplex.sync_apply_mode','memplex.sync_inbound_event'] LOOP
        PERFORM set_config(setting_name, saved_settings->>setting_name, true);
    END LOOP;
    RETURN response;
END;
$$;

-- CREATE OR REPLACE preserves ownership and ACL. Restore the existing pinned
-- per-schema function configuration explicitly; no grant or policy is added.
DO $$
DECLARE current_schema_name TEXT := current_schema();
BEGIN
    EXECUTE format('ALTER FUNCTION %I.memplex_sync_capture_before() SET search_path TO pg_catalog, %I', current_schema_name, current_schema_name);
    EXECUTE format('ALTER FUNCTION %I.memplex_sync_apply_inbound(BYTEA, TEXT) SET search_path TO pg_catalog, %I', current_schema_name, current_schema_name);
END;
$$;
