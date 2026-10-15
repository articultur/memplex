# Source V1 concurrency, crash, timeout and recovery evidence

T14 adds test-only barriers around the accepted production implementation. The six named boundaries are after_candidate_read, after_source_snapshot, before_final_clock, after_linearization, before_publish_compare and after_durable_decision. Original clocks, every final read fence and the tested C/ABA/crash/replay operations retain 250 ms; timeout cases retain their shorter 30 ms caller deadline. Only the three explicitly classified fixture-preparation functions use the existing two-second write setup convention. Policy replacement retains its internal 250 ms cap. Event arrival/release and actual native lock contention establish ordering; there is no sleep-arranged race or repeated product attempt.

The five families are test_source_commit_orders (C/R/L, recursive mid-closure, same instance/separate instance/spawned process, both Lite writer modes), test_old_worker_after_delete_recreate (source/direct-target ABA), test_payload_and_ledger_crash_atomicity (source/publication complete old or new, native client exit and acknowledgment loss), test_timeout_does_not_commit_later (terminal queue/lock/capacity cancellation), and test_restore_quarantine_survives_restart (old backup, durable quarantine and fresh off/v1 admission rejection).

PostgreSQL assertions require actual business backend PIDs. Held-reader races require the exact writer PID/advisory key to appear as not granted in pg_locks before release. The observer is a separate read-only control connection. Client exit is not claimed as PostgreSQL server crash. Commit-return-then-raise is a proven decision and must recover complete NEW with a faulted original resource and no success receipt. Only genuinely unknown Lite journal/fsync decisions allow complete OLD or NEW.

All owned resources capture their original durability/connection owner before risky setup; barriers release before terminal join/reap. No background model, enhancement or rebuild is required to expose readable U or hide stale content. Full Lite pair state and PostgreSQL native table state are read back together; recovery preserves source and target tombstones, current publication and policy watermarks.

PostgreSQL signed backup binds database/schema name. The fixture preserves newer state, closes its original resource and drops only its verified synthetic schema to create an empty target. begin_v1_restore then creates the same named schema and commits quarantine in COMMENT ON SCHEMA before import. No further DROP occurs during quarantine/restart proof. Q survives import and restart; this makes no claim that a schema comment survives DROP SCHEMA or that arbitrary archive remapping is supported.

## Execution status

Local run of the 77 new nodes plus the workflow pin: **55 passed, 2 failed, 21 PostgreSQL skipped** (2026-10-15, Python 3.12.14). The two remaining failures are genuine caller-deadline refusals and remain unresolved:

- `test_payload_and_ledger_crash_atomicity[lite1-source-after_journal_rename_and_parent_dir_fsync]`: the fresh whole-state comparison completed, then the cold public read exhausted its 250 ms fence during authoritative load/reconciliation, before L.
- `test_payload_and_ledger_crash_atomicity[lite1-source-after_changelog_replace]`: the child source replacement exhausted its 250 ms deadline during target-envelope reconciliation, before the requested fault point.

No passing crash interleaving is claimed for a branch that failed before its named fault. The first run’s ten budget refusals and one missing child terminal response, and the second run’s three budget refusals, remain separately preserved. The original child terminal response lacked a stage handshake, so its internal phase remains unknown. The later ready/start protocol shares one original ten-second total parent deadline. Earlier test-authoring failures around guard/final session binding and quarantine exception wrapping are distinguished from product behavior. The workflow’s missing-new-suite pin produced the explicit initial oracle RED.

Ruff, six import contracts, mypy’s 139-file gate and offline `uv lock --check` passed. The first lock check stopped on a historical environment variable pointing to an absent wheel directory; the same check passed with only that child variable removed. No dependency or lock file changed. Local uv 0.12.23 differs from the CI pin 0.10.9.

Native parameters are explicitly skipped without MEMPLEX_TEST_POSTGRES_DSN. A collected or skipped branch is not PostgreSQL evidence. The existing authenticated CI native gate includes both new files; acceptance requires a reviewed exact candidate to pass. Existing accepted T13 checks are not rerun as T14 evidence. T14 is not accepted while the two local failures and native execution remain unresolved.

The source/test manifests, immutable candidate copies, raw/JUnit output, runner owner/start/cleanup records, frozen original expectations and independent reviews are retained together in the review evidence package. Case ordering uses explicit events; random UUIDs are synthetic identities, not random race scheduling. No loop-until-pass result or seeded probabilistic schedule is claimed.

## Frozen A01–A40 traceability

These are source-level bridges to existing reviewed tests, plus the new T14 family where applicable. They do not claim all 40 scenarios were newly executed. Pure-validator and fake-session coverage retains that limitation. A39 uses existing recursive native publication and pure-validator evidence; the new direct-target ABA branch is not recursive A39 proof.

| ID | Existing test definitions | New T14 family |
| --- | --- | --- |
| A01 | `tests/test_source_v1_service_reads.py::test_public_query_replaces_every_body_field` | test_source_commit_orders |
| A02 | `tests/test_source_v1_service_reads.py::test_final_L_and_inflight_read_order` | test_source_commit_orders |
| A03 | `tests/test_source_v1_lite_transactions.py::test_reader_after_commit_refreshes_cross_process`; `tests/test_source_v1_pg_transactions.py::test_pg_post_commit_new_reader` | test_source_commit_orders |
| A04 | `tests/test_source_v1_pg_transactions.py::test_linearized_reader_blocks_writer_until_exit` | test_source_commit_orders |
| A05 | `tests/test_source_v1_validation.py::test_shared_ancestor_is_not_cycle` | Existing bridge only |
| A06 | `tests/test_source_v1_validation.py::test_cycle_and_missing_fail_per_root` | Existing bridge only |
| A07 | `tests/test_source_v1_validation.py::test_lookup_errors_and_unsafe_are_local` | Existing bridge only |
| A08 | `tests/test_source_v1_validation.py::test_fair_budget_counts_cached_closure`; `tests/test_source_v1_validation.py::test_depth_boundary_and_independent_control` | Existing bridge only |
| A09 | `tests/test_source_v1_validation.py::test_small_control_survives_large_request_budget_in_any_rank` | Existing bridge only |
| A10 | `tests/test_source_v1_service_reads.py::test_expiry_uses_single_final_authoritative_L` | Existing bridge only |
| A11 | `tests/test_source_v1_publication.py::test_worker_source_revision_fence` | test_old_worker_after_delete_recreate |
| A12 | `tests/test_source_v1_publication.py::test_late_worker_rejected_across_processes` | Existing bridge only |
| A13 | `tests/test_source_v1_publication.py::test_target_aba_and_late_worker` | Existing bridge only |
| A14 | `tests/test_source_v1_lite_transactions.py::test_recreate_changes_incarnation`; `tests/test_source_v1_publication.py::test_worker_source_revision_fence` | test_old_worker_after_delete_recreate |
| A15 | `tests/test_source_v1_lite_transactions.py::test_source_compare_and_write_one_decision`; `tests/test_source_v1_pg_transactions.py::test_pg_source_cas_and_rollback` | Existing bridge only |
| A16 | `tests/test_source_v1_lifecycle_matrix.py::test_explicit_cross_subject_grant_and_regrant_do_not_recertify` | Existing bridge only |
| A17 | `tests/test_source_v1_policy.py::test_policy_commit_precedes_read_in_stale_process`; `tests/test_source_v1_publication.py::test_source_auth_and_policy_checked_in_publication_fence` | Existing bridge only |
| A18 | `tests/test_source_v1_lite_transactions.py::test_fault_boundary_leaves_whole_source_pair`; `tests/test_source_v1_pg_transactions.py::test_pg_source_cas_and_rollback` | test_payload_and_ledger_crash_atomicity |
| A19 | `tests/test_source_v1_publication.py::test_process_exit_preserves_atomic_publication`; `tests/test_source_v1_publication.py::test_native_postgres_publication_rollback` | test_payload_and_ledger_crash_atomicity |
| A20 | `tests/test_source_v1_lite_transactions.py::test_native_cross_process_cas_has_one_winner`; `tests/test_source_v1_pg_transactions.py::test_reader_started_before_commit_waits_then_observes_it` | test_source_commit_orders |
| A21 | `tests/test_source_v1_service_reads.py::test_invalidated_source_drops_only_its_derived_body` | test_source_commit_orders |
| A22 | `tests/test_source_v1_adapter_reads.py::test_native_adapters_drop_stale_derivation_keep_control`; `tests/test_source_v1_service_reads.py::test_explanation_and_graph_context_cannot_leak` | Existing bridge only |
| A23 | `tests/test_source_v1_service_reads.py::test_reranker_receives_only_guarded_projection` | Existing bridge only |
| A24 | `tests/test_source_v1_adapter_reads.py::test_prefetch_consumption_revalidates_after_source_change` | Existing bridge only |
| A25 | `tests/test_source_v1_compatibility.py::test_trusted_v1_service_cold_open_serves_exact_authority`; `tests/test_source_v1_publication.py::test_process_exit_preserves_atomic_publication` | test_payload_and_ledger_crash_atomicity |
| A26 | `tests/test_source_v1_restore.py::test_restore_failure_never_reopens_live`; `tests/test_source_v1_restore.py::test_native_pg_explicit_restore_live_bytes_remain_quarantined` | test_restore_quarantine_survives_restart |
| A27 | `tests/test_source_v1_compatibility.py::test_sync_typed_without_raw_stays_quarantined` | Existing bridge only |
| A28 | `tests/test_source_v1_compatibility.py::test_v1_rejects_aliases_and_sync_before_open`; `tests/test_source_v1_compatibility.py::test_sync_v1_egress_refuses_old_peer` | Existing bridge only |
| A29 | `tests/test_source_v1_codec.py::test_four_existing_models_preserve_carrier_exactly`; `tests/test_source_v1_pg_transactions.py::test_native_all_source_kinds_roundtrip_and_atomic_delete` | Existing bridge only |
| A30 | `tests/test_source_v1_lite_format.py::test_old_reader_rejects_v3`; `tests/test_source_v1_codec.py::test_reserved_carrier_rejects_ambiguous_json` | Existing bridge only |
| A31 | `tests/test_source_v1_mutation_routes.py::test_legacy_mutation_admission_is_closed`; `tests/test_source_v1_publication.py::test_postgres_ordinary_guard_precedes_identity_and_sql` | Existing bridge only |
| A32 | `tests/test_source_v1_lifecycle_matrix.py::test_independent_e2_survives_invalid_e1`; `tests/test_source_v1_codec.py::test_reserved_carrier_rejects_ambiguous_json` | Existing bridge only |
| A33 | `tests/test_source_v1_lite_format.py::test_carrier_ledger_mismatch_is_quarantined`; `tests/test_source_v1_validation.py::test_publication_proof_must_match_authority` | Existing bridge only |
| A34 | `tests/test_source_v1_compatibility.py::test_worker_producers_reject_before_side_effects`; `tests/test_source_v1_mutation_routes.py::test_legacy_mutation_admission_is_closed` | Existing bridge only |
| A35 | `tests/test_source_v1_adapter_reads.py::test_unbound_development_markers_are_rejected_but_legacy_remains`; `tests/test_source_v1_adapter_reads.py::test_historical_flag_never_restores_expired_v1` | Existing bridge only |
| A36 | `tests/test_source_v1_validation.py::test_utc_rollback_does_not_extend_monotonic_budget`; `tests/test_source_v1_service_reads.py::test_round1_native_deadline_finalization_keeps_proof_stages`; `tests/test_source_v1_lite_transactions.py::test_native_canceled_source_job_cannot_publish_later` | test_timeout_does_not_commit_later |
| A37 | `tests/test_source_v1_capture.py::test_full_model_input_manifest_not_self_citations`; `tests/test_source_v1_capture.py::test_independent_b_only_publication_survives_unrelated_a_deletion` | Existing bridge only |
| A38 | `tests/test_source_v1_publication.py::test_target_aba_and_late_worker` | test_old_worker_after_delete_recreate |
| A39 | `tests/test_source_v1_publication.py::test_stale_upstream_publication_cannot_publish_downstream`; `tests/test_source_v1_validation.py::test_current_publication_required_recursively` | Existing bridge only |
| A40 | `tests/test_source_v1_capture.py::test_capture_input_hidden_but_internal_closure_checked` | Existing bridge only |
