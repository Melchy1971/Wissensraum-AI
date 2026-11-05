"""Metadata editing with a complete history (Release 2.2, steps 8A.1 and 8A.2)."""
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

import rules
from core import Store

MODEL = 'test-embedding'


def entries(store, doc_id):
    return [(row['field'], row['old_value'], row['new_value'], row['origin']) for row in store.metadata_history(doc_id)]


def check_metadata_contract(case, store):
    """Same behaviour on SQLite and PostgreSQL; ``store`` must be empty."""
    doc_id, _ = store.add('Wartungsplan', 'Die Therme bekommt jährlich eine Wartung.', category='Haus',
                          filename='plan.md', original=b'\x00original')
    before = store.get(doc_id)
    changes = store.update_metadata(doc_id, title='Heizungsplan 2026', category='Technik',
                                    add_tags='Heizung, Wartung')
    case.assertEqual([c['field'] for c in changes], ['title', 'category', 'tag', 'tag'])
    after = store.get(doc_id)
    case.assertEqual((after['title'], after['category']), ('Heizungsplan 2026', 'Technik'))
    for field in ('content', 'original', 'sha256', 'filename'):  # content, original and hash stay unchanged
        case.assertEqual(bytes(after[field]) if field == 'original' else after[field],
                         bytes(before[field]) if field == 'original' else before[field])
    case.assertEqual({row['document_id'] for row in store.search('Heizungsplan')}, {doc_id})  # title searchable
    case.assertEqual(entries(store, doc_id), [
        ('title', 'Wartungsplan', 'Heizungsplan 2026', 'manual'), ('category', 'Haus', 'Technik', 'manual'),
        ('tag', '', 'Heizung', 'manual'), ('tag', '', 'Wartung', 'manual')])
    case.assertEqual(store.update_metadata(doc_id, title='Heizungsplan  2026', category='Technik',
                                           add_tags='heizung', remove_tags='Unbekannt'), [])  # nothing changed
    case.assertEqual(len(store.metadata_history(doc_id)), 4)
    case.assertEqual(store.update_metadata(doc_id, remove_tags='wartung'),
                     [{'field': 'tag', 'old_value': 'Wartung', 'new_value': ''}])
    for bad in [dict(title=' '), dict(title='x' * 301), dict(category=''), dict(add_tags='x' * 81)]:
        with case.assertRaises(ValueError):
            store.update_metadata(doc_id, **bad)
    with case.assertRaises(ValueError):
        store.update_metadata(10**9, title='Fremd')
    case.assertEqual(len(store.metadata_history(doc_id)), 5)  # failed edits left no trace
    case.assertRegex(store.metadata_history(doc_id)[0]['created_at'], r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$')

    # rule runs are part of the same history, including their revert
    with store.connect() as db:
        result = rules.apply(db, store.backend, [{'document_id': doc_id, 'category': 'Technik',
                                                   'new_category': 'Archiv', 'new_tags': ['Altbestand']}])
    case.assertEqual(entries(store, doc_id)[-2:], [('category', 'Technik', 'Archiv', 'rule'),
                                                  ('tag', '', 'Altbestand', 'rule')])
    case.assertEqual({row['run_id'] for row in store.metadata_history(doc_id)[-2:]}, {result['run_id']})
    with store.connect() as db:
        rules.revert(db, store.backend, result['run_id'])
    case.assertEqual(entries(store, doc_id)[-2:], [('category', 'Archiv', 'Technik', 'rule_revert'),
                                                  ('tag', 'Altbestand', '', 'rule_revert')])
    store.delete(doc_id)
    case.assertEqual(store.metadata_history(doc_id), [])


def check_status_contract(case, store):
    """Status, owner, review date and the exclusion from AI answers (8A.2); ``store`` must be empty."""
    current, _ = store.add('Heizung aktuell', 'Wartung der Therme jährlich im März')
    old, _ = store.add('Heizung alt', 'Wartung der Therme alle zwei Jahre')
    document = store.get(current)
    case.assertEqual((document['status'], document['owner'], document['review_date']), ('new', '', None))

    changes = store.update_metadata(old, status='outdated', owner='  Hausverwaltung ', review_date='2026-03-01')
    case.assertEqual(changes, [{'field': 'status', 'old_value': 'new', 'new_value': 'outdated'},
                               {'field': 'owner', 'old_value': '', 'new_value': 'Hausverwaltung'},
                               {'field': 'review_date', 'old_value': '', 'new_value': '2026-03-01'}])
    case.assertEqual(store.update_metadata(old, status='outdated', owner='Hausverwaltung',
                                           review_date='2026-03-01'), [])  # unchanged: nothing logged
    for bad in [dict(status='geprüft'), dict(owner='x' * 101), dict(review_date='01.03.2026'),
                dict(review_date='2026-02-30'), dict(review_date='2026-3-1')]:
        with case.assertRaises(ValueError):
            store.update_metadata(old, **bad)
    case.assertEqual(len(store.metadata_history(old)), 3)
    store.update_metadata(current, review_date='2026-12-01')
    case.assertEqual([(row['id'], row['review_date']) for row in store.due_reviews('2026-11-05')],
                     [(old, '2026-03-01')])
    case.assertEqual([row['id'] for row in store.due_reviews('2026-12-31')], [old, current])
    with case.assertRaises(ValueError):
        store.due_reviews('morgen')

    # Normal search keeps every document; AI answers leave outdated and archived ones out.
    case.assertEqual({row['document_id'] for row in store.search('Wartung Therme')}, {current, old})
    case.assertEqual({row['document_id'] for row in store.search('Wartung Therme', active_only=True)}, {current})
    store.update_metadata(old, status='archived')
    case.assertEqual({row['document_id'] for row in store.search('Wartung Therme', active_only=True)}, {current})
    pending = {row['content']: row['chunk_id'] for row in store.pending_vectors(MODEL)}
    store.save_vectors(MODEL, {pending['Wartung der Therme jährlich im März']: [1.0, 0.0],
                               pending['Wartung der Therme alle zwei Jahre']: [0.9, 0.1]})
    case.assertEqual({hit['document_id'] for hit in store.vector_search(MODEL, [1.0, 0.0])}, {current, old})
    case.assertEqual({hit['document_id'] for hit in store.vector_search(MODEL, [1.0, 0.0], active_only=True)},
                     {current})
    store.update_metadata(old, status='reviewed', review_date='')
    case.assertIsNone(store.get(old)['review_date'])
    case.assertEqual({row['document_id'] for row in store.search('Wartung Therme', active_only=True)}, {current, old})
    case.assertEqual(entries(store, old)[-2:], [('status', 'archived', 'reviewed', 'manual'),
                                                ('review_date', '2026-03-01', '', 'manual')])

    case.assertTrue(store.exclude_retired())  # default: exclude
    store.set_exclude_retired(False)
    case.assertFalse(store.exclude_retired())
    store.set_exclude_retired(True)
    case.assertTrue(store.exclude_retired())


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.store = Store(self.folder / 'wissen.db')

    def tearDown(self):
        self.temp.cleanup()

    def test_contract(self):
        check_metadata_contract(self, self.store)

    def test_status_contract(self):
        check_status_contract(self, self.store)

    def test_ai_answers_follow_the_exclusion_setting(self):
        from core import AI
        seen = []

        class Recorder:
            def __init__(self, store, *args):
                pass

            def search(self, question, limit=8, active_only=False, **kwargs):
                seen.append(active_only)
                raise RuntimeError('stop')
        ai = AI.__new__(AI)
        ai.store = self.store
        with patch('retrieval.Retriever', Recorder):
            for enabled in (True, False):
                self.store.set_exclude_retired(enabled)
                self.assertEqual(ai.ask('Wann ist die Wartung?').status, 'unavailable')  # stopped in the fake
        self.assertEqual(seen, [True, False])

    def test_cli_edit_and_history(self):
        import cli
        doc_id, _ = self.store.add('Notiz', 'Inhalt der Notiz')

        def run(*argv):
            out = io.StringIO()
            with patch.object(cli, 'BASE', self.folder), patch.dict(os.environ, {}, clear=True), \
                    redirect_stdout(out), redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(['--db', str(self.folder / 'wissen.db'), *argv]), 0)
            return json.loads(out.getvalue())
        result = run('edit', str(doc_id), '--title', 'Neue Notiz', '--add-tags', 'Privat')
        self.assertEqual([c['field'] for c in result['aenderungen']], ['title', 'tag'])
        self.assertEqual([(row['field'], row['origin']) for row in run('history', str(doc_id))],
                         [('title', 'manual'), ('tag', 'manual')])
        result = run('edit', str(doc_id), '--status', 'reviewed', '--owner', 'Team', '--review-date', '2026-01-15')
        self.assertEqual([c['field'] for c in result['aenderungen']], ['status', 'owner', 'review_date'])
        self.assertEqual([row['id'] for row in run('review-due', '--until', '2026-02-01')], [doc_id])
        self.assertEqual(run('ai-retired'), {'ki_antworten_ohne_veraltete_und_archivierte': True})
        self.assertEqual(run('ai-retired', 'include'), {'ki_antworten_ohne_veraltete_und_archivierte': False})


if __name__ == '__main__':
    unittest.main()
