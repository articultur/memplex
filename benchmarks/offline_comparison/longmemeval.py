"""Offline cleaned LongMemEval-S retrieval pilot, never a QA or SOTA score.

Only the pinned official corpus is accepted. The product receives whitelisted
history/query fields, via chronological public service writes into a fresh bank.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import re
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
DATASET_SHA256 = 'd6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442'
DATASET_REVISION = '98d7416c24c778c2fee6e6f3006e7a073259d48f'
DATASET_URL = f'https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/{DATASET_REVISION}/longmemeval_s_cleaned.json'
SEED = 'memplex-offline-pilot-20261007-v1'
CONFIG = {'storage': 'lite', 'embedding': 'tfidf', 'provider': 'rule-based',
          'factual_capture': False, 'orchestrated': False, 'candidate_nodes': 100,
          'session_cutoffs': [5, 10], 'query_max_tokens': 0, 'bm25_k1': 1.5,
          'bm25_b': .75, 'token_measure': 'unicode-word-count',
          'background_worker': False, 'compaction': False,
          'ingest_granularity': 'one full session per public service.write call'}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def file_hash(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def load_dataset(path):
    if file_hash(path) != DATASET_SHA256:
        raise ValueError('Dataset checksum mismatch; only pinned official cleaned-S is accepted')
    rows = json.loads(Path(path).read_text())
    if len(rows) != 500 or len({r['question_id'] for r in rows}) != 500:
        raise ValueError('Expected 500 unique official questions')
    return rows


def stratum(row):
    return 'abstention' if row['question_id'].endswith('_abs') else row['question_type']


def build_manifest(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[stratum(row)].append(row['question_id'])
    allocations = {k: len(v) * 100 // len(rows) for k, v in groups.items()}
    remainders = sorted(groups, key=lambda k: (-(len(groups[k]) * 100 % len(rows)), k))
    for key in remainders[:100 - sum(allocations.values())]:
        allocations[key] += 1
    selected = []
    for key in sorted(groups):
        ranked = sorted(groups[key], key=lambda q: hashlib.sha256(f'{SEED}:{q}'.encode()).hexdigest())
        selected.extend(ranked[:allocations[key]])
    return {'protocol_version': 1, 'seed': SEED, 'dataset_sha256': DATASET_SHA256,
            'dataset_revision': DATASET_REVISION, 'dataset_url': DATASET_URL,
            'license': 'MIT (official dataset card; upstream code LICENSE)',
            'selection': 'proportional strata; largest remainder; SHA256(seed:id) ranking',
            'pilot_ids': sorted(selected),
            'heldout_ids': sorted({r['question_id'] for r in rows} - set(selected)),
            'pilot_strata': dict(sorted(allocations.items()))}


def validate_manifest(manifest):
    pilot, heldout = manifest['pilot_ids'], manifest['heldout_ids']
    if (len(pilot) != 100 or len(set(pilot)) != 100 or len(heldout) != 400
            or len(set(heldout)) != 400 or set(pilot) & set(heldout)):
        raise ValueError('Primary manifest needs exactly 100 pilot and 400 disjoint heldout IDs')


def retriever_input(row):
    """Gold answer, answer_session_ids and has_answer never cross this boundary."""
    ids, dates, histories = (row[k] for k in ('haystack_session_ids', 'haystack_dates', 'haystack_sessions'))
    if len(ids) != len(dates) or len(ids) != len(histories):
        raise ValueError('Unaligned session metadata')
    sessions = [{'id': sid, 'date': date,
                 'turns': [{'role': t['role'], 'content': t['content']} for t in turns]}
                for sid, date, turns in zip(ids, dates, histories, strict=True)]
    sessions.sort(key=lambda s: (s['date'][:10], s['date'][-5:], str(s['id'])))
    return {'question_id': row['question_id'], 'query': row['question'],
            'question_date': row['question_date'], 'sessions': sessions}


def session_text(session):
    return f"Session date: {session['date']}\n\n" + '\n\n'.join(
        f"{turn['role']}: {turn['content']}" for turn in session['turns'])


def tokens(text):
    return re.findall(r'\w+', text.lower(), re.UNICODE)


def score_sessions(ranked, gold, cutoff):
    targets = set(gold)
    if not targets:
        return {'recall': None, 'complete': None}
    found = targets & set(ranked[:cutoff])
    return {'recall': len(found) / len(targets), 'complete': found == targets}


def run_bm25(public):
    started = time.perf_counter()
    corpus = [Counter(tokens(session_text(s))) for s in public['sessions']]
    lengths = [sum(c.values()) for c in corpus]
    avg = statistics.mean(lengths) if lengths else 1
    df = Counter(t for c in corpus for t in c)
    ingest_ms = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    query = set(tokens(public['query']))
    scores = []
    for i, counts in enumerate(corpus):
        score = sum(math.log(1 + (len(corpus) - df[t] + .5) / (df[t] + .5)) *
                    counts[t] * 2.5 / (counts[t] + 1.5 * (.25 + .75 * lengths[i] / avg))
                    for t in query if counts[t])
        scores.append((score, i))
    ranked = sorted(scores, key=lambda p: (-p[0], str(public['sessions'][p[1]]['id'])))
    elapsed = (time.perf_counter() - started) * 1000
    return {'status': 'complete', 'ranked_session_ids': [public['sessions'][i]['id'] for _, i in ranked[:10]],
            'ingest_ms': ingest_ms, 'query_ms': elapsed,
            'returned_word_tokens': {str(k): sum(lengths[i] for _, i in ranked[:k]) for k in (5, 10)},
            'storage_bytes': len(json.dumps([dict(c) for c in corpus], sort_keys=True).encode()),
            'storage_measure': 'serialized term frequencies; excludes Python object overhead',
            'ingest_sessions': len(corpus), 'ingest_path': 'session BM25 (k1=1.5,b=.75)'}


def rank_evidence(hits, provenance):
    """Count unresolved provenance across all candidates, rank sessions by first hit."""
    unmapped = sum(not provenance.get(hit.func_id) for hit in hits)
    ranked, word_counts = [], []
    for hit in hits:
        added = [sid for sid in sorted(provenance.get(hit.func_id, set())) if sid not in ranked]
        for offset, sid in enumerate(added):
            ranked.append(sid)
            word_counts.append(len(tokens(hit.summary)) if offset == 0 else 0)
        if len(ranked) >= 10:
            break
    return ranked[:10], word_counts[:10], unmapped


def run_memplex(public, bank):
    """No pre-extracted memories or direct storage writes, even on failure."""
    from memplex.config import MemplexConfig
    from memplex.models import SourceDocument
    from memplex.models.paragraph import persisted_paragraph_id
    from memplex.service import MemplexService

    config = MemplexConfig()
    config.storage.backend = 'lite'
    config.storage.path = str(bank)
    config.embedding.model = 'tfidf'
    config.embedding.hyde_enabled = False
    config.llm.provider = 'rule-based'
    config.llm.fallback_chain = ['rule-based']
    config.llm.query_enhancement = False
    config.llm.observation_compression = False
    config.llm.factual_capture = False
    config.wiki.enabled = False
    config.wiki.dir = str(bank / 'wiki')
    config.sleep_time.enabled = False
    config.compaction.warn_threshold = 10**9
    config.compaction.hard_limit = 10**9
    config.retrieval.orchestrated = False
    service = MemplexService(config=config)
    provenance = defaultdict(set)
    started = time.perf_counter()
    ingested = 0
    try:
        for session in public['sessions']:
            extracted = service.write(SourceDocument(type='text', content=session_text(session)))
            for node in [*extracted.functions, *extracted.facts, *extracted.preferences]:
                provenance[node.id].add(session['id'])
            for paragraph in extracted.paragraphs:
                pid = persisted_paragraph_id('text', paragraph.id, paragraph.raw_text.strip())
                provenance[pid].add(session['id'])
            ingested += 1
        ingest_ms = (time.perf_counter() - started) * 1000
        queried = time.perf_counter()
        result = service.query(public['query'], top_k=100, max_tokens=0, orchestrated=False)
        query_ms = (time.perf_counter() - queried) * 1000
        ranked, word_counts, unmapped = rank_evidence(result.results, provenance)
        return {'status': 'complete', 'ranked_session_ids': ranked[:10],
                'ingest_ms': ingest_ms, 'query_ms': query_ms,
                'returned_word_tokens': {str(k): sum(word_counts[:k]) for k in (5, 10)},
                'storage_bytes': sum(p.stat().st_size for p in bank.rglob('*') if p.is_file()),
                'storage_measure': 'on-disk Lite bank before shutdown',
                'unmapped_results': unmapped, 'candidate_nodes_returned': len(result.results),
                'ingest_sessions': ingested, 'ingest_path': 'MemplexService.write'}
    except Exception as error:
        return {'status': 'error', 'phase': 'ingestion' if ingested < len(public['sessions']) else 'query',
                'error_type': type(error).__name__, 'error': str(error),
                'ingest_sessions': ingested, 'expected_sessions': len(public['sessions']),
                'failed_session_id': public['sessions'][ingested]['id'] if ingested < len(public['sessions']) else None,
                'elapsed_ms': (time.perf_counter() - started) * 1000}
    finally:
        service.stop()


def offline_worker(input_path, output_path, bank):
    import socket
    def deny(*_args, **_kwargs):
        raise RuntimeError('Network is forbidden by offline benchmark protocol')
    socket.socket.connect = deny
    socket.create_connection = deny
    socket.getaddrinfo = deny
    public = json.loads(Path(input_path).read_text())
    result = run_memplex(public, Path(bank))
    Path(output_path).write_text(json.dumps(result))


def source_hash(root=ROOT):
    # Extraction dictionaries, SQL schemas and packaged adapters affect behavior too.
    files = [p for p in (root / 'memplex').rglob('*') if p.is_file()
             and '__pycache__' not in p.parts and p.suffix not in ('.pyc', '.pyo')]
    files.extend(p for p in (root / 'pyproject.toml', root / 'uv.lock') if p.exists())
    return digest({str(p.relative_to(root)): file_hash(p) for p in sorted(files)})


def aggregate(records):
    summary = {'expected_questions': 100, 'recorded_questions': len(records),
               'status': 'incomplete', 'systems': {},
               'claim': 'offline retrieval pilot, not answer accuracy or SOTA'}
    for system in ('bm25', 'memplex'):
        complete = [r for r in records if r.get(system, {}).get('status') == 'complete']
        scored = [r for r in complete if not r['abstention']]
        stats = {'completed': len(complete), 'failed': len(records) - len(complete),
                 'recall_denominator': len(scored), 'abstention_excluded': len(complete) - len(scored)}
        for k in (5, 10):
            for metric in ('recall', 'complete'):
                values = [r[system]['scores'][str(k)][metric] for r in scored]
                stats[f'{metric}@{k}'] = statistics.mean(values) if values else None
            words = [r[system]['returned_word_tokens'][str(k)] for r in complete]
            stats[f'mean_returned_word_tokens@{k}'] = statistics.mean(words) if words else None
        for field in ('ingest_ms', 'query_ms', 'storage_bytes'):
            values = sorted(r[system][field] for r in complete)
            stats[f'{field}_p50'] = statistics.median(values) if values else None
            stats[f'{field}_p95'] = values[max(0, math.ceil(.95 * len(values)) - 1)] if values else None
        stats['errors'] = dict(Counter(r.get(system, {}).get('error', r.get(system, {}).get('status', 'absent'))
                                      for r in records if r not in complete))
        summary['systems'][system] = stats
    if len(records) == 100:
        summary['status'] = 'complete' if all(s['completed'] == 100 for s in summary['systems'].values()) else 'blocked'
    return summary


def atomic_json(path, value):
    """Publish complete JSON only, retaining the prior version across interruption."""
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w') as output:
        output.write(json.dumps(value, indent=2) + '\n')
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


def prepare_resume(out, receipt, manifest):
    receipt_path, records_path = out / 'receipt.json', out / 'records.jsonl'
    if records_path.exists() and records_path.stat().st_size and not receipt_path.exists():
        raise ValueError('Nonempty records lack their original receipt; refusing to certify them')
    if receipt_path.exists() and json.loads(receipt_path.read_text()) != receipt:
        raise ValueError('Resume receipt mismatch; use a separate output directory')
    records = [json.loads(line) for line in records_path.read_text().splitlines()] if records_path.exists() else []
    ids = [r['question_id'] for r in records]
    if len(ids) != len(set(ids)) or not set(ids) <= set(manifest['pilot_ids']):
        raise ValueError('Resume records contain duplicate or foreign question IDs')
    if any(not {'bm25', 'memplex', 'abstention', 'stratum'} <= r.keys() for r in records):
        raise ValueError('Resume record is incomplete')
    # Repair stale/missing summary even when no question remains to be processed.
    atomic_json(out / 'summary.json', aggregate(records))
    if not receipt_path.exists():
        atomic_json(receipt_path, receipt)
    return records


def child_run(public, timeout):
    with tempfile.TemporaryDirectory(prefix='memplex-offline-') as directory:
        temp = Path(directory)
        input_path, output_path = temp / 'input.json', temp / 'result.json'
        input_path.write_text(json.dumps(public))
        env = {'PATH': os.defpath, 'HOME': str(temp), 'PYTHONHASHSEED': '0', 'PYTHONPATH': str(ROOT),
               'MEMPLEX_STORAGE_BACKEND': 'lite', 'MEMPLEX_RAW_PARAGRAPH_LAYER': '1',
               'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1', 'OMP_NUM_THREADS': '1'}
        try:
            process = subprocess.run([sys.executable, '-m', 'benchmarks.offline_comparison.longmemeval',
                                      'worker', str(input_path), str(output_path), str(temp / 'bank')],
                                     cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            return {'status': 'timeout', 'error': f'Exceeded per-question {timeout}s budget'}
        if process.returncode or not output_path.exists():
            return {'status': 'error', 'phase': 'worker', 'error': process.stderr[-1500:]}
        return json.loads(output_path.read_text())


def run(args):
    rows = load_dataset(args.dataset)
    manifest = json.loads(Path(args.manifest).read_text())
    validate_manifest(manifest)
    if manifest != build_manifest(rows):
        raise ValueError('Frozen manifest differs from pinned protocol')
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    versions = {}
    for package in ('memplex', 'numpy', 'PyYAML', 'requests'):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = 'not installed'
    receipt = {'dataset_sha256': DATASET_SHA256, 'manifest_sha256': digest(manifest), 'config': CONFIG,
               'product_source_sha256': source_hash(), 'harness_sha256': file_hash(__file__),
               'git_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
               'python': platform.python_version(), 'platform': platform.platform(), 'dependencies': versions,
               'timing_context': 'shared cloud runner; one cold query per fresh bank; concurrent checks may contend',
               'timeout_seconds': args.timeout, 'dense_baseline': 'not run: no pre-provisioned dense model selected'}
    records_path = out / 'records.jsonl'
    records = prepare_resume(out, receipt, manifest)
    ids = [r['question_id'] for r in records]
    lookup = {r['question_id']: r for r in rows}
    for qid in manifest['pilot_ids']:
        if qid in ids:
            continue
        row = lookup[qid]
        public = retriever_input(row)
        record = {'question_id': qid, 'stratum': stratum(row), 'abstention': qid.endswith('_abs'),
                  'bm25': run_bm25(public), 'memplex': child_run(public, args.timeout)}
        gold = [] if record['abstention'] else row['answer_session_ids']
        for system in ('bm25', 'memplex'):
            if record[system]['status'] == 'complete':
                record[system]['scores'] = {str(k): score_sessions(record[system]['ranked_session_ids'], gold, k) for k in (5, 10)}
        with records_path.open('a') as output:
            output.write(json.dumps(record, sort_keys=True) + '\n')
            output.flush()
            os.fsync(output.fileno())
        records.append(record)
        atomic_json(out / 'summary.json', aggregate(records))
        print(f'{len(records)}/100 {qid} memplex={record["memplex"]["status"]}', flush=True)
    atomic_json(out / 'summary.json', aggregate(records))
    return 0 if aggregate(records)['status'] == 'complete' else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    freeze = commands.add_parser('freeze')
    freeze.add_argument('--dataset', required=True)
    freeze.add_argument('--out', required=True)
    runner = commands.add_parser('run')
    for option in ('dataset', 'manifest', 'out'):
        runner.add_argument(f'--{option}', required=True)
    runner.add_argument('--timeout', type=int, default=120)
    worker = commands.add_parser('worker')
    for option in ('input', 'output', 'bank'):
        worker.add_argument(option)
    args = parser.parse_args()
    if args.command == 'freeze':
        manifest = build_manifest(load_dataset(args.dataset))
        validate_manifest(manifest)
        Path(args.out).write_text(json.dumps(manifest, indent=2) + '\n')
        return 0
    if args.command == 'worker':
        offline_worker(args.input, args.output, args.bank)
        return 0
    return run(args)


if __name__ == '__main__':
    raise SystemExit(main())
