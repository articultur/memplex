"""LiteMemoryStore -- in-memory + JSON persistence backend.

Data paths::

    ~/.memplex/memory.json      Functions + graph edges
    ~/.memplex/changelog.json   Changelog events (via ChangelogStore)
    ~/.memplex/memory.json.fts5.db  Local SQLite FTS5 sidecar index

All data is held in memory and flushed to JSON on every write.
Atomic replacement (write-to-temp + rename) guards against partial writes.

Single-thread assumption: optimistic lock is skipped.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from memplex.models import (
    BatchResult,
    ChangelogEvent,
    FieldValue,
    Function,
    GraphData,
    GraphEdge,
    MergeResult,
    Observation,
    SearchFilters,
    SearchResult,
    SourceDocument,
    SourceType,
)
from memplex.storage.changelog import ChangelogStore
from memplex.storage.lite.search_index import SQLiteFTSIndex, local_bm25_search

logger = logging.getLogger(__name__)


# ── Serialization helpers ────────────────────────────────────────────


def _json_serializer(obj: Any) -> Any:
    """Default serializer for ``json.dumps``."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, SourceType):
        return obj.value
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _serialize_field_value(fv: FieldValue) -> dict:
    return {
        "desc": fv.desc,
        "sources": fv.sources,
        "source_method": fv.source_method,
        "weight": fv.weight,
        "observation": fv.observation,
        "created_at": (
            fv.created_at.isoformat() if isinstance(fv.created_at, datetime) else fv.created_at
        ),
        "status": fv.status,
    }


def _deserialize_field_value(d: dict) -> FieldValue:
    created_at = d.get("created_at")
    if isinstance(created_at, str):
        created_at = datetime.fromisoformat(created_at)
    return FieldValue(
        desc=d["desc"],
        sources=d.get("sources", []),
        source_method=d.get("source_method", "rule_based"),
        weight=d.get("weight", 1.0),
        observation=d.get("observation"),
        created_at=created_at,
        status=d.get("status", "active"),
    )


def _serialize_function(func: Function) -> dict:
    return {
        "id": func.id,
        "memory_type": func.memory_type,
        "name": func.name,
        "name_normalized": func.name_normalized,
        "domain": func.domain,
        "confidence": func.confidence,
        "source_type": func.source_type.value
        if isinstance(func.source_type, SourceType)
        else func.source_type,
        "owner": func.owner,
        "version": func.version,
        "created_at": func.created_at,
        "updated_at": func.updated_at,
        "origin_session": func.origin_session,
        "access_count": func.access_count,
        "last_accessed_at": func.last_accessed_at,
        "source_paragraphs": func.source_paragraphs,
        "needs_review": func.needs_review,
        "needs_review_until": func.needs_review_until,
        "content_hash": func.content_hash,
        "trigger": [_serialize_field_value(fv) for fv in func.trigger],
        "condition": [_serialize_field_value(fv) for fv in func.condition],
        "action": [_serialize_field_value(fv) for fv in func.action],
        "benefit": [_serialize_field_value(fv) for fv in func.benefit],
        "attributes": func.attributes,
        "cross_references": func.cross_references,
        "priority_from_source": func.priority_from_source,
        "source_authority": func.source_authority,
    }


def _deserialize_function(d: dict) -> Function:
    source_type = d.get("source_type", "wiki")
    if isinstance(source_type, str):
        try:
            source_type = SourceType(source_type)
        except ValueError:
            source_type = SourceType.WIKI
    return Function(
        id=d["id"],
        memory_type=d.get("memory_type", "function"),
        name=d.get("name", ""),
        name_normalized=d.get("name_normalized", ""),
        domain=d.get("domain"),
        confidence=d.get("confidence", 1.0),
        source_type=source_type,
        owner=d.get("owner"),
        version=d.get("version", 1),
        created_at=d.get("created_at"),
        updated_at=d.get("updated_at"),
        origin_session=d.get("origin_session"),
        access_count=d.get("access_count", 0),
        last_accessed_at=d.get("last_accessed_at"),
        source_paragraphs=d.get("source_paragraphs", []),
        needs_review=d.get("needs_review", False),
        needs_review_until=d.get("needs_review_until"),
        content_hash=d.get("content_hash"),
        trigger=[_deserialize_field_value(fv) for fv in d.get("trigger", [])],
        condition=[_deserialize_field_value(fv) for fv in d.get("condition", [])],
        action=[_deserialize_field_value(fv) for fv in d.get("action", [])],
        benefit=[_deserialize_field_value(fv) for fv in d.get("benefit", [])],
        attributes=d.get("attributes", {}),
        cross_references=d.get("cross_references", []),
        priority_from_source=d.get("priority_from_source"),
        source_authority=d.get("source_authority"),
    )


def _serialize_edge(edge: GraphEdge) -> dict:
    return {
        "source": edge.source,
        "target": edge.target,
        "edge_type": edge.edge_type,
        "weight": edge.weight,
        "evidence": edge.evidence,
        "created_at": (
            edge.created_at.isoformat()
            if isinstance(edge.created_at, datetime)
            else edge.created_at
        ),
    }


def _deserialize_edge(d: dict) -> GraphEdge:
    created_at = d.get("created_at")
    if isinstance(created_at, str):
        created_at = datetime.fromisoformat(created_at)
    return GraphEdge(
        source=d["source"],
        target=d["target"],
        edge_type=d["edge_type"],
        weight=d.get("weight", 1.0),
        evidence=d.get("evidence", []),
        created_at=created_at,
    )


def _serialize_observation(obs: Observation) -> dict:
    return {
        "id": obs.id,
        "memory_type": obs.memory_type,
        "name": obs.name,
        "domain": obs.domain,
        "event": obs.event,
        "context": obs.context,
        "observed_at": obs.observed_at,
        "actor": obs.actor,
        "origin_session": obs.origin_session,
        "created_at": obs.created_at,
        "updated_at": obs.updated_at,
    }


def _deserialize_observation(d: dict) -> Observation:
    return Observation(
        id=d.get("id", ""),
        memory_type=d.get("memory_type", "observation"),
        name=d.get("name", ""),
        domain=d.get("domain"),
        event=d.get("event", ""),
        context=d.get("context", ""),
        observed_at=d.get("observed_at"),
        actor=d.get("actor", "system"),
        origin_session=d.get("origin_session"),
        created_at=d.get("created_at"),
        updated_at=d.get("updated_at"),
    )


# ── Merge helpers ────────────────────────────────────────────────────


def _merge_field_values(
    existing: List[FieldValue],
    incoming: List[FieldValue],
) -> List[FieldValue]:
    """Merge incoming FieldValues into existing.  Duplicates (by desc) are
    skipped; weight and observation are taken from the newer entry.
    """
    seen = {fv.desc for fv in existing}
    merged = list(existing)
    for fv in incoming:
        if fv.desc not in seen:
            merged.append(fv)
            seen.add(fv.desc)
    return merged


def _normalize_name(name: str) -> str:
    """Produce a normalised form for dedup matching."""
    return name.strip().lower()


# ── LiteMemoryStore ──────────────────────────────────────────────────


class LiteMemoryStore:
    """InMemory + JSON persistence backend.

    Parameters
    ----------
    path:
        Root JSON file path.  Defaults to ``~/.memplex/memory.json``.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or Path("~/.memplex/memory.json").expanduser()
        self._functions: Dict[str, Function] = {}
        self._name_index: Dict[str, str] = {}  # name_normalized -> func_id
        self._edges: List[GraphEdge] = []
        self._observations: List[Observation] = []
        self._changelog = ChangelogStore(path=self._path.parent / "changelog.json")
        self._fts_index = SQLiteFTSIndex(
            path=self._path.with_name(f"{self._path.name}.fts5.db"),
            functions=self._functions,
            text_factory=self._function_to_search_text,
        )
        self._load()

    # ── Public: Write ───────────────────────────────────────────────

    def add(self, func: Function, source: SourceDocument) -> None:
        norm = _normalize_name(func.name_normalized or func.name)
        existing_id = self._name_index.get(norm)

        if existing_id and existing_id in self._functions:
            existing = self._functions[existing_id]
            # Merge FieldValues
            existing.trigger = _merge_field_values(existing.trigger, func.trigger)
            existing.condition = _merge_field_values(existing.condition, func.condition)
            existing.action = _merge_field_values(existing.action, func.action)
            existing.benefit = _merge_field_values(existing.benefit, func.benefit)
            # Merge source paragraphs
            for sp in func.source_paragraphs:
                if sp not in existing.source_paragraphs:
                    existing.source_paragraphs.append(sp)
            existing.updated_at = datetime.now(timezone.utc).isoformat()
            existing.version += 1

            self._changelog.append(
                ChangelogEvent(
                    func_id=existing.id,
                    timestamp=datetime.now(),
                    event_type="updated",
                    description="Merged fields from source",
                    source=getattr(source, "source_path", None) or getattr(source, "url", "") or "",
                    actor="system",
                )
            )
        else:
            self._functions[func.id] = func
            self._name_index[norm] = func.id

            self._changelog.append(
                ChangelogEvent(
                    func_id=func.id,
                    timestamp=datetime.now(),
                    event_type="created",
                    description=f"Created function: {func.name}",
                    source=getattr(source, "source_path", None) or getattr(source, "url", "") or "",
                    actor="system",
                )
            )

        self._save()

    def add_batch(
        self,
        funcs: List[Function],
        sources: List[SourceDocument],
    ) -> BatchResult:
        result = BatchResult(total=len(funcs))
        for func, src in zip(funcs, sources):
            try:
                self.add(func, src)
                result.succeeded += 1
            except Exception as exc:
                result.failed_items.append(
                    {
                        "func_id": func.id,
                        "name": func.name,
                        "error": str(exc),
                    }
                )
        return result

    def add_observation(self, observation: Observation) -> None:
        self._observations.append(observation)
        self._save()

    def increment_access(self, func_id: str) -> None:
        func = self._functions.get(func_id)
        if func is None:
            return
        func.access_count += 1
        func.last_accessed_at = datetime.now(timezone.utc).isoformat()
        self._save()

    def increment_access_batch(self, func_ids) -> None:
        """Update access_count for many funcs with a SINGLE persistence pass.

        Overrides the base default (which would call increment_access N
        times -> N full JSON rewrites). Critical for query latency: a
        query returning K results used to trigger K full-store writes;
        now it triggers one.
        """
        now = datetime.now(timezone.utc).isoformat()
        touched = False
        for func_id in func_ids:
            func = self._functions.get(func_id)
            if func is None:
                continue
            func.access_count += 1
            func.last_accessed_at = now
            touched = True
        if touched:
            self._save()

    # ── Public: Retrieval ───────────────────────────────────────────

    def vector_search(self, text: str, top_k: int = 5) -> List[SearchResult]:
        """Local SQLite FTS5/BM25 + trigram search over Function text."""
        return self._search_with_fallback(text, top_k=top_k)

    def fts_search(self, text: str, top_k: int = 10) -> List[SearchResult]:
        """Local full-text search using FTS5 BM25, phrase, and trigram matching."""
        return self._search_with_fallback(text, top_k=top_k)

    def filter(self, filters: SearchFilters) -> List[Function]:
        results: List[Function] = []
        for func in self._functions.values():
            if not self._matches_filter(func, filters):
                continue
            results.append(func)
        return results

    # ── Public: Read ────────────────────────────────────────────────

    def get(self, func_id: str) -> Optional[Function]:
        return self._functions.get(func_id)

    def get_neighbors(
        self,
        func_id: str,
        edge_types: Optional[List[str]] = None,
        max_hops: int = 1,
    ) -> List[Function]:
        if max_hops < 1:
            return []

        # BFS
        visited: set = {func_id}
        current_level = {func_id}
        neighbor_ids: set = set()

        for _ in range(max_hops):
            next_level: set = set()
            for fid in current_level:
                for edge in self._edges:
                    if edge_types and edge.edge_type not in edge_types:
                        continue
                    if edge.source == fid and edge.target not in visited:
                        next_level.add(edge.target)
                    elif edge.target == fid and edge.source not in visited:
                        next_level.add(edge.source)
            visited |= next_level
            neighbor_ids |= next_level
            current_level = next_level

        return [self._functions[fid] for fid in neighbor_ids if fid in self._functions]

    def get_graph(self, func_ids: Optional[List[str]] = None) -> GraphData:
        if func_ids is None:
            nodes = list(self._functions.values())
            edges = list(self._edges)
        else:
            id_set = set(func_ids)
            nodes = [self._functions[fid] for fid in func_ids if fid in self._functions]
            edges = [e for e in self._edges if e.source in id_set or e.target in id_set]
        return GraphData(nodes=nodes, edges=edges)

    def get_timeline(self, func_id: str, limit: int = 20) -> List[ChangelogEvent]:
        return self._changelog.get_timeline(func_id, limit)

    def list_functions(
        self,
        offset: int = 0,
        limit: int = 1000,
        owner: Optional[str] = None,
    ) -> List[Function]:
        funcs = list(self._functions.values())
        if owner is not None:
            funcs = [f for f in funcs if f.owner == owner]
        return funcs[offset : offset + limit]

    def list_changes_since(
        self, since: Optional[str] = None, limit: int = 100000
    ) -> List[Function]:
        """Incremental sync query: filter by updated_at at the dict level.

        Avoids serializing all Functions when only a few changed since the
        last pull. Overrides the base default for the lite in-memory store.
        """
        if since is None:
            return list(self._functions.values())[:limit]
        return [f for f in self._functions.values() if (f.updated_at or "") > since][:limit]

    # ── Public: Delete / Merge / Clear ──────────────────────────────

    def delete(self, func_id: str) -> None:
        self._functions.pop(func_id, None)
        # Remove from name index
        to_remove = [norm for norm, fid in self._name_index.items() if fid == func_id]
        for norm in to_remove:
            del self._name_index[norm]
        # Remove edges referencing this function
        self._edges = [e for e in self._edges if e.source != func_id and e.target != func_id]
        self._save()

    def merge(self, sub_graph: GraphData) -> MergeResult:
        result = MergeResult(merged=True)
        # Merge nodes
        for node in sub_graph.nodes:
            func_id = getattr(node, "id", None)
            if not func_id:
                continue
            if func_id in self._functions:
                existing = self._functions[func_id]
                if hasattr(node, "trigger"):
                    existing.trigger = _merge_field_values(existing.trigger, node.trigger)
                if hasattr(node, "condition"):
                    existing.condition = _merge_field_values(existing.condition, node.condition)
                if hasattr(node, "action"):
                    existing.action = _merge_field_values(existing.action, node.action)
                if hasattr(node, "benefit"):
                    existing.benefit = _merge_field_values(existing.benefit, node.benefit)
                existing.updated_at = datetime.now(timezone.utc).isoformat()
                existing.version += 1
                result.updated_functions += 1
            else:
                self._functions[func_id] = node
                norm = _normalize_name(
                    getattr(node, "name_normalized", "") or getattr(node, "name", "")
                )
                if norm:
                    self._name_index[norm] = func_id
                result.new_functions += 1

        # Merge edges (skip duplicates)
        existing_edge_keys = {(e.source, e.target, e.edge_type) for e in self._edges}
        for edge in sub_graph.edges:
            key = (edge.source, edge.target, edge.edge_type)
            if key not in existing_edge_keys:
                self._edges.append(edge)
                existing_edge_keys.add(key)
                result.new_edges += 1

        self._save()
        return result

    def clear(self) -> None:
        self._functions.clear()
        self._name_index.clear()
        self._edges.clear()
        self._observations.clear()
        self._changelog.clear()
        self._save()

    # ── Persistence ─────────────────────────────────────────────────

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except PermissionError as exc:
            raise PermissionError(
                f"Cannot write to storage path {self._path.parent}. "
                f"Set MEMPLEX_STORAGE_PATH to a writable directory. "
                f"Original error: {exc}"
            ) from exc
        data = {
            "schema_version": 1,
            "functions": [_serialize_function(f) for f in self._functions.values()],
            "edges": [_serialize_edge(e) for e in self._edges],
            "observations": [_serialize_observation(o) for o in self._observations],
        }
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
        try:
            with open(tmp_fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, default=_json_serializer, ensure_ascii=False, indent=2)
            # Backup the previous good file before replacing (data safety).
            bak_path = self._path.with_suffix(".json.bak")
            if self._path.exists():
                try:
                    import shutil

                    shutil.copy2(str(self._path), str(bak_path))
                except Exception:
                    pass  # best-effort backup; do not block the write
            Path(tmp_path).replace(self._path)
        except Exception:
            Path(tmp_path).unlink(missing_ok=True)
            raise

    def _load(self) -> None:
        if not self._path.exists():
            return
        raw = None
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("Failed to load memory from %s, trying .bak", self._path)
            # Try the backup file if the primary is corrupted.
            bak = self._path.with_suffix(".json.bak")
            if bak.exists():
                try:
                    raw = json.loads(bak.read_text(encoding="utf-8"))
                    logger.info("Recovered memory from backup %s", bak)
                except Exception:
                    pass
            if raw is None:
                return

        for fd in raw.get("functions", []):
            func = _deserialize_function(fd)
            self._functions[func.id] = func
            norm = _normalize_name(func.name_normalized or func.name)
            if norm:
                self._name_index[norm] = func.id

        for ed in raw.get("edges", []):
            self._edges.append(_deserialize_edge(ed))

        for od in raw.get("observations", []):
            self._observations.append(_deserialize_observation(od))

    # ── Internal helpers ────────────────────────────────────────────

    def _search_with_fallback(self, text: str, top_k: int) -> List[SearchResult]:
        """Search with SQLite FTS5 first, then pure-Python local search."""
        try:
            results = self._sqlite_fts_search(text, top_k=top_k)
        except sqlite3.Error as exc:
            logger.debug("SQLite FTS5 search unavailable: %s", exc)
            self._fts_disabled = True
            results = []

        if results:
            return results
        return self._local_search(text, top_k=top_k)

    def _sqlite_fts_search(self, text: str, top_k: int) -> List[SearchResult]:
        """Search the SQLite FTS5 sidecar using bm25() plus trigram overlap."""
        ranked = self._fts_index.search(text, top_k=top_k)
        results: List[SearchResult] = []
        for func_id, score in ranked:
            func = self._functions.get(func_id)
            if func is None:
                continue
            func_text = self._function_to_search_text(func)
            relevance = score / (score + 1.0)
            results.append(
                SearchResult(
                    func_id=func.id,
                    name=func.name,
                    domain=func.domain or "",
                    relevance_score=relevance,
                    summary=func_text,
                    source_type=func.source_type,
                    created_at=func.created_at,
                    updated_at=func.updated_at,
                    origin=func.origin_session or "",
                )
            )
        return results

    def _local_search(self, text: str, top_k: int) -> List[SearchResult]:
        """Search Functions with local BM25 and fuzzy character overlap."""
        ranked = local_bm25_search(
            text=text,
            functions=self._functions,
            text_factory=self._function_to_search_text,
            top_k=top_k,
        )
        results: List[SearchResult] = []
        for func, func_text, score in ranked:
            relevance = score / (score + 1.0)
            results.append(
                SearchResult(
                    func_id=func.id,
                    name=func.name,
                    domain=func.domain or "",
                    relevance_score=relevance,
                    summary=func_text,
                    source_type=func.source_type,
                    created_at=func.created_at,
                    updated_at=func.updated_at,
                    origin=func.origin_session or "",
                )
            )
        return results

    @staticmethod
    def _function_to_search_text(func: Function) -> str:
        parts = [func.name, func.domain or ""]
        for fv in func.trigger:
            parts.append(fv.desc)
        for fv in func.action:
            parts.append(fv.desc)
        for fv in func.benefit:
            parts.append(fv.desc)
        return " ".join(parts)

    @staticmethod
    def _matches_filter(func: Function, filters: SearchFilters) -> bool:
        if filters.domain and func.domain not in filters.domain:
            return False
        if filters.source_type and func.source_type not in filters.source_type:
            return False
        if filters.confidence_min is not None:
            if func.confidence < filters.confidence_min:
                return False
        if filters.owner is not None and func.owner != filters.owner:
            return False
        if filters.needs_review is not None:
            if func.needs_review != filters.needs_review:
                return False
        # Datetime filters: compare ISO strings lexicographically
        if filters.updated_after is not None:
            after = (
                filters.updated_after.isoformat()
                if hasattr(filters.updated_after, "isoformat")
                else str(filters.updated_after)
            )
            if func.updated_at and func.updated_at < after:
                return False
        if filters.updated_before is not None:
            before = (
                filters.updated_before.isoformat()
                if hasattr(filters.updated_before, "isoformat")
                else str(filters.updated_before)
            )
            if func.updated_at and func.updated_at > before:
                return False
        return True
