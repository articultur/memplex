"""Test-only completed peer mutations for current-context regression controls."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event

from memplex.models import FieldValue, SourceDocument


def replace_function_action_for_context_test(store, owner, identifier, text):
    """Commit an exact test body through the existing PG transaction helpers.

    Public add/merge retain prior FieldValues, so this fixture deliberately
    uses the canonical locked row and its unchanged relational identity.
    The body and its audit event share one owner-bound application transaction;
    this helper introduces no production replacement API or write policy.
    """
    with store._function_write_transaction(owner, "context test replacement") as cur:
        locked = store._locked_function_by_id(cur, identifier, owner)
        assert locked is not None, "context test replacement requires an existing visible row"
        replacement, identity = locked
        replacement.action = [FieldValue(desc=text)]
        replacement.updated_at = datetime.now(UTC).isoformat()
        store._upsert_function(cur, replacement, identity)
        store._record_changelog(
            cur, identifier, "updated", "Replaced action for context test",
            SourceDocument(type="test"), node=replacement,
        )


def run_completed_peer_mutation(mutate, *, timeout=10):
    completed = Event()

    def worker():
        mutate()
        completed.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(worker)
        pending.result(timeout=timeout)
        assert completed.is_set()
