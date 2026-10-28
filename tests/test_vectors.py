"""Chunk vectors (schema version 10) and the embedding adapter (Phase 7, step 7.2a)."""
import io
import json
import math
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import QUERY_INSTRUCTION, Embedder, Store

MODEL = 'test-embedding'


def axis(index, dimension=4, tilt=0.0):
    values = [0.0] * dimension
    values[index] = 1.0
    values[(index + 1) % dimension] = tilt
    return values


def check_vector_contract(case, store):
    """Same behaviour on SQLite and PostgreSQL; ``store`` must be empty."""
    heating, _ = store.add('Heizung', 'Wartung der Therme im März')
    car, _ = store.add('Auto', 'Teilkasko mit Selbstbeteiligung')
    old, _ = store.add('Notiz alt', 'Erste Fassung', filename='notiz.md', versioned=True)
    new, _ = store.add('Notiz neu', 'Zweite Fassung', filename='notiz.md', versioned=True)
    pending = store.pending_vectors(MODEL)
    case.assertEqual([row['content'] for row in pending],
                     ['Wartung der Therme im März', 'Teilkasko mit Selbstbeteiligung', 'Erste Fassung', 'Zweite Fassung'])
    by_title = {row['content']: row['chunk_id'] for row in pending}
    case.assertEqual(store.save_vectors(MODEL, {by_title['Wartung der Therme im März']: axis(0),
                                                by_title['Teilkasko mit Selbstbeteiligung']: axis(1),
                                                by_title['Erste Fassung']: axis(2),
                                                by_title['Zweite Fassung']: axis(2, tilt=0.5)}), 4)
    case.assertEqual(store.pending_vectors(MODEL), [])
    case.assertEqual(store.vector_counts(MODEL), (4, 4))
    store.save_vectors(MODEL, {999_999: axis(0)})  # chunk deleted meanwhile: skipped, no error
    case.assertEqual(store.vector_counts(MODEL), (4, 4))

    hits = store.vector_search(MODEL, axis(0, tilt=0.2), limit=2)
    case.assertEqual([hit['title'] for hit in hits], ['Heizung', 'Auto'])
    case.assertAlmostEqual(hits[0]['score'], 1 / math.sqrt(1.04), places=3)
    case.assertEqual(set(hits[0]) >= {'chunk_id', 'document_id', 'title', 'category', 'ordinal', 'content'}, True)
    # Older versions stay out unless asked for, like the full-text search.
    case.assertEqual([hit['document_id'] for hit in store.vector_search(MODEL, axis(2), limit=5)
                      if hit['title'].startswith('Notiz')], [new])
    case.assertEqual({hit['document_id'] for hit in store.vector_search(MODEL, axis(2), limit=5, all_versions=True)
                      if hit['title'].startswith('Notiz')}, {old, new})

    # Another model sees nothing and reports every chunk as pending; saving replaces the row.
    case.assertEqual(store.vector_search('other-model', axis(0)), [])
    case.assertEqual(len(store.pending_vectors('other-model')), 4)
    store.save_vectors('other-model', {by_title['Wartung der Therme im März']: axis(3, dimension=8)})
    case.assertEqual(len(store.pending_vectors(MODEL)), 1)
    with case.assertRaisesRegex(ValueError, 'Dimension'):
        store.vector_search('other-model', axis(0))

    for bad in ([0.0, 0.0, 0.0, 0.0], [float('nan'), 1.0, 0.0, 0.0], [[1.0, 0.0]], []):
        with case.assertRaisesRegex(ValueError, 'Ungültiger Vektor'):
            store.save_vectors(MODEL, {by_title['Erste Fassung']: bad})
    with case.assertRaisesRegex(ValueError, 'Dimension'):
        store.save_vectors(MODEL, {by_title['Erste Fassung']: axis(0), by_title['Zweite Fassung']: axis(0, 8)})
    with case.assertRaisesRegex(ValueError, 'Modell-ID'):
        store.pending_vectors('kein modell; drop')

    store.delete(car)
    case.assertEqual([hit['title'] for hit in store.vector_search(MODEL, axis(1), limit=5)
                      if hit['title'] == 'Auto'], [])
    with store.connect() as db:
        case.assertEqual(db.execute('SELECT count(*) AS n FROM chunk_vectors').fetchone()['n'], 3)
    case.assertIsNotNone(heating)


class SqliteVectorTests(unittest.TestCase):
    def test_vector_contract(self):
        with tempfile.TemporaryDirectory() as folder:
            check_vector_contract(self, Store(Path(folder) / 'wissen.db'))


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class EmbedderTests(unittest.TestCase):
    def transport(self, reply):
        self.requests = []

        def send(request, timeout):
            body = json.loads(request.data)
            self.requests.append((request.full_url, dict(request.header_items()), body))
            return Response(json.dumps(reply(body)).encode())
        return send

    def test_ollama_batches_and_adds_query_instruction_for_qwen3(self):
        embedder = Embedder('ollama', transport=self.transport(
            lambda body: {'embeddings': [[float(len(text)), 1.0] for text in body['input']]}))
        vectors = embedder.embed([f'Text {i}' for i in range(33)])
        self.assertEqual(len(vectors), 33)
        self.assertEqual([len(body['input']) for _, _, body in self.requests], [32, 1])
        self.assertEqual(self.requests[0][0], 'http://127.0.0.1:11434/api/embed')
        self.assertEqual(self.requests[0][2]['model'], 'qwen3-embedding:0.6b')
        self.assertEqual(self.requests[0][2]['input'][0], 'Text 0')
        embedder.embed(['Wann ist Wartung?'], query=True)
        self.assertEqual(self.requests[-1][2]['input'], [QUERY_INSTRUCTION + 'Wann ist Wartung?'])
        other = Embedder('ollama', model='nomic-embed-text', transport=self.transport(
            lambda body: {'embeddings': [[1.0]] * len(body['input'])}))
        other.embed(['Frage'], query=True)
        self.assertEqual(self.requests[-1][2]['input'], ['Frage'])

    def test_openai_sends_key_and_restores_order(self):
        embedder = Embedder('openai', key='sk-test-synthetic', transport=self.transport(
            lambda body: {'data': [{'index': 1, 'embedding': [2.0]}, {'index': 0, 'embedding': [1.0]}]}))
        self.assertEqual(embedder.embed(['a', 'b']), [[1.0], [2.0]])
        url, headers, body = self.requests[0]
        self.assertEqual(url, 'https://api.openai.com/v1/embeddings')
        self.assertEqual(headers['Authorization'], 'Bearer sk-test-synthetic')
        self.assertEqual(body, {'model': 'text-embedding-3-small', 'input': ['a', 'b']})
        with self.assertRaisesRegex(ValueError, 'API-Schlüssel'):
            Embedder('openai', key='')

    def test_errors_are_reported_without_details_of_the_request(self):
        def missing(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 404, 'not found', {}, None)

        def offline(request, timeout):
            raise urllib.error.URLError('refused')
        with self.assertRaisesRegex(ValueError, 'Modell in Ollama laden'):
            Embedder('ollama', transport=missing).embed(['x'])
        with self.assertRaisesRegex(ValueError, 'nicht erreichbar'):
            Embedder('ollama', transport=offline).embed(['x'])
        with self.assertRaisesRegex(ValueError, 'nicht für jeden Text'):
            Embedder('ollama', transport=self.transport(lambda body: {'embeddings': [[1.0]]})).embed(['a', 'b'])
        with self.assertRaisesRegex(ValueError, 'Unbekannter'):
            Embedder('gemini')
        with self.assertRaisesRegex(ValueError, 'Modell-ID'):
            Embedder('ollama', model='bad model')


if __name__ == '__main__':
    unittest.main()
