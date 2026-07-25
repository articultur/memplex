"""PostgreSQL memory backend (R1 roadmap item).

Stores Functions as JSONB rows in a single ``memplex_functions`` table,
with a generated ``tsvector`` column for native PostgreSQL full-text
search (replacing the SQLite FTS5 sidecar used by the lite backend).
Edges and observations get their own tables.

Synchronous (psycopg2); the MemoryStore protocol is synchronous, so we
avoid asyncpg's async/sync bridging complexity. A connection is opened
lazily on first use so importing this module never requires a database.

Backend selection: ``create_store("postgres", path="dbname=memplex ...")``
or via config (``storage.backend = "postgres"``, ``storage.path`` = DSN).

Schema::

    CREATE TABLE IF NOT EXISTS memplex_functions (
        id          TEXT PRIMARY KEY,
        data        JSONB NOT NULL,
        updated_at  TIMESTAMPTZ,
        search_tsv  TSVECTOR GENERATED ALWAYS AS (
            to_tsvector('simple', coalesce(data->>'name','') || ' ' ||
                                 coalesce(data->>'domain','') || ' ' ||
                                 coalesce((data->>'trigger_text'),''))
        ) STORED
    );
    CREATE INDEX IF NOT EXISTS fts_functions_idx
        ON memplex_functions USING GIN (search_tsv);

The tsvector is generated from the JSONB so inserts/updates stay in sync
without triggers. For simplicity, trigger/action text is pre-flattened
into ``data->>'trigger_text'`` at write time by this backend.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, List, Optional

from memplex.models import (
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

logger = logging.getLogger(__name__)


# ── Serialization helpers (mirror LiteMemoryStore shape, JSONB-safe) ──


def _func_to_json(func: Function) -> dict:
    """Flatten a Function into a JSONB-safe dict, including a pre-built
    search text field for the generated tsvector column."""
    trigger_text = " ".join(fv.desc for fv in func.trigger)
    action_text = " ".join(fv.desc for fv in func.action)
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
        "created_at": _iso(func.created_at),
        "updated_at": _iso(func.updated_at),
        "origin_session": func.origin_session,
        "access_count": func.access_count,
        "last_accessed_at": _iso(func.last_accessed_at),
        "source_paragraphs": func.source_paragraphs,
        "needs_review": func.needs_review,
        "content_hash": func.content_hash,
        "trigger": [_fv_to_json(fv) for fv in func.trigger],
        "condition": [_fv_to_json(fv) for fv in func.condition],
        "action": [_fv_to_json(fv) for fv in func.action],
        "benefit": [_fv_to_json(fv) for fv in func.benefit],
        "attributes": func.attributes,
        "cross_references": func.cross_references,
        # Flattened text for tsvector generation (PG side).
        "trigger_text": trigger_text,
        "action_text": action_text,
    }


def _fv_to_json(fv: FieldValue) -> dict:
    return {
        "desc": fv.desc,
        "sources": fv.sources,
        "source_method": fv.source_method,
        "weight": fv.weight,
    }


def _fv_from_json(d: dict) -> FieldValue:
    return FieldValue(
        desc=d.get("desc", ""),
        sources=d.get("sources", []),
        source_method=d.get("source_method", "manual"),
        weight=d.get("weight", 1.0),
    )


def _iso(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _func_from_json(d: dict) -> Function:
    try:
        st = SourceType(d.get("source_type", "wiki"))
    except ValueError:
        st = SourceType.WIKI
    return Function(
        id=d["id"],
        memory_type=d.get("memory_type", "function"),
        name=d.get("name", ""),
        name_normalized=d.get("name_normalized", d.get("name", "").lower()),
        domain=d.get("domain", ""),
        confidence=d.get("confidence", 0.5),
        source_type=st,
        owner=d.get("owner"),
        version=d.get("version", 1),
        created_at=d.get("created_at") or None,
        updated_at=d.get("updated_at") or None,
        origin_session=d.get("origin_session"),
        access_count=d.get("access_count", 0),
        last_accessed_at=d.get("last_accessed_at") or None,
        source_paragraphs=d.get("source_paragraphs", []),
        needs_review=d.get("needs_review", False),
        content_hash=d.get("content_hash"),
        trigger=[_fv_from_json(fv) for fv in d.get("trigger", [])],
        condition=[_fv_from_json(fv) for fv in d.get("condition", [])],
        action=[_fv_from_json(fv) for fv in d.get("action", [])],
        benefit=[_fv_from_json(fv) for fv in d.get("benefit", [])],
        attributes=d.get("attributes", {}),
        cross_references=d.get("cross_references", []),
    )


class PostgresMemoryStore:
    """PostgreSQL-backed MemoryStore (JSONB + tsvector + optional pgvector).

    Construction is lazy: the connection is opened on first use so the
    module imports cleanly without a database. Requires the optional
    ``postgres`` dependency (psycopg2).

    Optional semantic search: when ``vector_dim`` > 0 (set via
    ``MEMPLEX_PGVECTOR_DIM`` env or constructor arg), the store enables
    the pgvector extension, adds an ``embedding`` column of that
    dimension, and ``vector_search`` runs a hybrid (tsv + vector cosine)
    merge. An optional ``embedder`` (any object with ``.embed(text) ->
    list[float]``) supplies the vectors written on ``add``; without it,
    pgvector columns stay NULL and search degrades to tsvector-only.
    """

    def __init__(self, dsn: str, vector_dim: int = 0, embedder: Any = None) -> None:
        import os

        self._dsn = dsn
        self._conn: Any = None
        # pgvector dimension. 0 disables vector search (tsvector-only).
        self._vector_dim: int = int(os.environ.get("MEMPLEX_PGVECTOR_DIM", vector_dim) or 0)
        self._embedder = embedder  # optional: object with .embed(text) -> list[float]

    # ── Connection + schema ─────────────────────────────────────────

    def _connect(self):
        if self._conn is not None:
            return self._conn
        try:
            import psycopg2  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "PostgresMemoryStore requires psycopg2. Install with: "
                "pip install memplex[postgres] (or psycopg2-binary)."
            ) from exc
        self._conn = psycopg2.connect(self._dsn)
        self._conn.autocommit = False
        self._ensure_schema()
        return self._conn

    def _ensure_schema(self) -> None:
        cur = self._conn.cursor()
        try:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS memplex_functions (
                    id          TEXT PRIMARY KEY,
                    data        JSONB NOT NULL,
                    updated_at  TIMESTAMPTZ,
                    search_tsv  TSVECTOR GENERATED ALWAYS AS (
                        to_tsvector('simple',
                            coalesce(data->>'name','') || ' ' ||
                            coalesce(data->>'domain','') || ' ' ||
                            coalesce(data->>'trigger_text','') || ' ' ||
                            coalesce(data->>'action_text','')
                        )
                    ) STORED
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS fts_functions_idx "
                "ON memplex_functions USING GIN (search_tsv)"
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS memplex_edges (
                    source      TEXT,
                    target      TEXT,
                    edge_type   TEXT,
                    weight      REAL,
                    evidence    JSONB,
                    created_at  TIMESTAMPTZ,
                    PRIMARY KEY (source, target, edge_type)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS memplex_observations (
                    id          TEXT PRIMARY KEY,
                    data        JSONB NOT NULL,
                    created_at  TIMESTAMPTZ
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS memplex_changelog (
                    id          SERIAL PRIMARY KEY,
                    func_id     TEXT,
                    ts          TIMESTAMPTZ,
                    event_type  TEXT,
                    description TEXT,
                    source      TEXT,
                    actor       TEXT
                )
                """
            )
            # Optional pgvector semantic search. When enabled, create the
            # extension + an embedding column of the configured dimension.
            # Idempotent: re-runs are no-ops once the column exists.
            if self._vector_dim > 0:
                try:
                    cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
                    cur.execute(
                        f"ALTER TABLE memplex_functions "
                        f"ADD COLUMN IF NOT EXISTS embedding vector({self._vector_dim})"
                    )
                    # IVFFlat index for approximate nearest-neighbour search.
                    cur.execute(
                        "CREATE INDEX IF NOT EXISTS fts_functions_vec_idx "
                        "ON memplex_functions USING ivfflat (embedding vector_cosine_ops) "
                        "WITH (lists = 100)"
                    )
                except Exception as exc:
                    # pgvector not installed -> degrade gracefully to tsvector-only.
                    logger.warning("pgvector unavailable, falling back to tsvector search: %s", exc)
                    self._vector_dim = 0
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()

    def _execute(self, sql: str, params: tuple = (), *, commit: bool = True):
        conn = self._connect()
        cur = conn.cursor()
        try:
            cur.execute(sql, params)
            if commit:
                conn.commit()
            return cur
        except Exception:
            conn.rollback()
            raise

    # ── Write operations ────────────────────────────────────────────

    def _embed_text(self, func: Function) -> Optional[str]:
        """Return a pgvector-literal string for *func* or None when disabled.

        pgvector accepts the text form ``[1.0, 2.0, ...]``; we pass it as
        a string parameter to avoid adapter complexity.
        """
        if self._vector_dim <= 0 or self._embedder is None:
            return None
        try:
            text = f"{func.name} {func.domain or ''} " + " ".join(
                fv.desc for fv in (func.trigger + func.action)
            )
            vec = self._embedder.embed(text)
            if vec and len(vec) == self._vector_dim:
                return str(list(vec))
        except Exception as exc:
            logger.debug("pgvector embed failed for %s, storing NULL: %s", func.id, exc)
        return None

    def add(self, func: Function, source: SourceDocument) -> None:
        data = _func_to_json(func)
        embedding = self._embed_text(func)
        if embedding is not None:
            self._execute(
                """
                INSERT INTO memplex_functions (id, data, updated_at, embedding)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    data = EXCLUDED.data,
                    updated_at = EXCLUDED.updated_at,
                    embedding = EXCLUDED.embedding
                """,
                (func.id, json.dumps(data), _iso(func.updated_at) or datetime.now(timezone.utc), embedding),
            )
        else:
            self._execute(
                """
                INSERT INTO memplex_functions (id, data, updated_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    data = EXCLUDED.data,
                    updated_at = EXCLUDED.updated_at
                """,
                (func.id, json.dumps(data), _iso(func.updated_at) or datetime.now(timezone.utc)),
            )

    def add_batch(self, funcs, source: SourceDocument) -> None:
        conn = self._connect()
        cur = conn.cursor()
        try:
            for func in funcs:
                data = _func_to_json(func)
                cur.execute(
                    """
                    INSERT INTO memplex_functions (id, data, updated_at)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        data = EXCLUDED.data,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (
                        func.id,
                        json.dumps(data),
                        _iso(func.updated_at) or datetime.now(timezone.utc),
                    ),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()

    def add_observation(self, observation: Observation) -> None:
        data = {
            "id": observation.id,
            "event": getattr(observation, "event", ""),
            "context": getattr(observation, "context", ""),
        }
        self._execute(
            """
            INSERT INTO memplex_observations (id, data, created_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data
            """,
            (observation.id, json.dumps(data), datetime.now(timezone.utc)),
        )

    def increment_access(self, func_id: str) -> None:
        self._execute(
            """
            UPDATE memplex_functions
            SET data = jsonb_set(
                    jsonb_set(data, '{access_count}',
                        to_jsonb((data->>'access_count')::int + 1)),
                    '{last_accessed_at}', to_jsonb(%s))
            WHERE id = %s
            """,
            (datetime.now(timezone.utc).isoformat(), func_id),
        )

    def increment_access_batch(self, func_ids) -> None:
        conn = self._connect()
        cur = conn.cursor()
        try:
            now = datetime.now(timezone.utc).isoformat()
            for fid in func_ids:
                cur.execute(
                    """
                    UPDATE memplex_functions
                    SET data = jsonb_set(
                            jsonb_set(data, '{access_count}',
                                to_jsonb((data->>'access_count')::int + 1)),
                            '{last_accessed_at}', to_jsonb(%s))
                    WHERE id = %s
                    """,
                    (now, fid),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()

    # ── Retrieval ───────────────────────────────────────────────────

    def vector_search(self, text: str, top_k: int = 5) -> List[SearchResult]:
        """Hybrid search: tsvector full-text + optional pgvector cosine.

        When pgvector is enabled and an embedder is configured, runs both a
        tsvector and a vector-cosine query and merges them with Reciprocal
        Rank Fusion (RRF). Otherwise degrades to tsvector-only.
        """
        # --- tsvector leg (always runs) ---
        cur = self._execute(
            """
            SELECT id, data, ts_rank(search_tsv, plainto_tsquery('simple', %s)) AS score
            FROM memplex_functions
            WHERE search_tsv @@ plainto_tsquery('simple', %s)
            ORDER BY score DESC
            LIMIT %s
            """,
            (text, text, top_k * 2),
            commit=False,
        )
        tsv_rows = cur.fetchall()
        cur.close()

        # --- pgvector leg (only when enabled + embedder available) ---
        vec_rows = []
        if self._vector_dim > 0 and self._embedder is not None:
            try:
                qvec = self._embedder.embed(text)
                if qvec and len(qvec) == self._vector_dim:
                    cur = self._execute(
                        """
                        SELECT id, data, 1 - (embedding <=> %s::vector) AS score
                        FROM memplex_functions
                        WHERE embedding IS NOT NULL
                        ORDER BY embedding <=> %s::vector
                        LIMIT %s
                        """,
                        (str(list(qvec)), str(list(qvec)), top_k * 2),
                        commit=False,
                    )
                    vec_rows = cur.fetchall()
                    cur.close()
            except Exception as exc:
                logger.debug("pgvector search leg failed, using tsv only: %s", exc)

        # --- RRF merge ---
        return self._rrf_merge(tsv_rows, vec_rows, top_k)

    @staticmethod
    def _rrf_merge(tsv_rows, vec_rows, top_k, k: int = 60) -> List[SearchResult]:
        """Reciprocal Rank Fusion of the two result legs."""
        scores: dict = {}
        meta: dict = {}
        for rank, row in enumerate(tsv_rows):
            fid = row[0]
            scores[fid] = scores.get(fid, 0.0) + 1.0 / (k + rank + 1)
            meta[fid] = row[1]
        for rank, row in enumerate(vec_rows):
            fid = row[0]
            scores[fid] = scores.get(fid, 0.0) + 1.0 / (k + rank + 1)
            if fid not in meta:
                meta[fid] = row[1]
        ordered = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_k]
        results = []
        for fid, score in ordered:
            data = meta[fid]
            data = data if isinstance(data, dict) else json.loads(data)
            results.append(
                SearchResult(
                    func_id=fid,
                    name=data.get("name", ""),
                    domain=data.get("domain", ""),
                    relevance_score=score,
                    summary=data.get("trigger_text", "") or data.get("name", ""),
                )
            )
        return results

    def fts_search(self, text: str, top_k: int = 10) -> List[SearchResult]:
        return self.vector_search(text, top_k=top_k)

    def filter(self, filters: SearchFilters) -> List[Function]:
        # Simple owner filter; JSONB attributes filter could be added.
        owner = getattr(filters, "owner", None)
        if owner:
            cur = self._execute(
                "SELECT data FROM memplex_functions WHERE data->>'owner' = %s",
                (owner,),
                commit=False,
            )
        else:
            cur = self._execute("SELECT data FROM memplex_functions", commit=False)
        funcs = []
        for row in cur.fetchall():
            data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
            funcs.append(_func_from_json(data))
        cur.close()
        return funcs

    def get(self, func_id: str) -> Optional[Function]:
        cur = self._execute(
            "SELECT data FROM memplex_functions WHERE id = %s",
            (func_id,),
            commit=False,
        )
        row = cur.fetchone()
        cur.close()
        if row is None:
            return None
        data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        return _func_from_json(data)

    def get_neighbors(self, func_id: str, max_hops: int = 1) -> List[Function]:
        cur = self._execute(
            """
            WITH RECURSIVE hop(id) AS (
                SELECT target FROM memplex_edges WHERE source = %s
                UNION
                SELECT e.target FROM memplex_edges e JOIN hop ON e.source = hop.id
            )
            SELECT f.data FROM memplex_functions f WHERE f.id IN (SELECT id FROM hop)
            """,
            (func_id,),
            commit=False,
        )
        funcs = []
        for row in cur.fetchall():
            data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
            funcs.append(_func_from_json(data))
        cur.close()
        return funcs

    def get_graph(self, func_ids: Optional[List[str]] = None) -> GraphData:
        if func_ids:
            cur = self._execute(
                "SELECT source, target, edge_type, weight, evidence, created_at "
                "FROM memplex_edges WHERE source = ANY(%s)",
                (list(func_ids),),
                commit=False,
            )
        else:
            cur = self._execute(
                "SELECT source, target, edge_type, weight, evidence, created_at FROM memplex_edges",
                commit=False,
            )
        edges = []
        for row in cur.fetchall():
            evidence = row[4]
            if isinstance(evidence, str):
                try:
                    evidence = json.loads(evidence)
                except Exception:
                    evidence = []
            edges.append(
                GraphEdge(
                    source=row[0],
                    target=row[1],
                    edge_type=row[2],
                    weight=float(row[3] or 1.0),
                    evidence=evidence or [],
                    created_at=row[5],
                )
            )
        cur.close()
        nodes = self.list_functions(limit=100000)
        return GraphData(nodes=nodes, edges=edges)

    def get_timeline(self, func_id: str, limit: int = 20) -> List[ChangelogEvent]:
        cur = self._execute(
            "SELECT func_id, ts, event_type, description, source, actor "
            "FROM memplex_changelog WHERE func_id = %s ORDER BY ts DESC LIMIT %s",
            (func_id, limit),
            commit=False,
        )
        events = []
        for row in cur.fetchall():
            events.append(
                ChangelogEvent(
                    func_id=row[0],
                    timestamp=row[1],
                    event_type=row[2],
                    description=row[3],
                    source=row[4],
                    actor=row[5],
                )
            )
        cur.close()
        return events

    def list_functions(
        self, offset: int = 0, limit: int = 1000, owner: Optional[str] = None
    ) -> List[Function]:
        if owner:
            cur = self._execute(
                "SELECT data FROM memplex_functions WHERE data->>'owner' = %s "
                "ORDER BY data->>'updated_at' DESC OFFSET %s LIMIT %s",
                (owner, offset, limit),
                commit=False,
            )
        else:
            cur = self._execute(
                "SELECT data FROM memplex_functions ORDER BY data->>'updated_at' DESC "
                "OFFSET %s LIMIT %s",
                (offset, limit),
                commit=False,
            )
        funcs = []
        for row in cur.fetchall():
            data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
            funcs.append(_func_from_json(data))
        cur.close()
        return funcs

    def list_changes_since(self, since: Optional[str] = None, limit: int = 100000) -> List[Function]:
        """Incremental query: push the updated_at filter into Postgres.

        Overrides the base default (which loads all then filters in Python)
        so /sync/changes does not scan the entire table on every pull.
        """
        if since is None:
            return self.list_functions(limit=limit)
        cur = self._execute(
            "SELECT data FROM memplex_functions "
            "WHERE data->>'updated_at' > %s "
            "ORDER BY data->>'updated_at' ASC LIMIT %s",
            (since, limit),
            commit=False,
        )
        funcs = []
        for row in cur.fetchall():
            data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
            funcs.append(_func_from_json(data))
        cur.close()
        return funcs

    # ── Delete / merge / clear ──────────────────────────────────────

    def delete(self, func_id: str) -> None:
        self._execute("DELETE FROM memplex_functions WHERE id = %s", (func_id,))
        self._execute(
            "DELETE FROM memplex_edges WHERE source = %s OR target = %s", (func_id, func_id)
        )

    def merge(self, sub_graph: GraphData) -> MergeResult:
        # Insert edges; nodes are added via add() by the caller.
        conn = self._connect()
        cur = conn.cursor()
        added_edges = 0
        try:
            for edge in sub_graph.edges:
                evidence = json.dumps(edge.evidence or [])
                cur.execute(
                    """
                    INSERT INTO memplex_edges (source, target, edge_type, weight, evidence, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (source, target, edge_type) DO UPDATE SET weight = EXCLUDED.weight
                    """,
                    (
                        edge.source,
                        edge.target,
                        edge.edge_type,
                        float(edge.weight),
                        evidence,
                        edge.created_at or datetime.now(timezone.utc),
                    ),
                )
                added_edges += 1
            # Re-add nodes (upsert).
            for node in sub_graph.nodes:
                if hasattr(node, "id"):
                    data = _func_to_json(node)
                    cur.execute(
                        """
                        INSERT INTO memplex_functions (id, data, updated_at)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data, updated_at = EXCLUDED.updated_at
                        """,
                        (
                            node.id,
                            json.dumps(data),
                            _iso(getattr(node, "updated_at", None)) or datetime.now(timezone.utc),
                        ),
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
        return MergeResult(
            functions_merged=len(sub_graph.nodes),
            edges_added=added_edges,
            conflicts_detected=0,
        )

    def clear(self) -> None:
        self._execute("DELETE FROM memplex_functions")
        self._execute("DELETE FROM memplex_edges")
        self._execute("DELETE FROM memplex_observations")
        self._execute("DELETE FROM memplex_changelog")
