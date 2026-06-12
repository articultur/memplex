"""Prometheus metrics instrumentation.

All metrics are OPTIONAL: if prometheus_client is not installed,
every function in this module is a no-op.

Metrics are lazily initialised on first access via a singleton pattern
so the prometheus_client import only happens when metrics are actually
used.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from prometheus_client import CollectorRegistry

# Sentinel to distinguish "not yet initialised" from None
_NOT_SET = object()


class _NoOpMetrics:
    """No-op fallback when prometheus_client is unavailable."""

    __slots__ = ()

    def query_duration(self, *args, **kwargs) -> None:
        pass

    def query_results(self, *args, **kwargs) -> None:
        pass

    def query_tokens(self, *args, **kwargs) -> None:
        pass

    def add_duration(self, *args, **kwargs) -> None:
        pass

    def add_total(self, *args, **kwargs) -> None:
        pass

    def functions_total(self, *args, **kwargs) -> None:
        pass

    def graph_edges_total(self, *args, **kwargs) -> None:
        pass

    def llm_calls(self, *args, **kwargs) -> None:
        pass

    def llm_duration(self, *args, **kwargs) -> None:
        pass

    def background_task_duration(self, *args, **kwargs) -> None:
        pass

    def background_task_total(self, *args, **kwargs) -> None:
        pass

    def compaction_duration(self, *args, **kwargs) -> None:
        pass

    def compaction_removed(self, *args, **kwargs) -> None:
        pass


class Metrics:
    """Thread-safe lazy singleton for Prometheus metrics.

    The prometheus_client library is only imported when :meth:`_ensure_init`
    is first called.  Until then, all metric methods are no-ops.

    Use :func:`get_metrics` to obtain the singleton instance.
    """

    __slots__ = ("_lock", "_metrics", "_registry")
    __instance__: "Metrics | None" = None

    def __new__(cls) -> "Metrics":
        if cls.__instance__ is None:
            cls.__instance__ = super().__new__(cls)
            cls.__instance__._initialized()
        return cls.__instance__

    def _initialized(self) -> None:
        self._lock = threading.Lock()
        self._metrics: object = _NOT_SET
        self._registry = None

    @property
    def _m(self) -> _NoOpMetrics | _PrometheusMetrics:
        """Lazily initialise the prometheus_client metrics."""
        if self._metrics is _NOT_SET:
            with self._lock:
                if self._metrics is _NOT_SET:
                    self._metrics = self._create_metrics()
        return self._metrics

    def _create_metrics(self) -> _NoOpMetrics | _PrometheusMetrics:
        try:
            from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
            from prometheus_client.metrics import Histogram as HistogramClass

            registry = CollectorRegistry()

            # ── Query metrics ──────────────────────────────────────────
            query_duration = Histogram(
                "memplex_query_duration_seconds",
                "Query latency in seconds",
                ["scope", "backend"],
                buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
                registry=registry,
            )
            query_results = Counter(
                "memplex_query_results_total",
                "Total query results returned",
                ["scope", "truncated"],
                registry=registry,
            )
            query_tokens = Histogram(
                "memplex_query_tokens_used",
                "Estimated tokens used per query",
                ["scope"],
                buckets=(100, 250, 500, 1000, 2500, 5000, 10000),
                registry=registry,
            )

            # ── Add / write metrics ───────────────────────────────────
            add_duration = Histogram(
                "memplex_add_duration_seconds",
                "Write/add operation latency in seconds",
                ["backend"],
                buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
                registry=registry,
            )
            add_total = Counter(
                "memplex_add_total",
                "Total write/add operations",
                ["status"],
                registry=registry,
            )

            # ── Memory gauges ─────────────────────────────────────────
            functions_total = Gauge(
                "memplex_functions_total",
                "Total number of stored functions",
                ["memory_type", "backend"],
                registry=registry,
            )
            graph_edges_total = Gauge(
                "memplex_graph_edges_total",
                "Total number of graph edges",
                ["edge_type"],
                registry=registry,
            )

            # ── LLM metrics ───────────────────────────────────────────
            llm_calls = Counter(
                "memplex_llm_calls_total",
                "Total LLM API calls",
                ["operation"],
                registry=registry,
            )
            llm_duration = Histogram(
                "memplex_llm_duration_seconds",
                "LLM API call latency in seconds",
                ["operation"],
                buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
                registry=registry,
            )

            # ── Background task metrics ────────────────────────────────
            background_task_duration = Histogram(
                "memplex_background_task_duration_seconds",
                "Background task execution latency",
                ["task_type"],
                buckets=(0.1, 0.5, 1.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0),
                registry=registry,
            )
            background_task_total = Counter(
                "memplex_background_task_total",
                "Total background tasks processed",
                ["task_type", "status"],
                registry=registry,
            )

            # ── Compaction metrics ────────────────────────────────────
            compaction_duration = Histogram(
                "memplex_compaction_duration_seconds",
                "Compaction pipeline execution time",
                ["trigger"],
                buckets=(1.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0),
                registry=registry,
            )
            compaction_removed = Counter(
                "memplex_compaction_removed_total",
                "Total items removed by compaction",
                ["stage"],
                registry=registry,
            )

            self._registry = registry

            return _PrometheusMetrics(
                query_duration=query_duration,
                query_results=query_results,
                query_tokens=query_tokens,
                add_duration=add_duration,
                add_total=add_total,
                functions_total=functions_total,
                graph_edges_total=graph_edges_total,
                llm_calls=llm_calls,
                llm_duration=llm_duration,
                background_task_duration=background_task_duration,
                background_task_total=background_task_total,
                compaction_duration=compaction_duration,
                compaction_removed=compaction_removed,
            )
        except ImportError:
            return _NoOpMetrics()

    # ── Public metric methods (all delegate to _m) ────────────────────

    def query_duration(self, scope: str, backend: str, duration_seconds: float) -> None:
        self._m.query_duration(scope, backend, duration_seconds)

    def query_results(self, scope: str, truncated: bool, count: int) -> None:
        self._m.query_results(scope, truncated, count)

    def query_tokens(self, scope: str, tokens: int) -> None:
        self._m.query_tokens(scope, tokens)

    def add_duration(self, backend: str, duration_seconds: float) -> None:
        self._m.add_duration(backend, duration_seconds)

    def add_total(self, status: str) -> None:
        self._m.add_total(status)

    def functions_total(self, memory_type: str, backend: str, value: float) -> None:
        self._m.functions_total(memory_type, backend, value)

    def graph_edges_total(self, edge_type: str, value: float) -> None:
        self._m.graph_edges_total(edge_type, value)

    def llm_calls(self, operation: str) -> None:
        self._m.llm_calls(operation)

    def llm_duration(self, operation: str, duration_seconds: float) -> None:
        self._m.llm_duration(operation, duration_seconds)

    def background_task_duration(self, task_type: str, duration_seconds: float) -> None:
        self._m.background_task_duration(task_type, duration_seconds)

    def background_task_total(self, task_type: str, status: str) -> None:
        self._m.background_task_total(task_type, status)

    def compaction_duration(self, trigger: str, duration_seconds: float) -> None:
        self._m.compaction_duration(trigger, duration_seconds)

    def compaction_removed(self, stage: str, count: int) -> None:
        self._m.compaction_removed(stage, count)


class _PrometheusMetrics:
    """Concrete metrics wrapper backed by prometheus_client objects."""

    __slots__ = (
        "_query_duration",
        "_query_results",
        "_query_tokens",
        "_add_duration",
        "_add_total",
        "_functions_total",
        "_graph_edges_total",
        "_llm_calls",
        "_llm_duration",
        "_background_task_duration",
        "_background_task_total",
        "_compaction_duration",
        "_compaction_removed",
    )

    def __init__(
        self,
        query_duration,
        query_results,
        query_tokens,
        add_duration,
        add_total,
        functions_total,
        graph_edges_total,
        llm_calls,
        llm_duration,
        background_task_duration,
        background_task_total,
        compaction_duration,
        compaction_removed,
    ) -> None:
        self._query_duration = query_duration
        self._query_results = query_results
        self._query_tokens = query_tokens
        self._add_duration = add_duration
        self._add_total = add_total
        self._functions_total = functions_total
        self._graph_edges_total = graph_edges_total
        self._llm_calls = llm_calls
        self._llm_duration = llm_duration
        self._background_task_duration = background_task_duration
        self._background_task_total = background_task_total
        self._compaction_duration = compaction_duration
        self._compaction_removed = compaction_removed

    def query_duration(self, scope: str, backend: str, duration_seconds: float) -> None:
        self._query_duration.labels(scope=scope, backend=backend).observe(
            duration_seconds
        )

    def query_results(self, scope: str, truncated: bool, count: int) -> None:
        self._query_results.labels(scope=scope, truncated=str(truncated).lower()).inc(
            count
        )

    def query_tokens(self, scope: str, tokens: int) -> None:
        self._query_tokens.labels(scope=scope).observe(tokens)

    def add_duration(self, backend: str, duration_seconds: float) -> None:
        self._add_duration.labels(backend=backend).observe(duration_seconds)

    def add_total(self, status: str) -> None:
        self._add_total.labels(status=status).inc()

    def functions_total(self, memory_type: str, backend: str, value: float) -> None:
        self._functions_total.labels(memory_type=memory_type, backend=backend).set(
            value
        )

    def graph_edges_total(self, edge_type: str, value: float) -> None:
        self._graph_edges_total.labels(edge_type=edge_type).set(value)

    def llm_calls(self, operation: str) -> None:
        self._llm_calls.labels(operation=operation).inc()

    def llm_duration(self, operation: str, duration_seconds: float) -> None:
        self._llm_duration.labels(operation=operation).observe(duration_seconds)

    def background_task_duration(self, task_type: str, duration_seconds: float) -> None:
        self._background_task_duration.labels(task_type=task_type).observe(
            duration_seconds
        )

    def background_task_total(self, task_type: str, status: str) -> None:
        self._background_task_total.labels(task_type=task_type, status=status).inc()

    def compaction_duration(self, trigger: str, duration_seconds: float) -> None:
        self._compaction_duration.labels(trigger=trigger).observe(duration_seconds)

    def compaction_removed(self, stage: str, count: int) -> None:
        self._compaction_removed.labels(stage=stage).inc(count)


# ── Module-level singleton accessor ───────────────────────────────────────


_metrics_instance: "Metrics | None" = None


def get_metrics() -> Metrics:
    """Return the thread-safe Metrics singleton.

    The underlying prometheus_client objects are only created on first call.
    """
    global _metrics_instance
    if _metrics_instance is None:
        _metrics_instance = Metrics()
    return _metrics_instance


def get_registry() -> "CollectorRegistry | None":
    """Return the CollectorRegistry, or None if prometheus_client is unavailable."""
    m = get_metrics()
    return m._registry  # type: ignore[attr-defined]
