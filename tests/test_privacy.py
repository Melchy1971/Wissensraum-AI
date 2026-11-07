"""Data protection export and selective erasure (Release 2.4, step 9.6)."""
import io
import json
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import privacy
from core import Store
from test_graph_view import build

SECRET = 'Erika-Musterfrau-Zebra'


def fill(store):
    """A graph document mentioning the secret person, an unrelated note and AI runs citing each."""
    _, _, ids, doc = build(store)
    store.update_metadata(doc, title=f'Organigramm {SECRET}')
    other, _ = store.add('Einkaufsliste', 'Milch, Brot und Käse.', filename='liste.txt', original=b'Milch')
    store.log('question', 'Wer arbeitet wo?', f'{SECRET} arbeitet bei Nordwerk [Q1].',
              {'format': 2, 'sources': [{'document_id': doc, 'title': 'Organigramm', 'ordinal': 1}]}, 'gpt')
    store.log('question', 'Was kaufen?', 'Milch [Q1].', {'format': 2, 'sources': [{'document_id': other}]}, 'gpt')
    store.log('question', 'Alt', 'Alt', [{'document_id': doc}], 'gpt')  # list format before step 7.4
    with store.connect() as db:
        job = db.execute("INSERT INTO import_jobs(kind,status) VALUES('files','completed') RETURNING id").fetchone()['id']
        db.execute("INSERT INTO import_items(job_id,ordinal,path,locator,status,document_id) "
                   "VALUES(?,0,?,'0','done',?)", (job, f'C:/{SECRET}.txt', doc))
    return ids, doc, other


def check_privacy_contract(case, store):
    """Find, impact and erase; same on both backends, ``store`` freshly migrated."""
    ids, doc, other = fill(store)
    case.assertEqual([row['id'] for row in privacy.find(store, 'erika-MUSTERFRAU')], [doc])
    case.assertEqual([row['id'] for row in privacy.find(store, 'liste.txt')], [other])
    case.assertEqual(privacy.find(store, '100%_sicher'), [])  # wildcards are taken literally
    with case.assertRaises(ValueError):
        privacy.find(store, 'a')
    case.assertEqual(privacy.impact(store, [doc]), {'dokumente': 1, 'ki_verlauf': 2, 'erwaehnungen': 6,
                                                    'relationen': 4, 'importeintraege': 1})
    with case.assertRaises(ValueError):
        privacy.erase(store, [doc, 99999])
    case.assertIsNotNone(store.get(doc))  # nothing removed by the refused call
    counts = privacy.erase(store, [doc])
    for key, value in {'dokumente': 1, 'ki_verlauf': 2, 'erwaehnungen': 6, 'relationen': 4, 'importeintraege': 1,
                       'entitaeten': 6}.items():
        case.assertEqual(counts[key], value, key)
    case.assertGreater(counts['protokoll'], 0)
    with store.connect() as db:
        for table in ('entities', 'entity_mentions', 'graph_relations', 'import_items'):
            case.assertEqual(int(db.execute(f'SELECT count(*) AS n FROM {table}').fetchone()['n']), 0, table)
        case.assertEqual(int(db.execute('SELECT count(*) AS n FROM graph_changes').fetchone()['n']), 0)
        case.assertEqual([r['question'] for r in db.execute('SELECT question FROM ai_runs')], ['Was kaufen?'])
    case.assertEqual(privacy.find(store, SECRET), [])
    case.assertEqual(store.search('Nordwerk'), [])
    case.assertEqual(store.get(other)['title'], 'Einkaufsliste')
    case.assertEqual(len(store.search('Milch')), 1)


class PrivacyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.path = self.base / 'wissen.db'
        self.store = Store(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_contract(self):
        check_privacy_contract(self, self.store)

    def test_erased_text_is_not_left_in_the_sqlite_file_and_backups_are_reported(self):
        import backups
        _, doc, _ = fill(self.store)
        backups.create(self.store)
        wal = Path(str(self.path) + '-wal')
        stored = lambda: self.path.read_bytes() + (wal.read_bytes() if wal.exists() else b'')
        self.assertIn(SECRET.encode(), stored())
        self.assertIn(b'Nordwerk', stored())
        counts = privacy.erase(self.store, [doc])
        self.assertEqual(counts['sicherungen_mit_daten'], 1)
        self.assertNotIn(SECRET.encode(), stored())
        self.assertNotIn(b'Nordwerk', stored())  # also gone from the full text index

    def test_export_contains_every_table_and_the_originals(self):
        _, doc, other = fill(self.store)
        target = self.base / 'export.zip'
        result = privacy.export(self.store, target)
        self.assertEqual(result['tabellen']['documents'], 2)
        with zipfile.ZipFile(target) as archive:
            names = set(archive.namelist())
            manifest = json.loads(archive.read('manifest.json'))
            documents = {d['id']: d for d in json.loads(archive.read('daten/documents.json'))}
            self.assertEqual(archive.read(documents[other]['original']['datei']), b'Milch')
            runs = json.loads(archive.read('daten/ai_runs.json'))
        self.assertIn(SECRET, documents[doc]['title'])
        self.assertEqual(len(runs), 3)
        self.assertNotIn('daten/chunks.json', names)
        self.assertNotIn('daten/chunk_vectors.json', names)
        self.assertIn('daten/entities.json', names)
        self.assertEqual(manifest['format'], 'wissensraum-datenexport')
        with self.assertRaisesRegex(ValueError, 'gibt es schon'):
            privacy.export(self.store, target)
        self.assertEqual(sorted(p.name for p in self.base.iterdir() if p.name.startswith('.datenexport')), [])

    def test_cli(self):
        import cli
        _, doc, _ = fill(self.store)

        def run(*argv):
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli.main(['--db', str(self.path), *argv])
            return code, out.getvalue()
        code, out = run('privacy-find', 'Musterfrau')
        self.assertEqual((code, [d['id'] for d in json.loads(out)['dokumente']]), (0, [doc]))
        self.assertEqual(json.loads(out)['folgen']['ki_verlauf'], 2)
        self.assertEqual(run('privacy-erase', str(doc))[0], 2)  # no confirmation
        self.assertIsNotNone(self.store.get(doc))
        code, out = run('privacy-erase', str(doc), '--confirm')
        self.assertEqual((code, json.loads(out)['dokumente']), (0, 1))
        code, out = run('privacy-export', str(self.base / 'alles.zip'))
        self.assertEqual((code, json.loads(out)['tabellen']['documents']), (0, 1))


if __name__ == '__main__':
    unittest.main()
