"""Collections, favorites and saved searches (Release 2.2, step 8A.3)."""
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

from core import Store


def check_organization_contract(case, store):
    """Same behaviour on SQLite and PostgreSQL; ``store`` must be empty."""
    heating, _ = store.add('Wartungsplan', 'Die Therme bekommt jährlich eine Wartung.', category='Haus')
    car, _ = store.add('Fahrzeug', 'Kasko für den Wagen.', category='Auto')
    trip, _ = store.add('Urlaub', 'Flug und Hotel gebucht.', category='Reise')
    before = {doc_id: store.get(doc_id) for doc_id in (heating, car, trip)}

    house = store.create_collection('  Haus   und Hof ')
    case.assertEqual(store.collections(), [{'id': house, 'name': 'Haus und Hof', 'documents': 0}])
    for bad in ('', ' ', 'x' * 101):
        with case.assertRaises(ValueError):
            store.create_collection(bad)
    with case.assertRaises(ValueError):
        store.create_collection('haus UND hof')  # names are unique regardless of case
    case.assertEqual(store.add_to_collection(house, [heating, car, heating]), 2)
    case.assertEqual(store.add_to_collection(house, [heating]), 0)  # already contained
    with case.assertRaises(ValueError):
        store.add_to_collection(house, [10**9])
    with case.assertRaises(ValueError):
        store.add_to_collection(10**9, [heating])
    case.assertEqual([row['id'] for row in store.list(collection=house)], [car, heating])
    case.assertEqual(store.document_collections(heating), [{'id': house, 'name': 'Haus und Hof'}])

    travel = store.create_collection('Reisen')
    store.add_to_collection(travel, [trip, car])
    store.rename_collection(travel, 'Reisen 2026')
    with case.assertRaises(ValueError):
        store.rename_collection(travel, 'HAUS und Hof')
    case.assertEqual([(row['name'], row['documents']) for row in store.collections()],
                     [('Haus und Hof', 2), ('Reisen 2026', 2)])
    case.assertEqual(store.remove_from_collection(house, [car, trip]), 1)
    case.assertEqual([row['id'] for row in store.list(collection=house)], [heating])

    store.set_favorite(trip)
    store.set_favorite(trip)  # idempotent
    store.set_favorite(heating)
    case.assertTrue(store.is_favorite(trip))
    case.assertEqual([row['id'] for row in store.list(favorites=True)], [trip, heating])
    case.assertEqual([row['id'] for row in store.list(favorites=True, collection=travel)], [trip])
    store.set_favorite(heating, False)
    case.assertFalse(store.is_favorite(heating))
    with case.assertRaises(ValueError):
        store.set_favorite(10**9)

    first = store.save_search('Wartung', 'Wann ist die  Wartung?', mode='fulltext', tag='Haus')
    case.assertEqual(store.saved_searches(), [{'id': first, 'name': 'Wartung', 'query': 'Wann ist die Wartung?',
                                               'mode': 'fulltext', 'tag': 'Haus'}])
    case.assertEqual(store.save_search('wartung', 'Therme', tag=''), first)  # same name: replaced
    case.assertEqual(store.saved_searches(), [{'id': first, 'name': 'wartung', 'query': 'Therme',
                                               'mode': None, 'tag': None}])
    for bad in [dict(name='', query='x'), dict(name='a', query=' '), dict(name='a', query='x' * 501),
                dict(name='a', query='x', mode='semantisch')]:
        with case.assertRaises(ValueError):
            store.save_search(**bad)
    store.delete_saved_search(first)
    case.assertEqual(store.saved_searches(), [])
    with case.assertRaises(ValueError):
        store.delete_saved_search(first)

    # Nothing of this touches a document; deletions cascade only in one direction.
    for doc_id, document in before.items():
        case.assertEqual(store.get(doc_id), document)
    store.delete(trip)
    case.assertEqual([row['documents'] for row in store.collections()], [1, 1])
    case.assertEqual(store.list(favorites=True), [])
    store.delete_collection(travel)
    case.assertEqual([row['name'] for row in store.collections()], ['Haus und Hof'])
    case.assertEqual(store.get(car)['title'], 'Fahrzeug')
    with case.assertRaises(ValueError):
        store.delete_collection(travel)


def check_export_contract(case, store, folder):
    """Collection export with source manifest (8A.5); same on both backends, ``store`` must be empty."""
    import hashlib
    import zipfile
    plan, _ = store.add('Wartungsplan', 'Die Therme bekommt jährlich eine Wartung.', category='Haus',
                        filename='C:\\Ordner\\plan?.pdf', original=b'%PDF-1.4 original\x00bytes')
    note, _ = store.add('Notiz: Kosten', 'Wartung kostet 120 Euro.')
    outside, _ = store.add('Fremd', 'Gehört nicht zur Sammlung.')
    store.update_metadata(plan, add_tags='Heizung', status='reviewed', owner='Hausverwaltung', review_date='2026-12-01')
    store.relate(plan, note, 'references', 'Kosten')
    store.relate(plan, outside, 'related')
    house = store.create_collection('Haus')
    store.add_to_collection(house, [note, plan])
    target = Path(folder) / 'haus.zip'
    report = store.export_collection(house, target)
    with zipfile.ZipFile(target) as archive:
        case.assertEqual(sorted(archive.namelist()),
                         ['dokumente/001-plan_.pdf', 'dokumente/002-Notiz_ Kosten.txt', 'manifest.json'])
        manifest = json.loads(archive.read('manifest.json'))
        case.assertEqual(manifest, report)
        for entry in manifest['documents']:  # every listed file is in the archive with its hash
            data = archive.read(entry['file'])
            case.assertEqual((len(data), hashlib.sha256(data).hexdigest()), (entry['bytes'], entry['sha256']))
        case.assertEqual(archive.read('dokumente/001-plan_.pdf'), b'%PDF-1.4 original\x00bytes')
        case.assertEqual(archive.read('dokumente/002-Notiz_ Kosten.txt').decode(), 'Wartung kostet 120 Euro.')
    case.assertEqual((manifest['format'], manifest['format_version'], manifest['collection']),
                     ('wissensraum-sammlung', 1, {'name': 'Haus', 'documents': 2}))
    first = manifest['documents'][0]
    case.assertEqual({key: first[key] for key in ('id', 'title', 'category', 'tags', 'status', 'owner', 'review_date',
                                                  'version', 'source_type', 'filename', 'original',
                                                  'content_sha256')},
                     {'id': plan, 'title': 'Wartungsplan', 'category': 'Haus', 'tags': ['Heizung'],
                      'status': 'reviewed', 'owner': 'Hausverwaltung', 'review_date': '2026-12-01', 'version': 1,
                      'source_type': 'file', 'filename': 'plan?.pdf', 'original': True, 'content_sha256': store.get(plan)['sha256']})
    case.assertRegex(first['created_at'], r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$')
    case.assertEqual((manifest['documents'][1]['original'], manifest['documents'][1]['source_type']), (False, 'note'))
    case.assertEqual(manifest['relations'], [{'source_id': plan, 'target_id': note, 'kind': 'references',
                                              'note': 'Kosten'}])  # the relation to the outside stays out
    with case.assertRaises(ValueError):
        store.export_collection(house, target)  # never overwrites without being told
    store.export_collection(house, target, overwrite=True)
    with case.assertRaises(ValueError):
        store.export_collection(10**9, Path(folder) / 'fehlt.zip')
    case.assertFalse((Path(folder) / 'fehlt.zip').exists())
    case.assertEqual([path.name for path in Path(folder).iterdir() if path.name.startswith('.sammlung-')], [])
    empty = store.create_collection('Leer')
    case.assertEqual(store.export_collection(empty, Path(folder) / 'leer.zip')['documents'], [])


class OrganizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.store = Store(self.folder / 'wissen.db')

    def tearDown(self):
        self.temp.cleanup()

    def test_contract(self):
        check_organization_contract(self, self.store)

    def test_export_contract(self):
        export = self.folder / 'export'; export.mkdir()
        check_export_contract(self, self.store, export)

    def test_cli(self):
        import cli
        doc_id, _ = self.store.add('Notiz', 'Inhalt der Notiz')

        def run(*argv):
            out = io.StringIO()
            with patch.object(cli, 'BASE', self.folder), patch.dict(os.environ, {}, clear=True), \
                    redirect_stdout(out), redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(['--db', str(self.folder / 'wissen.db'), *argv]), 0)
            return json.loads(out.getvalue())
        created = run('collection-create', 'Projekt')
        run('collection-add', str(created['id']), str(doc_id))
        self.assertEqual(run('collections'), [{'id': created['id'], 'name': 'Projekt', 'documents': 1}])
        self.assertEqual([row['id'] for row in run('list', '--collection', str(created['id']))], [doc_id])
        run('favorite', str(doc_id))
        self.assertEqual([row['id'] for row in run('list', '--favorites')], [doc_id])
        run('favorite', str(doc_id), '--remove')
        self.assertEqual(run('list', '--favorites'), [])
        saved = run('search-save', 'Notizen', 'Inhalt', '--mode', 'fulltext')
        self.assertEqual(run('searches'), [{'id': saved['id'], 'name': 'Notizen', 'query': 'Inhalt',
                                            'mode': 'fulltext', 'tag': None}])
        run('search-delete', str(saved['id']))
        self.assertEqual(run('searches'), [])
        exported = run('collection-export', str(created['id']), str(self.folder / 'projekt.zip'))
        self.assertEqual((exported['datei'], exported['dokumente']), (str(self.folder / 'projekt.zip'), 1))
        self.assertTrue((self.folder / 'projekt.zip').is_file())
        run('collection-remove', str(created['id']), str(doc_id))
        run('collection-delete', str(created['id']))
        self.assertEqual(run('collections'), [])


if __name__ == '__main__':
    unittest.main()
