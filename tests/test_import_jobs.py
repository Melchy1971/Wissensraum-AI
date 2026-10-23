"""Persistent import jobs: status, cancel, resume, crash recovery and report (Phase 6.1)."""
import csv
import hashlib
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import import_fixtures as fx
from core import Store
from import_jobs import ImportJobs


class ImportJobTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(self.base / 'wissen.db')
        self.jobs = ImportJobs(self.store)
        self.folder = self.base / 'quelle'
        files = {'a.txt': b'Erster Text', 'unter/b.md': b'# Zweiter Text',
                 'unter/c.pdf': fx.pdf_with_text('Dritter Text'), 'd.docx': fx.docx_with_text('Vierter Text'),
                 'e.exe': b'MZ', 'f.pdf': b'kein pdf', 'g.pdf': fx.blank_pdf(),
                 'kopie.txt': b'Erster Text'}
        for name, data in files.items():
            path = self.folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def count(self, table):
        with self.store.connect() as db:
            return db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]

    def statuses(self, job_id):
        return {item['locator']: (item['status'], item['reason']) for item in self.jobs.items(job_id)}

    def test_folder_job_classifies_every_file_and_records_source_and_checksum(self):
        job_id = self.jobs.create_for_folder(self.folder)
        summary = self.jobs.run(job_id)
        self.assertEqual(self.statuses(job_id), {
            'a.txt': ('imported', ''), 'd.docx': ('imported', ''), 'e.exe': ('quarantined', 'unsupported'),
            'f.pdf': ('quarantined', 'signature'), 'g.pdf': ('quarantined', 'no_text'),
            'kopie.txt': ('duplicate', ''), 'unter/b.md': ('imported', ''), 'unter/c.pdf': ('imported', '')})
        self.assertEqual((summary['status'], summary['imported'], summary['duplicate'], summary['quarantined'],
                          summary['resumable']), ('completed', 4, 1, 3, False))
        for item in self.jobs.items(job_id):
            if item['status'] in ('imported', 'duplicate'):
                data = Path(item['path']).read_bytes()
                self.assertEqual(item['sha256'], hashlib.sha256(data).hexdigest())
                self.assertEqual(item['size'], len(data))
            if item['status'] == 'quarantined':
                self.assertEqual(len(self.jobs.errors(item['id'])), 1)  # error history
        with self.store.connect() as db:
            sources = {row[0] for row in db.execute("SELECT locator FROM sources WHERE source_type='file'")}
        self.assertEqual(sources, {'a.txt', 'd.docx', 'unter/b.md', 'unter/c.pdf'})
        report = self.jobs.write_report(job_id, self.base / 'bericht.csv')
        with report.open(encoding='utf-8-sig', newline='') as file:
            rows = list(csv.reader(file, delimiter=';'))
        self.assertEqual(len(rows), 9)
        self.assertIn(['Quarantäne', 'Inhalt passt nicht zur Dateiendung'],
                      [row[3:5] for row in rows if row[2] == 'f.pdf'])

    def test_cancel_and_resume_without_duplicate_documents(self):
        job_id = self.jobs.create_for_folder(self.folder)
        cancel = threading.Event()
        def stop_after_two(done, total, locator):
            if done == 2: cancel.set()
        self.assertEqual(self.jobs.run(job_id, cancel, stop_after_two)['status'], 'cancelled')
        self.assertTrue(self.jobs.summary(job_id)['resumable'])
        self.assertEqual(self.jobs.run(job_id)['status'], 'completed')
        self.assertEqual(self.count('documents'), 4)
        self.assertEqual(self.count('sources'), 4)
        self.assertTrue(all(item['attempts'] == 1 for item in self.jobs.items(job_id)))

    def test_interruption_between_document_and_status_resumes_as_imported(self):
        job_id = self.jobs.create_for_folder(self.folder)
        real_add = self.store.add
        def add_then_crash(*args, **kwargs):
            result = real_add(*args, **kwargs)
            raise KeyboardInterrupt  # process ends after the document commit, before the item update
        with patch.object(self.store, 'add', side_effect=add_then_crash):
            with self.assertRaises(KeyboardInterrupt):
                self.jobs.run(job_id)
        self.assertEqual(self.statuses(job_id)['a.txt'], ('running', ''))
        self.assertEqual(self.count('documents'), 1)
        self.jobs.run(job_id)
        self.assertEqual(self.statuses(job_id)['a.txt'], ('imported', ''))
        self.assertEqual(self.statuses(job_id)['kopie.txt'], ('duplicate', ''))
        self.assertEqual(self.count('documents'), 4)

    def test_transient_failure_is_retried_but_quarantine_is_final(self):
        job_id = self.jobs.create_for_folder(self.folder)
        import core
        real = core.import_document
        def locked_once(path):
            if Path(path).name == 'd.docx':
                raise PermissionError(13, 'Datei ist gesperrt')
            return real(path)
        with patch('import_jobs.import_document', side_effect=locked_once):
            summary = self.jobs.run(job_id)
        self.assertEqual((summary['failed'], summary['resumable']), (1, True))
        self.jobs.run(job_id)
        items = {item['locator']: item for item in self.jobs.items(job_id)}
        self.assertEqual((items['d.docx']['status'], items['d.docx']['attempts']), ('imported', 2))
        self.assertEqual([error['reason'] for error in self.jobs.errors(items['d.docx']['id'])], ['unreadable'])
        self.assertEqual(items['e.exe']['attempts'], 1)  # quarantined items are not retried
        self.assertFalse(self.jobs.summary(job_id)['resumable'])

    def test_unreadable_folders_are_listed_as_quarantine(self):
        with patch('import_jobs.discover_documents',
                   return_value=([(self.folder / 'a.txt', 'a.txt')], 0, ['geheim: Zugriff verweigert'])):
            job_id = self.jobs.create_for_folder(self.folder)
        self.assertEqual(self.statuses(job_id)['geheim: Zugriff verweigert'], ('quarantined', 'unreadable'))
        self.assertEqual(self.jobs.run(job_id)['imported'], 1)

    def test_selected_files_job_uses_file_names(self):
        job_id = self.jobs.create_for_files([self.folder / 'a.txt', self.folder / 'unter' / 'b.md'])
        self.jobs.run(job_id)
        self.assertEqual(self.statuses(job_id), {'a.txt': ('imported', ''), 'b.md': ('imported', '')})

    def test_deleting_a_document_keeps_the_job_history(self):
        job_id = self.jobs.create_for_files([self.folder / 'a.txt'])
        self.jobs.run(job_id)
        item = self.jobs.items(job_id)[0]
        self.store.delete(item['document_id'])
        item = self.jobs.items(job_id)[0]
        self.assertEqual((item['status'], item['document_id']), ('imported', None))


if __name__ == '__main__':
    unittest.main()
