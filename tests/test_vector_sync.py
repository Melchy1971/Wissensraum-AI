"""Background embedding while the application is open (Phase 7, step 7.2b)."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import vector_sync
from core import Store
from vector_sync import RETRY_SECONDS, VectorSync


class FakeEmbedder:
    """Stands in for Ollama; records every call and can be switched off."""
    calls = []
    offline = False

    def __init__(self, provider, model):
        self.provider, self.model = provider, model

    def embed(self, texts, query=False):
        FakeEmbedder.calls.append((self.provider, self.model, list(texts)))
        if FakeEmbedder.offline:
            raise ValueError('Embedding-Dienst nicht erreichbar.')
        return [[float(len(text)), 1.0, 0.5] for text in texts]


class VectorSyncTests(unittest.TestCase):
    def setUp(self):
        FakeEmbedder.calls, FakeEmbedder.offline = [], False
        self.store = Store(Path(self.enterContext(tempfile.TemporaryDirectory())) / 'wissen.db')
        self.now = [1_000_000.0]
        self.sync = VectorSync(self.store, FakeEmbedder, lambda: self.now[0])
        for i in range(5):
            self.store.add(f'Dokument {i}', f'Inhalt Nummer {i}')

    def test_nothing_is_sent_without_activation(self):
        self.assertFalse(self.sync.due())
        with self.assertRaisesRegex(ValueError, 'nicht aktiviert'):
            self.sync.run()
        self.assertEqual(FakeEmbedder.calls, [])
        self.assertEqual(self.sync.progress(), (0, 5))

    def test_activation_probes_ollama_and_stays_off_when_it_fails(self):
        FakeEmbedder.offline = True
        with self.assertRaisesRegex(ValueError, 'nicht erreichbar'):
            self.sync.activate()
        self.assertFalse(self.sync.config()['enabled'])
        FakeEmbedder.offline = False
        state = self.sync.activate()
        self.assertTrue(state['enabled'])
        self.assertEqual(FakeEmbedder.calls, [('ollama', 'qwen3-embedding:0.6b', ['Verbindungstest'])] * 2)

    def test_switching_off_during_a_run_stays_off(self):
        """Parity roadmap phase 2: worker and web server write apart; a run never switches the search back on."""
        self.sync.activate()
        admin = VectorSync(self.store, FakeEmbedder)

        class SwitchedOffMeanwhile(FakeEmbedder):
            def embed(inner, texts, query=False):
                admin.deactivate()  # the administrator, while the worker embeds
                return super().embed(texts, query)
        worker = VectorSync(self.store, SwitchedOffMeanwhile, lambda: self.now[0])
        state = worker.run()
        self.assertEqual(state['embedded'], 5)
        current = VectorSync(self.store).config()
        self.assertFalse(current['enabled'])
        self.assertEqual(current['last_result'], state['last_result'])  # the run result is still shown
        self.assertFalse(worker.due())
        admin.enable('qwen3-embedding:0.6b')
        self.assertEqual(VectorSync(self.store).config()['last_result'], 'Aktiviert.')  # old result dropped

    def test_runs_are_bounded_and_new_imports_become_due(self):
        self.sync.activate()
        state = self.sync.run(limit=3)
        self.assertEqual((state['embedded'], self.sync.progress()), (3, (3, 5)))
        self.assertTrue(self.sync.due())
        self.assertEqual(self.sync.run()['embedded'], 2)
        self.assertIn('5 von 5 fertig', self.sync.config()['last_result'])
        self.assertFalse(self.sync.due())
        self.store.add('Neu', 'Später importiert')
        self.assertTrue(self.sync.due())
        self.sync.run()
        self.assertEqual(self.sync.progress(), (6, 6))
        # Only chunk texts are sent, never titles or other fields.
        self.assertEqual(FakeEmbedder.calls[-1][2], ['Später importiert'])

    def test_failure_keeps_activation_and_retries_later(self):
        self.sync.activate()
        self.sync.run(limit=2)
        FakeEmbedder.offline = True
        state = self.sync.run()
        self.assertTrue(state['failed'] and state['enabled'])
        self.assertIn('neuer Versuch', state['last_result'])
        self.assertEqual(self.sync.progress(), (2, 5))  # earlier batches stay saved
        self.assertFalse(self.sync.due())
        self.now[0] += RETRY_SECONDS
        self.assertTrue(self.sync.due())
        FakeEmbedder.offline = False
        self.assertFalse(self.sync.run()['failed'])
        self.assertEqual(self.sync.progress(), (5, 5))

    def test_model_change_recomputes_and_deactivation_keeps_vectors(self):
        self.sync.activate()
        self.sync.run()
        state = self.sync.activate('nomic-embed-text')
        self.assertIn('neu berechnet', state['last_result'])
        self.assertEqual(self.sync.progress(), (0, 5))
        self.sync.run()
        self.assertEqual(self.sync.progress(), (5, 5))
        self.sync.deactivate()
        self.assertFalse(self.sync.due())
        self.assertEqual(self.sync.progress(), (5, 5))

    def test_stop_request_ends_a_run_between_batches(self):
        self.sync.activate()
        self.assertEqual(self.sync.run(should_stop=lambda: True)['embedded'], 0)


class VectorCliTests(unittest.TestCase):
    def test_activate_run_and_status(self):
        import cli
        FakeEmbedder.calls, FakeEmbedder.offline = [], False
        base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        db = base / 'wissen.db'
        Store(db).add('Heizung', 'Wartung im März')

        def run(*argv):
            out = io.StringIO()
            with patch.object(cli, 'BASE', base), patch.object(vector_sync, 'Embedder', FakeEmbedder), \
                    patch.dict(os.environ, {}, clear=True), redirect_stdout(out):
                code = cli.main(['--db', str(db), 'vectors', *argv])
            return code, json.loads(out.getvalue())
        self.assertEqual(run()[1]['aktiv'], False)
        self.assertEqual(run('--activate')[1]['aktiv'], True)
        code, result = run('--run')
        self.assertEqual((code, result['eingebettet'], result['abschnitte'], result['in_diesem_lauf']), (0, 1, 1, 1))


if __name__ == '__main__':
    unittest.main()
