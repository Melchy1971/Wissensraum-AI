"""Hybrid retrieval with Reciprocal Rank Fusion and the vector cache (Phase 7, step 7.3)."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core
import retrieval
from core import VECTOR_CACHE, Store
from retrieval import RRF_K, STATUS_FALLBACK, STATUS_FULLTEXT, STATUS_HYBRID, STATUS_PARTIAL, Retriever, rrf
from vector_sync import VectorSync

WORDS = ('heizung', 'therme', 'wartung', 'auto', 'wagen', 'kasko', 'reise', 'flug', 'urlaub', 'rente')


class WordEmbedder:
    """Deterministic stand-in for Ollama: one dimension per known word, synonyms share one."""
    calls = 0
    offline = False
    dimension = len(WORDS)
    SYNONYMS = {'therme': 'heizung', 'wagen': 'auto', 'flug': 'reise'}

    def __init__(self, provider, model):
        self.model = model

    def embed(self, texts, query=False):
        WordEmbedder.calls += 1
        if WordEmbedder.offline:
            raise ValueError('Embedding-Dienst nicht erreichbar.')
        vectors = []
        for text in texts:
            vector = [0.01] * WordEmbedder.dimension
            for word in text.casefold().replace('?', ' ').replace('.', ' ').split():
                word = self.SYNONYMS.get(word, word)
                if word in WORDS:
                    vector[WORDS.index(word)] += 1.0
            vectors.append(vector)
        return vectors


def chunk(document_id, chunk_id, **extra):
    return {'document_id': document_id, 'chunk_id': chunk_id, 'title': f'D{document_id}', 'category': 'A',
            'ordinal': 1, 'content': f'Text {chunk_id}', **extra}


def embed_all(store, model='m'):
    sync = VectorSync(store, WordEmbedder)
    sync.activate(model)
    while sync.run()['embedded']:
        pass
    return sync


class RrfTests(unittest.TestCase):
    def test_exact_scores_ranks_and_methods(self):
        fulltext = [chunk(1, 10, rank=-3.0), chunk(2, 20, rank=-2.0)]
        vector = [chunk(3, 30, score=0.9), chunk(1, 11, score=0.8)]
        results = rrf(fulltext, vector)
        by_id = {r['document_id']: r for r in results}
        self.assertAlmostEqual(by_id[1]['rrf_score'], 1 / (RRF_K + 1) + 1 / (RRF_K + 2))
        self.assertAlmostEqual(by_id[2]['rrf_score'], 1 / (RRF_K + 2))
        self.assertAlmostEqual(by_id[3]['rrf_score'], 1 / (RRF_K + 1))
        self.assertEqual([r['document_id'] for r in results], [1, 3, 2])
        self.assertEqual((by_id[1]['fulltext_rank'], by_id[1]['vector_rank'], by_id[1]['retrieval_method']),
                         (1, 2, 'hybrid'))
        self.assertEqual((by_id[2]['vector_rank'], by_id[2]['retrieval_method']), (None, 'fulltext'))
        self.assertEqual((by_id[3]['fulltext_rank'], by_id[3]['retrieval_method']), (None, 'vector'))
        self.assertEqual((by_id[1]['fulltext_score'], by_id[1]['vector_score']), (-3.0, 0.8))
        self.assertEqual(by_id[1]['chunk_id'], 10)  # the passage of the list where the document ranks better

    def test_ties_are_ordered_by_document_id_and_limit_applies(self):
        results = rrf([chunk(9, 90), chunk(4, 40)], [chunk(4, 41), chunk(9, 91)])
        self.assertEqual([r['document_id'] for r in results], [4, 9])  # equal RRF score
        self.assertEqual(len(rrf([chunk(i, i) for i in range(30)], [], limit=10)), 10)

    def test_only_the_best_chunk_of_a_document_counts_per_list(self):
        results = rrf([chunk(1, 10), chunk(1, 11), chunk(2, 20)], [])
        self.assertEqual([(r['document_id'], r['fulltext_rank'], r['chunk_id']) for r in results],
                         [(1, 1, 10), (2, 2, 20)])


def check_hybrid_contract(case, store):
    """Same behaviour on both backends; ``store`` must be empty."""
    VECTOR_CACHE.clear()
    WordEmbedder.calls, WordEmbedder.offline = 0, False
    heating, _ = store.add('Wartungsplan', 'Die Therme bekommt jährlich eine Wartung.')
    car, _ = store.add('Fahrzeug', 'Kasko für den Wagen.')
    trip, _ = store.add('Urlaub', 'Flug und Hotel gebucht.', filename='reise.md', versioned=True)
    trip2, _ = store.add('Urlaub neu', 'Flug umgebucht, Hotel bleibt.', filename='reise.md', versioned=True)
    retriever = Retriever(store, WordEmbedder)

    # Function words do not match (D-018); a query of nothing but function words still searches.
    case.assertEqual([c['document_id'] for c in store.search('Was kostet die Kasko?')], [car])
    case.assertEqual({c['document_id'] for c in store.search('die eine')}, {heating})

    # Semantic search off: full text only, the model is never asked.
    result = retriever.search('Wann ist die Wartung der Heizung?')
    case.assertEqual((result.mode, result.status, WordEmbedder.calls), ('fulltext', STATUS_FULLTEXT, 0))
    case.assertEqual(result.results[0]['document_id'], heating)

    # Activated, but only part of the stock embedded: hybrid with a visible status.
    sync = VectorSync(store, WordEmbedder)
    sync.activate('m')
    sync.run(limit=2)
    result = retriever.search('Was kostet der Wagen?')
    case.assertEqual((result.mode, result.status), ('hybrid', STATUS_PARTIAL))
    while sync.run()['embedded']:
        pass
    # The synonym is found through the vector list although no word matches literally.
    result = retriever.search('Wo steht etwas zur Heizung?')
    case.assertEqual((result.mode, result.status), ('hybrid', STATUS_HYBRID))
    case.assertEqual(result.results[0]['document_id'], heating)
    case.assertEqual(result.results[0]['vector_rank'], 1)
    case.assertTrue(set(result.timings) >= {'fulltext', 'embedding', 'vector', 'fusion', 'total'})

    # Filters apply to the vector list too: no older version, tag, deleted document.
    ids = [r['document_id'] for r in retriever.search('Flug', mode='vector').results]
    case.assertIn(trip2, ids)
    case.assertNotIn(trip, ids)
    case.assertIn(trip, [r['document_id'] for r in retriever.search('Flug', mode='vector', all_versions=True).results])
    with store.connect() as db:
        import rules
        rules.add_tags(db, car, ['Kunde'])
    case.assertEqual([r['document_id'] for r in retriever.search('Heizung', tag='Kunde').results], [car])
    def generation():
        with store.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE name='vectors.generation'").fetchone()
        return row['value'] if row else None
    loads, token = VECTOR_CACHE.loads, generation()
    store.delete(car)
    case.assertNotEqual(generation(), token)  # same transaction as the deletion
    case.assertNotIn(car, [r['document_id'] for r in retriever.search('Wagen Kasko').results])
    case.assertEqual(VECTOR_CACHE.loads, loads + 1)  # the deletion changed the generation

    # Warm searches reuse the matrix: no reload, no second question embedding for the same text.
    loads, calls = VECTOR_CACHE.loads, WordEmbedder.calls
    with patch('core._load_vector_matrix', side_effect=AssertionError('reloaded')):
        for _ in range(3):
            retriever.search('Heizung  wartung?')
            retriever.search('heizung wartung?')
    case.assertEqual((VECTOR_CACHE.loads, WordEmbedder.calls), (loads, calls + 1))

    # Ollama stops: full text with fallback status, no exception.
    WordEmbedder.offline = True
    result = retriever.search('Ganz neue Frage zur Rente?')
    case.assertEqual((result.mode, result.status), ('fulltext', STATUS_FALLBACK))
    WordEmbedder.offline = False

    # Model change: a new matrix for the new model, old vectors pending again.
    sync.activate('m2')
    case.assertEqual(retriever.search('Heizung').mode, 'fulltext')  # no vectors of m2 yet
    while sync.run()['embedded']:
        pass
    loads = VECTOR_CACHE.loads
    case.assertEqual(retriever.search('Heizung').mode, 'hybrid')
    case.assertEqual(VECTOR_CACHE.loads, loads + 1)


class SqliteRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_hybrid_contract(self):
        check_hybrid_contract(self, Store(self.folder / 'wissen.db'))

    def test_full_text_runs_while_the_question_is_embedded(self):
        import threading
        store = Store(self.folder / 'wissen.db')
        store.add('Heizung', 'Wartung der Therme')
        embed_all(store)
        embedded = threading.Event()
        search = store.search

        def waiting_search(*args, **kwargs):
            self.assertTrue(embedded.wait(5), 'Volltext lief nicht parallel zum Frage-Embedding')
            return search(*args, **kwargs)

        class SignallingEmbedder(WordEmbedder):
            def embed(self, texts, query=False):
                embedded.set()
                return super().embed(texts, query)
        store.search = waiting_search
        result = Retriever(store, SignallingEmbedder).search('Wartung der Heizung')
        self.assertEqual((result.mode, [r['title'] for r in result.results]), ('hybrid', ['Heizung']))
        store.search = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('Volltext kaputt'))
        with self.assertRaisesRegex(RuntimeError, 'Volltext kaputt'):  # full-text errors still surface
            Retriever(store, WordEmbedder).search('Heizung')

    def test_new_vectors_and_another_database_invalidate_the_cache(self):
        first = Store(self.folder / 'a.db')
        first.add('Heizung', 'Wartung der Therme')
        embed_all(first)
        retriever = Retriever(first, WordEmbedder)
        retriever.search('Heizung')
        loads = VECTOR_CACHE.loads
        first.add('Auto', 'Kasko')
        VectorSync(first, WordEmbedder).run()
        self.assertEqual(retriever.search('Kasko').results[0]['title'], 'Auto')
        self.assertEqual(VECTOR_CACHE.loads, loads + 1)
        second = Store(self.folder / 'b.db')
        second.add('Reise', 'Flug gebucht')
        embed_all(second)
        self.assertEqual([r['title'] for r in Retriever(second, WordEmbedder).search('Flug').results], ['Reise'])
        self.assertEqual(VECTOR_CACHE.loads, loads + 2)
        # Vectors replaced without a generation change (data from step 7.2) are still detected.
        with first.connect() as db:
            db.execute("DELETE FROM settings WHERE name='vectors.generation'")
        VECTOR_CACHE.clear()
        retriever.search('Heizung')
        with first.connect() as db:
            db.execute('DELETE FROM chunk_vectors WHERE chunk_id=(SELECT max(chunk_id) FROM chunk_vectors)')
        loads = VECTOR_CACHE.loads
        retriever.search('Heizung')
        self.assertEqual(VECTOR_CACHE.loads, loads + 1)

    def test_overwritten_vector_is_seen_although_count_and_ids_stay_equal(self):
        store = Store(self.folder / 'wissen.db')
        store.add('Heizung', 'Wartung der Therme')
        embed_all(store)
        retriever = Retriever(store, WordEmbedder)
        before = retriever.search('Rente', mode='vector').results[0]['vector_score']
        chunk_id = store.pending_vectors('other')[0]['chunk_id']
        store.save_vectors('m', {chunk_id: WordEmbedder('ollama', 'm').embed(['Rente'])[0]})
        after = retriever.search('Rente', mode='vector').results[0]['vector_score']
        self.assertLess(before, 0.5)
        self.assertGreater(after, 0.9)

    def test_wrong_dimension_falls_back_and_drops_the_cache(self):
        store = Store(self.folder / 'wissen.db')
        store.add('Heizung', 'Wartung der Therme')
        embed_all(store)
        retriever = Retriever(store, WordEmbedder)
        retriever.search('Heizung')
        self.assertIsNotNone(VECTOR_CACHE.entry)
        with patch.object(WordEmbedder, 'dimension', len(WORDS) + 2):
            retriever.questions.clear()
            result = retriever.search('Heizung')
        self.assertEqual(result.mode, 'fulltext')
        self.assertIn('Vektorbestand nicht lesbar', result.status)
        self.assertIsNone(VECTOR_CACHE.entry)
        self.assertEqual(retriever.fallbacks, 1)
        with self.assertRaisesRegex(ValueError, 'Suchmodus'):
            retriever.search('x', mode='bm25')

    def test_question_cache_is_bounded(self):
        store = Store(self.folder / 'wissen.db')
        store.add('Heizung', 'Wartung der Therme')
        embed_all(store)
        retriever = Retriever(store, WordEmbedder)
        for number in range(retrieval.QUESTION_CACHE_SIZE + 20):
            retriever.search(f'Heizung {number}')
        self.assertEqual(len(retriever.questions), retrieval.QUESTION_CACHE_SIZE)

    def test_matrix_for_50000_vectors_stays_below_200_mib(self):
        import numpy
        rows, dimension = 50_000, 1024
        rng = numpy.random.default_rng(7)
        blob = rng.standard_normal(dimension).astype('<f2').tobytes()
        fake = [{'chunk_id': i, 'document_id': i // 5, 'dimension': dimension, 'vector': blob}
                for i in range(1, rows + 1)]

        class Db:
            def execute(self, *_):
                return type('R', (), {'fetchall': lambda _: fake})()
        matrix = core._load_vector_matrix(Db(), 'key', 'm')
        self.assertEqual(matrix.matrix.shape, (rows, dimension))
        self.assertTrue(matrix.matrix.flags['C_CONTIGUOUS'])
        self.assertLess(matrix.nbytes, 200 * 1024 * 1024)
        self.assertLess(core.SCORE_BLOCK * dimension * 4, 50 * 1024 * 1024)  # float32 working block

    def test_ask_uses_the_same_retrieval(self):
        store = Store(self.folder / 'wissen.db')
        store.add('Wartungsplan', 'Die Therme bekommt jährlich eine Wartung.')
        embed_all(store)
        seen = []

        class Ai(core.AI):
            def generate(self, question, sources):
                seen.extend(sources)
                return 'Antwort [Q1]'
        with patch.object(retrieval, 'Embedder', WordEmbedder):
            Ai(store, 'sk-synthetic-key-1234', 'gpt-4.1-mini').ask('Wann wird die Heizung geprüft?')
        self.assertEqual([s['title'] for s in seen], ['Wartungsplan'])

    def test_cli_search_reports_mode_and_hides_scores_unless_asked(self):
        import cli
        db = self.folder / 'wissen.db'
        store = Store(db)
        store.add('Wartungsplan', 'Die Therme bekommt jährlich eine Wartung.')
        embed_all(store)

        def run(*argv):
            out, err = io.StringIO(), io.StringIO()
            with patch.object(cli, 'BASE', self.folder), patch.object(retrieval, 'Embedder', WordEmbedder), \
                    patch.dict(os.environ, {}, clear=True), redirect_stdout(out), redirect_stderr(err):
                self.assertEqual(cli.main(['--db', str(db), 'search', *argv]), 0)
            return json.loads(out.getvalue()), err.getvalue()
        rows, status = run('Heizung')
        self.assertEqual(status.strip(), STATUS_HYBRID)
        self.assertEqual((rows[0]['title'], rows[0]['retrieval_method']), ('Wartungsplan', 'vector'))
        self.assertNotIn('rrf_score', rows[0])
        report, _ = run('Wartung', '--diagnose')
        self.assertEqual(report['modus'], 'hybrid')
        self.assertIn('embedding', report['laufzeit_ms'])
        self.assertEqual(report['treffer'][0]['fulltext_rank'], 1)
        rows, status = run('Wartung', '--mode', 'fulltext')
        self.assertEqual(status.strip(), STATUS_FULLTEXT)


if __name__ == '__main__':
    unittest.main()
