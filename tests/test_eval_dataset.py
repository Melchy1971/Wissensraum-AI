"""Versioned retrieval evaluation dataset (Phase 7, D-014): consistency and a runnable baseline."""
import sys
import unittest
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'eval'))

import evaluate


class EvaluationDatasetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus, cls.questions = evaluate.load('corpus.json'), evaluate.load('questions.json')
        cls.vectors = evaluate.load('vectors.json')

    def test_questions_point_to_existing_documents_and_cover_all_types(self):
        ids = [doc['id'] for doc in self.corpus]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len({q['id'] for q in self.questions}), len(self.questions))
        for item in self.questions:
            self.assertTrue(set(item['expected']) <= set(ids), item['id'])
            self.assertEqual(item['type'] == 'ausschluss', not item['expected'], item['id'])
        self.assertTrue({'wortgleich', 'umschreibung', 'ausschluss'} <= set(Counter(q['type'] for q in self.questions)))
        self.assertGreaterEqual(len(self.questions), 60)

    def test_vectors_match_texts_model_and_dimension(self):
        self.assertEqual(self.vectors['model'], 'qwen3-embedding:0.6b')
        self.assertEqual(self.vectors['dimension'], 1024)
        expected = {key: evaluate.sha256(text) for key, _, text in evaluate.corpus_chunks(self.corpus)}
        self.assertEqual({key: entry['sha256'] for key, entry in self.vectors['chunks'].items()}, expected,
                         'Korpus geändert: tests/eval/build_vectors.py erneut ausführen')
        self.assertEqual({key: entry['sha256'] for key, entry in self.vectors['questions'].items()},
                         {q['id']: evaluate.sha256(q['question']) for q in self.questions},
                         'Fragen geändert: tests/eval/build_vectors.py erneut ausführen')
        vector = evaluate.decode_vector(self.vectors['questions']['q01']['vector'], 1024)
        self.assertAlmostEqual(sum(v * v for v in vector), 1.0, places=2)

    def test_baseline_runs_through_the_product_search(self):
        result = evaluate.evaluate()
        for method in ('fulltext', 'vector'):
            metrics = result[method]
            self.assertEqual(metrics['answerable'] + metrics['excluded'], len(self.questions))
            self.assertTrue(0 <= metrics['recall@1'] <= metrics['recall@3'] <= metrics['recall@10'] <= 1)
        # Exact words must be found by today's search; this guards the harness, not a quality target.
        self.assertEqual(result['fulltext']['recall@10[wortgleich]'], 1.0)
        # Retrieval values measured for step 7.3; the answer context of 7.4 must not change them.
        for method, expected in (('vector', (0.759, 0.926, 1.0, 0.855)), ('hybrid', (0.704, 0.852, 0.963, 0.781)),
                                 ('fulltext', (0.537, 0.611, 0.63, 0.573))):
            self.assertEqual(tuple(result[method][key] for key in ('recall@1', 'recall@3', 'recall@10', 'mrr')),
                             expected, method)


class OwnQuestionTests(unittest.TestCase):
    def test_own_questions_use_product_search_and_report_missing_sources(self):
        import json
        import tempfile
        from unittest.mock import patch

        from core import Store

        class Fake:
            def __init__(self, provider, model):
                pass

            def embed(self, texts, query=False):
                return [[1.0, 0.0] if 'Heizung' in text else [0.0, 1.0] for text in texts]
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / 'kopie.db'
            store = Store(db)
            store.add('Heizung', 'Wartung der Heizung im März', filename='heizung.md')
            store.add('Auto', 'Teilkasko für den Wagen', filename='auto.pdf')
            store.save_vectors('m', {row['chunk_id']: Fake(0, 0).embed([row['content']])[0]
                                     for row in store.pending_vectors('m')})
            questions = Path(folder) / 'fragen.json'
            questions.write_text(json.dumps([
                {'id': 'e1', 'type': 'eigen', 'question': 'Wann war die Heizung dran?', 'expected': ['heizung.md']},
                {'id': 'e2', 'type': 'eigen', 'question': 'Was deckt die Kasko?', 'expected': ['auto.pdf']},
                {'id': 'e3', 'type': 'eigen', 'question': 'Rente?', 'expected': ['fehlt.pdf']}]), encoding='utf-8')
            with patch('core.Embedder', Fake):
                result = evaluate.evaluate_own(db, questions, 'm')
        self.assertEqual((result['missing_sources'], result['questions_without_source']), (['fehlt.pdf'], ['e3']))
        self.assertEqual((result['vector']['answerable'], result['vector']['recall@1']), (2, 1.0))
        self.assertEqual(result['chunks_with_vector'], 2)
        self.assertIn('fulltext', result)


if __name__ == '__main__':
    unittest.main()
