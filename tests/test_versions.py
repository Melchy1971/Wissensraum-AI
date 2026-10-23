"""Document versions and import preview (Phase 6.2)."""
import csv
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import import_fixtures as fx
from core import Store
from import_jobs import ImportJobs, file_source


def table_counts(path):
    with closing(sqlite3.connect(path)) as db:
        tables = [row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'search_index%'")]
        return {table: db.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] for table in tables}


class StoreVersionTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(self.base / 'wissen.db')
        self.place = ('file', '', str(self.base / 'plan.txt'))

    def add(self, text, source=None, versioned=True):
        return self.store.add('Plan', text, 'Allgemein', 'plan.txt', text.encode(), source or self.place,
                              versioned=versioned)

    def test_changed_content_at_same_place_becomes_next_version_of_same_lineage(self):
        first, _ = self.add('Erste Fassung Budget')
        second, created = self.add('Zweite Fassung Budget')
        third, _ = self.add('Dritte Fassung Budget')
        self.assertTrue(created)
        versions = self.store.versions(first)
        self.assertEqual([(row['id'], row['version']) for row in versions], [(third, 3), (second, 2), (first, 1)])
        self.assertEqual(self.store.versions(third), versions)  # same lineage from any member

    def test_older_versions_stay_retrievable(self):
        first, _ = self.add('Erste Fassung Budget')
        self.add('Zweite Fassung Budget')
        self.assertEqual(self.store.get(first)['content'], 'Erste Fassung Budget')
        self.assertEqual(self.store.get(first)['original'], b'Erste Fassung Budget')

    def test_list_and_search_return_only_the_current_version_by_default(self):
        first, _ = self.add('Erste Fassung Budget Altwert')
        second, _ = self.add('Zweite Fassung Budget Neuwert')
        other, _ = self.store.add('Notiz', 'Budget ohne Fundort')
        self.assertEqual([row['id'] for row in self.store.list()], [other, second])
        self.assertEqual([row['version'] for row in self.store.list()], [1, 2])
        self.assertEqual({row['id'] for row in self.store.list(all_versions=True)}, {first, second, other})
        self.assertEqual({row['document_id'] for row in self.store.search('Budget')}, {second, other})
        self.assertEqual(self.store.search('Altwert'), [])
        self.assertEqual([row['document_id'] for row in self.store.search('Altwert', all_versions=True)], [first])

    def test_unchanged_content_is_a_duplicate_not_a_new_version(self):
        first, _ = self.add('Gleicher Inhalt')
        again, created = self.add('Gleicher Inhalt')
        self.assertEqual((again, created), (first, False))
        self.assertEqual(len(self.store.versions(first)), 1)

    def test_without_versioning_or_place_documents_stay_independent(self):
        first, _ = self.add('Erste Fassung', versioned=False)
        second, _ = self.add('Zweite Fassung', versioned=False)
        note_a, _ = self.store.add('Notiz', 'Notiz eins')
        note_b, _ = self.store.add('Notiz', 'Notiz zwei', versioned=True)  # notes have no place
        for doc_id in (first, second, note_a, note_b):
            self.assertEqual([row['version'] for row in self.store.versions(doc_id)], [1])
        self.assertEqual(len(self.store.list()), 4)

    def test_other_place_starts_its_own_lineage(self):
        first, _ = self.add('Inhalt A')
        other, _ = self.add('Inhalt B', ('file', '', str(self.base / 'anderer' / 'plan.txt')))
        self.assertEqual(len(self.store.versions(first)), 1)
        self.assertEqual(len(self.store.versions(other)), 1)

    def test_deleting_the_current_version_makes_the_previous_one_current(self):
        first, _ = self.add('Erste Fassung Budget')
        second, _ = self.add('Zweite Fassung Budget')
        self.store.delete(second)
        self.assertEqual([row['id'] for row in self.store.list()], [first])
        self.assertEqual([row['document_id'] for row in self.store.search('Budget')], [first])
        third, _ = self.add('Dritte Fassung Budget')
        self.assertEqual([row['version'] for row in self.store.versions(third)], [2, 1])

    def test_match_predicts_add_without_writing(self):
        first, _ = self.add('Erste Fassung')
        before = table_counts(self.store.path)
        self.assertEqual(self.store.match('  Erste Fassung \n', self.place, versioned=True),
                         {'outcome': 'duplicate', 'document_id': first, 'title': 'Plan'})
        self.assertEqual(self.store.match('Neu', self.place, versioned=True),
                         {'outcome': 'version', 'document_id': first, 'title': 'Plan', 'version': 2})
        self.assertEqual(self.store.match('Neu', self.place), {'outcome': 'new'})
        self.assertEqual(self.store.match('Neu'), {'outcome': 'new'})
        self.assertEqual(table_counts(self.store.path), before)


class ImportVersionTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(self.base / 'wissen.db')
        self.jobs = ImportJobs(self.store)
        self.folder = self.base / 'quelle'
        (self.folder / 'unter').mkdir(parents=True)
        self.write('a.txt', b'Plan Fassung eins')
        self.write('unter/b.docx', fx.docx_with_text('Bericht Fassung eins'))
        self.write('c.pdf', fx.pdf_with_text('Protokoll'))

    def write(self, name, data):
        (self.folder / name).write_bytes(data)

    def run_folder(self):
        job_id = self.jobs.create_for_folder(self.folder)
        return job_id, self.jobs.run(job_id)

    def statuses(self, job_id):
        return {item['locator']: item['status'] for item in self.jobs.items(job_id)}

    def test_reimport_after_change_creates_versions_and_reports_them(self):
        self.run_folder()
        self.write('a.txt', b'Plan Fassung zwei')
        self.write('unter/b.docx', fx.docx_with_text('Bericht Fassung zwei'))
        job_id, summary = self.run_folder()
        self.assertEqual(self.statuses(job_id), {'a.txt': 'versioned', 'c.pdf': 'duplicate',
                                                 'unter/b.docx': 'versioned'})
        self.assertEqual((summary['imported'], summary['versioned'], summary['duplicate']), (0, 2, 1))
        current = {row['title']: row['version'] for row in self.store.list()}
        self.assertEqual(current, {'a': 2, 'b': 2, 'c': 1})
        self.assertEqual(len(self.store.list(all_versions=True)), 5)
        report = self.jobs.write_report(job_id, self.base / 'bericht.csv')
        with report.open(encoding='utf-8-sig', newline='') as file:
            rows = list(csv.reader(file, delimiter=';'))
        self.assertEqual(sum(row[3] == 'neue Version' for row in rows), 2)

    def test_crash_after_storing_a_version_resumes_without_a_second_document(self):
        self.run_folder()
        self.write('a.txt', b'Plan Fassung zwei')
        job_id = self.jobs.create_for_folder(self.folder)
        real_add = self.store.add
        def add_then_crash(*args, **kwargs):
            real_add(*args, **kwargs)
            raise KeyboardInterrupt
        with patch.object(self.store, 'add', side_effect=add_then_crash):
            with self.assertRaises(KeyboardInterrupt):
                self.jobs.run(job_id)
        self.jobs.run(job_id)
        self.assertEqual(self.statuses(job_id)['a.txt'], 'versioned')
        self.assertEqual(len(self.store.list(all_versions=True)), 4)

    def test_preview_reports_metadata_and_outcome_and_writes_nothing(self):
        self.run_folder()
        self.write('a.txt', b'Plan Fassung zwei')
        self.write('d.txt', b'Plan Fassung eins')
        self.write('e.png', b'\x89PNG')
        self.write('f.pdf', fx.blank_pdf())
        before = table_counts(self.store.path)
        rows = {row['locator']: row for row in self.jobs.preview_folder(self.folder)}
        self.assertEqual(table_counts(self.store.path), before)
        a = rows['a.txt']
        self.assertEqual((a['type'], a['size'], a['title'], a['excerpt']), ('txt', 17, 'a', 'Plan Fassung zwei'))
        self.assertEqual((a['outcome'], a['version'], a['existing_title']), ('version', 2, 'a'))
        self.assertEqual(rows['d.txt']['outcome'], 'duplicate')
        self.assertEqual(rows['c.pdf']['outcome'], 'duplicate')
        self.assertEqual((rows['e.png']['outcome'], rows['e.png']['reason']), ('quarantined', 'no_text'))
        self.assertEqual((rows['f.pdf']['outcome'], rows['f.pdf']['reason']), ('quarantined', 'no_text'))
        self.assertEqual(rows['f.pdf']['type'], 'pdf')
        self.assertGreater(rows['f.pdf']['size'], 0)

    def test_preview_excerpt_is_limited_and_new_files_are_new(self):
        self.write('lang.txt', ('Wort ' * 500).encode())
        rows = {row['locator']: row for row in self.jobs.preview_files([self.folder / 'lang.txt'])}
        self.assertEqual(rows['lang.txt']['outcome'], 'new')
        self.assertEqual(len(rows['lang.txt']['excerpt']), 400)

    def test_preview_can_be_cancelled(self):
        import threading
        cancel = threading.Event(); cancel.set()
        self.assertIsNone(self.jobs.preview_folder(self.folder, cancel))

    def test_place_is_the_absolute_path(self):
        self.assertEqual(file_source(self.folder / 'unter' / '..' / 'a.txt'),
                         ('file', '', str((self.folder / 'a.txt').resolve())))


if __name__ == '__main__':
    unittest.main()
