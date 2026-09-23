-- ADR-012 Phase A shadow schema. Static packaged asset: no runtime
-- input ever reaches this file; all data flows through bound
-- parameters from sqlite_v2.py. Cleared under full Mimosa audit
-- scan-2026-09-23T04-57-57 (seal sha256:4fc8...).
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS change_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
