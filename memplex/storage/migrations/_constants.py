"""Shared schema constants and data classes for the migration cluster.

This module is deliberately dependency-free (stdlib only) so ``runner`` and
the four split-out modules (``catalogue_checks``, ``acl_verification``,
``ledger_state``, ``catalogue_snapshot``) can all import the same names from
here in any order.  ``runner`` re-exports everything via its own
``from ..._constants import ...`` so existing ``from ...runner import X``
paths keep resolving.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

_LEDGER_TABLE: Final[str] = "memplex_schema_migrations"
_CAPABILITIES_TABLE: Final[str] = "memplex_schema_capabilities"
_MAX_VECTOR_DIM: Final[int] = 16_000
_CORE_TABLES: Final[tuple[str, ...]] = (
    "memplex_functions",
    "memplex_edges",
    "memplex_observations",
    "memplex_facts",
    "memplex_preferences",
    # ADR-013 Stage 2 raw layer (0007): same typed-node shape; sync
    # carriage arrives with Stage 3's SyncNodeType extension.
    "memplex_paragraphs",
    "memplex_changelog",
)
_SYNC_TABLES: Final[tuple[str, ...]] = (
    "memplex_sync_outbox",
    "memplex_sync_entity_versions",
    "memplex_sync_inbox",
    "memplex_sync_batches",
    "memplex_sync_targets",
    "memplex_sync_deliveries",
    "memplex_sync_cursors",
    "memplex_sync_stream_state",
    "memplex_sync_local_identity",
    "memplex_sync_ingress_principals",
    "memplex_sync_snapshots",
    "memplex_sync_snapshot_items",
)
_TASK_TABLES: Final[tuple[str, ...]] = ("memplex_background_tasks",)
_LEGACY_CORE_TABLES: Final[tuple[str, ...]] = (
    "memplex_functions",
    "memplex_edges",
    "memplex_observations",
    "memplex_changelog",
)

# Source-validity V1 schema contracts. This capability installs no authority rows.
_SOURCE_TABLES: Final[tuple[str, ...]] = ('memplex_source_authorities', 'memplex_source_heads', 'memplex_derivation_records')

_SOURCE_CAPABILITY: Final[tuple[str, str]] = ('source_validity_v1', '360810c3a7e55391bed0e6914172ada481757f5cfbeceb36f1e46ac1bc6dd73e')

_SOURCE_COLUMNS: Final[dict[str, tuple[tuple[str, str, bool, str], ...]]] = {'memplex_source_authorities': (('authority_id', 'uuid', True, ''),
                                ('tenant_id', 'text', True, ''),
                                ('workspace', 'text', True, ''),
                                ('state', 'text', True, ''),
                                ('policy_revision', 'bigint', True, ''),
                                ('commit_seq', 'bigint', True, ''),
                                ('policy', 'jsonb', True, ''),
                                ('contract_version', 'smallint', True, '')),
 'memplex_source_heads': (('authority_id', 'uuid', True, ''),
                          ('kind', 'text', True, ''),
                          ('logical_id', 'text', True, ''),
                          ('incarnation', 'uuid', True, ''),
                          ('content_revision', 'bigint', True, ''),
                          ('auth_revision', 'bigint', True, ''),
                          ('state', 'text', True, ''),
                          ('source_class', 'text', True, ''),
                          ('payload_locator', 'text', True, ''),
                          ('digest_version', 'smallint', True, ''),
                          ('semantic_digest', 'text', True, ''),
                          ('last_commit_seq', 'bigint', True, ''),
                          ('head_data', 'jsonb', True, ''),
                          ('tenant_id', 'text', True, ''),
                          ('owner_subject', 'text', True, ''),
                          ('workspace', 'text', True, ''),
                          ('visibility', 'text', True, ''),
                          ('source_agent', 'text', True, ''),
                          ('source_session', 'text', True, '')),
 'memplex_derivation_records': (('authority_id', 'uuid', True, ''),
                                ('record_kind', 'text', True, ''),
                                ('record_key', 'text', True, ''),
                                ('data', 'jsonb', True, ''),
                                ('tenant_id', 'text', True, ''),
                                ('owner_subject', 'text', True, ''),
                                ('workspace', 'text', True, ''),
                                ('visibility', 'text', True, ''),
                                ('source_agent', 'text', True, ''),
                                ('source_session', 'text', True, ''))}

_SOURCE_PRIMARY_KEYS: Final[dict[str, tuple[str, ...]]] = {'memplex_source_authorities': ('authority_id',),
 'memplex_source_heads': ('authority_id', 'kind', 'logical_id'),
 'memplex_derivation_records': ('authority_id', 'record_kind', 'record_key')}

_SOURCE_UNIQUE_KEYS: Final[dict[str, dict[str, tuple[str, ...]]]] = {'memplex_source_authorities': {'memplex_source_authorities_scope_key': ('authority_id',
                                                                         'tenant_id',
                                                                         'workspace'),
                                'memplex_source_authorities_tenant_workspace_key': ('tenant_id',
                                                                                    'workspace')},
 'memplex_source_heads': {'memplex_source_heads_logical_id_key': ('authority_id', 'logical_id')},
 'memplex_derivation_records': {}}

_SOURCE_CHECK_EXPRESSIONS: Final[dict[str, dict[str, str]]] = {'memplex_source_authorities': {'memplex_source_authorities_scope_check': '((tenant_id <> '
                                                                          "''::text) AND "
                                                                          '(tenant_id <> '
                                                                          "'__memplex_legacy__'::text) "
                                                                          'AND (workspace <> '
                                                                          "''::text))",
                                'memplex_source_authorities_authority_check': '((authority_id)::text '
                                                                              '~ '
                                                                              "'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'::text)",
                                'memplex_source_authorities_state_check': '(state = ANY '
                                                                          "(ARRAY['live'::text, "
                                                                          "'deleted'::text, "
                                                                          "'quarantined'::text]))",
                                'memplex_source_authorities_revision_check': '((policy_revision > '
                                                                             '0) AND (commit_seq > '
                                                                             '0) AND '
                                                                             '(contract_version = '
                                                                             '1))',
                                'memplex_source_authorities_policy_check': '(((jsonb_typeof(policy) '
                                                                           "= 'object'::text) AND "
                                                                           '(policy ?& '
                                                                           "ARRAY['revision'::text, "
                                                                           "'entries'::text, "
                                                                           "'algorithm_versions'::text]) "
                                                                           'AND ((policy - '
                                                                           "ARRAY['revision'::text, "
                                                                           "'entries'::text, "
                                                                           "'algorithm_versions'::text]) "
                                                                           "= '{}'::jsonb)) AND "
                                                                           "((jsonb_typeof((policy -> 'revision'::text)) = 'number'::text) "
                                                                           "AND ((policy ->> 'revision'::text) = (policy_revision)::text)) "
                                                                           'AND '
                                                                           '(jsonb_typeof((policy '
                                                                           "-> 'entries'::text)) = "
                                                                           "'array'::text) AND "
                                                                           '(jsonb_typeof((policy '
                                                                           '-> '
                                                                           "'algorithm_versions'::text)) "
                                                                           "= 'array'::text))"},
 'memplex_source_heads': {'memplex_source_heads_scope_check': "((tenant_id <> ''::text) AND "
                                                              '(tenant_id <> '
                                                              "'__memplex_legacy__'::text) AND "
                                                              "(workspace <> ''::text))",
                          'memplex_source_heads_authority_check': '((authority_id)::text ~ '
                                                                  "'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'::text)",
                          'memplex_source_heads_state_check': "(state = ANY (ARRAY['live'::text, "
                                                              "'deleted'::text, "
                                                              "'quarantined'::text]))",
                          'memplex_source_heads_identity_check': "((owner_subject <> ''::text) AND "
                                                                 "(source_agent <> ''::text) AND "
                                                                 '(visibility = ANY '
                                                                 "(ARRAY['user'::text, "
                                                                 "'workspace'::text, "
                                                                 "'session'::text])))",
                          'memplex_source_heads_kind_check': '(kind = ANY '
                                                             "(ARRAY['raw_paragraph'::text, "
                                                             "'function'::text, 'fact'::text, "
                                                             "'preference'::text, "
                                                             "'observation'::text]))",
                          'memplex_source_heads_class_check': '(source_class = ANY '
                                                              "(ARRAY['captured_input'::text, "
                                                              "'direct_input'::text, "
                                                              "'derived_v1'::text, "
                                                              "'legacy_unverified'::text]))",
                          'memplex_source_heads_revision_check': '((content_revision > 0) AND '
                                                                 '(auth_revision > 0) AND '
                                                                 '(last_commit_seq > 0) AND '
                                                                 '(digest_version = 1))',
                          'memplex_source_heads_token_check': "((logical_id <> ''::text) AND "
                                                              '((incarnation)::text ~ '
                                                              "'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'::text) "
                                                              'AND (semantic_digest ~ '
                                                              "'^[0-9a-f]{64}$'::text))",
                          'memplex_source_heads_data_check': '(((jsonb_typeof(head_data) = '
                                                             "'object'::text) AND (head_data ?& "
                                                             "ARRAY['input_context'::text, "
                                                             "'root_refs'::text]) AND ((head_data "
                                                             "- ARRAY['input_context'::text, "
                                                             "'root_refs'::text]) = '{}'::jsonb)) "
                                                             'AND (jsonb_typeof((head_data -> '
                                                             "'input_context'::text)) = "
                                                             "'object'::text) AND "
                                                             '(jsonb_typeof((head_data -> '
                                                             "'root_refs'::text)) = "
                                                             "'array'::text))",
                          'memplex_source_heads_tombstone_check': 'CASE WHEN (state = '
                                                                  "'deleted'::text) THEN "
                                                                  "((payload_locator = ''::text) "
                                                                  'AND ((head_data -> '
                                                                  "'input_context'::text) = "
                                                                  "'{}'::jsonb) AND ((head_data -> "
                                                                  "'root_refs'::text) = "
                                                                  "'[]'::jsonb)) ELSE "
                                                                  "(payload_locator <> ''::text) "
                                                                  'END'},
 'memplex_derivation_records': {'memplex_derivation_records_scope_check': '((tenant_id <> '
                                                                          "''::text) AND "
                                                                          '(tenant_id <> '
                                                                          "'__memplex_legacy__'::text) "
                                                                          'AND (workspace <> '
                                                                          "''::text))",
                                'memplex_derivation_records_authority_check': '((authority_id)::text '
                                                                              '~ '
                                                                              "'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'::text)",
                                'memplex_derivation_records_identity_check': '((owner_subject <> '
                                                                             "''::text) AND "
                                                                             '(source_agent <> '
                                                                             "''::text) AND "
                                                                             '(visibility = ANY '
                                                                             "(ARRAY['user'::text, "
                                                                             "'workspace'::text, "
                                                                             "'session'::text])))",
                                'memplex_derivation_records_kind_check': '(record_kind = ANY '
                                                                         "(ARRAY['target'::text, "
                                                                         "'publication'::text]))",
                                'memplex_derivation_records_version_check': '((jsonb_typeof((data '
                                                                            '-> '
                                                                            "'contract_version'::text)) "
                                                                            "= 'number'::text) AND "
                                                                            '((data ->> '
                                                                            "'contract_version'::text) "
                                                                            "= '1'::text))",
                                'memplex_derivation_records_data_check': '(CASE WHEN (record_kind = '
                                                                         "'target'::text) THEN "
                                                                         '(((jsonb_typeof(data) = '
                                                                         "'object'::text) AND "
                                                                         '(data ?& '
                                                                         "ARRAY['contract_version'::text, "
                                                                         "'derivation_key'::text, "
                                                                         "'token'::text, "
                                                                         "'state'::text, "
                                                                         "'last_commit_seq'::text]) "
                                                                         'AND ((data - '
                                                                         "ARRAY['contract_version'::text, "
                                                                         "'derivation_key'::text, "
                                                                         "'token'::text, "
                                                                         "'state'::text, "
                                                                         "'last_commit_seq'::text]) "
                                                                         "= '{}'::jsonb)) AND "
                                                                         '(jsonb_typeof((data -> '
                                                                         "'derivation_key'::text)) "
                                                                         "= 'string'::text) AND "
                                                                         '((data ->> '
                                                                         "'derivation_key'::text) "
                                                                         '= record_key) AND '
                                                                         '(record_key ~ '
                                                                         "'^[0-9a-f]{64}$'::text) "
                                                                         'AND '
                                                                         '(((jsonb_typeof((data -> '
                                                                         "'token'::text)) = "
                                                                         "'object'::text) AND "
                                                                         "((data -> 'token'::text) "
                                                                         '?& '
                                                                         "ARRAY['incarnation'::text, "
                                                                         "'generation'::text, "
                                                                         "'current_publication'::text]) "
                                                                         'AND (((data -> '
                                                                         "'token'::text) - "
                                                                         "ARRAY['incarnation'::text, "
                                                                         "'generation'::text, "
                                                                         "'current_publication'::text]) "
                                                                         "= '{}'::jsonb)) AND "
                                                                         '(jsonb_typeof(((data -> '
                                                                         "'token'::text) -> "
                                                                         "'incarnation'::text)) = "
                                                                         "'string'::text) AND "
                                                                         '(((data -> '
                                                                         "'token'::text) ->> "
                                                                         "'incarnation'::text) ~ "
                                                                         "'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'::text) "
                                                                         'AND '
                                                                         '((jsonb_typeof(((data -> '
                                                                         "'token'::text) -> "
                                                                         "'generation'::text)) = "
                                                                         "'number'::text) AND "
                                                                         '(((data -> '
                                                                         "'token'::text) ->> "
                                                                         "'generation'::text) ~ "
                                                                         "'^[1-9][0-9]{0,18}$'::text) "
                                                                         'AND ((length(((data -> '
                                                                         "'token'::text) ->> "
                                                                         "'generation'::text)) < "
                                                                         '19) OR (((data -> '
                                                                         "'token'::text) ->> "
                                                                         "'generation'::text) <= "
                                                                         "'9223372036854775807'::text))) "
                                                                         'AND (jsonb_typeof(((data '
                                                                         "-> 'token'::text) -> "
                                                                         "'current_publication'::text)) "
                                                                         "= 'string'::text) AND "
                                                                         '(((data -> '
                                                                         "'token'::text) ->> "
                                                                         "'current_publication'::text) "
                                                                         '~ '
                                                                         "'^fact_derived_v1_[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}$'::text)) "
                                                                         'AND ((data ->> '
                                                                         "'state'::text) = ANY "
                                                                         "(ARRAY['live'::text, "
                                                                         "'deleted'::text, "
                                                                         "'quarantined'::text])) "
                                                                         'AND ((jsonb_typeof((data '
                                                                         '-> '
                                                                         "'last_commit_seq'::text)) "
                                                                         "= 'number'::text) AND "
                                                                         '((data ->> '
                                                                         "'last_commit_seq'::text) "
                                                                         '~ '
                                                                         "'^[1-9][0-9]{0,18}$'::text) "
                                                                         'AND ((length((data ->> '
                                                                         "'last_commit_seq'::text)) "
                                                                         '< 19) OR ((data ->> '
                                                                         "'last_commit_seq'::text) "
                                                                         '<= '
                                                                         "'9223372036854775807'::text)))) "
                                                                         'ELSE '
                                                                         '(((jsonb_typeof(data) = '
                                                                         "'object'::text) AND "
                                                                         '(data ?& '
                                                                         "ARRAY['contract_version'::text, "
                                                                         "'publication_id'::text, "
                                                                         "'target_key'::text, "
                                                                         "'target_token'::text, "
                                                                         "'state'::text, "
                                                                         "'envelope_digest'::text, "
                                                                         "'output_digest'::text, "
                                                                         "'source_refs'::text, "
                                                                         "'algorithm_version'::text, "
                                                                         "'request_id'::text, "
                                                                         "'request_digest'::text, "
                                                                         "'manifest_digest'::text, "
                                                                         "'observed_commit_seq'::text, "
                                                                         "'commit_seq'::text]) AND "
                                                                         '((data - '
                                                                         "ARRAY['contract_version'::text, "
                                                                         "'publication_id'::text, "
                                                                         "'target_key'::text, "
                                                                         "'target_token'::text, "
                                                                         "'state'::text, "
                                                                         "'envelope_digest'::text, "
                                                                         "'output_digest'::text, "
                                                                         "'source_refs'::text, "
                                                                         "'algorithm_version'::text, "
                                                                         "'request_id'::text, "
                                                                         "'request_digest'::text, "
                                                                         "'manifest_digest'::text, "
                                                                         "'observed_commit_seq'::text, "
                                                                         "'commit_seq'::text]) = "
                                                                         "'{}'::jsonb)) AND "
                                                                         '(jsonb_typeof((data -> '
                                                                         "'publication_id'::text)) "
                                                                         "= 'string'::text) AND "
                                                                         '((data ->> '
                                                                         "'publication_id'::text) "
                                                                         '= record_key) AND '
                                                                         '(record_key ~ '
                                                                         "'^fact_derived_v1_[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}$'::text) "
                                                                         'AND '
                                                                         '(((jsonb_typeof((data -> '
                                                                         "'target_token'::text)) = "
                                                                         "'object'::text) AND "
                                                                         '((data -> '
                                                                         "'target_token'::text) ?& "
                                                                         "ARRAY['incarnation'::text, "
                                                                         "'generation'::text, "
                                                                         "'current_publication'::text]) "
                                                                         'AND (((data -> '
                                                                         "'target_token'::text) - "
                                                                         "ARRAY['incarnation'::text, "
                                                                         "'generation'::text, "
                                                                         "'current_publication'::text]) "
                                                                         "= '{}'::jsonb)) AND "
                                                                         '(jsonb_typeof(((data -> '
                                                                         "'target_token'::text) -> "
                                                                         "'incarnation'::text)) = "
                                                                         "'string'::text) AND "
                                                                         '(((data -> '
                                                                         "'target_token'::text) "
                                                                         "->> 'incarnation'::text) "
                                                                         '~ '
                                                                         "'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'::text) "
                                                                         'AND '
                                                                         '((jsonb_typeof(((data -> '
                                                                         "'target_token'::text) -> "
                                                                         "'generation'::text)) = "
                                                                         "'number'::text) AND "
                                                                         '(((data -> '
                                                                         "'target_token'::text) "
                                                                         "->> 'generation'::text) "
                                                                         '~ '
                                                                         "'^[1-9][0-9]{0,18}$'::text) "
                                                                         'AND ((length(((data -> '
                                                                         "'target_token'::text) "
                                                                         "->> 'generation'::text)) "
                                                                         '< 19) OR (((data -> '
                                                                         "'target_token'::text) "
                                                                         "->> 'generation'::text) "
                                                                         '<= '
                                                                         "'9223372036854775807'::text))) "
                                                                         'AND (jsonb_typeof(((data '
                                                                         "-> 'target_token'::text) "
                                                                         '-> '
                                                                         "'current_publication'::text)) "
                                                                         "= 'string'::text) AND "
                                                                         '(((data -> '
                                                                         "'target_token'::text) "
                                                                         '->> '
                                                                         "'current_publication'::text) "
                                                                         '~ '
                                                                         "'^fact_derived_v1_[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}$'::text)) "
                                                                         'AND ((data #>> '
                                                                         "'{target_token,current_publication}'::text[]) "
                                                                         '= record_key) AND ((data '
                                                                         "->> 'state'::text) = ANY "
                                                                         "(ARRAY['active'::text, "
                                                                         "'superseded'::text, "
                                                                         "'deleted'::text, "
                                                                         "'quarantined'::text])) "
                                                                         'AND ((jsonb_typeof((data '
                                                                         "-> 'target_key'::text)) "
                                                                         "= 'string'::text) AND "
                                                                         '((data ->> '
                                                                         "'target_key'::text) ~ "
                                                                         "'^[0-9a-f]{64}$'::text)) "
                                                                         'AND ((jsonb_typeof((data '
                                                                         '-> '
                                                                         "'envelope_digest'::text)) "
                                                                         "= 'string'::text) AND "
                                                                         '((data ->> '
                                                                         "'envelope_digest'::text) "
                                                                         '~ '
                                                                         "'^[0-9a-f]{64}$'::text)) "
                                                                         'AND ((jsonb_typeof((data '
                                                                         '-> '
                                                                         "'output_digest'::text)) "
                                                                         "= 'string'::text) AND "
                                                                         '((data ->> '
                                                                         "'output_digest'::text) ~ "
                                                                         "'^[0-9a-f]{64}$'::text)) "
                                                                         'AND ((jsonb_typeof((data '
                                                                         '-> '
                                                                         "'request_digest'::text)) "
                                                                         "= 'string'::text) AND "
                                                                         '((data ->> '
                                                                         "'request_digest'::text) "
                                                                         '~ '
                                                                         "'^[0-9a-f]{64}$'::text)) "
                                                                         'AND ((jsonb_typeof((data '
                                                                         '-> '
                                                                         "'manifest_digest'::text)) "
                                                                         "= 'string'::text) AND "
                                                                         '((data ->> '
                                                                         "'manifest_digest'::text) "
                                                                         '~ '
                                                                         "'^[0-9a-f]{64}$'::text)) "
                                                                         'AND (jsonb_typeof((data '
                                                                         "-> 'request_id'::text)) "
                                                                         "= 'string'::text) AND "
                                                                         '((data ->> '
                                                                         "'request_id'::text) ~ "
                                                                         "'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'::text) "
                                                                         'AND ((data ->> '
                                                                         "'algorithm_version'::text) "
                                                                         '= '
                                                                         "'factual_capture/source-validity-v1'::text) "
                                                                         'AND (jsonb_typeof((data '
                                                                         "-> 'source_refs'::text)) "
                                                                         "= 'array'::text) AND "
                                                                         '(jsonb_array_length((data '
                                                                         "-> 'source_refs'::text)) "
                                                                         '> 0) AND '
                                                                         '(jsonb_array_length((data '
                                                                         "-> 'source_refs'::text)) "
                                                                         '<= 64) AND '
                                                                         '((jsonb_typeof((data -> '
                                                                         "'observed_commit_seq'::text)) "
                                                                         "= 'number'::text) AND "
                                                                         '((data ->> '
                                                                         "'observed_commit_seq'::text) "
                                                                         '~ '
                                                                         "'^[1-9][0-9]{0,18}$'::text) "
                                                                         'AND ((length((data ->> '
                                                                         "'observed_commit_seq'::text)) "
                                                                         '< 19) OR ((data ->> '
                                                                         "'observed_commit_seq'::text) "
                                                                         '<= '
                                                                         "'9223372036854775807'::text))) "
                                                                         'AND ((jsonb_typeof((data '
                                                                         "-> 'commit_seq'::text)) "
                                                                         "= 'number'::text) AND "
                                                                         '((data ->> '
                                                                         "'commit_seq'::text) ~ "
                                                                         "'^[1-9][0-9]{0,18}$'::text) "
                                                                         'AND ((length((data ->> '
                                                                         "'commit_seq'::text)) < "
                                                                         '19) OR ((data ->> '
                                                                         "'commit_seq'::text) <= "
                                                                         "'9223372036854775807'::text))) "
                                                                         'AND (((data ->> '
                                                                         "'observed_commit_seq'::text))::numeric "
                                                                         '<= ((data ->> '
                                                                         "'commit_seq'::text))::numeric)) "
                                                                         'END IS TRUE)'}}

_SOURCE_POLICY_EXPRESSIONS: Final[dict[str, tuple[str, str]]] = {'memplex_source_authorities': (("((tenant_id <> '__memplex_legacy__'::text) AND (tenant_id = "
                                "current_setting('memplex.tenant_id'::text, true)) AND (workspace "
                                "= current_setting('memplex.workspace_id'::text, true)))"),
                                ("((tenant_id <> '__memplex_legacy__'::text) AND (tenant_id = "
                                "current_setting('memplex.tenant_id'::text, true)) AND (workspace "
                                "= current_setting('memplex.workspace_id'::text, true)))")),
 'memplex_source_heads': (("(((tenant_id <> '__memplex_legacy__'::text) AND (tenant_id = "
                          "current_setting('memplex.tenant_id'::text, true)) AND (workspace = "
                          "current_setting('memplex.workspace_id'::text, true))) AND CASE "
                          "visibility WHEN 'user'::text THEN (owner_subject = "
                          "current_setting('memplex.subject_id'::text, true)) WHEN "
                          "'workspace'::text THEN true WHEN 'session'::text THEN ((owner_subject = "
                          "current_setting('memplex.subject_id'::text, true)) AND (source_agent = "
                          "current_setting('memplex.agent_id'::text, true)) AND (source_session = "
                          "current_setting('memplex.session_id'::text, true)) AND (source_session "
                          "<> ''::text)) ELSE false END)"),
                          ("(((tenant_id <> '__memplex_legacy__'::text) AND (tenant_id = "
                          "current_setting('memplex.tenant_id'::text, true)) AND (workspace = "
                          "current_setting('memplex.workspace_id'::text, true))) AND CASE "
                          "visibility WHEN 'user'::text THEN (owner_subject = "
                          "current_setting('memplex.subject_id'::text, true)) WHEN "
                          "'workspace'::text THEN true WHEN 'session'::text THEN ((owner_subject = "
                          "current_setting('memplex.subject_id'::text, true)) AND (source_agent = "
                          "current_setting('memplex.agent_id'::text, true)) AND (source_session = "
                          "current_setting('memplex.session_id'::text, true)) AND (source_session "
                          "<> ''::text)) ELSE false END AND (owner_subject = "
                          "current_setting('memplex.subject_id'::text, true)) AND (source_agent = "
                          "current_setting('memplex.agent_id'::text, true)) AND (source_session = "
                          "current_setting('memplex.session_id'::text, true)))")),
 'memplex_derivation_records': (("(((tenant_id <> '__memplex_legacy__'::text) AND (tenant_id = "
                                "current_setting('memplex.tenant_id'::text, true)) AND (workspace "
                                "= current_setting('memplex.workspace_id'::text, true))) AND CASE "
                                "visibility WHEN 'user'::text THEN (owner_subject = "
                                "current_setting('memplex.subject_id'::text, true)) WHEN "
                                "'workspace'::text THEN true WHEN 'session'::text THEN "
                                "((owner_subject = current_setting('memplex.subject_id'::text, "
                                'true)) AND (source_agent = '
                                "current_setting('memplex.agent_id'::text, true)) AND "
                                "(source_session = current_setting('memplex.session_id'::text, "
                                "true)) AND (source_session <> ''::text)) ELSE false END)"),
                                ("(((tenant_id <> '__memplex_legacy__'::text) AND (tenant_id = "
                                "current_setting('memplex.tenant_id'::text, true)) AND (workspace "
                                "= current_setting('memplex.workspace_id'::text, true))) AND CASE "
                                "visibility WHEN 'user'::text THEN (owner_subject = "
                                "current_setting('memplex.subject_id'::text, true)) WHEN "
                                "'workspace'::text THEN true WHEN 'session'::text THEN "
                                "((owner_subject = current_setting('memplex.subject_id'::text, "
                                'true)) AND (source_agent = '
                                "current_setting('memplex.agent_id'::text, true)) AND "
                                "(source_session = current_setting('memplex.session_id'::text, "
                                "true)) AND (source_session <> ''::text)) ELSE false END AND "
                                "(owner_subject = current_setting('memplex.subject_id'::text, "
                                'true)) AND (source_agent = '
                                "current_setting('memplex.agent_id'::text, true)) AND "
                                "(source_session = current_setting('memplex.session_id'::text, "
                                'true)))'))}

_SOURCE_INDEX_KEYS: Final[dict[str, dict[str, tuple[tuple[str, ...], str | None]]]] = {'memplex_source_authorities': {'memplex_source_authorities_scope_key': (('authority_id',
                                                                          'tenant_id',
                                                                          'workspace'),
                                                                         None),
                                'memplex_source_authorities_tenant_workspace_key': (('tenant_id',
                                                                                     'workspace'),
                                                                                    None)},
 'memplex_source_heads': {'memplex_source_heads_logical_id_key': (('authority_id', 'logical_id'),
                                                                  None)},
 'memplex_derivation_records': {'memplex_derivation_request_key': (('authority_id',
                                                                    ('(data ->> '
                                                                    "'request_id'::text)")),
                                                                   ('(record_kind = '
                                                                   "'publication'::text)"))}}


_MANAGED_TABLES: Final[tuple[str, ...]] = (
    *_CORE_TABLES,
    *_SYNC_TABLES,
    *_TASK_TABLES,
    *_SOURCE_TABLES,
    "feedback",
    _CAPABILITIES_TABLE,
    _LEDGER_TABLE,
)
_APPLICATION_ACL_TABLES: Final[tuple[str, ...]] = (
    *_CORE_TABLES,
    "feedback",
    *_SYNC_TABLES,
    *_TASK_TABLES,
    *_SOURCE_TABLES,
)
_APPLICATION_ACL: Final[dict[str, frozenset[str]]] = {
    "memplex_functions": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "memplex_edges": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "memplex_observations": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "memplex_facts": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "memplex_preferences": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "memplex_paragraphs": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "memplex_changelog": frozenset({"SELECT", "INSERT", "DELETE"}),
    "feedback": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "memplex_sync_outbox": frozenset({"SELECT", "INSERT", "DELETE"}),
    "memplex_sync_entity_versions": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "memplex_sync_inbox": frozenset({"SELECT", "INSERT"}),
    "memplex_sync_batches": frozenset({"SELECT", "INSERT"}),
    "memplex_sync_targets": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "memplex_sync_deliveries": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "memplex_sync_cursors": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "memplex_sync_stream_state": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "memplex_sync_local_identity": frozenset(),
    "memplex_sync_ingress_principals": frozenset(),
    "memplex_sync_snapshots": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "memplex_sync_snapshot_items": frozenset({"SELECT", "INSERT", "DELETE"}),
    "memplex_background_tasks": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    **{name: frozenset({"SELECT", "INSERT", "UPDATE"}) for name in _SOURCE_TABLES},
}
_APPLICATION_ACL_FUNCTIONS: Final[tuple[str, ...]] = (
    "memplex_sync_capture_before",
    "memplex_sync_capture_local_change",
    "memplex_sync_assert_delivery_quota",
    "memplex_sync_snapshot_admission_counts",
    "memplex_sync_compact",
)
_SYNC_FUNCTIONS: Final[tuple[str, ...]] = (
    "memplex_configure_sync_local_identity",
    "memplex_sync_assert_delivery_quota",
    "memplex_sync_snapshot_admission_counts",
    "memplex_sync_compact",
    "memplex_sync_capture_before",
    "memplex_sync_capture_local_change",
    "memplex_configure_sync_ingress_principal",
    "memplex_sync_apply_inbound",
    "memplex_sync_require_canonical_entity_key",
    "memplex_sync_require_canonical_version",
    "memplex_sync_encode_string_array",
)
_ACL_COLUMNS: Final[tuple[str, ...]] = (
    "tenant_id",
    "owner_subject",
    "workspace",
    "visibility",
    "source_agent",
    "source_session",
)

# ``pg_class`` is the authoritative managed-object namespace. PostgreSQL
# represents primary-key backing indexes and BIGSERIAL sequences there too, so
# scanning only ordinary relations would let a memplex-named object hide behind
# an ignored relkind.
_KNOWN_MEMPLEX_RELATION_KINDS: Final[dict[str, str]] = {
    **{name: "r" for name in (*_CORE_TABLES, _CAPABILITIES_TABLE, _LEDGER_TABLE)},
    **{name: "r" for name in _SYNC_TABLES},
    **{name: "r" for name in _TASK_TABLES},
    **{name: "r" for name in _SOURCE_TABLES},
    **{f"{name}_pkey": "i" for name in _SOURCE_TABLES},
    **{index: "i" for indexes in _SOURCE_INDEX_KEYS.values() for index in indexes},
    **{f"{name}_pkey": "i" for name in (*_CORE_TABLES, _CAPABILITIES_TABLE, _LEDGER_TABLE)},
    "memplex_changelog_id_seq": "S",
    "memplex_sync_local_identity_pkey": "i",
    "memplex_sync_local_identity_node_id_key": "i",
    "memplex_sync_ingress_principals_pkey": "i",
    "memplex_functions_tenant_updated_idx": "i",
    "memplex_functions_tenant_idx": "i",
    "memplex_edges_tenant_source_type_target_idx": "i",
    "memplex_edges_tenant_target_type_source_idx": "i",
    "memplex_edges_tenant_idx": "i",
    "memplex_edges_tenant_target_function_idx": "i",
    "memplex_observations_tenant_idx": "i",
    "memplex_facts_tenant_idx": "i",
    "memplex_preferences_tenant_idx": "i",
    "memplex_paragraphs_tenant_idx": "i",
    "memplex_changelog_tenant_idx": "i",
    "memplex_functions_workspace_normalized_name_key": "i",
    "memplex_functions_user_normalized_name_key": "i",
    "memplex_functions_session_normalized_name_key": "i",
    **{f"{name}_pkey": "i" for name in _SYNC_TABLES},
    "memplex_sync_targets_tenant_id_remote_node_id_key": "i",
    "memplex_sync_snapshots_tenant_id_remote_id_consumer_id_requ_key": "i",
    "memplex_sync_outbox_tenant_id_origin_node_id_event_id_key": "i",
    "memplex_sync_outbox_stream_seq_seq": "S",
    "memplex_sync_outbox_tenant_stream_idx": "i",
    "memplex_sync_deliveries_claim_idx": "i",
    "memplex_sync_deliveries_retention_idx": "i",
    "memplex_sync_cursors_tenant_after_idx": "i",
    "memplex_sync_snapshots_expiry_idx": "i",
    "memplex_background_tasks_pkey": "i",
    "memplex_background_tasks_due_idx": "i",
    "memplex_background_tasks_lease_idx": "i",
}

_CORE_POLICY_DIGESTS: Final[tuple[str, str]] = (
    "966aa3ee5c0224eafbba062f2bfee28f4a34b0f0f86f9ee23b72e6f62d08de0d",
    "3f1fe3be69457e86e5c3f595d60141abcd127e987d5a7d01eab84f871c0d4493",
)
_FEEDBACK_CURRENT_POLICY_DIGESTS: Final[tuple[str, str]] = (
    "5b5ddc963fccdcb941c7eccbcb646b053ef079d7bf47272964fc184891152025",
    "f2c2439d4d159bdc189dc2cd46d116b69799483dd879af36d05f9b03ee98e679",
)
_FEEDBACK_RUNTIME_V1_POLICY_DIGESTS: Final[tuple[str, str]] = (
    "dfa495e062435c70a0cbd2e3b971cdce89abc48a19b7a215897ad7b7fe03e5c2",
    "8c8dd0553aab40d8e7c042fd759fa97ded54317596a291407d6a864fda2cb5c6",
)
_SEARCH_TSV_GENERATION_DIGEST: Final[str] = (
    "542630b9761cfc19062f8e4c97e4b75cdfabc0f715170d779c5d33c0ec6968d7"
)


class MigrationIntegrityError(RuntimeError):
    """Raised when versioned SQL resources cannot be safely trusted."""


@dataclass(frozen=True, slots=True)
class Migration:
    """One immutable SQL resource discovered from the installed package."""

    version: int
    name: str
    sql_bytes: bytes
    checksum: str


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    """Read-only migration status returned by the Task 2 runner state machine."""

    current_version: int
    known_version: int
    pending: tuple[Migration, ...]
    state: Literal["ready", "upgrade_required", "blocked"]


@dataclass(frozen=True, slots=True)
class SchemaVariantFeatures:
    """Structured features of one recognised post-G002 schema variant.

    This is the authoritative classification; the variant *name* is a derived
    display string (see ``display_name``) consumed by status output, digests,
    and legacy adoption-baseline mapping only.
    """

    layout: str
    feedback_v1: bool = False
    current: bool = False
    has_edge_integrity: bool = False
    has_reliable_sync: bool = False
    has_background_tasks: bool = False
    vector_dim: int | None = None
    has_source_validity: bool = False

    def display_name(self) -> str:
        """Render the canonical variant name for status output and digests."""
        if self.feedback_v1:
            name = "post_g002_runtime_v1_feedback_v1"
        else:
            name = f"post_g002_{self.layout}"
            if self.current:
                if self.has_edge_integrity:
                    name = f"{name}_edge_integrity"
                name = f"{name}_current"
        if self.has_reliable_sync:
            name = f"{name}_reliable_sync_v5"
        if self.has_background_tasks:
            name = f"{name}_background_tasks_v6"
        if self.has_source_validity:
            name = f"{name}_source_validity_v1"
        if self.vector_dim is not None:
            name = f"{name}_vector_{self.vector_dim}"
        return name


@dataclass(frozen=True, slots=True)
class SchemaFingerprint:
    """Canonical, catalogue-derived classification of the current schema."""

    kind: Literal["empty", "pre_g002_3_2_7", "post_g002_current", "unknown"]
    digest: str
    variant: str = ""
    features: SchemaVariantFeatures | None = None


@dataclass(frozen=True, slots=True)
class ApplicationAclContract:
    """Data-only authorization allowance for one exact application role."""

    role: str

    def __post_init__(self) -> None:
        if type(self.role) is not str or not self.role:
            raise TypeError("application ACL role must be an exact non-empty str")


@dataclass(frozen=True, slots=True)
class IngressAclContract:
    """Exact relation-less LOGIN principal allowed to submit inbound batches."""

    role: str

    def __post_init__(self) -> None:
        if type(self.role) is not str or not self.role:
            raise TypeError("ingress ACL role must be an exact non-empty str")


@dataclass(frozen=True, slots=True)
class _LedgerEntry:
    version: int
    name: str
    checksum: str
    execution_mode: str
    baseline_fingerprint: str | None
