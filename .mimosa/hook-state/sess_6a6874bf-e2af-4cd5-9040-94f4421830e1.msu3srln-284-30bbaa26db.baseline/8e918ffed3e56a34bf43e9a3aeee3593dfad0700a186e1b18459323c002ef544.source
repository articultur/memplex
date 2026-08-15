"""Data-only verified inbound executor.

The executor owns only SQL invocation/parsing behavior.  No connection string or
pool selection logic lives here; callers inject a prepared transaction context
factory.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from memplex.sync_ingress import ValidatedIngressBatch

if TYPE_CHECKING:
    from memplex.sync_protocol import SyncBatchResult, SyncReceipt


_TransactionFactory = Callable[[], Any]


class InboundSyncExecutor:
    """Apply a verified inbound batch through a transaction-bound cursor.

    The constructor accepts either a transaction callable that returns a context
    manager, or an object exposing ``transaction()`` that returns one.
    """

    def __init__(
        self,
        transaction: _TransactionFactory | Any,
        *,
        authority_check: Callable[[], None] | None = None,
    ) -> None:
        if authority_check is not None and not callable(authority_check):
            raise TypeError("authority_check must be callable")
        self._make_transaction = self._resolve_transaction(transaction)
        self._authority_check = authority_check

    @staticmethod
    def _resolve_transaction(transaction: _TransactionFactory | Any) -> _TransactionFactory:
        if callable(transaction) and not hasattr(transaction, "transaction"):
            return transaction
        if hasattr(transaction, "transaction"):
            manager = transaction
            if not callable(manager.transaction):
                raise TypeError("transaction must be callable or expose a transaction() callable")
            return manager.transaction
        raise TypeError("transaction must be callable or expose a transaction() callable")

    def apply(self, batch: ValidatedIngressBatch) -> "SyncBatchResult":
        if type(batch) is not ValidatedIngressBatch:
            raise TypeError("apply accepts only ValidatedIngressBatch")
        if self._authority_check is not None:
            self._authority_check()

        with self._make_transaction() as (_, cursor):
            cursor.execute(
                "SELECT memplex_sync_apply_inbound(%s,%s)",
                (batch.canonical_bytes, batch.request_digest),
            )
            raw_result = cursor.fetchone()
            payload = _extract_single_value(raw_result)
            return _parse_batch_result(payload, batch)


def _extract_single_value(row: Any) -> Any:
    if type(row) is not tuple:
        raise TypeError("inbound apply returned malformed row")
    if len(row) != 1:
        raise TypeError("inbound apply returned malformed row")
    return row[0]


def _parse_batch_result(payload: Any, batch: ValidatedIngressBatch) -> "SyncBatchResult":
    from memplex.sync_protocol import SyncBatchResult

    if type(payload) is not dict:
        raise TypeError("inbound apply returned malformed payload")
    required = {"accepted", "duplicate", "conflict", "receipts"}
    if set(payload) != required:
        raise ValueError("inbound apply returned malformed payload")

    accepted = _require_non_negative_int(payload["accepted"])
    duplicate = _require_non_negative_int(payload["duplicate"])
    conflict = _require_non_negative_int(payload["conflict"])
    event_ids = tuple(event.event_id for event in batch.batch.events)

    receipts = _parse_receipts(payload["receipts"], len(event_ids), event_ids)

    if accepted + duplicate + conflict != len(event_ids):
        raise ValueError("inbound apply returned inconsistent outcome counts")
    observed = {
        "accepted": 0,
        "duplicate": 0,
        "rejected_conflict": 0,
    }
    for receipt in receipts:
        if receipt.outcome not in observed:
            raise ValueError("inbound apply returned malformed receipt")
        observed[receipt.outcome] += 1
    if (observed["accepted"], observed["duplicate"], observed["rejected_conflict"]) != (
        accepted,
        duplicate,
        conflict,
    ):
        raise ValueError("inbound apply returned inconsistent outcome counts")
    return SyncBatchResult(
        batch_id=batch.batch.batch_id,
        request_digest=batch.batch.request_digest,
        outcome="accepted",
        receipts=receipts,
    )


def _parse_receipts(value: Any, event_count: int, batch_event_ids: tuple[str, ...]) -> tuple["SyncReceipt", ...]:
    from memplex.sync_protocol import SyncReceipt

    if type(value) is not list:
        raise TypeError("inbound receipts must be an exact list")

    if len(value) != event_count:
        raise ValueError("inbound apply returned malformed receipts")

    parsed: list[SyncReceipt] = []
    ordered_event_ids: list[str] = []
    seen_event_ids = set()

    for item in value:
        if type(item) is not dict:
            raise TypeError("inbound receipt must be an exact dict")

        outcome = item.get("outcome")
        if type(outcome) is not str:
            raise TypeError("inbound receipt outcome must be a string")
        if outcome not in {"accepted", "duplicate", "rejected_conflict"}:
            raise ValueError("inbound receipt outcome is invalid")

        if outcome == "accepted":
            if set(item) != {"event_id", "outcome", "stream_seq"}:
                raise ValueError("inbound receipt has invalid keys")
            stream_seq = item.get("stream_seq")
            if type(stream_seq) is not int or stream_seq <= 0:
                raise TypeError("inbound accepted receipt stream_seq must be a positive int")
        else:
            if set(item) != {"event_id", "outcome"}:
                raise ValueError("inbound receipt has invalid keys")

        if type(item["event_id"]) is not str:
            raise TypeError("inbound receipt event_id must be a string")
        ordered_event_ids.append(item["event_id"])
        if item["event_id"] in seen_event_ids:
            raise ValueError("inbound apply returned duplicate receipt event ids")
        seen_event_ids.add(item["event_id"])
        parsed.append(SyncReceipt(item["event_id"], outcome))

    if ordered_event_ids != list(batch_event_ids):
        raise ValueError("inbound apply returned malformed receipt order")

    return tuple(parsed)


def _require_non_negative_int(value: Any) -> int:
    if type(value) is not int:
        raise TypeError("inbound apply returned malformed count")
    if value < 0:
        raise ValueError("inbound apply returned malformed count")
    return value
