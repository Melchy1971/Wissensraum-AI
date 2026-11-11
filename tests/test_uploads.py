"""Parity roadmap phase 3: uploads as import jobs of the worker (schema 24).

All files are synthetic and built in memory; the worker runs in the test process.
"""
import io
import os
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import accounts
import audit
import worker
from core import Store
from import_jobs import MAX_ARCHIVE_RATIO, UPLOAD, ImportJobs, archive_entries, upload_name
import test_rights
from test_rights import TENANT


def archive(entries, compression=zipfile.ZIP_DEFLATED):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression) as zipped:
        for name, data in entries:
            zipped.writestr(name, data)
    return buffer.getvalue()


def check_upload_contract(case, store):
    """Upload jobs on both backends: bytes kept until decided, order, cancel and resume, owner and audit."""
    account = accounts.create_account(store, TENANT, 'oid-upload', 'editor', 'Hochlader')
    jobs = ImportJobs(store)
    first = jobs.create_uploads(account, [('a.txt', 'Erster Upload-Text.'.encode()), ('b.txt', b'Zweiter Upload.')])
    second = jobs.create_uploads(account, [('c.txt', b'Dritter Upload.')], 'Technik')
    case.assertEqual((jobs.job(first)['account_id'], jobs.job(first)['kind']), (account, UPLOAD))
    case.assertEqual(jobs.next_upload_job(), first)
    with store.connect() as db:
        stored = [bytes(r['upload']) for r in db.execute('SELECT upload FROM import_items WHERE job_id=? ORDER BY ordinal',
                                                         (first,))]
    case.assertEqual(stored, ['Erster Upload-Text.'.encode(), b'Zweiter Upload.'])
    case.assertNotIn('upload', jobs.items(first)[0])  # lists never load the bytes
    case.assertTrue(jobs.set_status(first, 'cancelled'))
    case.assertEqual(jobs.next_upload_job(), second)
    case.assertTrue(jobs.set_status(first, 'pending'))
    summary = jobs.run(first)
    case.assertEqual((summary['status'], summary['imported']), ('completed', 2))
    case.assertFalse(jobs.set_status(first, 'cancelled'))  # finished stays finished
    ids = [item['document_id'] for item in jobs.items(first)]
    case.assertEqual({g['document_id'] for g in accounts.grants(store, account) if g['access'] == 'owner'}, set(ids))
    case.assertEqual([e['source'] for e in audit.entries(store, 2)], ['worker', 'worker'])
    with store.connect() as db:
        case.assertEqual(db.execute('SELECT count(*) AS n FROM import_items WHERE job_id=? AND upload IS NOT NULL',
                                    (first,)).fetchone()['n'], 0)
    case.assertEqual([s['job_id'] for s in jobs.upload_jobs(account)], [second, first])
    case.assertEqual(jobs.upload_jobs(account + 999), [])
    case.assertTrue(audit.verify(store)['ok'])


class ContractTests(unittest.TestCase):
    def test_contract(self):
        with tempfile.TemporaryDirectory() as folder:
            check_upload_contract(self, Store(Path(folder) / 'wissen.db'))


class ArchiveTests(unittest.TestCase):
    def test_names_and_entries(self):
        self.assertEqual(upload_name('../../etc/passwd'), 'passwd')
        self.assertEqual(upload_name('C:\\Users\\x\\bericht.pdf'), 'bericht.pdf')
        self.assertEqual(upload_name('\x00\x1f'), 'Datei')
        data = archive([('ordner/', b''), ('ordner/a.txt', b'Alpha'), ('__MACOSX/._a.txt', b'x'), ('.DS_Store', b'x'),
                        ('tief/innen/b.md', b'Beta')])
        entries, problems = archive_entries('paket.zip', data)
        self.assertEqual(entries, [('paket.zip/a.txt', b'Alpha'), ('paket.zip/b.md', b'Beta')])
        self.assertEqual(problems, [])

    def test_limits(self):
        self.assertEqual(archive_entries('kaputt.zip', b'kein zip')[1], [('kaputt.zip', 'damaged', 'ZIP-Archiv ist beschädigt.')])
        bomb = archive([('null.txt', b'0' * (MAX_ARCHIVE_RATIO * 1000 * 4))])
        self.assertEqual(archive_entries('bombe.zip', bomb), ([], [('bombe.zip', 'limits',
                                                                    'ZIP-Archiv überschreitet die Entpackgrenzen.')]))
        with mock.patch('import_jobs.MAX_ARCHIVE_ENTRIES', 1):
            self.assertEqual(archive_entries('viel.zip', archive([('a.txt', b'a'), ('b.txt', b'b')]))[1][0][1], 'limits')
        with mock.patch('import_jobs.MAX_DOCUMENT_BYTES', 3):
            self.assertEqual(archive_entries('gross.zip', archive([('a.txt', b'abcdef')], zipfile.ZIP_STORED))[1],
                             [('gross.zip/a.txt', 'too_large', 'Datei im Archiv ist zu groß.')])


class UploadPageTests(test_rights.EndpointTests):
    """Jobs through the pages: own and foreign jobs, ZIP, cancel and resume, locked account, report."""
    locals().update({name: None for name in dir(test_rights.EndpointTests) if name.startswith('test_')})

    def setUp(self):
        super().setUp()
        self.heartbeat = Path(self.enterContext(tempfile.TemporaryDirectory())) / 'alive'
        self.jobs = ImportJobs(self.store)

    def upload(self, client, files):
        response = client.post('/hochladen', files=[('file', f) for f in files], data={'category': 'Technik'})
        self.assertEqual(response.status_code, 202, response.text)
        return int(response.headers['HX-Redirect'].rsplit('/', 1)[1])

    def work(self):
        return worker.import_once(self.store, threading.Event(), self.heartbeat)

    def test_zip_and_several_files_in_one_job(self):
        editor = self.clients['oid-editor']
        zipped = archive([('a.txt', 'Text A im Archiv.'.encode()), ('b.exe', b'MZ'), ('innen.zip', archive([('x.txt', b'x')]))])
        job = self.upload(editor, [('paket.zip', zipped, 'application/zip'), ('c.md', b'# Notiz C', 'text/markdown')])
        self.work()
        result = {i['locator']: (i['status'], i['reason']) for i in self.jobs.items(job)}
        self.assertEqual(result, {'paket.zip/a.txt': ('imported', ''), 'paket.zip/b.exe': ('quarantined', 'unsupported'),
                                  'paket.zip/innen.zip': ('quarantined', 'unsupported'), 'c.md': ('imported', '')})
        self.assertTrue(self.heartbeat.exists())
        report = editor.get(f'/importe/{job}/bericht')
        self.assertEqual(report.headers['content-type'], 'text/csv; charset=utf-8')
        self.assertIn('paket.zip/b.exe', report.content.decode('utf-8-sig'))

    def test_foreign_jobs_stay_invisible(self):
        job = self.upload(self.clients['oid-editor'], [('a.txt', b'Nur meins.', 'text/plain')])
        accounts.create_account(self.store, TENANT, 'oid-editor-2', 'editor', 'Zweite')
        other, admin = self.sign_in('oid-editor-2'), self.clients['oid-admin']
        for path in (f'/importe/{job}', f'/importe/{job}/stand', f'/importe/{job}/bericht'):
            with self.subTest(path):
                self.assertEqual(other.get(path).status_code, 404)
                self.assertEqual(other.get(path).text, other.get('/importe/999999').text)
        self.assertEqual(other.post(f'/importe/{job}/abbrechen').status_code, 404)
        self.assertNotIn(f'Import {job}', other.get('/importe').text)
        self.assertIn(f'Import {job}', admin.get('/importe').text)  # administrators see every job

    def test_cancel_resume_and_worker_restart(self):
        editor = self.clients['oid-editor']
        job = self.upload(editor, [('a.txt', b'Erster Text zum Abbrechen.', 'text/plain'),
                                   ('b.txt', b'Zweiter Text zum Abbrechen.', 'text/plain')])
        self.assertEqual(editor.post(f'/importe/{job}/abbrechen').status_code, 303)
        self.assertIsNone(self.work())  # a cancelled job is not taken up
        self.assertIn('Fortsetzen', editor.get(f'/importe/{job}').text)
        self.assertEqual(editor.post(f'/importe/{job}/fortsetzen').status_code, 303)
        stop = threading.Event()
        stop.set()  # the worker is shut down before the first item
        self.assertEqual(worker.import_once(self.store, stop, self.heartbeat)['status'], 'running')
        self.assertEqual(self.jobs.next_upload_job(), job)  # after the restart it continues
        self.assertEqual(self.work()['imported'], 2)
        self.assertEqual(editor.post(f'/importe/{job}/abbrechen').status_code, 409)
        self.assertEqual(editor.post(f'/importe/{job}/loeschen').status_code, 404)

    def test_locked_account_stops_its_job(self):
        job = self.upload(self.clients['oid-editor'], [('a.txt', b'Text eines gesperrten Kontos.', 'text/plain')])
        account = accounts.account(self.store, self.editor)
        accounts.update_account(self.store, self.editor, account['version'], disabled=True)
        documents = len(self.store.list(all_versions=True))
        with self.assertRaises(PermissionError):
            self.work()
        self.assertEqual(self.jobs.job(job)['status'], 'cancelled')
        self.assertEqual(len(self.store.list(all_versions=True)), documents)
        self.assertIsNone(self.work())

    def test_item_with_failing_audit_leaves_nothing_and_stays_open(self):
        job = self.upload(self.clients['oid-editor'], [('a.txt', b'Text mit ausgefallenem Audit.', 'text/plain')])
        documents = len(self.store.list(all_versions=True))
        with mock.patch('audit.record', side_effect=RuntimeError('Audit ausgefallen')):
            self.work()
        (item,) = self.jobs.items(job)
        self.assertEqual((item['status'], item['reason'], item['document_id']), ('failed', 'error', None))
        self.assertEqual(len(self.store.list(all_versions=True)), documents)  # neither document nor grant
        self.assertEqual(self.clients['oid-editor'].post(f'/importe/{job}/fortsetzen').status_code, 303)
        self.assertEqual(self.work()['imported'], 1)  # the kept bytes are imported on resume

    @patch.dict(os.environ, {'WISSENSRAUM_OCR': '0'})  # without OCR; with it see test_ocr_server
    def test_image_waits_in_quarantine_for_ocr(self):
        job = self.upload(self.clients['oid-editor'], [('scan.png', b'\x89PNG\r\n\x1a\n' + b'0' * 64, 'image/png')])
        self.work()
        (item,) = self.jobs.items(job)
        self.assertEqual((item['status'], item['reason']), ('quarantined', 'no_text'))


if __name__ == '__main__':
    unittest.main()
