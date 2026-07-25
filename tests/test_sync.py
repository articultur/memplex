"""Multi-node sync: server endpoints + SyncableStore + end-to-end sharing.

Covers the central-server / local-cache architecture added in
memplex/sync.py + http_api.py /sync/* endpoints:
- server /sync/changes returns changed Functions + tombstones
- server /sync/push merges with LWW by updated_at
- SyncableStore writes local-first, pushes best-effort
- SyncableStore.pull_incremental applies LWW + tombstones
- end-to-end: two nodes share one memory through a server
- offline degradation: unreachable remote never breaks local writes
"""

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

import pytest  # noqa: E402

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from memplex.adapters.http_api import create_app  # noqa: E402
from memplex.models import FieldValue, Function, SourceDocument, SourceType  # noqa: E402
from memplex.storage.lite.store import LiteMemoryStore  # noqa: E402
from memplex.sync import RemoteSyncConfig, SyncableStore, maybe_wrap_sync  # noqa: E402

# ── Helpers ──────────────────────────────────────────────────────────


def _func(
    fid="f1",
    name="login",
    updated_at="2026-01-01T00:00:00+00:00",
    trigger_desc="t",
):
    return Function(
        id=fid,
        name=name,
        name_normalized=name.lower(),
        domain="auth",
        updated_at=updated_at,
        trigger=[FieldValue(desc=trigger_desc, sources=["s"], source_method="manual", weight=1.0)],
        source_type=SourceType.CODE,
    )


@pytest.fixture
def server_client(tmp_path, monkeypatch):
    """A TestClient over a fresh server app with its own storage path.

    Enters the app lifespan so app.state.memplex_service is initialised
    before any request runs.
    """
    monkeypatch.setenv("MEMPLEX_STORAGE_BACKEND", "lite")
    monkeypatch.setenv("MEMPLEX_STORAGE_PATH", str(tmp_path / "server"))
    monkeypatch.delenv("MEMPLEX_API_KEY", raising=False)
    monkeypatch.delenv("MEMPLEX_BEARER_TOKEN", raising=False)
    with TestClient(create_app()) as client:
        yield client


def _write_via_api(client, text, fid_marker="sync-canary"):
    """Write a memory through the server's /memories endpoint."""
    r = client.post("/memories", json={"type": "text", "content": text})
    assert r.status_code == 200, r.text
    return r.json()


# ── Server: /sync/changes ────────────────────────────────────────────


def test_sync_changes_returns_all_when_no_since(server_client):
    client = server_client
    _write_via_api(client, "changes-canary: a memory")
    r = client.get("/sync/changes")
    assert r.status_code == 200, r.text
    data = r.json()
    assert "changes" in data
    assert "tombstones" in data
    assert "server_time" in data
    assert len(data["changes"]) >= 1


def test_sync_changes_filters_by_since(server_client):
    client = server_client
    _write_via_api(client, "old-canary: early memory")
    # A future cutoff should return nothing.
    future = "2099-01-01T00:00:00+00:00"
    r = client.get("/sync/changes", params={"since": future})
    assert r.status_code == 200
    assert r.json()["changes"] == []


def test_sync_changes_includes_tombstones_after_delete(server_client):
    client = server_client
    written = _write_via_api(client, "tomb-canary: will be deleted")
    fid = written["functions"][0]["id"]
    del_r = client.delete(f"/memories/{fid}")
    assert del_r.status_code == 200
    r = client.get("/sync/changes")
    tombstones = r.json()["tombstones"]
    assert any(t["func_id"] == fid for t in tombstones)


# ── Server: /sync/push (LWW) ─────────────────────────────────────────


def test_sync_push_accepts_new_function(server_client):
    client = server_client
    from memplex.adapters.http_api import _dataclass_to_dict as dataclass_to_dict

    f = _func(fid="push-1", updated_at="2026-02-01T00:00:00+00:00")
    r = client.post("/sync/push", json={"functions": [dataclass_to_dict(f)]})
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] == 1
    assert body["rejected_older"] == 0
    # It is now retrievable.
    assert client.get(f"/memories/push-1").status_code == 200


def test_sync_push_rejects_older_version_lww(server_client):
    client = server_client
    from memplex.adapters.http_api import _dataclass_to_dict as dataclass_to_dict

    # Seed a newer version.
    newer = _func(fid="lww-1", updated_at="2026-03-01T00:00:00+00:00")
    client.post("/sync/push", json={"functions": [dataclass_to_dict(newer)]})
    # Push an OLDER version of the same id -> rejected.
    older = _func(fid="lww-1", updated_at="2026-01-01T00:00:00+00:00", name="stale")
    r = client.post("/sync/push", json={"functions": [dataclass_to_dict(older)]})
    body = r.json()
    assert body["accepted"] == 0
    assert body["rejected_older"] == 1
    # Server keeps the newer version.
    got = client.get("/memories/lww-1").json()
    assert got["updated_at"] == "2026-03-01T00:00:00+00:00"


# ── SyncableStore: local-first + offline resilience ──────────────────


def test_syncable_store_writes_locally_when_remote_unreachable(tmp_path, monkeypatch):
    """An unreachable remote must never break a local write."""
    monkeypatch.setenv("MEMPLEX_REMOTE_URL", "http://127.0.0.1:1")  # nothing listening
    monkeypatch.setenv("MEMPLEX_STORAGE_PATH", str(tmp_path / "node"))
    local = LiteMemoryStore(path=tmp_path / "node" / "memory.json")
    store = SyncableStore(local, config=RemoteSyncConfig())
    assert store._config.active
    # Write succeeds despite dead remote.
    store.add(_func(fid="offline-1"), SourceDocument(type="text", source_type=SourceType.WIKI))
    assert store.get("offline-1") is not None
    assert store._push_failures >= 1  # push attempted + failed silently


def test_syncable_store_read_delegates_to_local(tmp_path):
    local = LiteMemoryStore(path=tmp_path / "m.json")
    local.add(_func(fid="del-1"), SourceDocument(type="text", source_type=SourceType.WIKI))
    # config inactive -> SyncableStore still delegates reads.
    store = SyncableStore(local, config=RemoteSyncConfig())
    assert store.get("del-1") is not None
    assert "del-1" in [f.id for f in store.list_functions(limit=10)]


def test_maybe_wrap_sync_returns_local_when_inactive(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMPLEX_REMOTE_URL", raising=False)
    local = LiteMemoryStore(path=tmp_path / "m.json")
    assert maybe_wrap_sync(local) is local


def test_maybe_wrap_sync_wraps_when_active(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMPLEX_REMOTE_URL", "http://example.org")
    local = LiteMemoryStore(path=tmp_path / "m.json")
    wrapped = maybe_wrap_sync(local)
    assert isinstance(wrapped, SyncableStore)
    assert wrapped.local is local


# ── SyncableStore.pull_incremental: LWW + tombstones ─────────────────


class _StubHttp:
    """Stand-in for the requests module, injected via store._http.

    Returns canned /sync/changes payloads so pull_incremental is testable
    without a live server. Only .get is used by pull; .post/.delete are
    stubbed for completeness (push tests may use them later).
    """

    def __init__(
        self, changes_payload, tombstones_payload, server_time="2026-04-01T00:00:00+00:00"
    ):
        from memplex.adapters.http_api import _dataclass_to_dict

        self._changes = [
            c if isinstance(c, dict) else _dataclass_to_dict(c) for c in changes_payload
        ]
        self._tombstones = tombstones_payload
        self._server_time = server_time

    def get(self, url, params=None, headers=None, timeout=None):
        import types

        resp = types.SimpleNamespace()
        resp.status_code = 200
        resp.raise_for_status = lambda: None
        resp.json = lambda: {
            "changes": self._changes,
            "tombstones": self._tombstones,
            "server_time": self._server_time,
        }
        return resp

    def post(self, url, json=None, headers=None, timeout=None):
        import types

        resp = types.SimpleNamespace(status_code=200)
        resp.json = lambda: {
            "accepted": len((json or {}).get("functions", [])),
            "rejected_older": 0,
        }
        return resp

    def delete(self, url, headers=None, timeout=None):
        import types

        return types.SimpleNamespace(status_code=200)


def _active_config():
    cfg = RemoteSyncConfig()
    cfg.url = "http://stub"
    cfg.api_key = None
    cfg.enabled = True
    return cfg


def test_pull_incremental_applies_new_function(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMPLEX_REMOTE_URL", raising=False)
    local = LiteMemoryStore(path=tmp_path / "m.json")
    store = SyncableStore(local, config=_active_config())
    incoming = _func(fid="pulled-1", updated_at="2026-05-01T00:00:00+00:00")
    store._http = _StubHttp([incoming], [])
    summary = store.pull_incremental()
    assert summary["applied"] == 1
    assert local.get("pulled-1") is not None


def test_pull_incremental_rejects_older_local(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMPLEX_REMOTE_URL", raising=False)
    local = LiteMemoryStore(path=tmp_path / "m.json")
    local.add(
        _func(fid="lww-pull", updated_at="2026-06-01T00:00:00+00:00"),
        SourceDocument(type="text", source_type=SourceType.WIKI),
    )
    store = SyncableStore(local, config=_active_config())
    older = _func(fid="lww-pull", updated_at="2026-01-01T00:00:00+00:00", name="stale")
    store._http = _StubHttp([older], [])
    summary = store.pull_incremental()
    assert summary["rejected_older"] == 1
    assert summary["applied"] == 0
    assert local.get("lww-pull").updated_at == "2026-06-01T00:00:00+00:00"


def test_pull_incremental_applies_tombstones(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMPLEX_REMOTE_URL", raising=False)
    local = LiteMemoryStore(path=tmp_path / "m.json")
    local.add(
        _func(fid="tomb-pull", updated_at="2026-01-01T00:00:00+00:00"),
        SourceDocument(type="text", source_type=SourceType.WIKI),
    )
    store = SyncableStore(local, config=_active_config())
    store._http = _StubHttp(
        [], [{"func_id": "tomb-pull", "deleted_at": "2026-07-01T00:00:00+00:00"}]
    )
    summary = store.pull_incremental()
    assert summary["deleted"] == 1
    assert local.get("tomb-pull") is None


def test_pull_incremental_inactive_returns_noop(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMPLEX_REMOTE_URL", raising=False)
    store = SyncableStore(LiteMemoryStore(path=tmp_path / "m.json"))
    summary = store.pull_incremental()
    assert summary["pulled"] == 0
    assert "skipped" in summary


# ── End-to-end: two nodes share via a real server TestClient ─────────


def test_e2e_two_nodes_share_one_memory(server_client, tmp_path, monkeypatch):
    """Node A writes -> server has it -> node B pulls -> node B has it.

    Uses the real server TestClient as the remote for both nodes by
    injecting it as their _http layer, exercising the actual push/pull
    HTTP-shaped calls against live endpoints.
    """
    server = server_client

    class _ServerHttp:
        """Adapter: SyncableStore calls -> TestClient calls."""

        def get(self, url, params=None, headers=None, timeout=None):
            path = url.replace("http://stub", "")
            r = server.get(path, params=params)
            import types

            resp = types.SimpleNamespace(status_code=r.status_code)
            resp.raise_for_status = lambda: None
            resp.json = r.json
            return resp

        def post(self, url, json=None, headers=None, timeout=None):
            path = url.replace("http://stub", "")
            r = server.post(path, json=json)
            import types

            resp = types.SimpleNamespace(status_code=r.status_code)
            resp.json = r.json
            return resp

        def delete(self, url, headers=None, timeout=None):
            path = url.replace("http://stub", "")
            r = server.delete(path)
            import types

            return types.SimpleNamespace(status_code=r.status_code)

    # Node A: write locally + push to server.
    node_a = SyncableStore(LiteMemoryStore(path=tmp_path / "a.json"), config=_active_config())
    node_a._http = _ServerHttp()
    node_a.add(
        _func(fid="shared-1", name="e2e-canary", updated_at="2026-08-01T00:00:00+00:00"),
        SourceDocument(type="text", source_type=SourceType.WIKI),
    )
    # Server now has shared-1.
    assert server.get("/memories/shared-1").status_code == 200

    # Node B: separate local store, pull from server.
    node_b = SyncableStore(LiteMemoryStore(path=tmp_path / "b.json"), config=_active_config())
    node_b._http = _ServerHttp()
    assert node_b.get("shared-1") is None  # not yet
    summary = node_b.pull_incremental()
    assert summary["applied"] >= 1
    assert node_b.get("shared-1") is not None  # arrived via pull


# ── R4: auto-pull worker (periodic background sync) ──────────────────


def test_auto_pull_disabled_when_interval_zero(tmp_path, monkeypatch):
    """auto_pull_interval=0 (default) -> start_auto_pull is a no-op."""
    monkeypatch.delenv("MEMPLEX_REMOTE_URL", raising=False)
    store = SyncableStore(LiteMemoryStore(path=tmp_path / "ap.json"), config=_active_config())
    store._config.auto_pull_interval = 0
    store.start_auto_pull()
    assert store._auto_pull_thread is None  # no thread started


def test_auto_pull_starts_thread_when_interval_positive(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMPLEX_REMOTE_URL", raising=False)
    store = SyncableStore(LiteMemoryStore(path=tmp_path / "ap2.json"), config=_active_config())
    # Stub http so pull_incremental returns a no-op quickly.
    store._http = _StubHttp([], [])
    store.start_auto_pull(interval=0.05)  # 50ms cadence
    try:
        assert store._auto_pull_thread is not None
        assert store._auto_pull_thread.is_alive()
    finally:
        store.stop_auto_pull()
    assert store._auto_pull_thread is None


def test_auto_pull_thread_stops_cleanly(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMPLEX_REMOTE_URL", raising=False)
    store = SyncableStore(LiteMemoryStore(path=tmp_path / "ap3.json"), config=_active_config())
    store._http = _StubHttp([], [])
    store.start_auto_pull(interval=0.05)
    store.stop_auto_pull()
    # After stop, the thread reference is cleared and the event is set.
    assert store._auto_pull_thread is None
    assert store._auto_pull_stop.is_set()


def test_auto_pull_does_not_crash_on_pull_failure(tmp_path, monkeypatch):
    """A failing pull must not kill the auto-pull thread."""
    monkeypatch.delenv("MEMPLEX_REMOTE_URL", raising=False)
    store = SyncableStore(LiteMemoryStore(path=tmp_path / "ap4.json"), config=_active_config())

    class _BoomHttp:
        def get(self, *a, **kw):
            raise RuntimeError("simulated network failure")

    store._http = _BoomHttp()
    store.start_auto_pull(interval=0.02)
    import time

    time.sleep(0.1)  # let a couple of ticks fire + fail
    assert store._auto_pull_thread is not None
    assert store._auto_pull_thread.is_alive()  # still running despite failures
    store.stop_auto_pull()


# ── P2P mesh: MEMPLEX_PEERS multi-target sync ────────────────────────


def test_remote_config_peers_parsed_from_env(monkeypatch):
    monkeypatch.setenv("MEMPLEX_PEERS", "http://a:8900, http://b:8900 ,http://c:8900")
    monkeypatch.delenv("MEMPLEX_REMOTE_URL", raising=False)
    cfg = RemoteSyncConfig()
    assert cfg.peers == ["http://a:8900", "http://b:8900", "http://c:8900"]
    assert cfg.active  # peers alone enable sync


def test_remote_config_all_targets_combines_url_and_peers(monkeypatch):
    monkeypatch.setenv("MEMPLEX_REMOTE_URL", "http://primary:8900")
    monkeypatch.setenv("MEMPLEX_PEERS", "http://p1:8900,http://p2:8900")
    cfg = RemoteSyncConfig()
    assert cfg.all_targets() == ["http://primary:8900", "http://p1:8900", "http://p2:8900"]


def test_remote_config_all_targets_dedupes(monkeypatch):
    monkeypatch.setenv("MEMPLEX_REMOTE_URL", "http://x:8900")
    monkeypatch.setenv("MEMPLEX_PEERS", "http://x:8900,http://y:8900")
    cfg = RemoteSyncConfig()
    # x appears in both url and peers -> deduped to one entry.
    assert cfg.all_targets().count("http://x:8900") == 1


def test_p2p_push_fans_out_to_all_peers(tmp_path, monkeypatch):
    """A write must push to every configured peer, not just the primary."""
    monkeypatch.delenv("MEMPLEX_REMOTE_URL", raising=False)
    monkeypatch.setenv("MEMPLEX_PEERS", "http://peer-a,http://peer-b")
    local = LiteMemoryStore(path=tmp_path / "p2p.json")
    store = SyncableStore(local, config=RemoteSyncConfig())
    pushed_to: list = []

    class _MeshHttp:
        def post(self, url, json=None, headers=None, timeout=None):
            pushed_to.append(url)
            import types

            return types.SimpleNamespace(status_code=200)

        def get(self, *a, **kw):
            import types

            r = types.SimpleNamespace(status_code=200)
            r.raise_for_status = lambda: None
            r.json = lambda: {"changes": [], "tombstones": [], "server_time": None}
            return r

        def delete(self, *a, **kw):
            import types

            return types.SimpleNamespace(status_code=200)

    store._http = _MeshHttp()
    store.add(
        _func(fid="mesh-1", updated_at="2026-09-01T00:00:00+00:00"),
        SourceDocument(type="text", source_type=SourceType.WIKI),
    )
    assert "http://peer-a/sync/push" in pushed_to
    assert "http://peer-b/sync/push" in pushed_to


def test_p2p_pull_merges_from_all_peers(tmp_path, monkeypatch):
    """pull_incremental must fetch from every peer and merge results."""
    monkeypatch.delenv("MEMPLEX_REMOTE_URL", raising=False)
    monkeypatch.setenv("MEMPLEX_PEERS", "http://peer-a,http://peer-b")
    local = LiteMemoryStore(path=tmp_path / "p2p-pull.json")
    store = SyncableStore(local, config=RemoteSyncConfig())
    fetched_from: list = []

    class _MeshHttp:
        def __init__(self):
            self._peer_data = {
                "http://peer-a": {
                    "changes": [
                        {"id": "from-a", "name": "a", "updated_at": "2026-09-01T00:00:00+00:00"}
                    ],
                    "tombstones": [],
                    "server_time": "2026-09-01T00:00:00+00:00",
                },
                "http://peer-b": {
                    "changes": [
                        {"id": "from-b", "name": "b", "updated_at": "2026-09-01T00:00:00+00:00"}
                    ],
                    "tombstones": [],
                    "server_time": "2026-09-01T00:00:00+00:00",
                },
            }

        def get(self, url, params=None, headers=None, timeout=None):
            for peer, data in self._peer_data.items():
                if url.startswith(peer):
                    fetched_from.append(peer)
                    import types

                    r = types.SimpleNamespace(status_code=200)
                    r.raise_for_status = lambda: None
                    r.json = lambda d=data: d
                    return r
            raise RuntimeError("unknown peer")

        def post(self, *a, **kw):
            import types

            return types.SimpleNamespace(status_code=200)

        def delete(self, *a, **kw):
            import types

            return types.SimpleNamespace(status_code=200)

    store._http = _MeshHttp()
    summary = store.pull_incremental()
    assert set(fetched_from) == {"http://peer-a", "http://peer-b"}
    assert summary["applied"] == 2  # one from each peer
    assert local.get("from-a") is not None
    assert local.get("from-b") is not None
