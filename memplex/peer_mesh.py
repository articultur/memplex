"""Peer-mesh reconciliation core (campaign ⑩, Phase 1: pure protocol).

Implements the protocol layer designed across the blockchain research and
the Merkle anti-entropy experiment (measured 5000x bandwidth reduction on
real store payloads):

* :class:`VersionVector` — per-object causal ordering; answers "did A
  see B's edit?" without a central sequencer (Syncthing/ipfs-log
  lineage). Complements bi-temporal supersede: one answers "when was it
  true", the other "who saw what".
* :class:`ObjectMerkleTree` — RFC 6962-shaped Merkle tree over ordered
  logical objects (0x00/0x01 domain separation, required for second
  preimage resistance). Tree heads make divergence detection O(1);
  walks make repair bandwidth proportional to the difference.
* :func:`gossip` — two-tier detection: exchange 32-byte roots first;
  only on mismatch pay for a diff walk.
* :func:`reconcile` — pure reconciliation over local/remote object maps,
  applying the per-object-type conflict table: immutable observations
  union; fact corrections resolve by supersession edges (never wall-clock
  LWW); derived records keep the newest verifiable generation; ACL
  revocations are remove-wins; deletions are tombstones that survive
  until the causal GC watermark.

Phase 2 (network transport over the existing HTTP sync surface) layers
on top; everything here is I/O-free and deterministic so the convergence
contract is exhaustively testable.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum


class Resolution(str, Enum):
    """What reconcile decided for one divergent object."""

    FETCH_REMOTE = "fetch_remote"  # remote is causally newer
    SEND_LOCAL = "send_local"  # local is causally newer
    UNION = "union"  # immutable: keep both sides
    SUPERSEDED = "superseded"  # supersession edge resolves the conflict
    REMOVE_WINS = "remove_wins"  # revocation/delete beats presence
    CONFLICT = "conflict"  # concurrent writes: surfaced, never silently merged


@dataclass
class VersionVector:
    """Per-object causal clock: {replica_id: observed edit count}.

    A vector dominates another when every component is >= and at least
    one is >; equal vectors are the same write. Incomparable vectors are
    concurrent writes.
    """

    counts: dict[str, int] = field(default_factory=dict)

    def bump(self, replica: str) -> VersionVector:
        next_counts = dict(self.counts)
        next_counts[replica] = next_counts.get(replica, 0) + 1
        return VersionVector(next_counts)

    def dominates(self, other: VersionVector) -> bool:
        strictly_greater = False
        for replica, count in other.counts.items():
            mine = self.counts.get(replica, 0)
            if mine < count:
                return False
            if mine > count:
                strictly_greater = True
        return strictly_greater or (not other.counts and bool(self.counts))

    def concurrent_with(self, other: VersionVector) -> bool:
        return not self.dominates(other) and not other.dominates(self)

    def merge(self, other: VersionVector) -> VersionVector:
        merged = dict(self.counts)
        for replica, count in other.counts.items():
            merged[replica] = max(merged.get(replica, 0), count)
        return VersionVector(merged)


@dataclass(frozen=True)
class MeshObject:
    """One logical object as peers exchange it.

    ``kind`` drives the conflict table; ``payload`` is opaque bytes for
    hashing; ``tombstone`` marks deletion (survives until the causal GC
    watermark — never dropped silently).
    """

    id: str
    kind: str
    payload: bytes
    vector: VersionVector
    tombstone: bool = False
    superseded_by: str | None = None


# Kinds whose conflict rule is "keep both sides".
_IMMUTABLE_KINDS = frozenset({"observation", "raw_turn"})
# Kinds whose presence/absence conflict resolves remove-wins.
_REMOVE_WINS_KINDS = frozenset({"acl_grant", "memory"})
# Kinds where a supersession edge (not wall-clock) resolves divergence.
_SUPERSESSION_KINDS = frozenset({"fact"})


def _node_hash(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


class ObjectMerkleTree:
    """RFC 6962-shaped Merkle tree over objects ordered by id.

    Leaves hash ``id || kind || tombstone || payload`` (domain-separated
    with the 0x00 prefix); internal nodes hash ``0x01 || left || right``.
    Odd children duplicate the last node at each level (RFC 6962 MTH
    convention).
    """

    def __init__(self, objects: list[MeshObject]) -> None:
        self._leaves = {obj.id: self.leaf_hash(obj) for obj in objects}
        self._ids = sorted(self._leaves)
        hashes = [self._leaves[obj_id] for obj_id in self._ids]
        self._levels: list[list[bytes]] = [hashes]
        current = hashes
        while len(current) > 1:
            nxt = []
            for i in range(0, len(current), 2):
                left = current[i]
                right = current[i + 1] if i + 1 < len(current) else current[i]
                nxt.append(_node_hash(left, right))
            self._levels.append(nxt)
            current = nxt

    @staticmethod
    def leaf_hash(obj: MeshObject) -> bytes:
        payload = (
            obj.id.encode("utf-8")
            + b"\x00"
            + obj.kind.encode("utf-8")
            + b"\x00"
            + (b"\x01" if obj.tombstone else b"\x00")
            + obj.payload
        )
        return hashlib.sha256(b"\x00" + payload).digest()

    @property
    def root(self) -> bytes:
        if not self._levels[0]:
            return hashlib.sha256(b"").digest()
        return self._levels[-1][0]

    def divergent_ids(self, other: ObjectMerkleTree) -> list[str]:
        """Ids whose leaves differ — the repair worklist after gossip."""
        all_ids = sorted(set(self._ids) | set(other._ids))
        return [
            obj_id
            for obj_id in all_ids
            if self._leaves.get(obj_id) != other._leaves.get(obj_id)
        ]


def gossip(local_root: bytes, remote_root: bytes) -> bool:
    """Tier-1 detection: True when a Merkle walk is warranted.

    Equal 32-byte roots prove (collision-resistance) identical object
    sets — zero further traffic. Mismatch means at least one divergent
    leaf; pay for the walk only then.
    """
    return local_root != remote_root


def reconcile(
    local: dict[str, MeshObject], remote: dict[str, MeshObject]
) -> dict[str, Resolution]:
    """Pure per-object reconciliation over two replicas' object maps.

    Deterministic; never mutates inputs. Concurrent-edit conflicts are
    surfaced (``CONFLICT``), never silently merged — the caller decides
    policy (e.g. keep both and flag for the type's own resolver).
    """
    plan: dict[str, Resolution] = {}
    for obj_id in sorted(set(local) | set(remote)):
        lo, ro = local.get(obj_id), remote.get(obj_id)
        if lo is not None and ro is None:
            plan[obj_id] = Resolution.SEND_LOCAL
            continue
        if lo is None and ro is not None:
            plan[obj_id] = Resolution.FETCH_REMOTE
            continue
        assert lo is not None and ro is not None
        if lo.vector.dominates(ro.vector):
            plan[obj_id] = Resolution.SEND_LOCAL
        elif ro.vector.dominates(lo.vector):
            plan[obj_id] = Resolution.FETCH_REMOTE
        elif lo.kind in _IMMUTABLE_KINDS:
            # Immutable objects with different payloads on concurrent
            # vectors are distinct captures: union keeps both.
            plan[obj_id] = Resolution.UNION
        elif lo.tombstone or ro.tombstone:
            # Deletes/revocations win over concurrent presence.
            plan[obj_id] = Resolution.REMOVE_WINS
        elif lo.kind in _SUPERSESSION_KINDS and _supersession_pair(lo, ro):
            plan[obj_id] = Resolution.SUPERSEDED
        else:
            plan[obj_id] = Resolution.CONFLICT
    return plan


def _supersession_pair(lo: MeshObject, ro: MeshObject) -> bool:
    return lo.superseded_by == ro.id or ro.superseded_by == lo.id


def apply_plan(
    local: dict[str, MeshObject],
    remote: dict[str, MeshObject],
    plan: dict[str, Resolution],
    replica_id: str,
) -> dict[str, MeshObject]:
    """Materialize a reconcile plan into the local replica (pure).

    Returns the next local state; the merged vector of each touched
    object records that this replica has seen both sides.
    """
    next_state = dict(local)
    for obj_id, resolution in plan.items():
        lo, ro = local.get(obj_id), remote.get(obj_id)
        if resolution is Resolution.FETCH_REMOTE:
            assert ro is not None
            next_state[obj_id] = MeshObject(
                id=ro.id,
                kind=ro.kind,
                payload=ro.payload,
                vector=ro.vector.merge(lo.vector) if lo else ro.vector,
                tombstone=ro.tombstone,
                superseded_by=ro.superseded_by,
            )
        elif resolution is Resolution.UNION and lo is not None and ro is not None:
            # Concurrent immutable captures: keep local under the id and
            # register the remote payload under a content-suffixed id so
            # neither side is lost.
            suffix = hashlib.sha256(ro.payload).hexdigest()[:8]
            union_id = f"{obj_id}~{suffix}"
            differs = (
                next_state.get(obj_id) is None
                or next_state[obj_id].payload != ro.payload
            )
            if differs and union_id not in next_state:
                next_state[union_id] = MeshObject(
                    id=union_id,
                    kind=ro.kind,
                    payload=ro.payload,
                    vector=ro.vector,
                )
            touched = next_state.get(obj_id)
            if touched is not None:
                next_state[obj_id] = MeshObject(
                    id=touched.id,
                    kind=touched.kind,
                    payload=touched.payload,
                    vector=touched.vector.merge(ro.vector),
                    tombstone=touched.tombstone,
                    superseded_by=touched.superseded_by,
                )
        elif resolution is Resolution.REMOVE_WINS:
            winner = lo if (lo is not None and lo.tombstone) else ro
            assert winner is not None
            merged_vector = (
                lo.vector.merge(ro.vector)
                if lo is not None and ro is not None
                else winner.vector
            )
            next_state[obj_id] = MeshObject(
                id=winner.id,
                kind=winner.kind,
                payload=winner.payload,
                vector=merged_vector,
                tombstone=True,
                superseded_by=winner.superseded_by,
            )
        elif resolution is Resolution.SUPERSEDED and lo is not None and ro is not None:
            # The superseding fact wins; the superseded one stays as a
            # tombstoned historical anchor (bi-temporal history).
            superseding = ro if lo.superseded_by == ro.id else lo
            superseded = lo if superseding is ro else ro
            merged_vector = lo.vector.merge(ro.vector)
            next_state[superseding.id] = MeshObject(
                id=superseding.id,
                kind=superseding.kind,
                payload=superseding.payload,
                vector=merged_vector,
                tombstone=False,
            )
            next_state[superseded.id] = MeshObject(
                id=superseded.id,
                kind=superseded.kind,
                payload=superseded.payload,
                vector=merged_vector,
                tombstone=True,
                superseded_by=superseding.id,
            )
        # SEND_LOCAL / CONFLICT need no local change: local already
        # holds the newer value, or policy resolution is the caller's.
    _ = replica_id  # reserved: per-replica bump hooks for Phase 2
    return next_state
