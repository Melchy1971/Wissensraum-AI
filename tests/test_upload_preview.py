"""Parity roadmap, A03: preview of uploaded files with deselection before the import.

The worker reads the files without OCR like the desktop preview and shows title, excerpt and outcome; the
account chooses, only the chosen items are imported, the others lose their bytes. A matching document of
someone else is reported as existing, never with its title (decision 10.11.2026).
"""
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import accounts
import import_fixtures as fx
import worker
from import_jobs import ImportJobs
import test_rights
from test_rights import TENANT


def check_preview_contract(case, store):
    """Preview, selection and refused resume alike on both backends."""
    ids = test_rights.fill(store)
    _, editor, _ = test_rights.people(store, ids)
    jobs = ImportJobs(store)
    job = jobs.create_uploads(editor, [('a.txt', b'Erster Text fuer die Vorschau.'), ('b.txt', b'Zweiter Text.')],
                              preview=True)
    case.assertEqual(jobs.next_preview_job(), job)
    case.assertIsNone(jobs.next_upload_job())  # nothing is imported before the choice
    rows = jobs.build_preview(job)
    case.assertEqual(sorted(row['outcome'] for row in rows.values()), ['new', 'new'])
    case.assertEqual(jobs.upload_preview(job), rows)
    case.assertFalse(jobs.set_status(job, 'pending'))
    first = min(int(i) for i in rows)
    case.assertEqual(jobs.start_selection(job, [first]), 1)
    with case.assertRaises(ValueError):
        jobs.start_selection(job, [first])
    case.assertEqual(jobs.next_upload_job(), job)
    case.assertEqual(jobs.run(job)['imported'], 1)
    case.assertEqual(jobs.summary(job)['deselected'], 1)


class ContractTests(unittest.TestCase):
    def test_contract(self):
        from core import Store
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'WISSENSRAUM_OCR': '0'}):
            check_preview_contract(self, Store(Path(folder) / 'wissen.db'))


class UploadPreviewTests(test_rights.EndpointTests):
    locals().update({name: None for name in dir(test_rights.EndpointTests) if name.startswith('test_')})

    def setUp(self):
        super().setUp()
        self.heartbeat = Path(self.enterContext(tempfile.TemporaryDirectory())) / 'alive'
        self.jobs = ImportJobs(self.store)
        self.enterContext(patch.dict(os.environ, {'WISSENSRAUM_OCR': '0'}))

    def upload(self, client, files, preview=True):
        response = client.post('/hochladen', files=[('file', f) for f in files],
                               data={'category': 'Technik', **({'preview': '1'} if preview else {})})
        self.assertEqual(response.status_code, 202, response.text)
        return int(response.headers['HX-Redirect'].rsplit('/', 1)[1])

    def work(self):
        return worker.import_once(self.store, threading.Event(), self.heartbeat)

    def test_choose_before_import(self):
        editor = self.clients['oid-editor']
        secret_text = self.store.get(self.ids['secret'])['content']
        job = self.upload(editor, [('plan.txt', b'Neuer Wartungsplan Halle Ost.', 'text/plain'),
                                   ('kopie.txt', secret_text.encode(), 'text/plain'),
                                   ('scan.png', fx.blank_png(), 'image/png')])
        self.assertEqual(self.jobs.job(job)['status'], 'preview')
        self.assertIn('liest die Dateien', editor.get(f'/importe/{job}').text)
        self.assertIsNone(self.work())  # only the preview, nothing imported
        self.assertEqual(self.jobs.job(job)['status'], 'review')
        self.assertEqual(self.jobs.summary(job)['imported'], 0)
        page = editor.get(f'/importe/{job}').text
        self.assertIn('Neuer Wartungsplan Halle Ost', page)
        self.assertIn('bereits vorhanden', page)
        self.assertNotIn('bereits vorhanden: Geheim', page)  # no title of a document the account cannot see
        self.assertEqual(editor.post(f'/importe/{job}/fortsetzen').status_code, 409)  # no way around the choice
        accounts.create_account(self.store, TENANT, 'oid-editor-2', 'editor', 'Zweite')
        self.assertEqual(self.sign_in('oid-editor-2').post(f'/importe/{job}/auswahl', data={'item': ['1']}).status_code, 404)
        plan = next(i['id'] for i in self.jobs.items(job) if i['locator'] == 'plan.txt')
        self.assertEqual(editor.post(f'/importe/{job}/auswahl', data={'item': [str(plan)]}).status_code, 303)
        self.assertEqual(editor.post(f'/importe/{job}/auswahl', data={'item': [str(plan)]}).status_code, 409)
        summary = self.work()
        self.assertEqual((summary['imported'], summary['deselected']), (1, 2))
        self.assertEqual([bool(i['kept']) for i in self.jobs.items(job)], [False, False, False])
        self.assertEqual(self.jobs.upload_preview(job), {})

    def test_new_version_names_the_own_document_and_cancel_works(self):
        editor = self.clients['oid-editor']
        first = self.upload(editor, [('plan.txt', b'Plan eins.', 'text/plain')], preview=False)
        self.work()
        own = self.jobs.items(first)[0]['document_id']
        job = self.upload(editor, [('plan.txt', b'Plan zwei.', 'text/plain')])
        self.work()
        page = editor.get(f'/importe/{job}').text
        self.assertIn('neue Version', page)
        self.assertIn(f'(Fassung 2): {self.store.get(own)["title"]}', page)
        self.assertEqual(editor.post(f'/importe/{job}/abbrechen').status_code, 303)
        self.assertEqual(self.jobs.job(job)['status'], 'cancelled')
        self.assertEqual(self.jobs.upload_preview(job), {})
        self.assertIsNone(self.work())


if __name__ == '__main__':
    unittest.main()
