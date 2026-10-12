-- Source validity V1 ledger schema only. No authority bootstrap or old-row backfill.
-- The application has no DELETE privilege: tombstones and audit metadata persist.

CREATE TABLE memplex_source_authorities (
    authority_id UUID NOT NULL,
    tenant_id TEXT NOT NULL,
    workspace TEXT NOT NULL,
    state TEXT NOT NULL,
    policy_revision BIGINT NOT NULL,
    commit_seq BIGINT NOT NULL,
    policy JSONB NOT NULL,
    contract_version SMALLINT NOT NULL,
    PRIMARY KEY (authority_id),
    CONSTRAINT memplex_source_authorities_scope_key UNIQUE (authority_id, tenant_id, workspace),
    CONSTRAINT memplex_source_authorities_tenant_workspace_key UNIQUE (tenant_id, workspace),
    CONSTRAINT memplex_source_authorities_scope_check CHECK (((tenant_id <> ''::text) AND (tenant_id <> '__memplex_legacy__'::text) AND (workspace <> ''::text))),
    CONSTRAINT memplex_source_authorities_authority_check CHECK (((authority_id)::text ~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'::text)),
    CONSTRAINT memplex_source_authorities_state_check CHECK ((state = ANY (ARRAY['live'::text, 'deleted'::text, 'quarantined'::text]))),
    CONSTRAINT memplex_source_authorities_revision_check CHECK (((policy_revision > 0) AND (commit_seq > 0) AND (contract_version = 1))),
    CONSTRAINT memplex_source_authorities_policy_check CHECK ((((jsonb_typeof(policy) = 'object'::text) AND (policy ?& ARRAY['revision'::text, 'entries'::text, 'algorithm_versions'::text]) AND ((policy - ARRAY['revision'::text, 'entries'::text, 'algorithm_versions'::text]) = '{}'::jsonb)) AND ((jsonb_typeof((policy -> 'revision'::text)) = 'number'::text) AND ((policy ->> 'revision'::text) = (policy_revision)::text)) AND (jsonb_typeof((policy -> 'entries'::text)) = 'array'::text) AND (jsonb_typeof((policy -> 'algorithm_versions'::text)) = 'array'::text)))
);

ALTER TABLE memplex_source_authorities ENABLE ROW LEVEL SECURITY;
ALTER TABLE memplex_source_authorities FORCE ROW LEVEL SECURITY;
CREATE POLICY memplex_source_authorities_scope ON memplex_source_authorities
    USING (((tenant_id <> '__memplex_legacy__'::text) AND (tenant_id = current_setting('memplex.tenant_id'::text, true)) AND (workspace = current_setting('memplex.workspace_id'::text, true))))
    WITH CHECK (((tenant_id <> '__memplex_legacy__'::text) AND (tenant_id = current_setting('memplex.tenant_id'::text, true)) AND (workspace = current_setting('memplex.workspace_id'::text, true))));

CREATE TABLE memplex_source_heads (
    authority_id UUID NOT NULL,
    kind TEXT NOT NULL,
    logical_id TEXT NOT NULL,
    incarnation UUID NOT NULL,
    content_revision BIGINT NOT NULL,
    auth_revision BIGINT NOT NULL,
    state TEXT NOT NULL,
    source_class TEXT NOT NULL,
    payload_locator TEXT NOT NULL,
    digest_version SMALLINT NOT NULL,
    semantic_digest TEXT NOT NULL,
    last_commit_seq BIGINT NOT NULL,
    head_data JSONB NOT NULL,
    tenant_id TEXT NOT NULL,
    owner_subject TEXT NOT NULL,
    workspace TEXT NOT NULL,
    visibility TEXT NOT NULL,
    source_agent TEXT NOT NULL,
    source_session TEXT NOT NULL,
    PRIMARY KEY (authority_id, kind, logical_id),
    CONSTRAINT memplex_source_heads_logical_id_key UNIQUE (authority_id, logical_id),
    CONSTRAINT memplex_source_heads_authority_fk FOREIGN KEY (authority_id, tenant_id, workspace)
        REFERENCES memplex_source_authorities (authority_id, tenant_id, workspace),
    CONSTRAINT memplex_source_heads_scope_check CHECK (((tenant_id <> ''::text) AND (tenant_id <> '__memplex_legacy__'::text) AND (workspace <> ''::text))),
    CONSTRAINT memplex_source_heads_authority_check CHECK (((authority_id)::text ~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'::text)),
    CONSTRAINT memplex_source_heads_state_check CHECK ((state = ANY (ARRAY['live'::text, 'deleted'::text, 'quarantined'::text]))),
    CONSTRAINT memplex_source_heads_identity_check CHECK (((owner_subject <> ''::text) AND (source_agent <> ''::text) AND (visibility = ANY (ARRAY['user'::text, 'workspace'::text, 'session'::text])))),
    CONSTRAINT memplex_source_heads_kind_check CHECK ((kind = ANY (ARRAY['raw_paragraph'::text, 'function'::text, 'fact'::text, 'preference'::text, 'observation'::text]))),
    CONSTRAINT memplex_source_heads_class_check CHECK ((source_class = ANY (ARRAY['captured_input'::text, 'direct_input'::text, 'derived_v1'::text, 'legacy_unverified'::text]))),
    CONSTRAINT memplex_source_heads_revision_check CHECK (((content_revision > 0) AND (auth_revision > 0) AND (last_commit_seq > 0) AND (digest_version = 1))),
    CONSTRAINT memplex_source_heads_token_check CHECK (((logical_id <> ''::text) AND ((incarnation)::text ~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'::text) AND (semantic_digest ~ '^[0-9a-f]{64}$'::text))),
    CONSTRAINT memplex_source_heads_data_check CHECK ((((jsonb_typeof(head_data) = 'object'::text) AND (head_data ?& ARRAY['input_context'::text, 'root_refs'::text]) AND ((head_data - ARRAY['input_context'::text, 'root_refs'::text]) = '{}'::jsonb)) AND (jsonb_typeof((head_data -> 'input_context'::text)) = 'object'::text) AND (jsonb_typeof((head_data -> 'root_refs'::text)) = 'array'::text))),
    CONSTRAINT memplex_source_heads_tombstone_check CHECK (CASE WHEN (state = 'deleted'::text) THEN ((payload_locator = ''::text) AND ((head_data -> 'input_context'::text) = '{}'::jsonb) AND ((head_data -> 'root_refs'::text) = '[]'::jsonb)) ELSE (payload_locator <> ''::text) END)
);

ALTER TABLE memplex_source_heads ENABLE ROW LEVEL SECURITY;
ALTER TABLE memplex_source_heads FORCE ROW LEVEL SECURITY;
CREATE POLICY memplex_source_heads_scope ON memplex_source_heads
    USING ((((tenant_id <> '__memplex_legacy__'::text) AND (tenant_id = current_setting('memplex.tenant_id'::text, true)) AND (workspace = current_setting('memplex.workspace_id'::text, true))) AND CASE visibility WHEN 'user'::text THEN (owner_subject = current_setting('memplex.subject_id'::text, true)) WHEN 'workspace'::text THEN true WHEN 'session'::text THEN ((owner_subject = current_setting('memplex.subject_id'::text, true)) AND (source_agent = current_setting('memplex.agent_id'::text, true)) AND (source_session = current_setting('memplex.session_id'::text, true)) AND (source_session <> ''::text)) ELSE false END))
    WITH CHECK ((((tenant_id <> '__memplex_legacy__'::text) AND (tenant_id = current_setting('memplex.tenant_id'::text, true)) AND (workspace = current_setting('memplex.workspace_id'::text, true))) AND CASE visibility WHEN 'user'::text THEN (owner_subject = current_setting('memplex.subject_id'::text, true)) WHEN 'workspace'::text THEN true WHEN 'session'::text THEN ((owner_subject = current_setting('memplex.subject_id'::text, true)) AND (source_agent = current_setting('memplex.agent_id'::text, true)) AND (source_session = current_setting('memplex.session_id'::text, true)) AND (source_session <> ''::text)) ELSE false END AND (owner_subject = current_setting('memplex.subject_id'::text, true)) AND (source_agent = current_setting('memplex.agent_id'::text, true)) AND (source_session = current_setting('memplex.session_id'::text, true))));

CREATE TABLE memplex_derivation_records (
    authority_id UUID NOT NULL,
    record_kind TEXT NOT NULL,
    record_key TEXT NOT NULL,
    data JSONB NOT NULL,
    tenant_id TEXT NOT NULL,
    owner_subject TEXT NOT NULL,
    workspace TEXT NOT NULL,
    visibility TEXT NOT NULL,
    source_agent TEXT NOT NULL,
    source_session TEXT NOT NULL,
    PRIMARY KEY (authority_id, record_kind, record_key),
    CONSTRAINT memplex_derivation_records_authority_fk FOREIGN KEY (authority_id, tenant_id, workspace)
        REFERENCES memplex_source_authorities (authority_id, tenant_id, workspace),
    CONSTRAINT memplex_derivation_records_scope_check CHECK (((tenant_id <> ''::text) AND (tenant_id <> '__memplex_legacy__'::text) AND (workspace <> ''::text))),
    CONSTRAINT memplex_derivation_records_authority_check CHECK (((authority_id)::text ~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'::text)),
    CONSTRAINT memplex_derivation_records_identity_check CHECK (((owner_subject <> ''::text) AND (source_agent <> ''::text) AND (visibility = ANY (ARRAY['user'::text, 'workspace'::text, 'session'::text])))),
    CONSTRAINT memplex_derivation_records_kind_check CHECK ((record_kind = ANY (ARRAY['target'::text, 'publication'::text]))),
    CONSTRAINT memplex_derivation_records_version_check CHECK (((jsonb_typeof((data -> 'contract_version'::text)) = 'number'::text) AND ((data ->> 'contract_version'::text) = '1'::text))),
    CONSTRAINT memplex_derivation_records_data_check CHECK ((CASE WHEN (record_kind = 'target'::text) THEN (((jsonb_typeof(data) = 'object'::text) AND (data ?& ARRAY['contract_version'::text, 'derivation_key'::text, 'token'::text, 'state'::text, 'last_commit_seq'::text]) AND ((data - ARRAY['contract_version'::text, 'derivation_key'::text, 'token'::text, 'state'::text, 'last_commit_seq'::text]) = '{}'::jsonb)) AND (jsonb_typeof((data -> 'derivation_key'::text)) = 'string'::text) AND ((data ->> 'derivation_key'::text) = record_key) AND (record_key ~ '^[0-9a-f]{64}$'::text) AND (((jsonb_typeof((data -> 'token'::text)) = 'object'::text) AND ((data -> 'token'::text) ?& ARRAY['incarnation'::text, 'generation'::text, 'current_publication'::text]) AND (((data -> 'token'::text) - ARRAY['incarnation'::text, 'generation'::text, 'current_publication'::text]) = '{}'::jsonb)) AND (jsonb_typeof(((data -> 'token'::text) -> 'incarnation'::text)) = 'string'::text) AND (((data -> 'token'::text) ->> 'incarnation'::text) ~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'::text) AND ((jsonb_typeof(((data -> 'token'::text) -> 'generation'::text)) = 'number'::text) AND (((data -> 'token'::text) ->> 'generation'::text) ~ '^[1-9][0-9]{0,18}$'::text) AND ((length(((data -> 'token'::text) ->> 'generation'::text)) < 19) OR (((data -> 'token'::text) ->> 'generation'::text) <= '9223372036854775807'::text))) AND (jsonb_typeof(((data -> 'token'::text) -> 'current_publication'::text)) = 'string'::text) AND (((data -> 'token'::text) ->> 'current_publication'::text) ~ '^fact_derived_v1_[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}$'::text)) AND ((data ->> 'state'::text) = ANY (ARRAY['live'::text, 'deleted'::text, 'quarantined'::text])) AND ((jsonb_typeof((data -> 'last_commit_seq'::text)) = 'number'::text) AND ((data ->> 'last_commit_seq'::text) ~ '^[1-9][0-9]{0,18}$'::text) AND ((length((data ->> 'last_commit_seq'::text)) < 19) OR ((data ->> 'last_commit_seq'::text) <= '9223372036854775807'::text)))) ELSE (((jsonb_typeof(data) = 'object'::text) AND (data ?& ARRAY['contract_version'::text, 'publication_id'::text, 'target_key'::text, 'target_token'::text, 'state'::text, 'envelope_digest'::text, 'output_digest'::text, 'source_refs'::text, 'algorithm_version'::text, 'request_id'::text, 'request_digest'::text, 'manifest_digest'::text, 'observed_commit_seq'::text, 'commit_seq'::text]) AND ((data - ARRAY['contract_version'::text, 'publication_id'::text, 'target_key'::text, 'target_token'::text, 'state'::text, 'envelope_digest'::text, 'output_digest'::text, 'source_refs'::text, 'algorithm_version'::text, 'request_id'::text, 'request_digest'::text, 'manifest_digest'::text, 'observed_commit_seq'::text, 'commit_seq'::text]) = '{}'::jsonb)) AND (jsonb_typeof((data -> 'publication_id'::text)) = 'string'::text) AND ((data ->> 'publication_id'::text) = record_key) AND (record_key ~ '^fact_derived_v1_[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}$'::text) AND (((jsonb_typeof((data -> 'target_token'::text)) = 'object'::text) AND ((data -> 'target_token'::text) ?& ARRAY['incarnation'::text, 'generation'::text, 'current_publication'::text]) AND (((data -> 'target_token'::text) - ARRAY['incarnation'::text, 'generation'::text, 'current_publication'::text]) = '{}'::jsonb)) AND (jsonb_typeof(((data -> 'target_token'::text) -> 'incarnation'::text)) = 'string'::text) AND (((data -> 'target_token'::text) ->> 'incarnation'::text) ~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'::text) AND ((jsonb_typeof(((data -> 'target_token'::text) -> 'generation'::text)) = 'number'::text) AND (((data -> 'target_token'::text) ->> 'generation'::text) ~ '^[1-9][0-9]{0,18}$'::text) AND ((length(((data -> 'target_token'::text) ->> 'generation'::text)) < 19) OR (((data -> 'target_token'::text) ->> 'generation'::text) <= '9223372036854775807'::text))) AND (jsonb_typeof(((data -> 'target_token'::text) -> 'current_publication'::text)) = 'string'::text) AND (((data -> 'target_token'::text) ->> 'current_publication'::text) ~ '^fact_derived_v1_[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}$'::text)) AND ((data #>> '{target_token,current_publication}'::text[]) = record_key) AND ((data ->> 'state'::text) = ANY (ARRAY['active'::text, 'superseded'::text, 'deleted'::text, 'quarantined'::text])) AND ((jsonb_typeof((data -> 'target_key'::text)) = 'string'::text) AND ((data ->> 'target_key'::text) ~ '^[0-9a-f]{64}$'::text)) AND ((jsonb_typeof((data -> 'envelope_digest'::text)) = 'string'::text) AND ((data ->> 'envelope_digest'::text) ~ '^[0-9a-f]{64}$'::text)) AND ((jsonb_typeof((data -> 'output_digest'::text)) = 'string'::text) AND ((data ->> 'output_digest'::text) ~ '^[0-9a-f]{64}$'::text)) AND ((jsonb_typeof((data -> 'request_digest'::text)) = 'string'::text) AND ((data ->> 'request_digest'::text) ~ '^[0-9a-f]{64}$'::text)) AND ((jsonb_typeof((data -> 'manifest_digest'::text)) = 'string'::text) AND ((data ->> 'manifest_digest'::text) ~ '^[0-9a-f]{64}$'::text)) AND (jsonb_typeof((data -> 'request_id'::text)) = 'string'::text) AND ((data ->> 'request_id'::text) ~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'::text) AND ((data ->> 'algorithm_version'::text) = 'factual_capture/source-validity-v1'::text) AND (jsonb_typeof((data -> 'source_refs'::text)) = 'array'::text) AND (jsonb_array_length((data -> 'source_refs'::text)) > 0) AND (jsonb_array_length((data -> 'source_refs'::text)) <= 64) AND ((jsonb_typeof((data -> 'observed_commit_seq'::text)) = 'number'::text) AND ((data ->> 'observed_commit_seq'::text) ~ '^[1-9][0-9]{0,18}$'::text) AND ((length((data ->> 'observed_commit_seq'::text)) < 19) OR ((data ->> 'observed_commit_seq'::text) <= '9223372036854775807'::text))) AND ((jsonb_typeof((data -> 'commit_seq'::text)) = 'number'::text) AND ((data ->> 'commit_seq'::text) ~ '^[1-9][0-9]{0,18}$'::text) AND ((length((data ->> 'commit_seq'::text)) < 19) OR ((data ->> 'commit_seq'::text) <= '9223372036854775807'::text))) AND (((data ->> 'observed_commit_seq'::text))::numeric <= ((data ->> 'commit_seq'::text))::numeric)) END IS TRUE))
);

ALTER TABLE memplex_derivation_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE memplex_derivation_records FORCE ROW LEVEL SECURITY;
CREATE POLICY memplex_derivation_records_scope ON memplex_derivation_records
    USING ((((tenant_id <> '__memplex_legacy__'::text) AND (tenant_id = current_setting('memplex.tenant_id'::text, true)) AND (workspace = current_setting('memplex.workspace_id'::text, true))) AND CASE visibility WHEN 'user'::text THEN (owner_subject = current_setting('memplex.subject_id'::text, true)) WHEN 'workspace'::text THEN true WHEN 'session'::text THEN ((owner_subject = current_setting('memplex.subject_id'::text, true)) AND (source_agent = current_setting('memplex.agent_id'::text, true)) AND (source_session = current_setting('memplex.session_id'::text, true)) AND (source_session <> ''::text)) ELSE false END))
    WITH CHECK ((((tenant_id <> '__memplex_legacy__'::text) AND (tenant_id = current_setting('memplex.tenant_id'::text, true)) AND (workspace = current_setting('memplex.workspace_id'::text, true))) AND CASE visibility WHEN 'user'::text THEN (owner_subject = current_setting('memplex.subject_id'::text, true)) WHEN 'workspace'::text THEN true WHEN 'session'::text THEN ((owner_subject = current_setting('memplex.subject_id'::text, true)) AND (source_agent = current_setting('memplex.agent_id'::text, true)) AND (source_session = current_setting('memplex.session_id'::text, true)) AND (source_session <> ''::text)) ELSE false END AND (owner_subject = current_setting('memplex.subject_id'::text, true)) AND (source_agent = current_setting('memplex.agent_id'::text, true)) AND (source_session = current_setting('memplex.session_id'::text, true))));

CREATE UNIQUE INDEX memplex_derivation_request_key
ON memplex_derivation_records (authority_id, (data ->> 'request_id'))
WHERE record_kind = 'publication';

INSERT INTO memplex_schema_capabilities (capability_name, parameter_digest, applied_at)
VALUES ('source_validity_v1', '360810c3a7e55391bed0e6914172ada481757f5cfbeceb36f1e46ac1bc6dd73e', clock_timestamp());
