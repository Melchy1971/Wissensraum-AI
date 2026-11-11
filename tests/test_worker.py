"""Parity roadmap phase 2: Ollama address for embeddings, worker, semantic search switch and status.

Ollama is replaced by fakes; nothing leaves the machine.
"""
import io
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import audit
import core
import server
import worker
from core import Embedder, Store
import test_rights
from test_rights import COMMON, SECRET
from test_vector_sync import FakeEmbedder
from vector_sync import VectorSync


class OllamaAddressTests(unittest.TestCase):
    def tearDown(self):
        core.use_ollama_at(None)

    def test_embeddings_follow_wr_ollama_url(self):
        sent = []

        def transport(request, timeout):
            sent.append(request.full_url)
            return io.BytesIO(json.dumps({'embeddings': [[0.1, 0.2]]}).encode())
        Embedder('ollama', transport=transport).embed(['x'])
        server.configure_ollama({'WR_OLLAMA_URL': 'http://ollama:11434/'})
        Embedder('ollama', transport=transport).embed(['x'])
        server.configure_ollama({})  # without the variable the desktop default comes back
        Embedder('ollama', transport=transport).embed(['x'])
        self.assertEqual(sent, ['http://127.0.0.1:11434/api/embed', 'http://ollama:11434/api/embed',
                                'http://127.0.0.1:11434/api/embed'])
        for bad in ('ollama:11434', 'http://ollama:11434/api', 'file:///etc/passwd'):
            with self.subTest(bad), self.assertRaisesRegex(ValueError, 'WR_OLLAMA_URL'):
                core.use_ollama_at(bad)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        FakeEmbedder.calls, FakeEmbedder.offline = [], False
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store, self.heartbeat, self.events = Store(folder / 'wissen.db'), folder / 'alive', []
        for i in range(3):
            self.store.add(f'Dokument {i}', f'Inhalt Nummer {i} für die Einbettung.')
        self.enterContext(mock.patch('vector_sync.Embedder', FakeEmbedder))

    def log(self, event, severity='INFO', **metadata):
        self.events.append((event, severity, metadata))

    def test_rounds_embed_when_switched_on_and_beat(self):
        worker.loop(self.store, threading.Event(), self.log, interval=0, heartbeat=self.heartbeat, rounds=1)
        self.assertEqual((self.events, FakeEmbedder.calls), ([], []))  # off: nothing sent, still alive
        self.assertTrue(self.heartbeat.exists())
        VectorSync(self.store).activate()
        worker.loop(self.store, threading.Event(), self.log, interval=0, heartbeat=self.heartbeat, rounds=2)
        self.assertEqual(VectorSync(self.store).progress(), (3, 3))
        self.assertEqual(self.events, [('vectors_embedded', 'INFO', {'count': 3, 'outcome': 'ok'})])  # round 2: nothing due

    def test_a_failed_round_does_not_end_the_worker(self):
        VectorSync(self.store).activate()
        FakeEmbedder.offline = True
        with mock.patch('worker.run_once', side_effect=[RuntimeError('Datenbank weg'), None]):
            worker.loop(self.store, threading.Event(), self.log, interval=0, heartbeat=self.heartbeat, rounds=2)
        self.assertEqual(self.events, [('worker_round_failed', 'ERROR', {'outcome': 'RuntimeError'})])
        worker.loop(self.store, threading.Event(), self.log, interval=0, heartbeat=self.heartbeat, rounds=1)
        self.assertEqual(self.events[-1], ('vectors_embedded', 'WARNING', {'count': 0, 'outcome': 'failed'}))

    def test_stop_ends_the_loop(self):
        stop = threading.Event()
        stop.set()
        worker.loop(self.store, stop, self.log, interval=60, heartbeat=self.heartbeat)
        self.assertFalse(self.heartbeat.exists())


class SemanticSearchPageTests(test_rights.EndpointTests):
    """Switch for administrators with audit, status of search and answers on the pages."""
    locals().update({name: None for name in dir(test_rights.EndpointTests) if name.startswith('test_')})

    def setUp(self):
        super().setUp()
        FakeEmbedder.calls, FakeEmbedder.offline = [], False
        self.enterContext(mock.patch('vector_sync.Embedder', FakeEmbedder))

    def test_only_administrators_switch_and_every_switch_is_audited(self):
        reader, admin = self.clients['oid-reader'], self.clients['oid-admin']
        self.assertEqual(reader.post('/verwaltung/semantische-suche', data={'enabled': '1'}).status_code, 403)
        FakeEmbedder.offline = True
        refused = admin.post('/verwaltung/semantische-suche', data={'enabled': '1'})
        self.assertEqual(refused.status_code, 422)
        self.assertIn('nicht erreichbar', refused.text)
        self.assertFalse(VectorSync(self.store).config()['enabled'])
        FakeEmbedder.offline = False
        self.assertEqual(admin.post('/verwaltung/semantische-suche', data={'enabled': '1'}).status_code, 303)
        self.assertTrue(VectorSync(self.store).config()['enabled'])
        self.assertIn('eingeschaltet, Modell qwen3-embedding:0.6b', admin.get('/verwaltung').text)
        self.assertEqual(admin.post('/verwaltung/semantische-suche', data={'enabled': '0'}).status_code, 303)
        changes = [json.loads(e['details']) for e in audit.entries(self.store) if e['event'] == 'settings_changed']
        self.assertEqual([c['enabled'] for c in reversed(changes)], [True, False])
        self.assertTrue(audit.verify(self.store)['ok'])

    def test_switched_on_the_server_searches_hybrid_and_keeps_the_rights_filter(self):
        class Probe(FakeEmbedder):  # the question lies exactly on the vector of the secret section
            def embed(self, texts, query=False):
                return [[1.0, 0.0, 0.0] for _ in texts]
        VectorSync(self.store, Probe).enable('m')  # the vectors of the stock belong to model "m"
        with mock.patch('retrieval.Embedder', Probe):
            reader = self.clients['oid-reader'].get('/api/search', params={'q': 'Kesselhaus Halle'}).json()
            admin = self.clients['oid-admin'].get('/api/search', params={'q': 'Kesselhaus Halle'}).json()
        self.assertEqual((reader['mode'], admin['mode']), ('hybrid', 'hybrid'))
        self.assertEqual(admin['results'][0]['document_id'], self.ids['secret'])  # first by meaning
        self.assertNotIn(self.ids['secret'], [r['document_id'] for r in reader['results']])
        self.assertNotIn(SECRET, json.dumps(reader))

    def test_search_and_answers_show_their_status(self):
        reader = self.clients['oid-reader']
        self.assertIn('Volltextsuche', reader.get('/suche', params={'q': COMMON}).text)
        self.assertEqual(reader.get('/api/search', params={'q': COMMON}).json()['status'], 'Volltextsuche')
        answered = reader.post('/fragen', data={'question': f'Was gilt für {COMMON}?'}).text
        self.assertIn('Quellenlage', answered)
        self.assertIn('Quellen:', answered)
        refused = reader.post('/fragen', data={'question': f'Was ist {SECRET}?'}).text
        self.assertNotIn('Quellen:', refused)  # no answer: hits, if any, are only search hints
        self.assertIn('keine ausreichend belastbare Quelle', refused)


if __name__ == '__main__':
    unittest.main()
