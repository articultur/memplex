"""Shared, explicitly owned PostgreSQL business-connection resources.

Schema migration uses short administrative connections in the migration runner.
Business stores use this module only after that runner has accepted the
catalogue and negotiated optional vector capability.
"""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from threading import Condition, RLock
from typing import Any, Callable, Iterator, Literal
from uuid import uuid4

from memplex.auth import AuthorizationContext
from memplex.storage.migrations import (
    MigrationIntegrityError,
    PostgresApplicationPrincipal,
    PostgresMigrationRunner,
    PostgresTargetIdentity,
    inspect_postgres_connection_target,
)
from memplex.storage.migrations.runner import (
    _APPLICATION_ACL,
    _APPLICATION_ACL_TABLES,
    VectorCapabilityRequest,
    VectorCapabilityStatus,
)

BindScope = Callable[[Any, AuthorizationContext], None]


def _new_migration_runner(dsn: str) -> PostgresMigrationRunner:
    """Create one private readiness collaborator from a resource-owned DSN.

    Tests may replace this module-private constructor to model database
    outcomes.  It is not a public injection point: normal callers cannot
    supply executable migration behavior to ``ensure_ready``.  Deliberate
    same-process monkeypatching of private module memory is outside this
    capability boundary.
    """
    return PostgresMigrationRunner(dsn)


def _target_key(
    target: PostgresTargetIdentity,
) -> tuple[str | None, int | None, str, str]:
    """Return the native fields that identify a resolved PostgreSQL target."""
    if type(target) is not PostgresTargetIdentity:
        raise TypeError("target must be an exact PostgresTargetIdentity")
    if (
        type(target.database) is not str
        or not target.database
        or type(target.schema) is not str
        or not target.schema
        or (
            target.server_address is not None
            and (type(target.server_address) is not str or not target.server_address)
        )
        or (
            target.server_port is not None
            and (
                type(target.server_port) is not int
                or not 1 <= target.server_port <= 65_535
            )
        )
    ):
        raise MigrationIntegrityError("PostgreSQL target identity is invalid")
    return (target.server_address, target.server_port, target.database, target.schema)


class ResourceState(str, Enum):
    """Lifecycle of the service-owned PostgreSQL business resources."""

    NEW = "NEW"
    INITIALIZING = "INITIALIZING"
    READY = "READY"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    FAULTED = "FAULTED"


_READY_POOL_ISSUER = object()
_READY_POOL_AUTHORITY_LOCK = RLock()


@dataclass(frozen=True, slots=True)
class _ReadyPoolAuthority:
    """Snapshot of a seal that a live Resource instance has published."""

    seal: "ReadyPostgresPool"
    manager: "PostgresPoolManager"
    request: VectorCapabilityRequest
    request_dim: int
    request_policy: str
    status: VectorCapabilityStatus
    status_state: str
    status_dim: int
    status_digest: str | None
    effective_dim: int
    target: PostgresTargetIdentity
    target_key: tuple[str | None, int | None, str, str]


# A strong identity map is intentional.  A published seal is owned by a
# service resource for its entire useful lifetime, and revocation removes it
# eagerly on close/fault.  Keying by ``id`` plus the exact object prevents
# equality hooks, weak-reference lifetime and id-reuse from acting as
# authority.
_READY_POOL_AUTHORITIES: dict[int, _ReadyPoolAuthority] = {}


class ReadyPostgresPool:
    """Opaque capability issued only after migration/capability validation.

    Business stores receive this seal rather than a raw pool manager.  The
    private issuer prevents a caller from bypassing the readiness contract by
    assembling a manager, vector dimension and status independently.
    """

    __slots__ = (
        "manager",
        "request",
        "status",
        "effective_dim",
        "target",
        "_sealed",
    )

    manager: PostgresPoolManager
    request: VectorCapabilityRequest
    status: VectorCapabilityStatus
    effective_dim: int
    target: PostgresTargetIdentity
    _sealed: bool

    def __init__(
        self,
        *,
        manager: "PostgresPoolManager",
        request: VectorCapabilityRequest,
        status: VectorCapabilityStatus,
        effective_dim: int,
        target: PostgresTargetIdentity,
        issuer: object,
    ) -> None:
        if issuer is not _READY_POOL_ISSUER:
            raise TypeError("ReadyPostgresPool is issued by PostgresStorageResources only")
        _target_key(target)
        object.__setattr__(self, "manager", manager)
        object.__setattr__(self, "request", request)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "effective_dim", effective_dim)
        # Task 2 Fix-7 supplies the concrete target identity.  Retain an
        # explicit placeholder so the seal's trust boundary is stable now.
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: Any) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("ReadyPostgresPool is immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        raise AttributeError("ReadyPostgresPool is immutable")


def _publish_ready_pool_authority(seal: ReadyPostgresPool) -> None:
    """Register only a fully published Resource-issued seal as authority."""
    if type(seal) is not ReadyPostgresPool:
        raise TypeError("ready pool seal must have the exact ReadyPostgresPool type")
    manager = seal.manager
    request = seal.request
    status = seal.status
    target = seal.target
    if (
        type(manager) is not PostgresPoolManager
        or type(request) is not VectorCapabilityRequest
        or type(status) is not VectorCapabilityStatus
        or type(seal.effective_dim) is not int
        or type(request.dim) is not int
        or type(request.policy) is not str
        or type(status.state) is not str
        or type(status.dim) is not int
        or (status.parameter_digest is not None and type(status.parameter_digest) is not str)
    ):
        raise TypeError("ready pool seal has invalid authority fields")
    target_key = _target_key(target)
    authority = _ReadyPoolAuthority(
        seal=seal,
        manager=manager,
        request=request,
        request_dim=request.dim,
        request_policy=request.policy,
        status=status,
        status_state=status.state,
        status_dim=status.dim,
        status_digest=status.parameter_digest,
        effective_dim=seal.effective_dim,
        target=target,
        target_key=target_key,
    )
    with _READY_POOL_AUTHORITY_LOCK:
        _READY_POOL_AUTHORITIES[id(seal)] = authority


def _revoke_ready_pool_authority(seal: ReadyPostgresPool | None) -> None:
    """Remove a seal immediately when its owning resources cease readiness."""
    if seal is None:
        return
    with _READY_POOL_AUTHORITY_LOCK:
        authority = _READY_POOL_AUTHORITIES.get(id(seal))
        if authority is not None and authority.seal is seal:
            del _READY_POOL_AUTHORITIES[id(seal)]


def validate_ready_postgres_pool(value: object) -> ReadyPostgresPool:
    """Return an exact, currently-published, internally consistent seal.

    This is the public construction boundary, not a hostile-process security
    sandbox: code with deliberate access to this module's private registry can
    subvert Python process memory.  Normal callers cannot manufacture a
    service-issued capability from a class instance or by mutating a seal.
    """
    if type(value) is not ReadyPostgresPool:
        raise TypeError("Postgres backend requires a resource-issued ReadyPostgresPool")
    seal = value
    with _READY_POOL_AUTHORITY_LOCK:
        authority = _READY_POOL_AUTHORITIES.get(id(seal))
        if authority is None or authority.seal is not seal:
            raise TypeError("Postgres backend requires a resource-issued ReadyPostgresPool")
        if (
            type(seal.manager) is not PostgresPoolManager
            or seal.manager is not authority.manager
            or type(seal.request) is not VectorCapabilityRequest
            or seal.request is not authority.request
            or type(seal.status) is not VectorCapabilityStatus
            or seal.status is not authority.status
            or type(seal.effective_dim) is not int
            or seal.effective_dim != authority.effective_dim
            or type(seal.request.dim) is not int
            or seal.request.dim != authority.request_dim
            or type(seal.request.policy) is not str
            or seal.request.policy != authority.request_policy
            or type(seal.status.state) is not str
            or seal.status.state != authority.status_state
            or type(seal.status.dim) is not int
            or seal.status.dim != authority.status_dim
            or (
                seal.status.parameter_digest is not None
                and type(seal.status.parameter_digest) is not str
            )
            or seal.status.parameter_digest != authority.status_digest
        ):
            raise TypeError("Postgres backend requires a resource-issued ReadyPostgresPool")
        try:
            current_target_key = _target_key(seal.target)
        except (MigrationIntegrityError, TypeError) as exc:
            raise TypeError(
                "Postgres backend requires a resource-issued ReadyPostgresPool"
            ) from exc
        if (
            seal.target is not authority.target
            or current_target_key != authority.target_key
            or seal.manager._expected_target is not authority.target
            or seal.manager._expected_target_key != authority.target_key
            or not seal.manager._accepting_business_leases()
        ):
            raise TypeError("Postgres backend requires a resource-issued ReadyPostgresPool")
    return seal


class PooledReadCursor:
    """A read cursor whose lease is released exactly once on every exit path."""

    def __init__(
        self, manager: "PostgresPoolManager", token: object, connection: Any, cursor: Any
    ) -> None:
        self._manager = manager
        self._token = token
        self._connection = connection
        self._cursor = cursor
        self._state = "OPEN"
        self._rolled_back = False
        self._cursor_closed = False
        self._returned = False
        self._lock = RLock()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)

    def _fetch(self, method: str, *args: Any) -> Any:
        try:
            result = getattr(self._cursor, method)(*args)
        except BaseException as exc:
            primary_error = exc
            primary_traceback = exc.__traceback__
            try:
                try:
                    self.close()
                except BaseException:
                    pass
                finally:
                    try:
                        self.close()
                    except BaseException:
                        pass
            except BaseException:
                pass
            raise primary_error.with_traceback(primary_traceback)
        self.close()
        return result

    def fetchone(self) -> Any:
        return self._fetch("fetchone")

    def fetchmany(self, size: int | None = None) -> Any:
        return self._fetch("fetchmany") if size is None else self._fetch("fetchmany", size)

    def fetchall(self) -> Any:
        return self._fetch("fetchall")

    def close(self) -> None:
        """Release every resource, remaining retryable across BaseException.

        ``CLOSING`` is intentionally not terminal.  An interruption after one
        cleanup phase leaves the cursor retryable; a subsequent close resumes
        the remaining phases instead of treating an unreturned connection as
        closed.
        """
        with self._lock:
            if self._returned:
                self._state = "CLOSED"
                return
            self._state = "CLOSING"
            cleanup_error: BaseException | None = None
            try:
                if not self._rolled_back:
                    try:
                        self._connection.rollback()
                        self._rolled_back = True
                    except BaseException as exc:
                        self._manager._mark_fault(exc)
                        cleanup_error = exc
                if not self._cursor_closed:
                    try:
                        self._cursor.close()
                        self._cursor_closed = True
                    except BaseException as exc:
                        self._manager._mark_fault(exc)
                        cleanup_error = cleanup_error or exc
                if not self._returned:
                    try:
                        self._manager.release(self._token, rollback=False)
                        self._returned = True
                    except BaseException as exc:
                        cleanup_error = cleanup_error or exc
            finally:
                self._state = "CLOSED" if self._returned else "OPEN"
            if cleanup_error is not None:
                raise cleanup_error


class _BorrowCapacityReservation:
    """Exception-safe hand-off from FIFO capacity to a business lease.

    A reservation owns every pre-publication state transition.  Until
    ``publish`` is called, it can always unwind a queued ticket, a capacity
    slot, or a checked-out driver connection without letting asynchronous
    exceptions strand later borrowers.
    """

    def __init__(self, manager: "PostgresPoolManager") -> None:
        self._manager = manager
        self._token = object()
        self._connection: Any | None = None

    def __enter__(self) -> "_BorrowCapacityReservation":
        try:
            self._manager._reserve_capacity(self._token)
            return self
        except BaseException:
            self._manager.release(self._token)
            raise

    def __exit__(self, exc_type: object, _value: object, _traceback: object) -> None:
        # A normal ``return`` through the context is the only safe hand-off
        # point for a cursor handle.  If tracing/cancellation interrupts the
        # return boundary, ``exc_type`` is populated and this reservation
        # remains responsible for closing the otherwise-unreachable handle.
        if exc_type is None:
            try:
                self._manager.commit_publish(self._token)
            except BaseException as exc:
                primary_error = exc
                primary_traceback = exc.__traceback__
                try:
                    try:
                        self._manager.release(self._token)
                    except BaseException:
                        pass
                    finally:
                        try:
                            self._manager.release(self._token)
                        except BaseException:
                            pass
                except BaseException:
                    pass
                raise primary_error.with_traceback(primary_traceback)
        else:
            try:
                try:
                    self._manager.release(self._token)
                except BaseException:
                    pass
                finally:
                    try:
                        self._manager.release(self._token)
                    except BaseException:
                        pass
            except BaseException:
                # The body/return interruption is primary; a driver cleanup
                # fault is recorded by the manager but must not mask it.
                pass

    def borrow(self) -> Any:
        """Acquire and validate one connection under this reservation."""
        connection: Any | None = None
        try:
            connection = self._manager._pool.getconn()
            self._connection = connection
            self._manager._attach_borrowed_connection(self._token, connection)
            self._manager._verify_borrowed_connection_target(connection)
            self._manager._register_checked_out(self._token, connection)
            return connection
        except BaseException:
            # The local connection exists before record attachment.  Keep two
            # independent manager releases so a trace/cancellation at either
            # call boundary cannot replace the borrow primary or strand it.
            try:
                self._manager.release(self._token, fallback_connection=connection)
            except BaseException:
                pass
            finally:
                try:
                    self._manager.release(self._token, fallback_connection=connection)
                except BaseException:
                    pass
            raise

    def publish(self, connection: Any) -> None:
        """Atomically convert a validated checkout into business demand."""
        self._manager.mark_handoff_ready(self._token, connection)

    def _commit_handoff(self) -> None:
        """Record peak demand only once a handle crossed its return boundary."""
        self._manager.commit_publish(self._token)

    def release(self) -> None:
        """Return a non-business checkout after an internal readiness probe."""
        self._manager.release(self._token, rollback=False)


class _LeaseState(str, Enum):
    QUEUED = "QUEUED"
    RESERVED = "RESERVED"
    CHECKED_OUT = "CHECKED_OUT"
    PUBLISHED = "PUBLISHED"
    RETURNING = "RETURNING"
    RETURNED = "RETURNED"


@dataclass(slots=True)
class _LeaseRecord:
    state: _LeaseState
    connection: Any | None = None
    handoff_ready: bool = False


@dataclass(slots=True)
class _ReservationCleanupState:
    """Idempotence markers shared by duplicate interrupted cleanup passes."""

    rolled_back: bool = False
    cursor_closed: bool = False


class _TransactionContext:
    """Explicit transaction context with an exception-safe enter hand-off."""

    def __init__(
        self, manager: "PostgresPoolManager", bind_scope: BindScope, context: AuthorizationContext
    ) -> None:
        self._manager = manager
        self._bind_scope = bind_scope
        self._context = context
        self._reservation_context: Any | None = None
        self._reservation: _BorrowCapacityReservation | None = None
        self._connection: Any | None = None
        self._cursor: Any | None = None
        self._closed_cursor_ids: set[int] = set()

    def __enter__(self) -> tuple[Any, Any]:
        reservation_context: Any | None = None
        reservation: _BorrowCapacityReservation | None = None
        connection: Any | None = None
        cursor: Any | None = None
        try:
            reservation_context = self._manager._borrow_capacity_reservation()
            reservation = reservation_context.__enter__()
            self._reservation_context = reservation_context
            self._reservation = reservation
            connection = reservation.borrow()
            cursor = connection.cursor()
            self._connection = connection
            self._cursor = cursor
            self._bind_scope(cursor, self._context)
            reservation.publish(connection)
            return connection, cursor
        except BaseException as exc:
            primary_error = exc
            primary_traceback = exc.__traceback__
            # Every cleanup phase has a second route in a nested ``finally``.
            # A trace/cancellation at one phase gate therefore cannot skip the
            # cursor close or manager-authoritative release that follows it.
            try:
                try:
                    self._close_pre_enter_cursor(cursor)
                except BaseException:
                    pass
                finally:
                    try:
                        if reservation_context is not None:
                            try:
                                reservation_context.__exit__(type(exc), exc, exc.__traceback__)
                            except BaseException:
                                pass
                    finally:
                        try:
                            self._close_pre_enter_cursor(cursor)
                        except BaseException:
                            pass
                        finally:
                            try:
                                self._release_pre_enter_fallback(reservation, connection)
                            except BaseException:
                                pass
                            finally:
                                try:
                                    self._release_pre_enter_fallback(reservation, connection)
                                except BaseException:
                                    pass
            except BaseException:
                # The first failure is already the semantic cause.  A traced
                # cleanup gate is secondary after the nested fallbacks ran.
                pass
            raise primary_error.with_traceback(primary_traceback)

    def __exit__(self, exc_type: object, _value: object, _traceback: object) -> Literal[False]:
        assert self._reservation is not None
        primary_error = _value if isinstance(_value, BaseException) else None
        cleanup_error: BaseException | None = None
        try:
            # Enter completed, so every body outcome (including BaseException)
            # is a real business hand-off and must retain historical demand.
            self._reservation._commit_handoff()
            if exc_type is None:
                assert self._connection is not None
                self._connection.commit()
            else:
                assert self._connection is not None
                self._connection.rollback()
        except BaseException as exc:
            cleanup_error = exc
            self._manager._mark_fault(exc)
        finally:
            # Keep cleanup in nested finally blocks.  A single BaseException
            # injected at any call/gate still falls through to another cursor
            # close attempt and two manager-authoritative release attempts.
            try:
                cleanup_error = self._close_transaction_cursor(cleanup_error)
            except BaseException as exc:
                cleanup_error = cleanup_error or exc
            finally:
                try:
                    if self._reservation_context is not None:
                        self._reservation_context.__exit__(None, None, None)
                except BaseException as exc:
                    cleanup_error = cleanup_error or exc
                finally:
                    try:
                        cleanup_error = self._close_transaction_cursor(cleanup_error)
                    except BaseException as exc:
                        cleanup_error = cleanup_error or exc
                    finally:
                        try:
                            self._release_transaction_fallback()
                        except BaseException as exc:
                            cleanup_error = cleanup_error or exc
                        finally:
                            try:
                                self._release_transaction_fallback()
                            except BaseException as exc:
                                cleanup_error = cleanup_error or exc
        if primary_error is None and cleanup_error is not None:
            raise cleanup_error
        return False

    def _close_pre_enter_cursor(self, cursor: Any | None) -> None:
        """Best-effort local cursor cleanup before a failed ``__enter__``."""
        try:
            if cursor is not None and id(cursor) not in self._closed_cursor_ids:
                cursor.close()
                self._closed_cursor_ids.add(id(cursor))
        except BaseException as exc:
            self._manager._mark_fault(exc)

    def _release_pre_enter_fallback(
        self, reservation: _BorrowCapacityReservation | None, connection: Any | None
    ) -> None:
        """Return an enter-local connection even if ownership assignment lost."""
        if reservation is not None:
            self._manager.release(
                reservation._token, fallback_connection=connection
            )

    def _close_transaction_cursor(
        self, cleanup_error: BaseException | None
    ) -> BaseException | None:
        """Close at most once after normal transaction ownership is known."""
        try:
            if self._cursor is not None:
                cursor = self._cursor
                cursor.close()
                self._cursor = None
        except BaseException as exc:
            self._manager._mark_fault(exc)
            return cleanup_error or exc
        return cleanup_error

    def _release_transaction_fallback(self) -> None:
        """Use manager state, not context-local flags, as final cleanup owner."""
        assert self._reservation is not None
        self._manager.release(
            self._reservation._token,
            rollback=False,
            fallback_connection=self._connection,
        )


class PostgresPoolManager:
    """One bounded pool shared by all stores in a ready storage resource."""

    def __init__(
        self,
        dsn: str,
        *,
        min_connections: int = 1,
        max_connections: int = 8,
        pool: Any | None = None,
        on_closed: Callable[[BaseException | None], None] | None = None,
        on_fault: Callable[[BaseException], None] | None = None,
        expected_target: PostgresTargetIdentity | None = None,
        expected_application_principal: PostgresApplicationPrincipal | None = None,
        deployment_profile: str | None = None,
    ) -> None:
        if type(min_connections) is not int or type(max_connections) is not int:
            raise TypeError("PostgreSQL pool bounds must be exact int values")
        if min_connections < 1 or max_connections < min_connections:
            raise ValueError("invalid PostgreSQL pool bounds")
        if pool is None:
            try:
                from psycopg2.pool import ThreadedConnectionPool  # type: ignore
            except ImportError as exc:
                raise ImportError("PostgresPoolManager requires psycopg2") from exc
            pool = ThreadedConnectionPool(min_connections, max_connections, dsn)
        self._pool = pool
        self._condition = Condition(RLock())
        self._min_connections = min_connections
        self._max_connections = max_connections
        # Checked-out connections reserve capacity even before product SQL is
        # allowed.  Published business leases are a deliberately narrower
        # metric: readiness probes and failed scope binding must never be
        # counted as product demand.
        self._checked_out: set[int] = set()
        self._business_leases: set[int] = set()
        self._lease_records: dict[object, _LeaseRecord] = {}
        self._business_lease_high_watermark = 0
        self._returning: set[int] = set()
        self._closed = False
        self._close_requested = False
        self._fault: BaseException | None = None
        self._waiting_borrowers = 0
        self._borrow_queue: deque[object] = deque()
        self._closing_in_progress = False
        self._physical_closed = False
        self._close_error: BaseException | None = None
        self._on_closed = on_closed
        self._on_fault = on_fault
        self._expected_target = expected_target
        self._expected_target_key = (
            None if expected_target is None else _target_key(expected_target)
        )
        if expected_application_principal is not None and type(expected_application_principal) is not PostgresApplicationPrincipal:
            raise TypeError("expected application principal must be exact PostgresApplicationPrincipal")
        if deployment_profile is not None and deployment_profile not in {"development", "production"}:
            raise ValueError("invalid PostgreSQL deployment profile")
        self._expected_application_principal = expected_application_principal
        self._deployment_profile = deployment_profile

    def _mark_fault(self, error: BaseException) -> None:
        callback: Callable[[BaseException], None] | None = None
        with self._condition:
            if self._fault is None:
                self._fault = error
                callback = self._on_fault
            self._condition.notify_all()
        if callback is not None:
            try:
                callback(error)
            except BaseException:
                # A resource callback only mirrors an already-terminal pool
                # fault.  It must not replace the primary database failure.
                pass

    @property
    def business_lease_count(self) -> int:
        with self._condition:
            return len(self._business_leases)

    @property
    def min_connections(self) -> int:
        """Configured lower connection bound, retained for safe diagnostics."""
        return self._min_connections

    @property
    def max_connections(self) -> int:
        """Configured upper connection bound, retained for safe diagnostics."""
        return self._max_connections

    @property
    def business_lease_high_watermark(self) -> int:
        """Largest count of validated, published business leases so far.

        This is intentionally historical: terminal close/fault state must not
        erase the only local evidence of peak pool demand.
        """
        with self._condition:
            # Provisional transaction hand-offs contribute while their body is
            # live; an interrupted pre-yield hand-off is removed before this
            # property can retain history.
            return max(self._business_lease_high_watermark, len(self._business_leases))

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    def _accepting_business_leases(self) -> bool:
        with self._condition:
            return not (
                self._closed or self._close_requested or self._fault is not None
            )

    def _take_finalizer_locked(self) -> bool:
        """Reserve the one physical close once shutdown has become idle."""
        if (
            self._close_requested
            and not self._closed
            and not self._checked_out
            and not self._returning
            and not self._waiting_borrowers
            and not self._borrow_queue
            and not self._closing_in_progress
        ):
            self._closed = True
            self._closing_in_progress = True
            return True
        return False

    def _finish_physical_close(self) -> None:
        close_error: BaseException | None = None
        try:
            self._pool.closeall()
        except BaseException as exc:
            close_error = exc
            self._mark_fault(exc)
            with self._condition:
                self._close_error = exc
        finally:
            with self._condition:
                if self._close_error is None:
                    self._physical_closed = True
                self._closing_in_progress = False
                self._condition.notify_all()
        if self._on_closed is not None:
            try:
                self._on_closed(close_error)
            except BaseException as exc:
                self._mark_fault(exc)
                if close_error is None:
                    close_error = exc
        if close_error is not None:
            raise close_error

    def _finalize_if_idle(self) -> None:
        with self._condition:
            should_finalize = self._take_finalizer_locked()
        if should_finalize:
            self._finish_physical_close()

    def _closed_result_locked(self) -> bool:
        while self._closing_in_progress:
            self._condition.wait()
        if self._close_error is not None:
            raise self._close_error
        if self._fault is not None:
            failure = RuntimeError("PostgreSQL pool is faulted")
            failure.__cause__ = self._fault
            raise failure
        return True

    def _verify_borrowed_connection_target(self, connection: Any) -> None:
        """Verify this exact business lease before scope binding or store SQL."""
        expected_key = self._expected_target_key
        principal = self._expected_application_principal
        if expected_key is None and principal is None:
            return
        cursor = None
        primary_error: BaseException | None = None
        cleanup_error: BaseException | None = None
        try:
            cursor = connection.cursor()
            if expected_key is not None:
                actual_target = inspect_postgres_connection_target(connection, cursor)
                if _target_key(actual_target) != expected_key:
                    raise MigrationIntegrityError(
                        "PostgreSQL pool target identity does not match expected target"
                    )
            if principal is not None:
                cursor.execute(
                    """
                    SELECT current_user, session_user, role.rolsuper, role.rolbypassrls
                    FROM pg_catalog.pg_roles role WHERE role.rolname=current_user
                    """
                )
                row = cursor.fetchone()
                if (
                    row is None
                    or len(row) != 4
                    or row[0] != principal.role
                    or row[1] != principal.session_role
                    or (
                        self._deployment_profile == "production"
                        and (bool(row[2]) or bool(row[3]))
                    )
                ):
                    raise MigrationIntegrityError(
                        "PostgreSQL pool application principal does not match readiness identity"
                    )
        except BaseException as exc:
            primary_error = exc
            self._mark_fault(exc)
            raise
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except BaseException as exc:
                    self._mark_fault(exc)
                    cleanup_error = exc
            if primary_error is None and cleanup_error is not None:
                raise cleanup_error

    @contextmanager
    def _borrow_capacity_reservation(self) -> Iterator[_BorrowCapacityReservation]:
        """Own pre-publication capacity until a caller explicitly publishes."""
        with _BorrowCapacityReservation(self) as reservation:
            yield reservation

    def _notify_locked(self, *, suppress: bool = False) -> None:
        try:
            self._condition.notify_all()
        except BaseException:
            # State changes are complete before notifications.  Fault-injected
            # notifiers must not leave a manager record half transitioned.
            if not suppress:
                raise

    def _reserve_capacity(self, token: object) -> None:
        """Enter the authoritative FIFO state machine at RESERVED."""
        try:
            with self._condition:
                self._lease_records[token] = _LeaseRecord(_LeaseState.QUEUED)
                self._borrow_queue.append(token)
                while True:
                    if self._closed or self._close_requested or self._fault is not None:
                        raise RuntimeError("PostgreSQL pool is closed")
                    if (
                        self._borrow_queue[0] is token
                        and len(self._checked_out) + self._waiting_borrowers
                        < self._max_connections
                    ):
                        self._borrow_queue.popleft()
                        record = self._lease_records[token]
                        record.state = _LeaseState.RESERVED
                        self._waiting_borrowers += 1
                        self._notify_locked()
                        return
                    self._condition.wait()
        except BaseException:
            self.release(token)
            raise

    def _register_checked_out(self, token: object, connection: Any) -> None:
        with self._condition:
            record = self._lease_records.get(token)
            if (
                record is None
                or record.state is not _LeaseState.RESERVED
                or self._closed
                or self._close_requested
                or self._fault is not None
            ):
                raise RuntimeError("PostgreSQL pool is closed")
            record.connection = connection
            record.state = _LeaseState.CHECKED_OUT
            self._waiting_borrowers -= 1
            self._checked_out.add(id(connection))
            self._notify_locked()

    def _attach_borrowed_connection(self, token: object, connection: Any) -> None:
        with self._condition:
            record = self._lease_records.get(token)
            if record is None or record.state is not _LeaseState.RESERVED:
                raise RuntimeError("PostgreSQL pool is closed")
            record.connection = connection

    def mark_handoff_ready(self, token: object, connection: Any) -> None:
        with self._condition:
            record = self._lease_records.get(token)
            if (
                record is None
                or record.state is not _LeaseState.CHECKED_OUT
                or record.connection is not connection
                or self._closed
                or self._close_requested
                or self._fault is not None
            ):
                raise RuntimeError("PostgreSQL pool is closed")
            record.handoff_ready = True
            record.state = _LeaseState.PUBLISHED
            self._business_leases.add(id(connection))

    def commit_publish(self, token: object) -> None:
        """Atomically publish only after a normal return/yield hand-off."""
        previous_high_watermark: int | None = None
        try:
            with self._condition:
                record = self._lease_records.get(token)
                if record is None or record.state in {
                    _LeaseState.RETURNING,
                    _LeaseState.RETURNED,
                }:
                    return
                if record.state is not _LeaseState.PUBLISHED or not record.handoff_ready:
                    self.release(token)
                    return
                previous_high_watermark = self._business_lease_high_watermark
                self._business_lease_high_watermark = max(
                    self._business_lease_high_watermark, len(self._business_leases)
                )
                if self._business_lease_high_watermark < previous_high_watermark:
                    raise AssertionError("pool high-watermark regressed")
        except BaseException:
            if previous_high_watermark is not None:
                with self._condition:
                    self._business_lease_high_watermark = previous_high_watermark
            raise

    def release(
        self,
        token: object,
        *,
        rollback: bool = True,
        fallback_connection: Any | None = None,
    ) -> None:
        """Idempotently return one authoritative state-machine token."""
        connection: Any | None = None
        prior_state: _LeaseState | None = None
        put_attempted = False
        try:
            with self._condition:
                record = self._lease_records.get(token)
                if record is None or record.state is _LeaseState.RETURNED:
                    return
                if fallback_connection is not None:
                    if record.connection is None:
                        # ``getconn`` has returned, but a traced interruption
                        # may happen before the normal attach.  Make this
                        # authoritative record own the local connection before
                        # deciding which release path returns it.
                        record.connection = fallback_connection
                    elif record.connection is not fallback_connection:
                        mismatch = RuntimeError(
                            "PostgreSQL pool reservation connection mismatch"
                        )
                        self._mark_fault(mismatch)
                        raise mismatch
                if record.state is _LeaseState.QUEUED:
                    try:
                        self._borrow_queue.remove(token)
                    except ValueError:
                        pass
                    if record.connection is None:
                        record.state = _LeaseState.RETURNED
                        self._notify_locked(suppress=True)
                        self._lease_records.pop(token, None)
                        return
                    prior_state = _LeaseState.CHECKED_OUT
                if record.state is _LeaseState.RESERVED:
                    self._waiting_borrowers -= 1
                    prior_state = _LeaseState.CHECKED_OUT
                elif record.state is _LeaseState.RETURNING:
                    # Another release owns the driver call.  If it is
                    # interrupted, its finally restores a claimable state.
                    return
                elif record.state is _LeaseState.QUEUED:
                    # A local driver connection can only be attached to this
                    # branch by an exceptional pre-reservation hand-off.
                    # It still needs a physical return, but never counted as a
                    # checked-out business lease.
                    pass
                elif record.state not in {
                    _LeaseState.CHECKED_OUT,
                    _LeaseState.PUBLISHED,
                }:
                    return
                elif prior_state is None:
                    prior_state = record.state
                connection = record.connection
                if connection is None:
                    record.state = _LeaseState.RETURNED
                    self._lease_records.pop(token, None)
                    self._notify_locked(suppress=True)
                    return
                record.state = _LeaseState.RETURNING
            if rollback:
                try:
                    connection.rollback()
                except BaseException as exc:
                    self._mark_fault(exc)
            # Keep the attempt flag and driver call on one trace line.  A
            # trace/cancellation before this line remains retryable; once the
            # call has begun, even a driver error is at-most-once and logical
            # ownership is cleared in ``finally``.
            put_attempted = True; self._pool.putconn(connection)  # noqa: E702
        except BaseException as exc:
            self._mark_fault(exc)
            raise
        finally:
            if connection is not None:
                with self._condition:
                    record = self._lease_records.get(token)
                    if record is not None:
                        if put_attempted:
                            self._business_leases.discard(id(connection))
                            self._checked_out.discard(id(connection))
                            record.state = _LeaseState.RETURNED
                            self._lease_records.pop(token, None)
                        elif prior_state is not None:
                            # A trace/cancellation interruption happened
                            # before putconn completed; permit a later close
                            # to claim the same token and finish exactly once.
                            record.state = prior_state
                    self._notify_locked(suppress=True)
            # Callbacks may request non-waiting close while this token is
            # RETURNING.  Once the finally block clears that accounting, this
            # is the only reliable place to converge to physical close.  The
            # driver failure above remains the primary exception.
            try:
                self._finalize_if_idle()
            except BaseException:
                pass

    def _return_once(self, connection: Any) -> None:
        with self._condition:
            connection_id = id(connection)
            if connection_id not in self._checked_out or connection_id in self._returning:
                return
            # A lease must remain visible to concurrent close(wait=True)
            # callers until ThreadedConnectionPool.putconn has completed.
            self._returning.add(connection_id)
        return_error: BaseException | None = None
        try:
            self._pool.putconn(connection)
        except BaseException as exc:
            self._mark_fault(exc)
            return_error = exc
        finally:
            with self._condition:
                self._returning.discard(connection_id)
                self._business_leases.discard(connection_id)
                self._checked_out.discard(connection_id)
                self._condition.notify_all()
        try:
            self._finalize_if_idle()
        except BaseException:
            if return_error is None:
                raise
        if return_error is not None:
            raise return_error

    def transaction(
        self, bind_scope: BindScope, context: AuthorizationContext
    ) -> _TransactionContext:
        return _TransactionContext(self, bind_scope, context)

    def _cleanup_reservation_local_resources(
        self,
        connection: Any,
        cursor: Any | None,
        state: _ReservationCleanupState,
    ) -> BaseException | None:
        """Rollback and close once, even when a phase gate is interrupted."""
        cleanup_error: BaseException | None = None
        try:
            if not state.rolled_back:
                connection.rollback()
                state.rolled_back = True
        except BaseException as exc:
            self._mark_fault(exc)
            cleanup_error = exc
        try:
            if cursor is not None and not state.cursor_closed:
                cursor.close()
                state.cursor_closed = True
        except BaseException as exc:
            self._mark_fault(exc)
            cleanup_error = cleanup_error or exc
        return cleanup_error

    def _cleanup_reservation_with_fallback(
        self,
        reservation: _BorrowCapacityReservation,
        connection: Any,
        cursor: Any | None,
        state: _ReservationCleanupState,
    ) -> BaseException | None:
        """Run local cleanup twice before two idempotent manager releases."""
        cleanup_error: BaseException | None = None
        try:
            try:
                cleanup_error = self._cleanup_reservation_local_resources(
                    connection, cursor, state
                )
            except BaseException as exc:
                cleanup_error = exc
            finally:
                try:
                    retry_error = self._cleanup_reservation_local_resources(
                        connection, cursor, state
                    )
                    cleanup_error = cleanup_error or retry_error
                except BaseException as exc:
                    cleanup_error = cleanup_error or exc
                finally:
                    try:
                        reservation.release()
                    except BaseException as exc:
                        cleanup_error = cleanup_error or exc
                    finally:
                        try:
                            reservation.release()
                        except BaseException as exc:
                            cleanup_error = cleanup_error or exc
        except BaseException as exc:
            cleanup_error = cleanup_error or exc
        return cleanup_error

    def read_cursor(self, bind_scope: BindScope, context: AuthorizationContext) -> PooledReadCursor:
        with self._borrow_capacity_reservation() as reservation:
            connection = reservation.borrow()
            cursor = None
            try:
                cursor = connection.cursor()
                bind_scope(cursor, context)
                wrapped = PooledReadCursor(self, reservation._token, connection, cursor)
                reservation.publish(connection)
            except BaseException as exc:
                primary_error = exc
                primary_traceback = exc.__traceback__
                cleanup_state = _ReservationCleanupState()
                try:
                    try:
                        self._cleanup_reservation_with_fallback(
                            reservation, connection, cursor, cleanup_state
                        )
                    finally:
                        self._cleanup_reservation_with_fallback(
                            reservation, connection, cursor, cleanup_state
                        )
                except BaseException as exc:
                    # This is a secondary cleanup interruption.  The outer
                    # reservation context has an additional idempotent return
                    # path, and the failed bind/scope remains primary.
                    pass
                raise primary_error.with_traceback(primary_traceback)
            return wrapped

    def inspect_target(self) -> PostgresTargetIdentity:
        """Inspect the resolved target of one leased business connection."""
        with self._borrow_capacity_reservation() as reservation:
            connection = reservation.borrow()
            cursor = None
            primary_error: BaseException | None = None
            cleanup_error: BaseException | None = None
            cleanup_state = _ReservationCleanupState()
            try:
                # Target validation already ran for every target-bound pool.
                if self._expected_target is not None:
                    return self._expected_target
                cursor = connection.cursor()
                return inspect_postgres_connection_target(connection, cursor)
            except BaseException as exc:
                primary_error = exc
                raise
            finally:
                try:
                    cleanup_error = self._cleanup_reservation_with_fallback(
                        reservation, connection, cursor, cleanup_state
                    )
                except BaseException as exc:
                    cleanup_error = exc
                finally:
                    try:
                        retry_error = self._cleanup_reservation_with_fallback(
                            reservation, connection, cursor, cleanup_state
                        )
                        cleanup_error = cleanup_error or retry_error
                    except BaseException as exc:
                        cleanup_error = cleanup_error or exc
                if primary_error is None and cleanup_error is not None:
                    raise cleanup_error

    def verify_target(self, expected_target: PostgresTargetIdentity) -> None:
        """Fail closed when a business pool resolves to another target."""
        _target_key(expected_target)
        actual_target = self.inspect_target()
        if _target_key(actual_target) != _target_key(expected_target):
            raise MigrationIntegrityError(
                "PostgreSQL pool target identity does not match expected target"
            )

    def inspect_application_role(self) -> PostgresApplicationPrincipal:
        """Read the exact session principal from the staged business pool."""
        with self._borrow_capacity_reservation() as reservation:
            connection = reservation.borrow()
            cursor = None
            primary_error: BaseException | None = None
            cleanup_error: BaseException | None = None
            cleanup_state = _ReservationCleanupState()
            try:
                cursor = connection.cursor()
                cursor.execute("SELECT current_user, session_user")
                row = cursor.fetchone()
                if (
                    row is None
                    or len(row) != 2
                    or type(row[0]) is not str
                    or not row[0]
                    or row[0] != row[1]
                ):
                    raise MigrationIntegrityError("PostgreSQL application principal is invalid")
                return PostgresApplicationPrincipal(role=row[0], session_role=row[1])
            except BaseException as exc:
                primary_error = exc
                raise MigrationIntegrityError(
                    "PostgreSQL application principal is invalid"
                ) from None
            finally:
                try:
                    cleanup_error = self._cleanup_reservation_with_fallback(
                        reservation, connection, cursor, cleanup_state
                    )
                except BaseException as exc:
                    cleanup_error = exc
                finally:
                    try:
                        retry_error = self._cleanup_reservation_with_fallback(
                            reservation, connection, cursor, cleanup_state
                        )
                        cleanup_error = cleanup_error or retry_error
                    except BaseException as exc:
                        cleanup_error = cleanup_error or exc
                if primary_error is None and cleanup_error is not None:
                    raise RuntimeError("PostgreSQL application principal cleanup failed") from None

    def verify_application_access(
        self,
        *,
        target: PostgresTargetIdentity,
        profile: str,
        vector_dim: int,
    ) -> None:
        """Prove that this *candidate business pool* can serve the product.

        Migration credentials intentionally do not stand in for application
        credentials.  This probe borrows exactly one connection from the pool
        which will later be sealed, uses a fresh impossible-to-guess scope,
        and always rolls its transaction back.  Nothing written here survives
        readiness, including when a later statement or cleanup fails.
        """
        _target_key(target)
        if profile not in {"development", "production"} or type(vector_dim) is not int:
            raise ValueError("invalid PostgreSQL application access probe")
        with self._borrow_capacity_reservation() as reservation:
            connection = reservation.borrow()
            cursor = None
            primary_error: BaseException | None = None
            cleanup_error: BaseException | None = None
            cleanup_state = _ReservationCleanupState()
            try:
                cursor = connection.cursor()
                self._probe_application_access(cursor, target, profile, vector_dim)
            except BaseException as exc:
                primary_error = exc
            finally:
                try:
                    cleanup_error = self._cleanup_reservation_with_fallback(
                        reservation, connection, cursor, cleanup_state
                    )
                except BaseException as exc:
                    cleanup_error = exc
                finally:
                    try:
                        retry_error = self._cleanup_reservation_with_fallback(
                            reservation, connection, cursor, cleanup_state
                        )
                        cleanup_error = cleanup_error or retry_error
                    except BaseException as exc:
                        cleanup_error = cleanup_error or exc
        if primary_error is not None:
            # Do not disclose driver SQL, DSNs or random probe principals.
            raise MigrationIntegrityError(
                "PostgreSQL application role cannot access required storage tables"
            ) from None
        if cleanup_error is not None:
            raise RuntimeError("PostgreSQL application access probe cleanup failed") from None

    @staticmethod
    def _probe_application_access(  # noqa: C901  documented known debt
        cursor: Any,
        target: PostgresTargetIdentity,
        profile: str,
        vector_dim: int,
    ) -> None:
        """Run catalog and rollback-only business checks on one cursor."""
        schema = target.schema
        tables = (
            "memplex_functions",
            "memplex_edges",
            "memplex_observations",
            "memplex_facts",
            "memplex_preferences",
            "memplex_changelog",
            "feedback",
        )
        cursor.execute(
            """
            SELECT has_schema_privilege(current_user, %s, 'USAGE'),
                   has_table_privilege(current_user, format('%%I.%%I', %s, %s),
                                       'SELECT,INSERT,UPDATE,DELETE'),
                   has_table_privilege(current_user, format('%%I.%%I', %s, %s),
                                       'SELECT,INSERT,UPDATE,DELETE'),
                   has_table_privilege(current_user, format('%%I.%%I', %s, %s),
                                       'SELECT,INSERT,UPDATE,DELETE'),
                   has_table_privilege(current_user, format('%%I.%%I', %s, %s),
                                       'SELECT,INSERT,UPDATE,DELETE'),
                   has_table_privilege(current_user, format('%%I.%%I', %s, %s),
                                       'SELECT,INSERT,UPDATE,DELETE'),
                   has_table_privilege(current_user, format('%%I.%%I', %s, %s),
                                       'SELECT,INSERT,DELETE'),
                   has_table_privilege(current_user, format('%%I.%%I', %s, %s),
                                       'SELECT,INSERT,UPDATE,DELETE')
            """,
            (schema, *sum(((schema, table) for table in tables), ())),
        )
        row = cursor.fetchone()
        if row is None or len(row) != 8 or not all(row):
            raise PermissionError("required schema or table privilege is absent")
        cursor.execute(
            """
            SELECT has_sequence_privilege(current_user,
                   pg_get_serial_sequence(format('%%I.%%I', %s, %s), 'id'), 'USAGE')
            """,
            (schema, "memplex_changelog"),
        )
        sequence_row = cursor.fetchone()
        if sequence_row is None or sequence_row[0] is not True:
            raise PermissionError("required changelog sequence privilege is absent")
        # The probe matrix derives from the runner's application ACL (the
        # single source of truth) so readiness can never drift from the
        # granted least-privilege set.  The core/feedback tables are already
        # probed above; empty-ACL tables (runner-owned principal state) are
        # covered by the negative probes below instead.
        probed_tables = frozenset(tables)
        sync_privileges = tuple(
            (table, ",".join(sorted(_APPLICATION_ACL[table])))
            for table in _APPLICATION_ACL_TABLES
            if table not in probed_tables and _APPLICATION_ACL[table]
        )
        cursor.execute(
            """
            SELECT table_name, has_table_privilege(
                current_user, format('%%I.%%I', %s, table_name), privileges
            )
            FROM unnest(%s::text[], %s::text[]) AS required(table_name, privileges)
            ORDER BY table_name
            """,
            (schema, [item[0] for item in sync_privileges], [item[1] for item in sync_privileges]),
        )
        if tuple(cursor.fetchall()) != tuple(sorted((name, True) for name, _rights in sync_privileges)):
            raise PermissionError("required durable-sync table privilege is absent")
        cursor.execute(
            "SELECT has_sequence_privilege(current_user, %s::regclass, 'USAGE')",
            (f'"{schema}".memplex_sync_outbox_stream_seq_seq',),
        )
        if cursor.fetchone() != (True,):
            raise PermissionError("required durable-sync sequence privilege is absent")
        for function, signature in (
            ("memplex_sync_capture_before", ""),
            ("memplex_sync_capture_local_change", ""),
            ("memplex_sync_assert_delivery_quota", "text,bigint"),
            ("memplex_sync_snapshot_admission_counts", ""),
            ("memplex_sync_compact", "timestamptz,timestamptz,integer"),
        ):
            cursor.execute(
                "SELECT has_function_privilege(current_user, %s::regprocedure, 'EXECUTE')",
                (f'"{schema}".{function}({signature})',),
            )
            if cursor.fetchone() != (True,):
                raise PermissionError("required durable-sync function privilege is absent")
        if profile == "production":
            # Empty-ACL tables are runner-owned principal state: the
            # application role must hold no table privilege on them at all.
            for denied_table in (
                table for table in _APPLICATION_ACL_TABLES if not _APPLICATION_ACL[table]
            ):
                cursor.execute(
                    "SELECT has_table_privilege(current_user, %s::regclass, 'SELECT,INSERT,UPDATE,DELETE')",
                    (f'"{schema}".{denied_table}',),
                )
                if cursor.fetchone() != (False,):
                    raise PermissionError("application role can access runner-owned principal state")
            cursor.execute(
                "SELECT has_function_privilege(current_user, %s::regprocedure, 'EXECUTE')",
                (f'"{schema}".memplex_configure_sync_local_identity(text)',),
            )
            if cursor.fetchone() != (False,):
                raise PermissionError("application role can configure local sync identity")
        PostgresPoolManager._probe_verify_production_principal(
            cursor, profile, schema, tables,
        )
        PostgresPoolManager._probe_verify_vector_capability(
            cursor, profile, schema, vector_dim,
        )

        token = uuid4().hex
        tenant = "memplex-readiness-" + token
        subject = "subject-" + token
        workspace = "workspace-" + token
        agent = "agent-" + token
        session = "session-" + token
        function_a = "readiness-a-" + token
        function_b = "readiness-b-" + token
        cursor.execute("SELECT set_config('memplex.tenant_id', %s, true)", (tenant,))
        cursor.execute("SELECT set_config('memplex.subject_id', %s, true)", (subject,))
        cursor.execute("SELECT set_config('memplex.workspace_id', %s, true)", (workspace,))
        cursor.execute("SELECT set_config('memplex.agent_id', %s, true)", (agent,))
        cursor.execute("SELECT set_config('memplex.session_id', %s, true)", (session,))
        PostgresPoolManager._probe_verify_background_task_crud(cursor, token)
        remote = "remote-" + token
        consumer = "consumer-" + token
        cursor.execute("SELECT set_config('memplex.verified_remote_node_id', %s, true)", (remote,))
        cursor.execute("SELECT set_config('memplex.consumer_id', %s, true)", (consumer,))
        cursor.execute("SELECT memplex_sync_assert_delivery_quota(%s, 0)", (tenant,))
        cursor.execute("SAVEPOINT memplex_probe_sync_v5")
        event_id = PostgresPoolManager._probe_publish_sync_event(
            cursor, profile, tenant, subject, workspace, agent, session,
            token, remote, consumer,
        )
        metadata = (tenant, subject, workspace, "workspace", agent, session)
        payload = '{"id":"readiness","name":"readiness"}'
        payload_b = '{"id":"readiness-b","name":"readiness-b"}'
        insert_function = """
            INSERT INTO memplex_functions
              (tenant_id, id, data, updated_at, owner_subject, workspace,
               visibility, source_agent, source_session)
            VALUES (%s, %s, %s::jsonb, now(), %s, %s, %s, %s, %s)
            RETURNING id
        """
        cursor.execute(insert_function, (metadata[0], function_a, payload, *metadata[1:]))
        if cursor.fetchone() != (function_a,):
            raise PermissionError("function insert was not visible")
        cursor.execute(insert_function, (metadata[0], function_b, payload_b, *metadata[1:]))
        if cursor.fetchone() != (function_b,):
            raise PermissionError("function insert was not visible")
        cursor.execute(
            "UPDATE memplex_functions SET updated_at = now() WHERE tenant_id=%s AND id=%s RETURNING id",
            (tenant, function_a),
        )
        if cursor.fetchone() != (function_a,):
            raise PermissionError("function update was not visible")
        cursor.execute(
            "INSERT INTO memplex_edges (tenant_id, source, target, edge_type, weight, evidence, created_at, owner_subject, workspace, visibility, source_agent, source_session) VALUES (%s,%s,%s,'RELATED_TO',1,%s::jsonb,now(),%s,%s,%s,%s,%s) RETURNING source",
            (tenant, function_a, function_b, "{}", *metadata[1:]),
        )
        if cursor.fetchone() != (function_a,):
            raise PermissionError("edge insert was not visible")
        cursor.execute(
            "SELECT source FROM memplex_edges WHERE tenant_id=%s AND source=%s AND target=%s AND edge_type='RELATED_TO'",
            (tenant, function_a, function_b),
        )
        if cursor.fetchone() != (function_a,):
            raise PermissionError("edge select was not visible")
        cursor.execute("UPDATE memplex_edges SET weight=2 WHERE tenant_id=%s AND source=%s AND target=%s AND edge_type='RELATED_TO' RETURNING source", (tenant, function_a, function_b))
        if cursor.fetchone() != (function_a,):
            raise PermissionError("edge update was not visible")
        cursor.execute("DELETE FROM memplex_edges WHERE tenant_id=%s AND source=%s AND target=%s AND edge_type='RELATED_TO' RETURNING source", (tenant, function_a, function_b))
        if cursor.fetchone() != (function_a,):
            raise PermissionError("edge delete was not visible")
        # A development superuser is permitted as a convenience and bypasses
        # RLS by PostgreSQL design.  Production rejected it above, so only
        # production uses this as isolation evidence.
        rejected = PostgresPoolManager._probe_verify_function_rls(
            cursor, profile, tenant, function_a, insert_function, metadata, payload,
        )
        for table, stamp in (("memplex_observations", "created_at"), ("memplex_facts", "updated_at"), ("memplex_preferences", "updated_at")):
            record_id = table + "-" + token
            cursor.execute(
                f"INSERT INTO {table} (tenant_id,id,data,{stamp},owner_subject,workspace,visibility,source_agent,source_session) VALUES (%s,%s,%s::jsonb,now(),%s,%s,%s,%s,%s) RETURNING id",
                (tenant, record_id, payload, *metadata[1:]),
            )
            if cursor.fetchone() != (record_id,):
                raise PermissionError(f"{table} insert was not visible")
            cursor.execute(f"SELECT id FROM {table} WHERE tenant_id=%s AND id=%s", (tenant, record_id))
            if cursor.fetchone() != (record_id,):
                raise PermissionError(f"{table} select was not visible")
            cursor.execute(f"UPDATE {table} SET {stamp}=now() WHERE tenant_id=%s AND id=%s RETURNING id", (tenant, record_id))
            if cursor.fetchone() != (record_id,):
                raise PermissionError(f"{table} update was not visible")
            cursor.execute(f"DELETE FROM {table} WHERE tenant_id=%s AND id=%s RETURNING id", (tenant, record_id))
            if cursor.fetchone() != (record_id,):
                raise PermissionError(f"{table} delete was not visible")
        changelog_id = -int(token[:12], 16)
        cursor.execute(
            "INSERT INTO memplex_changelog (tenant_id,id,func_id,ts,event_type,description,source,actor,owner_subject,workspace,visibility,source_agent,source_session) VALUES (%s,%s,%s,now(),'readiness','readiness','readiness','readiness',%s,%s,%s,%s,%s) RETURNING id",
            (tenant, changelog_id, function_a, *metadata[1:]),
        )
        if cursor.fetchone() != (changelog_id,):
            raise PermissionError("changelog insert was not visible")
        cursor.execute("SELECT id FROM memplex_changelog WHERE tenant_id=%s AND id=%s", (tenant, changelog_id))
        if cursor.fetchone() != (changelog_id,):
            raise PermissionError("changelog select was not visible")
        cursor.execute("DELETE FROM memplex_changelog WHERE tenant_id=%s AND id=%s RETURNING id", (tenant, changelog_id))
        if cursor.fetchone() != (changelog_id,):
            raise PermissionError("changelog delete was not visible")
        feedback_id = "feedback-" + token
        cursor.execute(
            "INSERT INTO feedback (memory_id,field_role,value_index,verdict,timestamp,tenant_id,owner_subject_id,workspace_id,visibility,provenance) VALUES (%s,'readiness',0,'correct',now(),%s,%s,%s,'workspace',%s::jsonb) RETURNING memory_id",
            (feedback_id, tenant, subject, workspace, '{"agent_id":"' + agent + '","session_id":"' + session + '"}'),
        )
        if cursor.fetchone() != (feedback_id,):
            raise PermissionError("feedback insert was not visible")
        cursor.execute("SELECT memory_id FROM feedback WHERE tenant_id=%s AND memory_id=%s", (tenant, feedback_id))
        if cursor.fetchone() != (feedback_id,):
            raise PermissionError("feedback select was not visible")
        cursor.execute("UPDATE feedback SET reason='readiness' WHERE tenant_id=%s AND memory_id=%s RETURNING memory_id", (tenant, feedback_id))
        if cursor.fetchone() != (feedback_id,):
            raise PermissionError("feedback update was not visible")
        rejected = PostgresPoolManager._probe_verify_feedback_rls(
            cursor, profile, tenant, subject, agent, session, workspace, feedback_id,
        )
        cursor.execute("DELETE FROM feedback WHERE tenant_id=%s AND memory_id=%s RETURNING memory_id", (tenant, feedback_id))
        if cursor.fetchone() != (feedback_id,):
            raise PermissionError("feedback delete was not visible")
        cursor.execute("DELETE FROM memplex_functions WHERE tenant_id=%s AND id IN (%s,%s) RETURNING id", (tenant, function_a, function_b))
        if len(cursor.fetchall()) != 2:
            raise PermissionError("function delete was not visible")

    @staticmethod
    def _probe_verify_production_principal(
        cursor: Any, profile: str, schema: str, tables: tuple[str, ...]
    ) -> None:
        """Production-only principal/ownership audit over the managed catalogue."""
        if profile == "production":
            cursor.execute(
                """
                SELECT session_user = current_user,
                       NOT (SELECT rolsuper OR rolbypassrls
                            FROM pg_catalog.pg_roles WHERE rolname = current_user),
                       NOT EXISTS (
                           SELECT 1 FROM pg_catalog.pg_namespace n
                           WHERE n.nspname = %s
                             AND n.nspowner = (SELECT oid FROM pg_catalog.pg_roles
                                               WHERE rolname = current_user)
                       ),
                       NOT EXISTS (
                           SELECT 1 FROM pg_catalog.pg_class c
                           JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                           WHERE n.nspname = %s AND c.relname = ANY(%s)
                             AND c.relowner = (SELECT oid FROM pg_catalog.pg_roles
                                               WHERE rolname = current_user)
                       ),
                       current_setting('row_security', true) = 'on'
                """,
                (schema, schema, list(tables)),
            )
            row = cursor.fetchone()
            if row is None or len(row) != 5 or not all(row):
                raise PermissionError("production application principal is unsafe")


    @staticmethod
    def _probe_verify_vector_capability(
        cursor: Any, profile: str, schema: str, vector_dim: int
    ) -> None:
        """The negotiated pgvector capability matches the requested dimension."""
        if vector_dim:
            cursor.execute(
                """
                SELECT attribute.atttypid, attribute.atttypmod,
                       namespace.nspname, typ.typname
                FROM pg_catalog.pg_attribute attribute
                JOIN pg_catalog.pg_class relation ON relation.oid=attribute.attrelid
                JOIN pg_catalog.pg_namespace relation_namespace ON relation_namespace.oid=relation.relnamespace
                JOIN pg_catalog.pg_type typ ON typ.oid=attribute.atttypid
                JOIN pg_catalog.pg_namespace namespace ON namespace.oid=typ.typnamespace
                JOIN pg_catalog.pg_extension extension ON extension.extnamespace=namespace.oid
                WHERE relation_namespace.nspname=%s AND relation.relname='memplex_functions'
                  AND attribute.attname='embedding' AND NOT attribute.attisdropped
                  AND extension.extname='vector'
                """,
                (schema,),
            )
            vector = cursor.fetchone()
            if (
                vector is None
                or len(vector) != 4
                or type(vector[1]) is not int
                or vector[1] != vector_dim
                or type(vector[2]) is not str
                or type(vector[3]) is not str
            ):
                raise PermissionError("verified vector embedding type is absent")
            type_name = '"' + vector[2].replace('"', '""') + '"."' + vector[3].replace('"', '""') + '"'
            cursor.execute("SELECT has_type_privilege(current_user, %s::regtype, 'USAGE')", (type_name,))
            row = cursor.fetchone()
            if row is None or row[0] is not True:
                raise PermissionError("required vector type privilege is absent")
            cursor.execute(
                f"SELECT %s::{type_name}",
                ("[" + ",".join("0" for _ in range(vector_dim)) + "]",),
            )
            if cursor.fetchone() is None:
                raise PermissionError("vector capability probe failed")


    @staticmethod
    def _probe_verify_feedback_rls(
        cursor: Any,
        profile: str,
        tenant: str,
        subject: str,
        agent: str,
        session: str,
        workspace: str,
        feedback_id: str,
    ) -> None:
        """Cross-tenant feedback RLS isolation under savepoint round-trip."""
        if profile == "production":
            cursor.execute("SELECT set_config('memplex.tenant_id', %s, true)", (tenant + "-other",))
            cursor.execute("SELECT memory_id FROM feedback WHERE tenant_id=%s AND memory_id=%s", (tenant, feedback_id))
            if cursor.fetchone() is not None:
                raise PermissionError("feedback RLS isolation failed")
            cursor.execute("SAVEPOINT memplex_probe_feedback_rls")
            rejected = False
            try:
                cursor.execute(
                    "INSERT INTO feedback (memory_id,field_role,value_index,verdict,timestamp,tenant_id,owner_subject_id,workspace_id,visibility,provenance) VALUES (%s,'readiness',1,'correct',now(),%s,%s,%s,'workspace',%s::jsonb) RETURNING memory_id",
                    (
                        feedback_id + "-rls",
                        tenant,
                        subject,
                        workspace,
                        '{"agent_id":"' + agent + '","session_id":"' + session + '"}',
                    ),
                )
            except BaseException:
                rejected = True
            finally:
                cursor.execute("ROLLBACK TO SAVEPOINT memplex_probe_feedback_rls")
                cursor.execute("RELEASE SAVEPOINT memplex_probe_feedback_rls")
            if not rejected:
                raise PermissionError("feedback RLS WITH CHECK isolation failed")
            cursor.execute("SELECT set_config('memplex.tenant_id', %s, true)", (tenant,))


    @staticmethod
    def _probe_verify_function_rls(
        cursor: Any,
        profile: str,
        tenant: str,
        function_a: str,
        insert_function: str,
        metadata: tuple[str, ...],
        payload: str,
    ) -> None:
        """Cross-tenant function RLS isolation under savepoint round-trip."""
        if profile == "production":
            cursor.execute("SELECT set_config('memplex.tenant_id', %s, true)", (tenant + "-other",))
            cursor.execute("SELECT id FROM memplex_functions WHERE tenant_id=%s AND id=%s", (tenant, function_a))
            if cursor.fetchone() is not None:
                raise PermissionError("function RLS isolation failed")
            cursor.execute("SAVEPOINT memplex_probe_function_rls")
            rejected = False
            try:
                # The GUC deliberately names another tenant while the row
                # carries our original tenant.  A successful INSERT would
                # prove that WITH CHECK is not enforcing the RLS boundary.
                cursor.execute(
                    insert_function,
                    (metadata[0], function_a + "-rls", payload, *metadata[1:]),
                )
            except BaseException:
                rejected = True
            finally:
                cursor.execute("ROLLBACK TO SAVEPOINT memplex_probe_function_rls")
                cursor.execute("RELEASE SAVEPOINT memplex_probe_function_rls")
            if not rejected:
                raise PermissionError("function RLS WITH CHECK isolation failed")
            cursor.execute("SELECT set_config('memplex.tenant_id', %s, true)", (tenant,))


    @staticmethod
    def _probe_publish_sync_event(
        cursor: Any,
        profile: str,
        tenant: str,
        subject: str,
        workspace: str,
        agent: str,
        session: str,
        token: str,
        remote: str,
        consumer: str,
    ) -> Any:
        """Insert one sync-v5 outbox event under its savepoint and verify visibility."""
        try:
            event_id = "event-" + token
            cursor.execute(
                """
                INSERT INTO memplex_sync_outbox
                  (tenant_id,stream_seq,event_id,origin_node_id,node_type,entity_key,operation,
                   version_key,payload,visibility,owner_subject_id,workspace_id,agent_id,session_id)
                OVERRIDING SYSTEM VALUE
                VALUES (%s,-1,%s,%s,'function',%s,'upsert',%s,%s::jsonb,'user',%s,%s,%s,%s)
                """,
                (tenant, event_id, remote, "entity-" + token, "version-" + token, "{}", subject, workspace, agent, session),
            )
            cursor.execute(
                "INSERT INTO memplex_sync_entity_versions (tenant_id,node_type,entity_key,version_key,deleted,event_id,last_stream_seq) VALUES (%s,'function',%s,%s,false,%s,-1)",
                (tenant, "entity-" + token, "version-" + token, event_id),
            )
            cursor.execute(
                "INSERT INTO memplex_sync_targets (tenant_id,target_id,remote_node_id,bootstrap_seq) VALUES (%s,%s,%s,0)",
                (tenant, "target-" + token, remote),
            )
            cursor.execute(
                "INSERT INTO memplex_sync_deliveries (tenant_id,target_id,stream_seq,state) VALUES (%s,%s,-1,'pending')",
                (tenant, "target-" + token),
            )
            cursor.execute(
                "INSERT INTO memplex_sync_inbox (tenant_id,origin_node_id,event_id,outcome) VALUES (%s,%s,%s,'accepted')",
                (tenant, remote, "inbound-" + token),
            )
            cursor.execute(
                "INSERT INTO memplex_sync_batches (tenant_id,origin_node_id,batch_id,request_sha256,response) VALUES (%s,%s,%s,%s,%s::jsonb)",
                (tenant, remote, "batch-" + token, "0" * 64, "{}"),
            )
            cursor.execute(
                "INSERT INTO memplex_sync_cursors (tenant_id,remote_id,consumer_id,after_seq) VALUES (%s,%s,%s,0)",
                (tenant, remote, consumer),
            )
            cursor.execute(
                "INSERT INTO memplex_sync_stream_state (tenant_id) VALUES (%s)", (tenant,)
            )
            cursor.execute(
                "INSERT INTO memplex_sync_snapshots (tenant_id,snapshot_id,remote_id,consumer_id,request_id,resume_seq,expires_at) VALUES (%s,%s,%s,%s,%s,0,clock_timestamp() + interval '1 minute')",
                (tenant, "snapshot-" + token, remote, consumer, "request-" + token),
            )
            cursor.execute(
                "INSERT INTO memplex_sync_snapshot_items (tenant_id,snapshot_id,node_type,entity_key,event) VALUES (%s,%s,'function',%s,%s::jsonb)",
                (tenant, "snapshot-" + token, "entity-" + token, "{}"),
            )
            if profile == "production":
                cursor.execute("SELECT set_config('memplex.subject_id', %s, true)", (subject + "-other",))
                cursor.execute("SELECT payload FROM memplex_sync_outbox WHERE tenant_id=%s AND stream_seq=-1", (tenant,))
                if cursor.fetchone() is not None:
                    raise PermissionError("durable-sync scope RLS isolation failed")
                cursor.execute("SELECT set_config('memplex.subject_id', %s, true)", (subject,))
        finally:
            cursor.execute("ROLLBACK TO SAVEPOINT memplex_probe_sync_v5")
            cursor.execute("RELEASE SAVEPOINT memplex_probe_sync_v5")


    @staticmethod
    def _probe_verify_background_task_crud(cursor: Any, token: str) -> None:
        """Insert/update/select/delete one background task row as the probe role."""
        task_id = "task-" + token
        cursor.execute(
            """
            INSERT INTO memplex_background_tasks
              (task_id, task_type, status, payload, max_retries, next_attempt_at)
            VALUES (%s, 'build_index', 'pending', '{}'::jsonb, 1, clock_timestamp())
            RETURNING task_id
            """,
            (task_id,),
        )
        if cursor.fetchone() != (task_id,):
            raise PermissionError("background task insert was not visible")
        cursor.execute(
            "UPDATE memplex_background_tasks SET last_error_code='readiness' "
            "WHERE task_id=%s RETURNING task_id",
            (task_id,),
        )
        if cursor.fetchone() != (task_id,):
            raise PermissionError("background task update was not visible")
        cursor.execute(
            "SELECT task_id FROM memplex_background_tasks WHERE task_id=%s",
            (task_id,),
        )
        if cursor.fetchone() != (task_id,):
            raise PermissionError("background task select was not visible")
        cursor.execute(
            "DELETE FROM memplex_background_tasks WHERE task_id=%s RETURNING task_id",
            (task_id,),
        )
        if cursor.fetchone() != (task_id,):
            raise PermissionError("background task delete was not visible")


    def close(self, *, wait: bool = True) -> bool:
        with self._condition:
            if self._closed:
                return self._closed_result_locked()
            self._close_requested = True
            self._condition.notify_all()
            if (
                self._checked_out
                or self._returning
                or self._waiting_borrowers
                or self._borrow_queue
                or self._closing_in_progress
            ) and not wait:
                return False
            while (
                self._checked_out
                or self._returning
                or self._waiting_borrowers
                or self._borrow_queue
                or self._closing_in_progress
            ):
                self._condition.wait()
            if self._closed:
                return self._closed_result_locked()
            should_finalize = self._take_finalizer_locked()
        if should_finalize:
            self._finish_physical_close()
        return True



# Re-export the split-out resource classes so existing
# ``from memplex.storage.pool import PostgresStorageResources`` import paths
# (and monkeypatches of ``pool.PostgresStorageResources`` /
# ``pool.PostgresPoolManager``) keep working. Placed last so every symbol
# ``postgres_resources`` needs from this module is already defined above.
from memplex.storage.postgres_resources import (  # noqa: E402,F401
    PostgresStorageResources,
    PostgresSyncStorageResources,
)
