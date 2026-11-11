"""Parity roadmap phase 6: text recognition of uploads in the worker.

Images and scans are recognized by the worker (Tesseract in the shared image, decision 11.11.2026). Without
OCR, or when it finds no text, the item goes to quarantine ``no_text`` and keeps its bytes for 30 days so
that its owner or an administrator can run it again (D05, decision 11.11.2026). Recognition tests need
Tesseract with German and English and are skipped without it, unless WISSENSRAUM_REQUIRE_OCR=1.
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import accounts
import audit
import import_fixtures as fx
import worker
from core import Store
from import_jobs import ImportJobs
import test_rights
from test_ocr import REQUIRED, ready
from test_rights import TENANT, fill, people

NO_OCR = {'WISSENSRAUM_OCR': '0'}


def check_kept_upload_contract(case, store):
    """Keeping, retrying and dropping the bytes of uploads without text, alike on both backends."""
    ids = fill(store)
    admin, editor, _ = people(store, ids)
    jobs = ImportJobs(store)
    with patch.dict(os.environ, NO_OCR):
        job = jobs.create_uploads(editor, [('scan.png', fx.blank_png()), ('notiz.txt', b'Ein lesbarer Text.')])
        jobs.run(job)
    kept = {i['locator']: (i['status'], i['reason'], bool(i['kept'])) for i in jobs.items(job)}
    case.assertEqual(kept, {'scan.png': ('quarantined', 'no_text', True), 'notiz.txt': ('imported', '', False)})
    case.assertEqual(jobs.retry_candidates(), {})  # server uploads have no path for the desktop retry
    case.assertEqual(jobs.retry_uploads(job), 1)
    case.assertEqual(jobs.job(job)['status'], 'pending')
    with patch.dict(os.environ, NO_OCR):
        jobs.run(job)
    case.assertEqual(jobs.drop_kept_uploads(datetime.now(timezone.utc) + timedelta(days=29)), 0)
    case.assertEqual(jobs.drop_kept_uploads(datetime.now(timezone.utc) + timedelta(days=31)), 1)
    case.assertEqual([bool(i['kept']) for i in jobs.items(job)], [False, False])
    case.assertEqual(jobs.retry_uploads(job), 0)


class ContractTests(unittest.TestCase):
    def test_contract(self):
        with tempfile.TemporaryDirectory() as folder:
            check_kept_upload_contract(self, Store(Path(folder) / 'wissen.db'))


class WorkerTests(test_rights.EndpointTests):
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

    def test_retry_by_owner_or_administrator_while_kept(self):
        editor, admin = self.clients['oid-editor'], self.clients['oid-admin']
        with patch.dict(os.environ, NO_OCR):
            job = self.upload(editor, [('scan.png', fx.blank_png(), 'image/png')])
            self.work()
        page = editor.get(f'/importe/{job}').text
        self.assertIn('1 Dateien ohne erkannten Text erneut verarbeiten', page)
        accounts.create_account(self.store, TENANT, 'oid-editor-2', 'editor', 'Zweite')
        self.assertEqual(self.sign_in('oid-editor-2').post(f'/importe/{job}/erneut').status_code, 404)
        self.assertEqual(self.clients['oid-reader'].post(f'/importe/{job}/erneut').status_code, 403)
        self.assertEqual(editor.post(f'/importe/{job}/erneut').status_code, 303)
        self.assertEqual(editor.post(f'/importe/{job}/erneut').status_code, 409)  # already pending
        retried = [e for e in audit.entries(self.store, 100) if e['event'] == 'import_started']
        self.assertIn('"retry":1', retried[0]['details'])
        with patch.dict(os.environ, NO_OCR):
            self.work()
        self.assertEqual(admin.post(f'/importe/{job}/erneut').status_code, 303)  # administrators too
        with patch.dict(os.environ, NO_OCR):
            self.work()
        self.jobs.drop_kept_uploads(datetime.now(timezone.utc) + timedelta(days=31))
        self.assertNotIn('erneut verarbeiten', editor.get(f'/importe/{job}').text)
        self.assertEqual(editor.post(f'/importe/{job}/erneut').status_code, 409)

    def test_heartbeat_lives_during_a_long_item(self):
        def slow(*args, **kwargs):
            time.sleep(0.5)
            return {'total': 1, 'status': 'completed'}
        with patch.dict(os.environ, NO_OCR):
            self.upload(self.clients['oid-editor'], [('a.txt', b'Text A.', 'text/plain')])
        with patch.object(ImportJobs, 'run', slow):
            worker.import_once(self.store, threading.Event(), self.heartbeat, beat=0.1)
        self.assertTrue(self.heartbeat.exists())
        self.assertLess(time.time() - self.heartbeat.stat().st_mtime, 0.6)

    @unittest.skipUnless(ready() or REQUIRED, 'Tesseract mit Deutsch und Englisch nicht installiert')
    def test_worker_recognizes_images_and_scans(self):
        editor = self.clients['oid-editor']
        page = fx.text_page(fx.OCR_GERMAN)
        with patch.dict(os.environ, NO_OCR):  # uploaded while OCR was missing
            job = self.upload(editor, [('rechnung.png', fx.image_bytes(page, 'PNG'), 'image/png')])
            self.work()
        self.assertEqual(self.jobs.items(job)[0]['reason'], 'no_text')
        editor.post(f'/importe/{job}/erneut')
        self.work()
        (item,) = self.jobs.items(job)
        self.assertEqual((item['status'], bool(item['kept'])), ('imported', False))
        scan = self.upload(editor, [('scan.pdf', fx.scanned_pdf([fx.text_page(fx.OCR_ENGLISH)]), 'application/pdf')])
        self.work()
        (scanned,) = self.jobs.items(scan)
        self.assertEqual(scanned['status'], 'imported')
        hits = editor.get('/api/search', params={'q': 'Rechnung'}).json()['results']
        self.assertIn(item['document_id'], [h['document_id'] for h in hits])
        hits = editor.get('/api/search', params={'q': 'quarterly'}).json()['results']
        self.assertIn(scanned['document_id'], [h['document_id'] for h in hits])
        self.assertEqual(self.clients['oid-reader'].get(f'/dokument/{scanned["document_id"]}').status_code, 404)


if __name__ == '__main__':
    unittest.main()
