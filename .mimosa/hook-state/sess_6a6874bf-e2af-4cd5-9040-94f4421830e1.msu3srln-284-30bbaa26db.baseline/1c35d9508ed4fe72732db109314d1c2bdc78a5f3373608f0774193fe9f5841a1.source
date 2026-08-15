"""G004 冻结的持久同步仓储边界。

本模块只声明协议；具体 PostgreSQL 与 Lite 实现在后续任务提供。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from memplex.sync_ingress import ValidatedIngressBatch
    from memplex.sync_protocol import (
        SyncApplyResult,
        SyncBatch,
        SyncBatchResult,
        SyncCursorClaims,
        SyncDelivery,
        SyncEvent,
        SyncPage,
        SyncReceipt,
        SyncSnapshotPage,
        SyncStatus,
    )


@dataclass(frozen=True, slots=True)
class SyncCapturePolicy:
    """Per-connection policy for local durable-sync outbox capture."""

    mode: Literal["off", "required"]
    local_node_id: str = ""

    def __post_init__(self) -> None:
        if type(self.mode) is not str:
            raise TypeError("SyncCapturePolicy.mode must be exact type str")
        if type(self.local_node_id) is not str:
            raise TypeError(
                "SyncCapturePolicy.local_node_id must be exact type str"
            )
        if self.mode not in {"off", "required"}:
            raise ValueError("SyncCapturePolicy.mode must be 'off' or 'required'")
        if self.mode == "required" and not self.local_node_id:
            raise ValueError(
                "SyncCapturePolicy.local_node_id must be non-empty when mode is required"
            )
        if self.mode == "required":
            local_node_id = self.local_node_id.strip()
            if not local_node_id:
                raise ValueError(
                    "SyncCapturePolicy.local_node_id must be non-empty after strip"
                )
            object.__setattr__(self, "local_node_id", local_node_id)


class SyncBackpressureError(RuntimeError):
    """同步持久队列超过显式配置上限。"""


class SyncCursorExpired(ValueError):
    """所有外部 cursor 验证失败的固定、不透明错误。"""


class SyncBatchRejected(ValueError):
    """批次在任何持久化操作前被协议层拒绝。"""


class SyncDeliveryBusy(RuntimeError):
    """投递仍由有效 lease 占用。"""


def validate_incoming_page(
    page: SyncPage, *, tenant_id: str
) -> tuple[SyncEvent, ...]:
    """在入站写入前验证一个完整的远端 changes page。

    调用方必须在打开 mutation transaction 前调用本函数；它只读取 ``page``
    并返回已验证事件。Lite 与 PostgreSQL 共享此入口，以保证跨租户、同页
    ``(origin_node_id, event_id)`` 重复，以及伪造的 cursor/snapshot 连续性
    都不会进入任一后端的持久化路径。
    """
    # Delayed import avoids the sync_protocol -> sync_repository exception
    # dependency during module initialization.
    from memplex.sync_protocol import SyncEvent as ProtocolSyncEvent
    from memplex.sync_protocol import SyncPage as ProtocolSyncPage
    from memplex.sync_protocol import SyncStreamItem

    if type(tenant_id) is not str:
        raise TypeError("tenant_id must be an exact str")
    if not tenant_id:
        raise ValueError("tenant_id must be non-empty")
    if type(page) is not ProtocolSyncPage:
        raise TypeError("page must be an exact SyncPage")
    if type(page.items) is not tuple:
        raise TypeError("page items must be a tuple")
    if type(page.snapshot_seq) is not int:
        raise TypeError("page snapshot_seq must be an exact int")
    if type(page.next_after_seq) is not int:
        raise TypeError("page next_after_seq must be an exact int")
    if type(page.has_more) is not bool:
        raise TypeError("page has_more must be bool")
    if page.snapshot_seq < 0 or page.next_after_seq < 0:
        raise ValueError("page cursor sequences must be non-negative")

    previous_seq = 0
    identities: set[tuple[str, str]] = set()
    events: list[SyncEvent] = []
    for item in page.items:
        if type(item) is not SyncStreamItem:
            raise TypeError("page items must be exact SyncStreamItem")
        if type(item.stream_seq) is not int:
            raise TypeError("page stream_seq must be an exact int")
        if type(item.event) is not ProtocolSyncEvent:
            raise TypeError("page event must be an exact SyncEvent")
        if item.stream_seq <= previous_seq or item.stream_seq > page.snapshot_seq:
            raise ValueError("page stream items must be strictly ordered within snapshot")
        if item.event.scope.tenant_id != tenant_id:
            raise ValueError("page event tenant does not match repository tenant")
        identity = (item.event.origin_node_id, item.event.event_id)
        if identity in identities:
            raise ValueError("page contains duplicate event identities")
        identities.add(identity)
        events.append(item.event)
        previous_seq = item.stream_seq

    if page.has_more:
        if not page.items or page.next_after_seq != previous_seq:
            raise ValueError("page cursor must continue from its final item")
    elif page.next_after_seq != page.snapshot_seq:
        raise ValueError("complete page cursor must advance to snapshot sequence")
    return tuple(events)


@dataclass(frozen=True, slots=True)
class SyncDeadLetterEntry:
    """脱敏后的持久死信记录。"""

    target_id: str
    event_id: str
    attempt: int
    error_code: str

    def __post_init__(self) -> None:
        for name in ("target_id", "event_id", "error_code"):
            value = getattr(self, name)
            if type(value) is not str or not value:
                raise ValueError(f"{name} must be a non-empty exact str")
        if type(self.attempt) is not int or self.attempt < 1:
            raise ValueError("attempt must be a positive exact int")

    def to_dict(self) -> dict[str, object]:
        return {
            "target_id": self.target_id,
            "event_id": self.event_id,
            "attempt": self.attempt,
            "error_code": self.error_code,
        }


@runtime_checkable
class VerifiedInboundExecutor(Protocol):
    """执行已验证入站 envelope 的最小公开入口。"""

    def apply(self, batch: ValidatedIngressBatch) -> SyncBatchResult: ...


@runtime_checkable
class SyncRepository(Protocol):
    """可靠同步实现必须满足的最小原子操作集。"""

    def sync_page(
        self,
        remote_id: str,
        consumer_id: str,
        cursor: SyncCursorClaims | None,
        limit: int,
    ) -> SyncPage: ...

    def sync_create_snapshot(
        self,
        remote_id: str,
        consumer_id: str,
        request_id: str,
        limit: int,
    ) -> SyncSnapshotPage: ...

    def sync_snapshot_page(
        self,
        remote_id: str,
        consumer_id: str,
        cursor: SyncCursorClaims,
        limit: int,
    ) -> SyncSnapshotPage: ...

    def sync_apply_batch(self, batch: SyncBatch) -> SyncBatchResult: ...

    def sync_apply_page(self, remote_id: str, page: SyncPage) -> SyncApplyResult: ...

    def sync_register_target(self, target_id: str, *, bootstrap: str = "future") -> None: ...

    def sync_claim(
        self,
        target_id: str,
        *,
        limit: int,
        lease_seconds: int,
    ) -> list[SyncDelivery]: ...

    def sync_ack(self, delivery: SyncDelivery, receipt: SyncReceipt) -> None: ...

    def sync_ack_batch(
        self,
        deliveries: list[SyncDelivery],
        receipts: tuple[SyncReceipt, ...],
    ) -> None: ...

    def sync_fail(self, delivery: SyncDelivery, error_code: str, now: datetime) -> None: ...

    def sync_dead_letter(
        self, delivery: SyncDelivery, error_code: str, now: datetime
    ) -> None: ...

    def sync_replay_dead_letter(self, target_id: str, event_id: str) -> bool: ...

    def sync_list_dead_letters(self, *, limit: int) -> list[SyncDeadLetterEntry]: ...

    def sync_set_target_enabled(self, target_id: str, enabled: bool) -> None: ...

    def sync_compact(self, now: datetime, *, limit: int) -> int: ...

    def sync_status(self) -> SyncStatus: ...

    def sync_dispatch_status(self) -> SyncStatus: ...


class AbstractSyncRepository(ABC):
    """共享基类，用于两个具体的同步后端（Lite + PostgreSQL）。

    The concrete ``LiteSyncRepository`` and ``PostgresSyncRepository``
    implementations are kept in lockstep by hand: both must expose the same
    atomic sync operations. Inheriting this base makes that contract
    *enforced* -- Python rejects instantiation of any subclass that drops or
    renames one of these methods, so the two backends can no longer silently
    drift. Method signatures intentionally mirror the ``SyncRepository``
    Protocol above.
    """

    @abstractmethod
    def sync_page(
        self,
        remote_id: str,
        consumer_id: str,
        cursor: SyncCursorClaims | None,
        limit: int,
    ) -> SyncPage: ...

    @abstractmethod
    def sync_create_snapshot(
        self,
        remote_id: str,
        consumer_id: str,
        request_id: str,
        limit: int,
    ) -> SyncSnapshotPage: ...

    @abstractmethod
    def sync_snapshot_page(
        self,
        remote_id: str,
        consumer_id: str,
        cursor: SyncCursorClaims,
        limit: int,
    ) -> SyncSnapshotPage: ...

    @abstractmethod
    def sync_apply_batch(self, batch: SyncBatch) -> SyncBatchResult: ...

    @abstractmethod
    def sync_apply_page(self, remote_id: str, page: SyncPage) -> SyncApplyResult: ...

    @abstractmethod
    def sync_register_target(self, target_id: str, *, bootstrap: str = "future") -> None: ...

    @abstractmethod
    def sync_claim(
        self,
        target_id: str,
        *,
        limit: int,
        lease_seconds: int,
    ) -> list[SyncDelivery]: ...

    @abstractmethod
    def sync_ack(self, delivery: SyncDelivery, receipt: SyncReceipt) -> None: ...

    @abstractmethod
    def sync_ack_batch(
        self,
        deliveries: list[SyncDelivery],
        receipts: tuple[SyncReceipt, ...],
    ) -> None: ...

    @abstractmethod
    def sync_fail(self, delivery: SyncDelivery, error_code: str, now: datetime) -> None: ...

    @abstractmethod
    def sync_dead_letter(
        self, delivery: SyncDelivery, error_code: str, now: datetime
    ) -> None: ...

    @abstractmethod
    def sync_replay_dead_letter(self, target_id: str, event_id: str) -> bool: ...

    @abstractmethod
    def sync_list_dead_letters(self, *, limit: int) -> list[SyncDeadLetterEntry]: ...

    @abstractmethod
    def sync_set_target_enabled(self, target_id: str, enabled: bool) -> None: ...

    @abstractmethod
    def sync_compact(self, now: datetime, *, limit: int) -> int: ...

    @abstractmethod
    def sync_status(self) -> SyncStatus: ...

    @abstractmethod
    def sync_dispatch_status(self) -> SyncStatus: ...
