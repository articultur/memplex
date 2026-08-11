"""G004 冻结的持久同步仓储边界。

本模块只声明协议；具体 PostgreSQL 与 Lite 实现在后续任务提供。
"""

from __future__ import annotations

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
