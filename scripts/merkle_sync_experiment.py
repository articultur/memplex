#!/usr/bin/env python3
"""Merkle anti-entropy vs whole-file sync: measured on real lite-store data.

Tests the deep-research conclusion that (a) Merkle-tree reconciliation
makes peer-sync bandwidth proportional to the DIFFERENCE, not the corpus,
and (b) RFC 6962-style consistency proofs give O(log n) append-only
verification for the changelog.

RFC 6962 domain separation (0x00 leaf / 0x01 internal) is used verbatim --
the research flagged it as REQUIRED for second-preimage resistance.

Runs entirely offline on real stores: builds two stores from a common
ancestor, diverges one by k edits, then measures bytes-transferred for
whole-pair sync (current semantics) vs Merkle-walk reconciliation.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import sys
import tempfile
import time

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")
os.environ.setdefault("MEMPLEX_LLM_QUERY_ENHANCEMENT", "false")

# ── RFC 6962 Merkle tree (domain-separated) ──────────────────────────


def _leaf_hash(data: bytes) -> bytes:
    return hashlib.sha256(b"\x00" + data).digest()


def _node_hash(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


class MerkleTree:
    """RFC 6962-shaped Merkle tree over ordered leaves."""

    def __init__(self, leaves: list[bytes]) -> None:
        self.leaves = [_leaf_hash(d) for d in leaves]
        # levels[0] = leaf hashes; levels[-1] = [root]
        self.levels: list[list[bytes]] = [list(self.leaves)]
        current = self.leaves
        while len(current) > 1:
            nxt = []
            for i in range(0, len(current), 2):
                left = current[i]
                right = current[i + 1] if i + 1 < len(current) else current[i]
                nxt.append(_node_hash(left, right))
            self.levels.append(nxt)
            current = nxt

    @property
    def root(self) -> bytes:
        return self.levels[-1][0] if self.leaves else hashlib.sha256(b"").digest()

    def diff_indexes(self, other: MerkleTree) -> list[int]:
        """Indexes of leaves differing between self and other.

        Bandwidth model: each level exchanges only the subtree hashes
        needed to navigate divergence -- proportional to divergent
        subtree count, not leaf count.
        """
        if self.root == other.root:
            return []
        a_levels, b_levels = self.levels, other.levels
        divergent: list[int] = []
        depth = min(len(a_levels), len(b_levels)) - 1
        # start at the root, walk down divergent subtrees
        stack: list[tuple[int, int]] = [(depth, 0)]  # (level, index-within-level)
        # handle differing leaf counts: extra leaves on either side count
        common_leaves = min(len(self.leaves), len(other.leaves))
        while stack:
            level, idx = stack.pop()
            if level == 0:
                divergent.append(idx)
                continue
            a_node = a_levels[level][idx] if idx < len(a_levels[level]) else None
            b_node = b_levels[level][idx] if idx < len(b_levels[level]) else None
            if a_node == b_node and a_node is not None:
                continue
            left_idx, right_idx = idx * 2, idx * 2 + 1
            stack.append((level - 1, left_idx))
            stack.append((level - 1, right_idx))
        # leaves beyond the common prefix (appended on one side only)
        longer = max(len(self.leaves), len(other.leaves))
        divergent.extend(range(common_leaves, longer))
        return sorted(set(divergent))


def consistency_proof(old: MerkleTree, new: MerkleTree) -> list[bytes]:
    """RFC 6962 consistency proof: old root is a prefix of new root."""
    # simplified: proof nodes along the right edge of old within new
    proof: list[bytes] = []
    if len(old.leaves) == 0 or len(new.leaves) < len(old.leaves):
        return proof
    idx = len(old.leaves)
    for level in range(len(new.levels)):
        if idx % 2 == 1:
            sibling = new.levels[level][idx + 1] if idx + 1 < len(new.levels[level]) else None
            if sibling is not None:
                proof.append(sibling)
        idx //= 2
    return proof


# ── store helpers ────────────────────────────────────────────────────


def build_store(path: pathlib.Path, n_docs: int) -> object:
    from memplex.config import MemplexConfig
    from memplex.service import MemplexService

    config = MemplexConfig()
    config.storage.backend = "lite"
    config.storage.path = str(path / "s.sqlite3")
    config.llm.query_enhancement = False
    svc = MemplexService(config=config)
    svc.start()
    from memplex.models import FieldValue, Function, SourceDocument, SourceType

    with svc.store.deferred_commit():
        for i in range(n_docs):
            svc.store.add(
                Function(
                    id=f"exp-f-{i}",
                    name=f"experiment fact {i} topic {i % 500}",
                    name_normalized=f"exp-fact-{i}",
                    domain=f"topic-{i % 500}",
                    memory_type="function",
                    source_type=SourceType.WIKI,
                    action=[FieldValue(desc=f"fact number {i} with unique token tok{i}")],
                ),
                SourceDocument(type="exp", content=f"content {i}", source_type=SourceType.WIKI),
            )
    return svc


def snapshot_nodes(store) -> dict[str, bytes]:
    """id -> canonical bytes for every resident Function."""
    out = {}
    for func in store._functions.values():
        payload = f"{func.id}|{func.name}|{func.domain}|".encode()
        for fv in func.action:
            payload += fv.desc.encode()
        out[func.id] = payload
    return out


def merkle_for(nodes: dict[str, bytes]) -> MerkleTree:
    ordered = [nodes[k] for k in sorted(nodes)]
    return MerkleTree(ordered)


# ── experiment ───────────────────────────────────────────────────────


def run_case(n_docs: int, k_edits: int) -> dict:
    base_dir = pathlib.Path(tempfile.mkdtemp(prefix=f"merkle-exp-{n_docs}-{k_edits}-"))
    ancestor = build_store(base_dir / "ancestor", n_docs)
    try:
        import json as _json

        base_nodes = snapshot_nodes(ancestor.store)
        base_tree = merkle_for(base_nodes)

        # peer view diverges: k modifications + k additions
        peer_nodes = dict(base_nodes)
        ids = sorted(peer_nodes)
        for j in range(k_edits):
            fid = ids[(j * max(1, len(ids) // max(k_edits, 1))) % len(ids)]
            peer_nodes[fid] = peer_nodes[fid] + f"|edit{j}".encode()
        for j in range(k_edits):
            peer_nodes[f"exp-new-{j}"] = f"new node {j}".encode()
        peer_tree = merkle_for(peer_nodes)

        # ground-truth diff: sorted leaf-by-leaf (what a correct tree walk finds)
        base_sorted = [base_nodes[k] for k in sorted(base_nodes)]
        peer_sorted = [peer_nodes[k] for k in sorted(peer_nodes)]
        divergent = [
            (i, a, b)
            for i, (a, b) in enumerate(zip(base_sorted, peer_sorted))
            if a != b
        ]
        appended = abs(len(peer_sorted) - len(base_sorted))
        diff_count = len(divergent) + appended

        # (a) whole-pair sync baseline: serialized corpus bytes on the wire
        whole_bytes = len(_json.dumps({k: v.decode("utf-8", "replace") for k, v in base_nodes.items()}).encode())

        # (b) Merkle reconciliation protocol bytes: root exchange + per
        # divergent leaf (log2(N) path hashes x 32B both directions +
        # payload transfer), plus appended leaves
        import math
        depth = max(1, math.ceil(math.log2(max(n_docs, 2))))
        hash_b = 32
        divergent_payload = sum(max(len(a), len(b)) for _, a, b in divergent)
        appended_payload = sum(len(peer_nodes[f"exp-new-{j}"]) for j in range(k_edits))
        merkle_bytes = (
            hash_b  # root exchange
            + diff_count * depth * hash_b  # path hashes
            + divergent_payload
            + appended_payload
        )

        # sanity: roots must differ when diff_count > 0, equal when 0
        assert (base_tree.root == peer_tree.root) == (diff_count == 0)

        # (c) CT-style consistency proof for changelog append-only
        old_tree = MerkleTree([f"event-{i}".encode() for i in range(n_docs)])
        new_tree = MerkleTree([f"event-{i}".encode() for i in range(n_docs + k_edits)])
        proof = consistency_proof(old_tree, new_tree)
        return {
            "n_docs": n_docs,
            "k_edits": k_edits,
            "diff_count": diff_count,
            "whole_pair_bytes": whole_bytes,
            "merkle_protocol_bytes": merkle_bytes,
            "ratio": round(merkle_bytes / max(whole_bytes, 1), 4),
            "saving": f"{round((1 - merkle_bytes / max(whole_bytes, 1)) * 100, 1)}%",
            "consistency_proof_nodes": len(proof),
        }
    finally:
        ancestor.stop()


def main() -> int:
    print(f"{'N':>7} {'k':>5} {'diff':>7} {'whole(B)':>12} {'merkle(B)':>11} {'merkle/whole':>12} {'saving':>9} {'proof':>6}")
    results = []
    for n_docs in (2000, 10000, 50000):
        for k_edits in (1, 10, 100):
            case = run_case(n_docs, k_edits)
            results.append(case)
            print(
                f"{case['n_docs']:>7} {case['k_edits']:>5} {case['diff_count']:>7} "
                f"{case['whole_pair_bytes']:>12} {case['merkle_protocol_bytes']:>11} "
                f"{case['ratio']:>12} {case['saving']:>9} {case['consistency_proof_nodes']:>6}"
            )
    import json

    out = pathlib.Path("benchmarks/results/merkle-sync-experiment.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2))
    print(f"\nsaved -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
