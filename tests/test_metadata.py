"""Metadata editing with a complete history (Release 2.2, step 8A.1)."""
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


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.store = Store(self.folder / 'wissen.db')

    def tearDown(self):
        self.temp.cleanup()

    def test_contract(self):
        check_metadata_contract(self, self.store)

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


if __name__ == '__main__':
    unittest.main()
