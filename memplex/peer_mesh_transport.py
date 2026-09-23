"""Peer-mesh HTTP transport (campaign ⑩ Phase 2).

Layers the Phase-1 protocol core (:mod:`memplex.peer_mesh`) onto the
existing sync surface: each peer exposes three small HTTP endpoints
(roots, divergent-leaf fetch, reconciliation push) and runs a gossip
loop against configured peers. Transport is swap-able (``Fetcher``
protocol) so tests drive the full loop with an in-memory wire.

Endpoint contract (any peer):
  GET  /peer/roots          -> {"root": hex, "count": n}
  GET  /peer/objects?ids=a,b -> {"objects": [MeshObject wire dicts]}
  POST /peer/reconcile      -> {"plan": {id: resolution}, "objects": [...]}

Security: URLs must be http(s) and point at configured peer addresses;
object payloads are opaque bytes transferred base64; no SQL anywhere in
this layer (the store adapters own persistence).
"""

from __future__ import annotations

import base64
import logging
import threading
from dataclasses import dataclass
from typing import Any, Protocol

from memplex.peer_mesh import (
    MeshObject,
    ObjectMerkleTree,
    Resolution,
    VersionVector,
    apply_plan,
    gossip,
    reconcile,
)

logger = logging.getLogger(__name__)


def mesh_object_to_wire(obj: MeshObject) -> dict[str, Any]:
    return {
        "id": obj.id,
        "kind": obj.kind,
        "payload": base64.b64encode(obj.payload).decode("ascii"),
        "vector": obj.vector.counts,
        "tombstone": obj.tombstone,
        "superseded_by": obj.superseded_by,
    }


def mesh_object_from_wire(data: dict[str, Any]) -> MeshObject:
    return MeshObject(
        id=str(data["id"]),
        kind=str(data["kind"]),
        payload=base64.b64decode(data["payload"]),
        vector=VersionVector({str(k): int(v) for k, v in data["vector"].items()}),
        tombstone=bool(data.get("tombstone", False)),
        superseded_by=data.get("superseded_by"),
    )


class Fetcher(Protocol):
    """Transport client; production uses HTTP, tests use memory."""

    def fetch_roots(self, peer_url: str) -> dict[str, Any]: ...

    def fetch_objects(self, peer_url: str, ids: list[str]) -> dict[str, Any]: ...


@dataclass
class PeerSnapshot:
    """What a peer currently offers for reconciliation."""

    objects: dict[str, MeshObject]

    def roots_payload(self) -> dict[str, Any]:
        tree = ObjectMerkleTree(list(self.objects.values()))
        return {
            "root": tree.root.hex(),
            "count": len(self.objects),
        }


class PeerMeshService:
    """One replica's mesh loop: gossip roots, walk diffs, reconcile.

    Owns no storage: the caller supplies ``snapshot()`` (current objects
    by id) and ``commit()`` (apply a reconciled state). This keeps the
    transport testable end to end and leaves persistence to the store
    adapters (lite/Postgres) under their own transaction semantics.
    """

    def __init__(
        self,
        replica_id: str,
        peers: list[str],
        fetcher: Fetcher,
        snapshot,
        commit,
    ) -> None:
        self.replica_id = replica_id
        self.peers = list(peers)
        self._fetcher = fetcher
        self._snapshot = snapshot
        self._commit = commit
        self._lock = threading.Lock()
        self.last_gossip: dict[str, dict[str, Any]] = {}

    def gossip_once(self) -> dict[str, Any]:
        """One pass over every peer: roots -> walk -> reconcile.

        Returns a summary per peer: roots compared, objects exchanged,
        resolutions applied. Convergence detection is built in: equal
        roots short-circuit to zero object traffic (the 5000x property).
        """
        summary: dict[str, Any] = {}
        with self._lock:
            local = dict(self._snapshot())
            local_tree = ObjectMerkleTree(list(local.values()))
            for peer_url in self.peers:
                try:
                    remote_roots = self._fetcher.fetch_roots(peer_url)
                    remote_root = bytes.fromhex(remote_roots["root"])
                except Exception as exc:  # noqa: BLE001 - one peer down must not stop the loop
                    logger.warning("gossip roots failed for %s: %s", peer_url, exc)
                    summary[peer_url] = {"error": str(exc)}
                    continue
                if not gossip(local_tree.root, remote_root):
                    summary[peer_url] = {"converged": True, "exchanged": 0}
                    self.last_gossip[peer_url] = {"converged": True}
                    continue
                # Tier 2: walk the diff. The remote exposes every object
                # id; the ids whose remote leaves differ are the fetch
                # set (a full-id exchange is the Phase-2 simplicity
                # trade; the tree walk narrows it in Phase 3).
                try:
                    remote_all = self._fetcher.fetch_objects(peer_url, ["*"])
                except Exception as exc:  # noqa: BLE001
                    summary[peer_url] = {"error": str(exc)}
                    continue
                remote = {
                    rec["id"]: mesh_object_from_wire(rec)
                    for rec in remote_all.get("objects", [])
                }
                remote_tree = ObjectMerkleTree(list(remote.values()))
                divergent = local_tree.divergent_ids(remote_tree)
                plan = reconcile(local, remote)
                next_local = apply_plan(local, remote, plan, self.replica_id)
                resolutions = {
                    obj_id: plan[obj_id].value
                    for obj_id in divergent
                    if obj_id in plan
                }
                changed = sum(
                    1
                    for r in plan.values()
                    if r
                    in (
                        Resolution.FETCH_REMOTE,
                        Resolution.UNION,
                        Resolution.REMOVE_WINS,
                        Resolution.SUPERSEDED,
                    )
                )
                if changed:
                    self._commit(next_local)
                    local = next_local
                    local_tree = ObjectMerkleTree(list(local.values()))
                summary[peer_url] = {
                    "divergent": len(divergent),
                    "changed": changed,
                    "resolutions": resolutions,
                }
                self.last_gossip[peer_url] = summary[peer_url]
        return summary


class MemoryWire:
    """In-memory Fetcher + peer registry for tests and local loops.

    Phase 3: fetch_objects with an explicit id list returns only those
    objects; the wildcard ``["*"]`` remains for back-compat. Callers can
    inspect ``requested_ids`` to assert the protocol narrows exchanges.
    """

    def __init__(self) -> None:
        self._peers: dict[str, PeerSnapshot] = {}
        self.requested_ids: list[list[str]] = []

    def register(self, peer_url: str, snapshot: PeerSnapshot) -> None:
        self._peers[peer_url] = snapshot

    def fetch_roots(self, peer_url: str) -> dict[str, Any]:
        return self._peers[peer_url].roots_payload()

    def fetch_ids(self, peer_url: str) -> dict[str, Any]:
        """Phase-3 id manifest: object ids + leaf hashes for remote tree
        reconstruction without transferring payloads."""
        objects = self._peers[peer_url].objects
        return {
            "ids": {
                obj_id: ObjectMerkleTree.leaf_hash(obj).hex()
                for obj_id, obj in sorted(objects.items())
            }
        }

    def fetch_objects(self, peer_url: str, ids: list[str]) -> dict[str, Any]:
        self.requested_ids.append(list(ids))
        objects = self._peers[peer_url].objects
        selected = (
            objects
            if ids == ["*"]
            else {obj_id: objects[obj_id] for obj_id in ids if obj_id in objects}
        )
        return {
            "objects": [mesh_object_to_wire(o) for o in selected.values()]
        }


def divergent_ids_from_manifest(
    local_objects: dict[str, MeshObject],
    remote_manifest: dict[str, str],
) -> tuple[list[str], list[str]]:
    """Phase-3 narrow exchange: compute the fetch/send sets from an id
    manifest (object id -> remote leaf hash hex) without any payload
    transfer. Returns ``(fetch_ids, send_ids)``: ids whose leaves differ
    locally-absent or remote-absent, split by direction. Payloads are
    fetched only for ``fetch_ids``; ``send_ids`` piggyback on the next
    push by the remote's own mirrored gossip.
    """
    fetch_ids: list[str] = []
    send_ids: list[str] = []
    local_hashes = {
        obj_id: ObjectMerkleTree.leaf_hash(obj).hex()
        for obj_id, obj in local_objects.items()
    }
    for obj_id in sorted(set(local_hashes) | set(remote_manifest)):
        lo = local_hashes.get(obj_id)
        ro = remote_manifest.get(obj_id)
        if lo == ro:
            continue
        if ro is None:
            send_ids.append(obj_id)
        else:
            fetch_ids.append(obj_id)
    return fetch_ids, send_ids


def validate_peer_url(url: str) -> bool:
    """http(s) only, host explicitly required, no credentials in URL."""
    if not url.startswith(("http://", "https://")):
        return False
    if "@" in url:
        return False
    rest = url.split("://", 1)[1]
    host = rest.split("/", 1)[0].split(":", 1)[0]
    return bool(host)
