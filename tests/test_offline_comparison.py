"""Frozen offline comparison contracts, runnable without paid dependencies."""
import copy
import importlib.util
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace


def module():
    # Loading this standalone module avoids benchmarks/__init__ importing the product.
    path = Path(__file__).resolve().parents[1] / 'benchmarks/offline_comparison/longmemeval.py'
    spec = importlib.util.spec_from_file_location('offline_lme_protocol', path)
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def sample(qid='q1', kind='single-session-user'):
    return {'question_id': qid, 'question_type': kind, 'question': 'Where do I live?',
            'question_date': '2023/05/02 (Tue) 10:00', 'answer': 'EVALUATOR_ONLY',
            'answer_session_ids': ['s1'], 'haystack_session_ids': ['s2', 's1'],
            'haystack_dates': ['2023/05/01 (Mon) 09:00', '2023/04/01 (Sat) 09:00'],
            'haystack_sessions': [[{'role': 'user', 'content': 'I moved to Vienna.', 'has_answer': False}],
                                  [{'role': 'user', 'content': 'I live in Berlin.', 'has_answer': True}]]}


class TestOfflineProtocol(unittest.TestCase):
    def test_input_is_label_free_chronological_and_does_not_mutate(self):
        h = module()
        row = sample()
        original = copy.deepcopy(row)
        public = h.retriever_input(row)
        self.assertEqual([s['id'] for s in public['sessions']], ['s1', 's2'])
        for label in ('EVALUATOR_ONLY', 'has_answer', 'answer_session_ids'):
            self.assertNotIn(label, json.dumps(public))
        self.assertEqual(row, original)

    def test_exact_manifest_is_stratified_disjoint_and_stable(self):
        h = module()
        rows = [sample(f'{kind}{i}', kind) for kind in ('a', 'b') for i in range(250)]
        manifest = h.build_manifest(rows)
        self.assertEqual(manifest, h.build_manifest(rows[::-1]))
        self.assertEqual(manifest['pilot_strata'], {'a': 50, 'b': 50})
        self.assertEqual(len(manifest['pilot_ids']), 100)
        self.assertEqual(len(manifest['heldout_ids']), 400)
        self.assertFalse(set(manifest['pilot_ids']) & set(manifest['heldout_ids']))

    def test_primary_manifest_cannot_be_shortened(self):
        with self.assertRaisesRegex(ValueError, '100'):
            module().validate_manifest({'pilot_ids': ['q1'], 'heldout_ids': []})

    def test_recall_and_complete_coverage_are_distinct(self):
        h = module()
        self.assertEqual(h.score_sessions(['a'], ['a', 'b'], 5), {'recall': .5, 'complete': False})
        self.assertEqual(h.score_sessions(['a', 'b'], ['a', 'b'], 5), {'recall': 1., 'complete': True})
        self.assertEqual(h.score_sessions(['a'], [], 5), {'recall': None, 'complete': None})

    def test_bm25_uses_only_public_input(self):
        h = module()
        result = h.run_bm25(h.retriever_input(sample()))
        self.assertEqual(result['ranked_session_ids'][0], 's1')
        self.assertEqual(result['status'], 'complete')

    def test_dataset_drift_has_no_synthetic_fallback(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'data.json'
            path.write_text('[]')
            with self.assertRaisesRegex(ValueError, 'checksum'):
                module().load_dataset(path)

    def test_failed_product_not_scored_zero_or_hide_bm25(self):
        h = module()
        public = h.retriever_input(sample())
        result = h.run_bm25(public)
        result['scores'] = {'5': h.score_sessions(result['ranked_session_ids'], ['s1'], 5),
                            '10': h.score_sessions(result['ranked_session_ids'], ['s1'], 10)}
        summary = h.aggregate([{'question_id': 'q1', 'stratum': 'single-session-user',
                                'abstention': False, 'bm25': result,
                                'memplex': {'status': 'error', 'error': 'ingest failure'}}])
        self.assertEqual(summary['systems']['memplex']['completed'], 0)
        self.assertIsNone(summary['systems']['memplex']['recall@5'])
        self.assertEqual(summary['systems']['bm25']['recall@5'], 1.)
        self.assertEqual(summary['status'], 'incomplete')

    def test_repeated_headings_are_preserved(self):
        h = module()
        row = sample()
        row['haystack_sessions'][0][0]['content'] = '**Cost:**\n\nOne price.\n\n**Cost:**\n\nAnother price.'
        public = h.retriever_input(row)
        self.assertEqual(h.session_text(public['sessions'][1]).count('**Cost:**'), 2)


class TestProvenanceAccounting(unittest.TestCase):
    def test_unmapped_count_covers_all_candidates_not_just_top_ten_sessions(self):
        hits = [SimpleNamespace(func_id=f'n{i}', summary='one evidence') for i in range(100)]
        mapping = {f'n{i}': {f's{i}'} for i in range(10)}
        ranked, words, unmapped = module().rank_evidence(hits, mapping)
        self.assertEqual(len(ranked), 10)
        self.assertEqual(unmapped, 90)
        self.assertEqual(sum(words), 20)

    def test_shared_evidence_text_counted_once(self):
        hits = [SimpleNamespace(func_id='shared', summary='one evidence')]
        ranked, words, unmapped = module().rank_evidence(hits, {'shared': {'s1', 's2'}})
        self.assertEqual(ranked, ['s1', 's2'])
        self.assertEqual(sum(words), 2)
        self.assertEqual(unmapped, 0)


class TestResumeIntegrity(unittest.TestCase):
    def test_runtime_dictionary_change_invalidates_source_hash(self):
        h = module()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / 'memplex'
            package.mkdir()
            (package / 'module.py').write_text('VALUE = 1\n')
            dictionary = package / 'terms.yaml'
            dictionary.write_text('one: two\n')
            before = h.source_hash(root)
            dictionary.write_text('one: three\n')
            self.assertNotEqual(before, h.source_hash(root))

    def test_nonempty_records_without_receipt_fail_closed(self):
        h = module()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'records.jsonl').write_text('{"question_id": "q1"}\n')
            with self.assertRaisesRegex(ValueError, 'receipt'):
                h.prepare_resume(root, {'source': 'fresh'}, {'pilot_ids': ['q1']})
            self.assertFalse((root / 'receipt.json').exists())

    def test_resume_rebuilds_summary_even_with_no_new_records(self):
        h = module()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            receipt = {'source': 'same'}
            (root / 'receipt.json').write_text(json.dumps(receipt))
            record = {'question_id': 'q1', 'stratum': 'single-session-user', 'abstention': False,
                      'bm25': {'status': 'error', 'error': 'diagnostic'},
                      'memplex': {'status': 'error', 'error': 'diagnostic'}}
            (root / 'records.jsonl').write_text(json.dumps(record) + '\n')
            (root / 'summary.json').write_text('{"recorded_questions":0}')
            records = h.prepare_resume(root, receipt, {'pilot_ids': ['q1']})
            self.assertEqual(len(records), 1)
            self.assertEqual(json.loads((root / 'summary.json').read_text())['recorded_questions'], 1)

    def test_receipt_mismatch_does_not_overwrite_evidence(self):
        h = module()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'receipt.json').write_text('{"source":"original"}')
            with self.assertRaisesRegex(ValueError, 'mismatch'):
                h.prepare_resume(root, {'source': 'different'}, {'pilot_ids': []})
            self.assertEqual(json.loads((root / 'receipt.json').read_text()), {'source': 'original'})


if __name__ == '__main__':
    unittest.main()
