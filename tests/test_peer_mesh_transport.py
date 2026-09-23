"""Peer-mesh transport contract tests (campaign ⑩ Phase 2).

Drives the full gossip loop over an in-memory wire: two replicas diverge,
one gossip pass converges them, and the traffic summary proves the
two-tier property (equal roots -> zero object exchange).
"""

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

from memplex.peer_mesh import MeshObject, VersionVector
from memplex.peer_mesh_transport import (
    MemoryWire,
    PeerMeshService,
    PeerSnapshot,
    divergent_ids_from_manifest,
    mesh_object_from_wire,
    mesh_object_to_wire,
    validate_peer_url,
)


def _obj(id_, payload=b"x", vector=None, **kw):
    return MeshObject(
        id=id_,
        kind="memory",
        payload=payload,
        vector=vector or VersionVector({"a": 1}),
        **kw,
    )


def _replica(mesh_url, objects):
    state = dict(objects)
    snapshot = PeerSnapshot(objects=state)
    commits = []

    def commit(next_state):
        commits.append(next_state)
        state.clear()
        state.update(next_state)

    svc = PeerMeshService(
        replica_id=mesh_url,
        peers=[],
        fetcher=None,
        snapshot=lambda: dict(state),
        commit=commit,
    )
    return svc, snapshot, commits, state


def test_wire_roundtrip_preserves_object():
    obj = _obj("m1", payload=b"\x00binary\xff", vector=VersionVector({"a": 2, "b": 1}))
    restored = mesh_object_from_wire(mesh_object_to_wire(obj))
    assert restored == obj


def test_converged_peers_exchange_zero_objects():
    wire = MemoryWire()
    shared = {"m1": _obj("m1", payload=b"same")}
    wire.register("http://peer-a", PeerSnapshot(objects=dict(shared)))
    svc, _, commits, _ = _replica("local", dict(shared))
    svc.peers = ["http://peer-a"]
    svc._fetcher = wire
    summary = svc.gossip_once()
    assert summary["http://peer-a"]["converged"] is True
    assert summary["http://peer-a"]["exchanged"] == 0
    assert commits == [], "equal roots must trigger no commit"


def test_divergence_one_pass_converges_with_plan_summary():
    wire = MemoryWire()
    peer_objects = {
        "m1": _obj("m1", payload=b"newer", vector=VersionVector({"a": 3})),
        "m2": _obj("m2", payload=b"peer-only", vector=VersionVector({"b": 1})),
    }
    wire.register("http://peer-a", PeerSnapshot(objects=peer_objects))
    local_objects = {
        "m1": _obj("m1", payload=b"older", vector=VersionVector({"a": 1})),
        "m3": _obj("m3", payload=b"local-only", vector=VersionVector({"a": 1})),
    }
    svc, _, commits, state = _replica("local", local_objects)
    svc.peers = ["http://peer-a"]
    svc._fetcher = wire

    summary = svc.gossip_once()
    entry = summary["http://peer-a"]
    assert entry["divergent"] == 3  # m1 differs, m2 fetch, m3 send
    assert entry["changed"] == 2  # m1 fetch_remote + m2 fetch_remote
    assert entry["resolutions"]["m1"] == "fetch_remote"
    assert entry["resolutions"]["m2"] == "fetch_remote"
    assert entry["resolutions"]["m3"] == "send_local"
    assert len(commits) == 1
    assert state["m1"].payload == b"newer"
    assert state["m2"].payload == b"peer-only"
    assert state["m3"].payload == b"local-only", "send_local keeps local copy"

    # Second pass with a refreshed remote registry: converged.
    wire.register("http://peer-a", PeerSnapshot(objects=dict(state)))
    summary2 = svc.gossip_once()
    assert summary2["http://peer-a"]["converged"] is True


def test_unreachable_peer_isolated_not_fatal():
    class _Down:
        def fetch_roots(self, peer_url):
            raise ConnectionError("down")

        def fetch_objects(self, peer_url, ids):
            raise ConnectionError("down")

    svc, _, commits, state = _replica("local", {"m1": _obj("m1")})
    svc.peers = ["http://peer-down"]
    svc._fetcher = _Down()
    summary = svc.gossip_once()
    assert "error" in summary["http://peer-down"]
    assert commits == [] and state["m1"].payload == b"x"


def test_peer_url_validation():
    assert validate_peer_url("http://peer-a:8080")
    assert validate_peer_url("https://mesh.example.com/peer")
    assert not validate_peer_url("ftp://peer-a")
    assert not validate_peer_url("http://user:pass@peer-a")
    assert not validate_peer_url("http://")


# ── Phase 3: narrow exchange from id manifests ──────────────────────


def test_manifest_narrows_exchange_to_divergent_only():
    local = {
        "m1": _obj("m1", payload=b"same"),
        "m2": _obj("m2", payload=b"local-newer"),
        "m3": _obj("m3", payload=b"local-only"),
    }
    wire = MemoryWire()
    peer_objects = {
        "m1": _obj("m1", payload=b"same"),
        "m2": _obj("m2", payload=b"remote-newer"),
        "m4": _obj("m4", payload=b"peer-only"),
    }
    wire.register("http://peer-a", PeerSnapshot(objects=peer_objects))
    manifest = wire.fetch_ids("http://peer-a")["ids"]

    fetch_ids, send_ids = divergent_ids_from_manifest(local, manifest)
    assert fetch_ids == ["m2", "m4"], "only remote-differing ids fetched"
    assert send_ids == ["m3"], "only local-differing ids sent"

    # the object fetch must request exactly the divergent set (no wildcard)
    wire.fetch_objects("http://peer-a", fetch_ids)
    assert wire.requested_ids[-1] == ["m2", "m4"]
    fetched = wire.fetch_objects("http://peer-a", fetch_ids)["objects"]
    assert [rec["id"] for rec in fetched] == ["m2", "m4"], (
        "payloads transferred for exactly the divergent set"
    )


def test_manifest_equal_states_exchange_nothing():
    shared = {"m1": _obj("m1", payload=b"x"), "m2": _obj("m2", payload=b"y")}
    wire = MemoryWire()
    wire.register("http://peer-a", PeerSnapshot(objects=dict(shared)))
    manifest = wire.fetch_ids("http://peer-a")["ids"]
    fetch_ids, send_ids = divergent_ids_from_manifest(dict(shared), manifest)
    assert fetch_ids == [] and send_ids == []
