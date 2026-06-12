"""Local search helpers for the lite storage backend."""

from __future__ import annotations

import math
import re
import sqlite3
from collections import Counter
from hashlib import sha1
from pathlib import Path
from typing import Callable, Dict, List, Optional

from memplex.models import Function

_TOKEN_RE = re.compile(r"[a-z0-9_./:-]+|[\u4e00-\u9fff]+", re.IGNORECASE)
_BM25_K1 = 1.5
_BM25_B = 0.75
_MAX_SQLITE_QUERY_TERMS = 64


def _tokenize_search_text(text: str) -> List[str]:
    """Tokenize text for local BM25 search without external dependencies."""
    tokens: List[str] = []
    for match in _TOKEN_RE.finditer(text.lower()):
        part = match.group(0)
        tokens.append(part)

        # CJK text often has no whitespace; add char n-grams for recall.
        if any("\u4e00" <= ch <= "\u9fff" for ch in part):
            chars = [ch for ch in part if "\u4e00" <= ch <= "\u9fff"]
            tokens.extend(chars)
            for size in (2, 3):
                tokens.extend(
                    "".join(chars[i : i + size])
                    for i in range(0, max(len(chars) - size + 1, 0))
                )
            continue

        # Code identifiers and kebab/snake/path fragments benefit from pieces.
        if any(sep in part for sep in ("_", "-", "/", ".", ":")):
            tokens.extend(filter(None, re.split(r"[_\-./:]+", part)))

    return [token for token in tokens if token]


def _character_ngrams(text: str, size: int = 3) -> set[str]:
    """Return compact character n-grams for fuzzy local matching."""
    normalized = re.sub(r"\s+", "", text.lower())
    if len(normalized) < size:
        return {normalized} if normalized else set()
    return {normalized[i : i + size] for i in range(len(normalized) - size + 1)}


def _encoded_trigram_tokens(text: str) -> List[str]:
    """Encode fuzzy trigrams as FTS-safe ASCII tokens."""
    return [
        "tri" + gram.encode("utf-8").hex()
        for gram in sorted(_character_ngrams(text))
        if gram
    ]


def _sqlite_match_query(tokens: List[str]) -> str:
    """Build a safe OR query for SQLite FTS5 MATCH."""
    unique_tokens = list(dict.fromkeys(token for token in tokens if token))
    quoted = [
        '"' + token.replace('"', '""') + '"'
        for token in unique_tokens[:_MAX_SQLITE_QUERY_TERMS]
    ]
    return " OR ".join(quoted)


class SQLiteFTSIndex:
    """SQLite FTS5 sidecar index for LiteMemoryStore search."""

    def __init__(
        self,
        path: Path,
        functions: Dict[str, Function],
        text_factory: Callable[[Function], str],
    ) -> None:
        self._path = path
        self._functions = functions
        self._text_factory = text_factory
        self._signature: tuple | None = None
        self._disabled = False

    def search(self, text: str, top_k: int) -> List[tuple[str, float]]:
        """Return ``(func_id, score)`` matches from FTS5 BM25 plus trigrams."""
        query_terms = _tokenize_search_text(text)
        trigram_terms = _encoded_trigram_tokens(text)
        if not query_terms and not trigram_terms:
            return []

        conn = self._ensure_index()
        if conn is None:
            return []

        try:
            scores: dict[str, float] = {}
            limit = max(top_k * 4, top_k)
            self._score_match_query(
                conn=conn,
                table="memplex_fts",
                tokens=query_terms,
                weight=2.0,
                limit=limit,
                scores=scores,
            )
            self._score_match_query(
                conn=conn,
                table="memplex_trigram",
                tokens=trigram_terms,
                weight=0.75,
                limit=limit,
                scores=scores,
            )

            query_lower = text.lower()
            if query_lower:
                for func_id in list(scores):
                    func = self._functions.get(func_id)
                    if func is None:
                        continue
                    if query_lower in self._text_factory(func).lower():
                        scores[func_id] += 1.0

            return sorted(scores.items(), key=lambda item: item[1], reverse=True)[:top_k]
        finally:
            conn.close()

    def _score_match_query(
        self,
        *,
        conn: sqlite3.Connection,
        table: str,
        tokens: List[str],
        weight: float,
        limit: int,
        scores: dict[str, float],
    ) -> None:
        match_query = _sqlite_match_query(tokens)
        if not match_query:
            return
        rows = conn.execute(
            f"""
            SELECT func_id, bm25({table}) AS rank
            FROM {table}
            WHERE {table} MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (match_query, limit),
        ).fetchall()
        for idx, row in enumerate(rows):
            scores[str(row["func_id"])] = scores.get(str(row["func_id"]), 0.0) + (
                weight / (idx + 1)
            )

    def _ensure_index(self) -> Optional[sqlite3.Connection]:
        """Open and refresh the SQLite FTS5 sidecar for the current snapshot."""
        signature = self._search_signature()
        if self._disabled and self._signature == signature:
            return None

        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._path))
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS memplex_fts USING fts5(
                    func_id UNINDEXED,
                    name,
                    domain,
                    body,
                    tokenize='unicode61'
                )
                """
            )
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS memplex_trigram USING fts5(
                    func_id UNINDEXED,
                    trigrams,
                    tokenize='unicode61'
                )
                """
            )
            if self._signature != signature:
                conn.execute("DELETE FROM memplex_fts")
                conn.execute("DELETE FROM memplex_trigram")
                for func in self._functions.values():
                    func_text = self._text_factory(func)
                    conn.execute(
                        """
                        INSERT INTO memplex_fts(func_id, name, domain, body)
                        VALUES (?, ?, ?, ?)
                        """,
                        (func.id, func.name, func.domain or "", func_text),
                    )
                    conn.execute(
                        """
                        INSERT INTO memplex_trigram(func_id, trigrams)
                        VALUES (?, ?)
                        """,
                        (func.id, " ".join(_encoded_trigram_tokens(func_text))),
                    )
                conn.commit()
                self._signature = signature
                self._disabled = False
            return conn
        except sqlite3.Error:
            conn.close()
            self._disabled = True
            raise

    def _search_signature(self) -> tuple:
        """Return a compact signature of searchable in-memory content."""
        items = []
        for func in self._functions.values():
            search_text = self._text_factory(func)
            items.append(
                (
                    func.id,
                    func.version,
                    func.updated_at or "",
                    func.content_hash or "",
                    sha1(search_text.encode("utf-8")).hexdigest(),
                )
            )
        return tuple(sorted(items))


def local_bm25_search(
    *,
    text: str,
    functions: Dict[str, Function],
    text_factory: Callable[[Function], str],
    top_k: int,
) -> List[tuple[Function, str, float]]:
    """Rank functions with pure-Python BM25 plus phrase and trigram boosts."""
    query_terms = _tokenize_search_text(text)
    query_term_counts = Counter(query_terms)
    query_ngrams = _character_ngrams(text)
    if not query_terms and not query_ngrams:
        return []

    documents: List[tuple[Function, str, Counter[str], int, set[str]]] = []
    document_frequency: Counter[str] = Counter()
    for func in functions.values():
        func_text = text_factory(func)
        func_terms = _tokenize_search_text(func_text)
        term_counts = Counter(func_terms)
        documents.append(
            (
                func,
                func_text,
                term_counts,
                max(len(func_terms), 1),
                _character_ngrams(func_text),
            )
        )
        for term in query_term_counts:
            if term in term_counts:
                document_frequency[term] += 1

    if not documents:
        return []

    doc_count = len(documents)
    avg_doc_len = sum(doc_len for _, _, _, doc_len, _ in documents) / doc_count
    query_lower = text.lower()
    raw_scores: List[tuple[float, Function, str]] = []

    for func, func_text, term_counts, doc_len, doc_ngrams in documents:
        bm25 = 0.0
        for term, query_count in query_term_counts.items():
            term_frequency = term_counts.get(term, 0)
            if term_frequency <= 0:
                continue
            df = document_frequency[term]
            idf = math.log(1 + (doc_count - df + 0.5) / (df + 0.5))
            denom = term_frequency + _BM25_K1 * (
                1 - _BM25_B + _BM25_B * (doc_len / avg_doc_len)
            )
            bm25 += query_count * idf * (
                (term_frequency * (_BM25_K1 + 1)) / denom
            )

        phrase_boost = 0.0
        func_lower = func_text.lower()
        if query_lower and query_lower in func_lower:
            phrase_boost = 1.0

        ngram_overlap = 0.0
        if query_ngrams and doc_ngrams:
            ngram_overlap = len(query_ngrams & doc_ngrams) / len(query_ngrams)

        score = bm25 + phrase_boost + (0.75 * ngram_overlap)
        if score > 0:
            raw_scores.append((score, func, func_text))

    raw_scores.sort(key=lambda x: x[0], reverse=True)
    return [(func, func_text, score) for score, func, func_text in raw_scores[:top_k]]
