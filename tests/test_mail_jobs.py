"""E-mail and PST imports as import jobs (Phase 6.3b).

Selected messages and released attachments become job items with status, reason,
checksum and error history; jobs can be cancelled and resumed without duplicates.
"""
import csv
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
from importer import Importer, MailAccounts
from mail_fakes import FakeGraph, FakeImapServer, FakePstArchive, graph_attachment, mail
from test_settings import FakeVault

PDF = fx.pdf_with_text('Rechnung Anhangstext')
DOCX = fx.docx_with_text('Bericht aus dem Archiv')
PNG = b'\x89PNG\r\n\x1a\nBILD'


class MailJobTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(self.base / 'wissen.db')
        self.accounts = MailAccounts(self.store, FakeVault(), self.base)
        self.importer = Importer(self.store, self.accounts)
        self.jobs = ImportJobs(self.store, self.importer)

    def count(self, table):
        with self.store.connect() as db:
            return db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]

    def statuses(self, job_id):
        return [(item['path'], item['status'], item['reason']) for item in self.jobs.items(job_id)]

    # --- IMAP ------------------------------------------------------------------------
    def imap(self):
        account_id = self.accounts.save({'label': 'Arbeit', 'provider': 'imap', 'email': 'm@example.test',
                                         'username': 'm', 'host': 'imap.example.test', 'port': 993}, 'geheim')
        server = FakeImapServer({
            11: mail('Rechnung', 'Rechnung anbei.', [('rechnung.pdf', 'application/pdf', PDF),
                                                     ('logo.png', 'image/png', PNG)]),
            12: mail('Termin', 'Termin am Montag.'),
            13: mail('Leer', None, [('nur.pdf', 'application/pdf', fx.pdf_with_text('Nur Anhang'))]),
        })
        real = self.importer._imap_login
        self.importer._imap_login = lambda account, folder, factory=None: real(account, folder, server.client)
        items = [{'id': '11', 'subject': 'Rechnung', 'attachments': [{'id': '2', 'name': 'rechnung.pdf'},
                                                                     {'id': '3', 'name': 'logo.png'}]},
                 {'id': '12', 'subject': 'Termin', 'attachments': []}]
        return account_id, server, items

    def test_imap_job_imports_selection_and_only_released_attachments(self):
        account_id, server, items = self.imap()
        job_id = self.importer.create_mail_job(self.jobs, account_id, 'INBOX', items, {'11': ['2', '3']})
        self.assertEqual([item['locator'] for item in self.jobs.items(job_id)], ['11', '11#2', '11#3', '12'])
        summary = self.jobs.run(job_id)
        self.assertEqual((summary['kind'], summary['status'], summary['imported'], summary['quarantined']),
                         ('imap', 'completed', 3, 1))
        self.assertEqual(self.statuses(job_id), [
            ('E-Mail: Rechnung', 'imported', ''), ('Anhang: rechnung.pdf (E-Mail: Rechnung)', 'imported', ''),
            ('Anhang: logo.png (E-Mail: Rechnung)', 'quarantined', 'no_text'), ('E-Mail: Termin', 'imported', '')])
        self.assertFalse(any('BODY.PEEK[]' in spec for _, spec in server.fetches))
        self.assertEqual(self.count('email_attachments'), 1)
        items_by_locator = {item['locator']: item for item in self.jobs.items(job_id)}
        self.assertEqual(items_by_locator['11#2']['size'], len(PDF))
        import hashlib
        self.assertEqual(items_by_locator['11#2']['sha256'], hashlib.sha256(PDF).hexdigest())
        self.assertEqual(len(self.jobs.errors(items_by_locator['11#3']['id'])), 1)
        report = self.jobs.write_report(job_id, self.base / 'bericht.csv')
        with report.open(encoding='utf-8-sig', newline='') as file:
            rows = list(csv.reader(file, delimiter=';'))
        self.assertIn(['Anhang: logo.png (E-Mail: Rechnung)', 'Quarantäne', 'Kein lesbarer Text'],
                      [row[2:5] for row in rows])
        preview = {item['id']: item for item in self.importer.preview_imap(account_id, client_factory=server.client)}
        self.assertTrue(preview['11']['imported'])
        self.assertEqual([part['imported'] for part in preview['11']['attachments']], [True, False])

    def test_imap_job_cancel_and_crash_resume_without_duplicates(self):
        account_id, server, items = self.imap()
        job_id = self.importer.create_mail_job(self.jobs, account_id, 'INBOX', items, {'11': ['2']})
        cancel = threading.Event()
        def stop_after_first(done, total, label):
            if done == 1: cancel.set()
        self.assertEqual(self.jobs.run(job_id, cancel, stop_after_first)['status'], 'cancelled')
        self.assertTrue(self.jobs.summary(job_id)['resumable'])
        real_add = self.store.add
        def crash_after_storing(*args, **kwargs):
            real_add(*args, **kwargs)
            raise KeyboardInterrupt  # stored, but the job item still says "running"
        with patch.object(self.store, 'add', side_effect=crash_after_storing):
            with self.assertRaises(KeyboardInterrupt):
                self.jobs.run(job_id)
        summary = self.jobs.run(job_id)
        self.assertEqual((summary['status'], summary['imported'], summary['duplicate']), ('completed', 3, 0))
        self.assertEqual(self.count('documents'), 3)
        self.assertEqual(self.count('email_imports'), 3)  # two messages and one attachment
        self.assertEqual(self.count('email_attachments'), 1)

    def test_crash_after_storing_a_message_resumes_as_imported(self):
        account_id, server, items = self.imap()
        job_id = self.importer.create_mail_job(self.jobs, account_id, 'INBOX', items)
        real_add = self.store.add
        def crash_after_storing(*args, **kwargs):
            real_add(*args, **kwargs)
            raise KeyboardInterrupt
        with patch.object(self.store, 'add', side_effect=crash_after_storing):
            with self.assertRaises(KeyboardInterrupt):
                self.jobs.run(job_id)
        self.assertEqual([item['status'] for item in self.jobs.items(job_id)], ['running', 'pending'])
        summary = self.jobs.run(job_id)
        self.assertEqual((summary['imported'], summary['duplicate']), (2, 0))
        self.assertEqual(self.count('documents'), 2)
        self.assertEqual(self.count('email_imports'), 2)  # recorded on resume

    def test_message_without_text_or_attachments_is_quarantined(self):
        account_id, server, items = self.imap()
        server.raw['14'] = (b'From: a@example.test\r\nSubject: Ohne alles\r\nContent-Type: text/plain\r\n\r\n   \r\n')
        job_id = self.importer.create_mail_job(self.jobs, account_id, 'INBOX', [{'id': '14', 'subject': 'Ohne alles'}])
        self.jobs.run(job_id)
        self.assertEqual(self.statuses(job_id), [('E-Mail: Ohne alles', 'quarantined', 'no_text')])

    def test_message_with_attachments_only_is_imported_with_names(self):
        account_id, server, _ = self.imap()
        job_id = self.importer.create_mail_job(self.jobs, account_id, 'INBOX', [{'id': '13', 'subject': 'Leer'}])
        self.jobs.run(job_id)
        self.assertEqual(self.statuses(job_id), [('E-Mail: Leer', 'imported', '')])
        content = self.store.get(self.store.list()[0]['id'])['content']
        self.assertIn('(Kein Nachrichtentext)', content)
        self.assertIn('Anhänge: nur.pdf', content)

    def test_failed_login_leaves_the_job_resumable(self):
        account_id, server, items = self.imap()
        job_id = self.importer.create_mail_job(self.jobs, account_id, 'INBOX', items)
        with patch.object(self.importer, '_imap_login', side_effect=ValueError('IMAP-Server ist nicht erreichbar.')):
            with self.assertRaisesRegex(ValueError, 'nicht erreichbar'):
                self.jobs.run(job_id)
        summary = self.jobs.summary(job_id)
        self.assertEqual((summary['status'], summary['resumable'], summary['pending']), ('cancelled', True, 2))
        self.assertEqual(self.jobs.run(job_id)['imported'], 2)

    def test_invalid_selection_is_rejected_before_a_job_exists(self):
        account_id, _, _ = self.imap()
        with self.assertRaises(ValueError):
            self.importer.create_mail_job(self.jobs, account_id, 'INBOX', [{'id': '11; DELETE'}])
        with self.assertRaises(ValueError):
            self.importer.create_mail_job(self.jobs, account_id, 'INBOX', [], {'11': ['2]']})
        self.assertEqual(self.jobs.jobs(), [])

    # --- Microsoft Graph -------------------------------------------------------------
    def test_graph_job_imports_message_and_released_attachments(self):
        account_id = self.accounts.save({'label': 'M365', 'provider': 'microsoft', 'email': 'm@example.test',
                                         'username': 'm@example.test',
                                         'client_id': '11111111-2222-3333-4444-555555555555'})
        self.accounts.microsoft_token = lambda _: 'token'
        graph = FakeGraph({'G=1': ({'subject': 'Angebot', 'from': {'emailAddress': {'address': 'a@example.test'}},
                                    'toRecipients': [], 'receivedDateTime': '2026-10-23T10:00:00Z',
                                    'body': {'contentType': 'html', 'content': '<p>Angebot anbei</p>'}},
                                   [graph_attachment('A/1', 'angebot.pdf', 'application/pdf', PDF),
                                    graph_attachment('A/2', 'Mail', 'message/rfc822', b'',
                                                     '#microsoft.graph.itemAttachment'),
                                    graph_attachment('A/3', 'nicht.pdf', 'application/pdf', PDF)])})
        real = self.importer._graph_get
        self.importer._graph_get = graph
        job_id = self.importer.create_mail_job(
            self.jobs, account_id, 'INBOX', [{'id': 'G=1', 'subject': 'Angebot',
                                              'attachments': [{'id': 'A/1', 'name': 'angebot.pdf'}]}],
            {'G=1': ['A/1', 'A/2']})
        summary = self.jobs.run(job_id)
        self.importer._graph_get = real
        self.assertEqual((summary['kind'], summary['imported'], summary['quarantined']), ('graph', 2, 1))
        self.assertEqual([item['reason'] for item in self.jobs.items(job_id)], ['', '', 'unsupported'])
        self.assertEqual([url.split('/attachments/')[1] for url in graph.content_requests()], ['A%2F1', 'A%2F2'])

    # --- PST -------------------------------------------------------------------------
    def pst(self):
        archive = FakePstArchive([
            (mail('Archiv Bericht', 'Bericht im Archiv.'), [('bericht.docx',
                 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', DOCX),
                 ('foto.png', 'image/png', PNG)]),
            (mail('Archiv Notiz', 'Notiz im Archiv.'), []),
        ])
        path = self.base / 'archiv.pst'
        path.write_bytes(b'!BDN')
        self.importer._pst_archive = lambda p, factory=None: archive
        return archive, path

    def test_pst_preview_lists_messages_and_attachments_without_reading_content(self):
        archive, path = self.pst()
        items = self.importer.preview_pst(path)
        self.assertEqual([(item['id'], item['subject']) for item in items],
                         [('2100', 'Archiv Bericht'), ('2101', 'Archiv Notiz')])
        self.assertEqual([(part['id'], part['name'], part['size']) for part in items[0]['attachments']],
                         [('0', 'bericht.docx', len(DOCX)), ('1', 'foto.png', len(PNG))])
        self.assertEqual(archive.read_log, [])
        self.assertEqual(self.count('documents'), 0)

    def test_pst_job_reads_only_released_attachments_and_resumes_after_crash(self):
        archive, path = self.pst()
        items = self.importer.preview_pst(path)
        job_id = self.importer.create_pst_job(self.jobs, path, items, {'2100': ['0']})
        self.assertEqual([item['locator'] for item in self.jobs.items(job_id)], ['2100', '2100#0', '2101'])
        real_add = self.store.add
        calls = {'count': 0}
        def crash_on_attachment(*args, **kwargs):
            result = real_add(*args, **kwargs)
            calls['count'] += 1
            if calls['count'] == 2:
                raise KeyboardInterrupt
            return result
        with patch.object(self.store, 'add', side_effect=crash_on_attachment):
            with self.assertRaises(KeyboardInterrupt):
                self.jobs.run(job_id)
        summary = self.jobs.run(job_id)
        self.assertEqual((summary['kind'], summary['imported'], summary['duplicate']), ('pst', 3, 0))
        self.assertEqual(archive.read_log, ['bericht.docx', 'bericht.docx'])  # the crashed attempt read it once
        self.assertEqual(self.count('documents'), 3)
        with self.store.connect() as db:
            link = db.execute('SELECT email_document_id FROM email_attachments').fetchone()
            email_id = db.execute("SELECT document_id FROM sources WHERE source_type='pst' AND locator LIKE ?",
                                  ('%:2100',)).fetchone()['document_id']
        self.assertEqual(link['email_document_id'], email_id)
        again = {item['id']: item for item in self.importer.preview_pst(path)}
        self.assertTrue(again['2100']['imported'])
        self.assertEqual([part['imported'] for part in again['2100']['attachments']], [True, False])

    def test_pst_job_rejects_oversized_attachment_before_reading(self):
        archive, path = self.pst()
        archive.items[0].attachments[1].size = 21 * 1024 * 1024
        job_id = self.importer.create_pst_job(self.jobs, path, [], {'2100': ['1']})
        self.jobs.run(job_id)
        self.assertEqual([(item['status'], item['reason']) for item in self.jobs.items(job_id)],
                         [('quarantined', 'too_large')])
        self.assertEqual(archive.read_log, [])


if __name__ == '__main__':
    unittest.main()
