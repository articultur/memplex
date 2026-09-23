"""Peer-mesh reconciliation core contract tests (campaign ⑩ Phase 1).

Covers the three protocol pillars: version-vector causality, two-tier
gossip detection (roots first, walk on mismatch), and the per-type
conflict table (union / supersession / remove-wins / conflict surfaced).
All pure functions — convergence is checked end to end by applying plans
from BOTH sides' perspectives and asserting both replicas converge.
"""

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

from memplex.peer_mesh import (
    MeshObject,
    ObjectMerkleTree,
    Resolution,
    VersionVector,
    apply_plan,
    gossip,
    reconcile,
)


def _obj(id_, kind="memory", payload=b"x", vector=None, **kw):
    return MeshObject(
        id=id_,
        kind=kind,
        payload=payload,
        vector=vector or VersionVector({"a": 1}),
        **kw,
    )


# ── version vectors ─────────────────────────────────────────────────


def test_vector_dominance_and_concurrency():
    base = VersionVector({"a": 1, "b": 1})
    later = base.bump("a")
    assert later.dominates(base)
    assert not base.dominates(later)
    concurrent = base.bump("c")  # c saw base, a didn't see c
    other = base.bump("a")
    assert concurrent.concurrent_with(other)
    assert concurrent.merge(other).counts == {"a": 2, "b": 1, "c": 1}


# ── merkle + gossip ─────────────────────────────────────────────────


def test_gossip_equal_roots_zero_work():
    objs = [_obj("m1"), _obj("m2", payload=b"y")]
    assert ObjectMerkleTree(objs).root == ObjectMerkleTree(list(objs)).root
    assert not gossip(
        ObjectMerkleTree(objs).root, ObjectMerkleTree(list(objs)).root
    )


def test_gossip_mismatch_then_walk_finds_exactly_the_diff():
    local = [_obj("m1"), _obj("m2"), _obj("m3")]
    remote = [_obj("m1"), _obj("m2", payload=b"changed"), _obj("m4")]
    lt, rt = ObjectMerkleTree(local), ObjectMerkleTree(remote)
    assert gossip(lt.root, rt.root)
    assert lt.divergent_ids(rt) == ["m2", "m3", "m4"]


# ── conflict table ──────────────────────────────────────────────────


def test_causal_newer_wins_both_directions():
    old = {"m1": _obj("m1", vector=VersionVector({"a": 1}))}
    new = {"m1": _obj("m1", vector=VersionVector({"a": 2}))}
    assert reconcile(old, new)["m1"] is Resolution.FETCH_REMOTE
    assert reconcile(new, old)["m1"] is Resolution.SEND_LOCAL


def test_concurrent_memory_conflict_surfaced_never_merged():
    a = {"m1": _obj("m1", payload=b"from-a", vector=VersionVector({"a": 2}))}
    b = {"m1": _obj("m1", payload=b"from-b", vector=VersionVector({"b": 2}))}
    plan = reconcile(a, b)
    assert plan["m1"] is Resolution.CONFLICT


def test_concurrent_observation_union_keeps_both():
    a = {"o1": _obj("o1", kind="observation", payload=b"saw-a", vector=VersionVector({"a": 1}))}
    b = {"o1": _obj("o1", kind="observation", payload=b"saw-b", vector=VersionVector({"b": 1}))}
    plan = reconcile(a, b)
    assert plan["o1"] is Resolution.UNION
    merged = apply_plan(a, b, plan, replica_id="a")
    payloads = {o.payload for o in merged.values()}
    assert payloads == {b"saw-a", b"saw-b"}, "union must lose neither capture"


def test_remove_wins_for_concurrent_delete_vs_write():
    a = {"m1": _obj("m1", payload=b"alive", vector=VersionVector({"a": 2}))}
    tomb = MeshObject("m1", "memory", b"", VersionVector({"b": 2}), tombstone=True)
    b = {"m1": tomb}
    assert reconcile(a, b)["m1"] is Resolution.REMOVE_WINS
    merged = apply_plan(a, b, {"m1": Resolution.REMOVE_WINS}, replica_id="a")
    assert merged["m1"].tombstone, "revocation must win and tombstone"


def test_fact_supersession_edge_not_wall_clock():
    old_fact = _obj("f1", kind="fact", payload=b"mysql", vector=VersionVector({"a": 1}))
    new_fact = _obj(
        "f2",
        kind="fact",
        payload=b"postgres",
        vector=VersionVector({"a": 2}),
    )
    # remote knows the correction; local still holds the old fact
    # WITHOUT the edge (it hasn't seen f2 yet) -- the supersession is
    # visible on the new side.
    new_with_edge = MeshObject(
        "f2", "fact", b"postgres", VersionVector({"a": 2}), superseded_by="f1"
    )
    local = {"f1": old_fact}
    remote = {"f1": old_fact, "f2": new_with_edge}
    plan = reconcile(local, remote)
    # f2 is causally newer (fetch); f1 identical (no entry needed)
    assert plan["f2"] is Resolution.FETCH_REMOTE
    merged = apply_plan(local, remote, plan, replica_id="a")
    assert merged["f2"].payload == b"postgres"
    # old fact retained for as_of history (bi-temporal contract)
    assert merged["f1"].payload == b"mysql"


def test_both_sides_converge_after_exchanging_plans():
    local = {
        "m1": _obj("m1", vector=VersionVector({"a": 3})),
        "o1": _obj("o1", kind="observation", payload=b"cap-a", vector=VersionVector({"a": 1})),
    }
    remote = {
        "m1": _obj("m1", payload=b"newer", vector=VersionVector({"a": 5})),
        "o1": _obj("o1", kind="observation", payload=b"cap-b", vector=VersionVector({"b": 1})),
        "m2": _obj("m2", vector=VersionVector({"b": 2})),
    }
    plan_l = reconcile(local, remote)
    next_local = apply_plan(local, remote, plan_l, replica_id="a")
    # Simulate the remote applying the mirrored view: remote sees local's
    # o1 capture as new, m1 stays (its vector dominates), m2 sent later.
    plan_r = reconcile(remote, local)
    next_remote = apply_plan(remote, local, plan_r, replica_id="b")
    # m1 converges to the causally newer payload on both sides
    assert next_local["m1"].payload == b"newer"
    assert next_remote["m1"].payload == b"newer"
    # neither capture lost
    local_payloads = {o.payload for o in next_local.values()}
    assert {b"cap-a", b"cap-b"} <= local_payloads
    assert next_remote["m2"].payload == b"x"
