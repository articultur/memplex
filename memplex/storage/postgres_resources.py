"""Service-owned PostgreSQL storage resources, split from ``pool.py``.

``PostgresStorageResources`` owns the migration/capability gate and the shared
business connection pool; ``PostgresSyncStorageResources`` coordinates the
independent product and inbound resources used by sync ingress. Both are
re-exported from ``memplex.storage.pool`` so existing import paths (and the
test suite's ``monkeypatch.setattr(pool_module, ...)`` patches) stay stable.
"""

from __future__ import annotations

from hashlib import sha256
from threading import Condition, RLock
from typing import Any, Callable

from memplex.auth import local_development_context

# ``_pool.PostgresPoolManager`` / ``_pool._new_migration_runner`` are monkeypatched on the
# pool module by the test suite. Importing the pool *module* (not the names)
# keeps the moved code honouring those patches: attribute access resolves
# against the live module namespace at call time, so a patch on
# ``pool._pool.PostgresPoolManager`` is seen here. Construction of
# ``PostgresStorageResources`` is likewise routed through ``_pool`` so the
# inbound test's class-level patch takes effect.
from memplex.storage import pool as _pool
from memplex.storage.inbound import InboundSyncExecutor
from memplex.storage.migrations import (
    ApplicationAclContract,
    IngressAclContract,
    MigrationIntegrityError,
    PostgresTargetIdentity,
)
from memplex.storage.migrations.runner import VectorCapabilityRequest, VectorCapabilityStatus
from memplex.storage.pool import (  # names never monkeypatched at module scope
    _READY_POOL_ISSUER,
    ReadyPostgresPool,
    ResourceState,
    _publish_ready_pool_authority,
    _revoke_ready_pool_authority,
    _target_key,
)


class PostgresStorageResources:
    """Service-owned migration/capability gate and shared business pool.

    The condition-protected state machine makes readiness and shutdown
    mutually exclusive.  A close request during initialization is terminal:
    any staged raw pool is closed and no business seal can be published.
    """

    def __init__(
        self,
        dsn: str,
        *,
        migration_dsn: str | None = None,
        pool_factory: Callable[..., Any] | None = None,
        expected_target: PostgresTargetIdentity | None = None,
        ingress_acl: IngressAclContract | None = None,
    ) -> None:
        if type(dsn) is not str or not dsn or dsn != dsn.strip():
            raise TypeError("PostgreSQL application DSN must be a non-empty exact str")
        if migration_dsn is not None and (
            type(migration_dsn) is not str
            or not migration_dsn
            or migration_dsn != migration_dsn.strip()
        ):
            raise TypeError("PostgreSQL migration DSN must be a non-empty exact str")
        if expected_target is not None:
            _target_key(expected_target)
        if ingress_acl is not None and type(ingress_acl) is not IngressAclContract:
            raise TypeError("PostgreSQL ingress ACL must be an exact IngressAclContract")
        self.dsn = dsn
        self.migration_dsn = dsn if migration_dsn is None else migration_dsn
        self._pool_factory = pool_factory
        self._expected_target = expected_target
        self._ingress_acl = ingress_acl
        self._condition = Condition(RLock())
        self._state = ResourceState.NEW
        self._request: VectorCapabilityRequest | None = None
        self._profile: str | None = None
        self._pool_manager: _pool.PostgresPoolManager | None = None
        self._ready_pool: ReadyPostgresPool | None = None
        self._staged_raw_pool: Any | None = None
        self._staged_manager: _pool.PostgresPoolManager | None = None
        self._fault: BaseException | None = None
        self._close_error: BaseException | None = None
        self._close_in_progress = False
        self.vector_capability_status: VectorCapabilityStatus | None = None

    @property
    def state(self) -> str:
        with self._condition:
            return self._state.value

    @property
    def ready_pool(self) -> ReadyPostgresPool:
        with self._condition:
            if self._state is not ResourceState.READY or self._ready_pool is None:
                raise RuntimeError("PostgreSQL storage resources are not ready")
            return self._ready_pool

    @property
    def pool_manager(self) -> _pool.PostgresPoolManager:
        """Expose the manager for diagnostics; business stores require the seal."""
        return self.ready_pool.manager

    @property
    def pool_created(self) -> bool:
        with self._condition:
            return self._ready_pool is not None

    @property
    def business_lease_count(self) -> int:
        with self._condition:
            manager = self._pool_manager
        return 0 if manager is None else manager.business_lease_count

    @property
    def pool_max_connections(self) -> int:
        """Return the configured business-pool ceiling without exposing DSNs."""
        with self._condition:
            manager = self._pool_manager
        return 0 if manager is None else manager.max_connections

    @property
    def pool_high_watermark(self) -> int:
        """Return historical validated lease demand without connection details."""
        with self._condition:
            manager = self._pool_manager
        return 0 if manager is None else manager.business_lease_high_watermark

    @staticmethod
    def _profile_for(deployment_profile: str) -> str:
        profile = str(deployment_profile).strip().lower()
        if profile not in {"development", "production"}:
            raise ValueError("PostgreSQL resources require development or production profile")
        return profile

    @staticmethod
    def _validate_request_profile(
        request: VectorCapabilityRequest, profile: str
    ) -> None:
        if request.dim == 0:
            if request.policy != "disabled":
                raise ValueError("vector dim=0 requires disabled policy")
            return
        expected_policy = "required" if profile == "production" else "best_effort"
        if request.policy != expected_policy:
            raise ValueError(
                f"{profile} vector dim>0 requires {expected_policy} policy"
            )

    @staticmethod
    def _validate_status(
        request: VectorCapabilityRequest,
        profile: str,
        status: VectorCapabilityStatus,
    ) -> int:
        """Validate the runner result before any business pool exists."""
        if type(request) is not VectorCapabilityRequest:
            raise ValueError("vector capability request must be VectorCapabilityRequest")
        if type(status) is not VectorCapabilityStatus:
            raise ValueError("vector capability result must be VectorCapabilityStatus")
        PostgresStorageResources._validate_request_profile(request, profile)
        state = getattr(status, "state", None)
        dim = getattr(status, "dim", None)
        digest = getattr(status, "parameter_digest", None)
        if request.policy == "disabled":
            if state != "disabled" or dim != 0 or digest is not None:
                raise ValueError("disabled vector status is inconsistent")
            return 0
        if state == "degraded":
            if (
                profile != "development"
                or request.policy != "best_effort"
                or dim != request.dim
                or digest is not None
            ):
                raise ValueError("degraded vector status is inconsistent")
            return 0
        expected_digest = sha256(f"pgvector:{request.dim}".encode("ascii")).hexdigest()
        if (
            state != "ready"
            or dim != request.dim
            or digest != expected_digest
        ):
            raise ValueError("ready vector status digest is inconsistent")
        return request.dim

    def _same_request_locked(
        self, request: VectorCapabilityRequest, profile: str
    ) -> bool:
        return self._request == request and self._profile == profile

    def _closed_error_locked(self) -> RuntimeError:
        return RuntimeError("PostgreSQL storage resources are closed")

    def _fault_error_locked(self) -> RuntimeError:
        error = self._fault
        if error is None:
            return RuntimeError("PostgreSQL storage resources are faulted")
        failure = RuntimeError("PostgreSQL storage resources are faulted")
        failure.__cause__ = error
        return failure

    def _revoke_ready_pool_locked(self) -> None:
        """Invalidate the published construction capability before shutdown."""
        _revoke_ready_pool_authority(self._ready_pool)

    @staticmethod
    def _close_staged(
        manager: _pool.PostgresPoolManager | None, raw_pool: Any | None
    ) -> BaseException | None:
        try:
            if manager is not None:
                manager.close(wait=True)
            elif raw_pool is not None:
                raw_pool.closeall()
        except BaseException as exc:
            return exc
        return None

    def _finish_initialization_failure(
        self,
        error: BaseException,
        manager: _pool.PostgresPoolManager | None,
        raw_pool: Any | None,
    ) -> None:
        cleanup_error = self._close_staged(manager, raw_pool)
        with self._condition:
            self._revoke_ready_pool_locked()
            self._staged_manager = None
            self._staged_raw_pool = None
            if cleanup_error is not None:
                self._fault = cleanup_error
                self._close_error = cleanup_error
                self._state = ResourceState.FAULTED
            elif self._state is ResourceState.CLOSING:
                self._state = ResourceState.CLOSED
            elif self._state is ResourceState.INITIALIZING:
                self._fault = error
                self._state = ResourceState.FAULTED
            self._condition.notify_all()

    def _on_pool_closed(self, close_error: BaseException | None) -> None:
        """Advance the resource terminal state after manager auto-finalization."""
        with self._condition:
            self._revoke_ready_pool_locked()
            if self._state is ResourceState.READY:
                self._close_in_progress = False
                self._fault = close_error or RuntimeError(
                    "PostgreSQL pool was closed outside PostgresStorageResources"
                )
                self._close_error = self._fault
                self._state = ResourceState.FAULTED
                self._condition.notify_all()
                return
            if self._state is not ResourceState.CLOSING:
                return
            self._close_in_progress = False
            if close_error is None:
                self._state = ResourceState.CLOSED
            else:
                self._fault = close_error
                self._close_error = close_error
                self._state = ResourceState.FAULTED
            self._condition.notify_all()

    def _on_pool_fault(self, error: BaseException) -> None:
        """Mirror a terminal manager fault into Resources and revoke its seal."""
        manager: _pool.PostgresPoolManager | None = None
        with self._condition:
            if self._state not in {ResourceState.READY, ResourceState.CLOSING}:
                return
            self._revoke_ready_pool_locked()
            self._fault = error
            self._close_error = error
            self._close_in_progress = False
            self._state = ResourceState.FAULTED
            manager = self._pool_manager
            self._condition.notify_all()
        if manager is not None:
            try:
                # A target/cleanup fault may occur before the caller obtains
                # a lease.  Request non-waiting shutdown now; the in-flight
                # borrower remains counted until it has returned its
                # connection, then the manager performs the one closeall.
                manager.close(wait=False)
            except BaseException:
                # The original pool fault remains the stable resource cause.
                pass

    def ensure_ready(
        self,
        request: VectorCapabilityRequest,
        deployment_profile: str,
    ) -> VectorCapabilityStatus:
        if type(request) is not VectorCapabilityRequest:
            raise ValueError("vector capability request must be VectorCapabilityRequest")
        profile = self._profile_for(deployment_profile)
        self._validate_request_profile(request, profile)
        with self._condition:
            if self._state is ResourceState.NEW:
                self._request = request
                self._profile = profile
                self._state = ResourceState.INITIALIZING
            elif self._state is ResourceState.INITIALIZING:
                if not self._same_request_locked(request, profile):
                    raise RuntimeError(
                        "PostgreSQL storage resources are already initialized differently"
                    )
                while self._state is ResourceState.INITIALIZING:
                    self._condition.wait()
                if self._state is ResourceState.READY:
                    assert self.vector_capability_status is not None
                    return self.vector_capability_status
                if self._state in {ResourceState.CLOSING, ResourceState.CLOSED}:
                    raise self._closed_error_locked()
                raise self._fault_error_locked()
            elif self._state is ResourceState.READY:
                if not self._same_request_locked(request, profile):
                    raise RuntimeError(
                        "PostgreSQL storage resources are already initialized differently"
                    )
                assert self.vector_capability_status is not None
                return self.vector_capability_status
            elif self._state in {ResourceState.CLOSING, ResourceState.CLOSED}:
                raise self._closed_error_locked()
            else:
                raise self._fault_error_locked()

        raw_pool: Any | None = None
        manager: _pool.PostgresPoolManager | None = None
        try:
            application_runner = _pool._new_migration_runner(self.dsn)
            application_target = application_runner.inspect_target()
            _target_key(application_target)
            if (
                self._expected_target is not None
                and _target_key(application_target) != _target_key(self._expected_target)
            ):
                raise MigrationIntegrityError(
                    "PostgreSQL application target does not match expected target"
                )
            application_principal = application_runner.inspect_application_principal(
                expected_target=application_target
            )
            application_acl = ApplicationAclContract(application_principal.role)
            migration_runner = _pool._new_migration_runner(self.migration_dsn)
            migration_target = migration_runner.inspect_target()
            if _target_key(migration_target) != _target_key(application_target):
                raise MigrationIntegrityError(
                    "PostgreSQL migration target does not match application target"
                )
            if profile == "production":
                migration_principal = migration_runner.inspect_application_principal(
                    expected_target=migration_target
                )
                application_roles = {
                    application_principal.role,
                    application_principal.session_role,
                }
                migration_roles = {
                    migration_principal.role,
                    migration_principal.session_role,
                }
                if application_roles & migration_roles:
                    raise MigrationIntegrityError(
                        "PostgreSQL application and migration principals must be distinct"
                    )
            if request.policy == "disabled":
                apply_kwargs: dict[str, Any] = {
                    "expected_target": application_target,
                    "application_acl": application_acl,
                }
                if self._ingress_acl is not None:
                    apply_kwargs["ingress_acl"] = self._ingress_acl
                migration_runner.apply(**apply_kwargs)
            else:
                # The mutator's return value is intentionally provisional.
                # A distinct readonly connection below supplies the only
                # status that may become a seal.
                ensure_kwargs: dict[str, Any] = {
                    "expected_target": application_target,
                    "application_acl": application_acl,
                }
                if self._ingress_acl is not None:
                    ensure_kwargs["ingress_acl"] = self._ingress_acl
                migration_runner.ensure_vector_capability(
                    request,
                    profile,
                    **ensure_kwargs,
                )
            with self._condition:
                if self._state is not ResourceState.INITIALIZING:
                    raise self._closed_error_locked()
            # Structural/ACL verification is deliberately completed before a
            # candidate business pool exists.  An unverified catalogue must
            # never cause a pool_factory side effect or leave a connection to
            # clean up; only the final DML proof needs the future pool.
            verifier = _pool._new_migration_runner(self.migration_dsn)
            verify_kwargs: dict[str, Any] = {
                "expected_target": application_target,
                "application_acl": application_acl,
            }
            if self._ingress_acl is not None:
                verify_kwargs["ingress_acl"] = self._ingress_acl
            status = verifier.verify_storage_readiness(
                request,
                profile,
                **verify_kwargs,
            )
            effective_dim = self._validate_status(request, profile, status)
            kwargs: dict[str, Any] = {}
            if self._pool_factory is not None:
                raw_pool = self._pool_factory(1, 8, self.dsn)
                with self._condition:
                    self._staged_raw_pool = raw_pool
                    if self._state is not ResourceState.INITIALIZING:
                        raise self._closed_error_locked()
                kwargs["pool"] = raw_pool
            kwargs["on_closed"] = self._on_pool_closed
            kwargs["on_fault"] = self._on_pool_fault
            kwargs["expected_target"] = application_target
            kwargs["expected_application_principal"] = application_principal
            kwargs["deployment_profile"] = profile
            manager = _pool.PostgresPoolManager(self.dsn, **kwargs)
            manager.verify_target(application_target)
            # Production and development both prove the application role can both
            # read and write required business tables.
            manager.verify_application_access(
                target=application_target,
                profile=profile,
                vector_dim=effective_dim,
            )
            with self._condition:
                self._staged_manager = manager
                if self._state is not ResourceState.INITIALIZING:
                    raise self._closed_error_locked()
                seal = ReadyPostgresPool(
                    manager=manager,
                    request=request,
                    status=status,
                    effective_dim=effective_dim,
                    target=application_target,
                    issuer=_READY_POOL_ISSUER,
                )
                self._pool_manager = manager
                self._ready_pool = seal
                self.vector_capability_status = status
                self._staged_manager = None
                self._staged_raw_pool = None
                self._state = ResourceState.READY
                _publish_ready_pool_authority(seal)
                self._condition.notify_all()
                return status
        except BaseException as exc:
            self._finish_initialization_failure(
                exc,
                manager,
                raw_pool,
            )
            raise

    def close(self, *, wait: bool = True) -> bool:
        manager: _pool.PostgresPoolManager | None = None
        with self._condition:
            if self._state is ResourceState.NEW:
                self._state = ResourceState.CLOSED
                self._condition.notify_all()
                return True
            if self._state is ResourceState.CLOSED:
                return True
            if self._state is ResourceState.FAULTED:
                raise self._fault_error_locked()
            if self._state is ResourceState.INITIALIZING:
                self._state = ResourceState.CLOSING
                self._condition.notify_all()
                if not wait:
                    return False
                while self._state is ResourceState.CLOSING:
                    self._condition.wait()
                if self._state is ResourceState.CLOSED:
                    return True
                raise self._fault_error_locked()
            if self._state is ResourceState.CLOSING and self._close_in_progress:
                if not wait:
                    return False
                while self._state is ResourceState.CLOSING and self._close_in_progress:
                    self._condition.wait()
                if self._state is ResourceState.CLOSED:
                    return True
                if self._state is ResourceState.FAULTED:
                    raise self._fault_error_locked()
            if self._state is ResourceState.READY:
                self._state = ResourceState.CLOSING
                self._revoke_ready_pool_locked()
            manager = self._pool_manager
            if manager is None:
                if not wait:
                    return False
                while self._state is ResourceState.CLOSING:
                    self._condition.wait()
                if self._state is ResourceState.CLOSED:
                    return True
                raise self._fault_error_locked()
            self._close_in_progress = True

        try:
            result = manager.close(wait=wait)
        except BaseException as exc:
            with self._condition:
                self._close_in_progress = False
                self._close_error = exc
                self._fault = exc
                self._state = ResourceState.FAULTED
                self._condition.notify_all()
            raise
        with self._condition:
            self._close_in_progress = False
            if self._state is ResourceState.FAULTED:
                raise self._fault_error_locked()
            if result:
                self._state = ResourceState.CLOSED
            self._condition.notify_all()
        return result


class PostgresSyncStorageResources:
    """Coordinate independent product and inbound resources for sync ingress."""

    def __init__(
        self,
        app_dsn: str,
        migration_dsn: str,
        inbound_dsn: str,
    ) -> None:
        if type(app_dsn) is not str:
            raise TypeError("PostgreSQL application DSN must be a non-empty exact str")
        if type(migration_dsn) is not str:
            raise TypeError("PostgreSQL migration DSN must be a non-empty exact str")
        if type(inbound_dsn) is not str:
            raise TypeError("PostgreSQL inbound DSN must be a non-empty exact str")
        app_dsn = app_dsn.strip()
        migration_dsn = migration_dsn.strip()
        inbound_dsn = inbound_dsn.strip()
        if not app_dsn:
            raise TypeError("PostgreSQL application DSN must be a non-empty exact str")
        if not migration_dsn:
            raise TypeError("PostgreSQL migration DSN must be a non-empty exact str")
        if not inbound_dsn:
            raise TypeError("PostgreSQL inbound DSN must be a non-empty exact str")
        if app_dsn == migration_dsn or app_dsn == inbound_dsn or migration_dsn == inbound_dsn:
            raise ValueError("PostgreSQL sync DSN values must be distinct")
        self.app_dsn = app_dsn
        self.migration_dsn = migration_dsn
        self.inbound_dsn = inbound_dsn
        self._condition = Condition(RLock())
        self._state = ResourceState.NEW
        self._request: VectorCapabilityRequest | None = None
        self._profile: str | None = None
        self._status: VectorCapabilityStatus | None = None
        self._ready_pool: ReadyPostgresPool | None = None
        self._executor: InboundSyncExecutor | None = None
        self._app_resources: PostgresStorageResources | None = None
        self._inbound_manager: _pool.PostgresPoolManager | None = None
        self._fault: BaseException | None = None
        self._close_in_progress = False
        self._close_error: BaseException | None = None

    @property
    def state(self) -> str:
        with self._condition:
            return self._state.value

    @property
    def ready_pool(self) -> ReadyPostgresPool:
        with self._condition:
            self._refresh_from_app_resource_fault()
            if self._state is not ResourceState.READY or self._ready_pool is None:
                raise RuntimeError("PostgreSQL sync resources are not ready")
            return self._ready_pool

    @property
    def executor(self) -> InboundSyncExecutor:
        with self._condition:
            self._refresh_from_app_resource_fault()
            if self._state is not ResourceState.READY or self._executor is None:
                raise RuntimeError("PostgreSQL sync resources are not ready")
            return self._executor

    @property
    def status(self) -> VectorCapabilityStatus:
        with self._condition:
            self._refresh_from_app_resource_fault()
            if self._state is not ResourceState.READY or self._status is None:
                raise RuntimeError("PostgreSQL sync resources are not ready")
            return self._status

    def _same_request_locked(self, request: VectorCapabilityRequest, profile: str) -> bool:
        return self._request == request and self._profile == profile

    def _assert_inbound_authority(self) -> None:
        """Reject a retained executor after either coordinated resource is revoked."""
        with self._condition:
            self._refresh_from_app_resource_fault()
            if (
                self._state is not ResourceState.READY
                or self._executor is None
                or self._inbound_manager is None
            ):
                raise RuntimeError("PostgreSQL sync resources are not ready")

    def _closed_error_locked(self) -> RuntimeError:
        return RuntimeError("PostgreSQL sync resources are closed")

    def _fault_error_locked(self) -> RuntimeError:
        error = self._fault or self._close_error
        if error is None:
            return RuntimeError("PostgreSQL sync resources are faulted")
        failure = RuntimeError("PostgreSQL sync resources are faulted")
        failure.__cause__ = error
        return failure

    def _close_staged_resources(
        self,
        app_resources: PostgresStorageResources | None,
        inbound_manager: _pool.PostgresPoolManager | None,
    ) -> BaseException | None:
        close_error: BaseException | None = None
        if inbound_manager is not None:
            try:
                inbound_manager.close(wait=True)
            except BaseException as exc:  # noqa: BLE001
                close_error = exc
        if app_resources is not None:
            try:
                app_resources.close(wait=True)
            except BaseException as exc:  # noqa: BLE001
                if close_error is None:
                    close_error = exc
        return close_error

    def _refresh_from_app_resource_fault(self) -> None:
        if self._state is not ResourceState.READY or self._app_resources is None:
            return
        if self._app_resources.state == ResourceState.READY.value:
            return

        inbound_manager = self._inbound_manager
        with self._condition:
            self._ready_pool = None
            self._executor = None
            self._app_resources = None
            self._inbound_manager = None
            self._status = None
            self._fault = RuntimeError("PostgreSQL sync resources are not ready")
            self._close_error = self._fault
            self._state = ResourceState.FAULTED
            self._condition.notify_all()

        if inbound_manager is not None:
            try:
                inbound_manager.close(wait=False)
            except BaseException:
                pass

    def _set_fault_from_inbound(self, error: BaseException | None) -> None:
        if error is None:
            error = RuntimeError("PostgreSQL sync resources were revoked by inbound pool fault")
        app_resources = None
        with self._condition:
            if self._state not in {ResourceState.READY, ResourceState.CLOSING}:
                return
            if self._state is ResourceState.CLOSING and self._close_in_progress:
                return
            self._ready_pool = None
            self._executor = None
            self._status = None
            self._fault = error
            self._close_error = error
            self._state = ResourceState.FAULTED
            app_resources = self._app_resources
            self._app_resources = None
            self._inbound_manager = None
            self._condition.notify_all()
        if app_resources is not None:
            try:
                app_resources.close(wait=False)
            except BaseException:
                pass

    def _set_fault_from_inbound_closed(self, close_error: BaseException | None) -> None:
        if close_error is not None:
            self._set_fault_from_inbound(close_error)
            return
        app_resources = None
        with self._condition:
            if self._state is not ResourceState.READY or self._close_in_progress:
                return
            self._ready_pool = None
            self._executor = None
            self._status = None
            app_resources = self._app_resources
            self._app_resources = None
            self._inbound_manager = None
            self._fault = RuntimeError(
                "PostgreSQL sync resources were revoked by inbound pool close"
            )
            self._close_error = self._fault
            self._state = ResourceState.FAULTED
            self._condition.notify_all()
        if app_resources is not None:
            try:
                app_resources.close(wait=False)
            except BaseException:
                pass

    def ensure_ready(
        self,
        request: VectorCapabilityRequest,
        deployment_profile: str,
    ) -> VectorCapabilityStatus:
        if type(request) is not VectorCapabilityRequest:
            raise ValueError("vector capability request must be a VectorCapabilityRequest")
        profile = PostgresStorageResources._profile_for(deployment_profile)
        PostgresStorageResources._validate_request_profile(request, profile)

        with self._condition:
            if self._state is ResourceState.NEW:
                self._request = request
                self._profile = profile
                self._state = ResourceState.INITIALIZING
            elif self._state is ResourceState.INITIALIZING:
                if not self._same_request_locked(request, profile):
                    raise RuntimeError(
                        "PostgreSQL sync resources are already initialized differently"
                    )
                while self._state is ResourceState.INITIALIZING:
                    self._condition.wait()
                if self._state is ResourceState.READY:
                    assert self._status is not None
                    return self._status
                if self._state in {ResourceState.CLOSING, ResourceState.CLOSED}:
                    raise self._closed_error_locked()
                raise self._fault_error_locked()
            elif self._state is ResourceState.READY:
                if not self._same_request_locked(request, profile):
                    raise RuntimeError(
                        "PostgreSQL sync resources are already initialized differently"
                    )
                assert self._status is not None
                return self._status
            elif self._state in {ResourceState.CLOSING, ResourceState.CLOSED}:
                raise self._closed_error_locked()
            else:
                raise self._fault_error_locked()

        staged_manager: _pool.PostgresPoolManager | None = None
        staged_app_resources: PostgresStorageResources | None = None
        status: VectorCapabilityStatus | None = None

        try:
            inbound_runner = _pool._new_migration_runner(self.inbound_dsn)
            inbound_target = inbound_runner.inspect_target()
            _target_key(inbound_target)
            inbound_principal = inbound_runner.inspect_application_principal(
                expected_target=inbound_target
            )

            staged_app_resources = _pool.PostgresStorageResources(
                self.app_dsn,
                migration_dsn=self.migration_dsn,
                ingress_acl=IngressAclContract(inbound_principal.role),
            )
            status = staged_app_resources.ensure_ready(request, profile)
            app_target = staged_app_resources.ready_pool.target
            if _target_key(app_target) != _target_key(inbound_target):
                raise MigrationIntegrityError(
                    "PostgreSQL inbound target does not match application target"
                )

            staged_manager = _pool.PostgresPoolManager(
                self.inbound_dsn,
                expected_target=inbound_target,
                expected_application_principal=inbound_principal,
                on_closed=self._set_fault_from_inbound_closed,
                on_fault=self._set_fault_from_inbound,
                deployment_profile="production",
            )
            staged_manager.verify_target(inbound_target)
            if staged_manager.inspect_application_role() != inbound_principal:
                raise MigrationIntegrityError(
                    "PostgreSQL inbound principal is not direct login/session login"
                )

            def noop_bind(_cursor: Any, _context: Any) -> None:  # pylint: disable=unused-argument
                return None

            executor = InboundSyncExecutor(
                lambda: staged_manager.transaction(noop_bind, local_development_context()),
                authority_check=self._assert_inbound_authority,
            )

            with self._condition:
                if self._state is not ResourceState.INITIALIZING:
                    raise self._closed_error_locked()
                self._ready_pool = staged_app_resources.ready_pool
                self._executor = executor
                self._app_resources = staged_app_resources
                self._inbound_manager = staged_manager
                self._status = status
                self._state = ResourceState.READY
                self._condition.notify_all()
                return status
        except BaseException as exc:
            close_error = self._close_staged_resources(
                staged_app_resources,
                staged_manager,
            )
            with self._condition:
                self._ready_pool = None
                self._executor = None
                self._app_resources = None
                self._inbound_manager = None
                self._status = None
                if self._state is ResourceState.INITIALIZING:
                    self._state = ResourceState.FAULTED
                    self._fault = exc
                    self._close_error = close_error or exc
                elif self._state is ResourceState.CLOSING:
                    if close_error is not None:
                        self._state = ResourceState.FAULTED
                        self._fault = close_error
                        self._close_error = close_error
                    else:
                        self._state = ResourceState.CLOSED
                self._condition.notify_all()
            raise

    def close(self, *, wait: bool = True) -> bool:
        inbound_result = True
        app_result = True
        close_error: BaseException | None = None

        with self._condition:
            if self._state is ResourceState.NEW:
                self._state = ResourceState.CLOSED
                self._condition.notify_all()
                return True
            if self._state is ResourceState.CLOSED:
                return True
            if self._state is ResourceState.FAULTED:
                raise self._fault_error_locked()
            if self._state is ResourceState.INITIALIZING:
                self._state = ResourceState.CLOSING
                if not wait:
                    return False
                while self._state is ResourceState.CLOSING:
                    self._condition.wait()
                if self._state is ResourceState.CLOSED:
                    return True
                raise self._fault_error_locked()
            if self._state is ResourceState.CLOSING and self._close_in_progress:
                if not wait:
                    return False
                while self._state is ResourceState.CLOSING and self._close_in_progress:
                    self._condition.wait()
                if self._state is ResourceState.CLOSED:
                    return True
                if self._state is ResourceState.FAULTED:
                    raise self._fault_error_locked()
            if self._state is ResourceState.READY:
                self._state = ResourceState.CLOSING
            app_resources = self._app_resources
            inbound_manager = self._inbound_manager
            self._ready_pool = None
            self._executor = None
            self._status = None
            self._close_in_progress = True

        if inbound_manager is not None:
            try:
                inbound_result = inbound_manager.close(wait=wait)
            except BaseException as exc:  # noqa: BLE001
                close_error = exc
        if app_resources is not None:
            try:
                app_result = app_resources.close(wait=wait)
            except BaseException as exc:  # noqa: BLE001
                if close_error is None:
                    close_error = exc

        result = bool(inbound_result and app_result)
        if close_error is not None:
            with self._condition:
                self._close_in_progress = False
                self._fault = close_error
                self._close_error = close_error
                self._state = ResourceState.FAULTED
                self._condition.notify_all()
            raise close_error

        with self._condition:
            self._close_in_progress = False
            if result and self._state is ResourceState.CLOSING:
                self._app_resources = None
                self._inbound_manager = None
                self._state = ResourceState.CLOSED
            self._condition.notify_all()
            return result
