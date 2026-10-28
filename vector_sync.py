"""Background embedding of text chunks while the application is open (Release 2.1, step 7.2b).

Off until explicitly activated. Activation checks once that the local Ollama answers with the
chosen model; only then does the timer of the open application embed chunks without a vector,
at most ``RUN_LIMIT`` per run, so new imports and the existing stock are covered step by step.
A failed run keeps the activation and is retried after ``RETRY_SECONDS``. Only Ollama on this
computer is used: chunk texts never leave the machine. State lives in the ``settings`` table
as JSON (name ``vectors``), without credentials.
"""
from __future__ import annotations

import json
import time

from core import EMBEDDING_PROVIDERS, Embedder, _vector_model

NAME = 'vectors'
PROVIDER = 'ollama'
BATCH = 32
RUN_LIMIT = 256
RETRY_SECONDS = 300


class VectorSync:
    def __init__(self, store, embedder=None, clock=time.time):
        self.store, self.embedder, self.clock = store, embedder or Embedder, clock

    def config(self):
        with self.store.connect() as db:
            row = db.execute('SELECT value FROM settings WHERE name=?', (NAME,)).fetchone()
        state = {'enabled': False, 'model': EMBEDDING_PROVIDERS[PROVIDER]['model'], 'failed': False,
                 'last_run': None, 'last_result': ''}
        if row:
            try:
                state.update(json.loads(row['value']))
            except (TypeError, ValueError):
                pass
        return state

    def _save(self, state):
        with self.store.connect() as db:
            db.execute('DELETE FROM settings WHERE name=?', (NAME,))
            db.execute('INSERT INTO settings(name,value) VALUES(?,?)',
                       (NAME, json.dumps(state, ensure_ascii=False, sort_keys=True)))

    def progress(self):
        return self.store.vector_counts(self.config()['model'])

    def activate(self, model=None):
        """Enable with ``model`` after one probe request; raises when Ollama or the model is missing."""
        model = _vector_model(model or EMBEDDING_PROVIDERS[PROVIDER]['model'])
        self.embedder(PROVIDER, model).embed(['Verbindungstest'])
        state = self.config()
        changed = state['model'] != model
        state.update(enabled=True, model=model, failed=False, last_run=None,
                     last_result='Aktiviert.' + (' Vorhandene Vektoren eines anderen Modells werden neu berechnet.'
                                                 if changed else ''))
        self._save(state)
        return state

    def deactivate(self):
        state = self.config()
        state.update(enabled=False, last_result='Deaktiviert. Vorhandene Vektoren bleiben gespeichert.')
        self._save(state)
        return state

    def due(self):
        state = self.config()
        if not state['enabled']:
            return False
        if state['failed'] and state['last_run'] is not None and self.clock() - state['last_run'] < RETRY_SECONDS:
            return False
        return bool(self.store.pending_vectors(state['model'], limit=1))

    def run(self, limit=RUN_LIMIT, should_stop=lambda: False):
        """Embed up to ``limit`` chunks without a vector; returns the state with ``embedded``."""
        state = self.config()
        if not state['enabled']:
            raise ValueError('Semantische Suche ist nicht aktiviert.')
        embedded = 0
        state['last_run'] = self.clock()
        try:
            embedder = self.embedder(PROVIDER, state['model'])
            while embedded < limit and not should_stop():
                pending = self.store.pending_vectors(state['model'], limit=min(BATCH, limit - embedded))
                if not pending:
                    break
                vectors = embedder.embed([row['content'] for row in pending])
                self.store.save_vectors(state['model'], {row['chunk_id']: vector
                                                         for row, vector in zip(pending, vectors)})
                embedded += len(pending)
            done, total = self.store.vector_counts(state['model'])
            state.update(failed=False, last_result=f'{embedded} Abschnitte eingebettet, {done} von {total} fertig.')
        except ValueError as error:  # Ollama stopped, model removed, invalid answer: retry later
            state.update(failed=True, last_result=f'Fehlgeschlagen, neuer Versuch in {RETRY_SECONDS // 60} Minuten: '
                                                  f'{error}')
        self._save(state)
        state['embedded'] = embedded
        return state
