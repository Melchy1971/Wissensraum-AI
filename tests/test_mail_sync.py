"""Mailbox sync while the application is open (Release 2.0.1, step S3)."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import import_fixtures as fx
from core import Store
from import_jobs import ImportJobs
from importer import Importer, MailAccounts
from mail_fakes import FakeGraph, FakeImapServer, graph_attachment, mail
from mail_sync import MailSync
from test_settings import FakeVault

PDF = fx.pdf_with_text('ANHANGSINHALT-SYNC')


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


class MailSyncTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(self.base / 'wissen.db')
        self.accounts = MailAccounts(self.store, FakeVault(), self.base)
        self.importer = Importer(self.store, self.accounts)
        self.jobs = ImportJobs(self.store, self.importer)
        self.clock = Clock()
        self.sync = MailSync(self.store, self.importer, self.jobs, self.clock)

    def imap(self):
        account_id = self.accounts.save({'label': 'Arbeit', 'provider': 'imap', 'email': 'm@example.test',
                                         'username': 'm', 'host': 'imap.example.test', 'port': 993}, 'geheim')
        server = FakeImapServer({11: mail('Alt', 'Schon vor der Aktivierung da.'),
                                 12: mail('Auch alt', 'Ebenfalls alt.')})
        real = self.importer._imap_login
        self.importer._imap_login = lambda account, folder, factory=None: real(account, folder, server.client)
        return account_id, server

    def titles(self):
        return sorted(row['title'] for row in self.store.list())

    def test_nothing_is_fetched_without_activation(self):
        account_id, server = self.imap()
        self.clock.now += 10 * 3600
        self.assertEqual(self.sync.due(), [])
        self.assertEqual(self.sync.run_due(), {})
        with self.assertRaisesRegex(ValueError, 'nicht aktiv'):
            self.sync.run(account_id)
        self.assertEqual((server.fetches, server.searches), ([], []))
        self.assertEqual(self.store.list(), [])

    def test_only_new_messages_without_attachments_and_without_duplicates(self):
        account_id, server = self.imap()
        state = self.sync.activate(account_id, 30)
        self.assertEqual(state['mark'], {'uidvalidity': 1, 'uid': 12})
        self.assertEqual(server.fetches, [])  # activation reads no message
        self.assertEqual(self.sync.due(), [])  # interval not yet over
        server.raw['13'] = mail('Neu mit Anhang', 'Bitte Anlage prüfen.', [('rechnung.pdf', 'application/pdf', PDF)])
        server.raw['14'] = mail('Neu ohne Anhang', 'Termin am Montag.')
        self.clock.now += 30 * 60
        self.assertEqual(self.sync.due(), [account_id])
        result = self.sync.run_due()[account_id]
        self.assertEqual((result['imported'], result['mark']['uid']), (2, 14))
        self.assertEqual(self.titles(), ['Neu mit Anhang', 'Neu ohne Anhang'])
        mail_doc = next(row for row in self.store.list() if row['title'] == 'Neu mit Anhang')
        stored = self.store.get(mail_doc['id'])
        self.assertIn('Anhänge: rechnung.pdf', stored['content'])
        self.assertNotIn(b'ANHANGSINHALT-SYNC', bytes(stored['original']))
        fetched = {spec for uid, spec in server.fetches}
        self.assertFalse(any('[2]' in spec or '[2.' in spec for spec in fetched), fetched)  # attachment part never read
        self.assertEqual({uid for uid, _ in server.fetches}, {'13', '14'})
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM documents d JOIN sources s ON s.document_id=d.id "
                                        "WHERE s.source_type='attachment'").fetchone()[0], 0)
        # Next run: nothing new; a message imported by hand in between is not imported again.
        server.raw['15'] = mail('Von Hand geholt', 'Schon importiert.')
        self.importer.import_imap_ids(account_id, 'INBOX', ['15'])
        self.clock.now += 30 * 60
        result = self.sync.run(account_id)
        self.assertEqual((result['imported'], result['last_result'], result['mark']['uid']),
                         (0, 'Keine neuen E-Mails.', 15))
        self.assertEqual(len(self.store.list()), 3)
        self.assertEqual(len(self.jobs.jobs()), 1)

    def test_sign_in_failure_stops_with_reason_until_reactivated(self):
        account_id, server = self.imap()
        self.sync.activate(account_id, 15)
        server.raw['13'] = mail('Neu', 'Text')
        server.refuse_login = True
        self.clock.now += 15 * 60
        state = self.sync.run_due()[account_id]
        self.assertIn('IMAP-Anmeldung fehlgeschlagen', state['stopped'])
        self.assertNotIn('Invalid credentials', state['stopped'])  # server text is not repeated
        self.clock.now += 10 * 3600
        self.assertEqual(self.sync.due(), [])  # stopped: no further attempts
        server.refuse_login = False
        with self.assertRaisesRegex(ValueError, 'nicht aktiv'):
            self.sync.run(account_id)
        self.sync.activate(account_id, 15)  # reactivation starts again from now
        self.clock.now += 15 * 60
        self.assertEqual(self.sync.run(account_id)['imported'], 0)
        self.assertEqual(self.store.list(), [])

    def test_unreachable_server_is_retried_at_the_next_interval(self):
        account_id, server = self.imap()
        self.sync.activate(account_id, 60)
        server.raw['13'] = mail('Neu', 'Text')
        real = self.importer._imap_login

        def unreachable(*_):
            raise ValueError('IMAP-Server ist nicht erreichbar.')
        self.importer._imap_login = unreachable
        self.clock.now += 3600
        state = self.sync.run(account_id)
        self.assertEqual(state['stopped'], '')
        self.assertIn('neuer Versuch', state['last_result'])
        self.assertEqual(self.sync.due(), [])
        self.importer._imap_login = real
        self.clock.now += 3600
        self.assertEqual(self.sync.run_due()[account_id]['imported'], 1)

    def test_renumbered_folder_stops_instead_of_importing_everything_again(self):
        account_id, server = self.imap()
        self.sync.activate(account_id, 15)
        server.uidvalidity = 2
        self.clock.now += 15 * 60
        state = self.sync.run(account_id)
        self.assertIn('neu nummeriert', state['stopped'])
        self.assertEqual(self.store.list(), [])

    def test_deactivation_and_interval_choices(self):
        account_id, _ = self.imap()
        for interval in (5, 45, 1440):
            with self.subTest(interval=interval), self.assertRaisesRegex(ValueError, 'Intervall'):
                self.sync.activate(account_id, interval)
        self.sync.activate(account_id, 240)
        self.clock.now += 239 * 60
        self.assertEqual(self.sync.due(), [])
        self.clock.now += 60
        self.assertEqual(self.sync.due(), [account_id])
        self.sync.deactivate(account_id)
        self.assertEqual(self.sync.due(), [])
        with self.store.connect() as db:
            stored = db.execute("SELECT value FROM settings WHERE name LIKE 'mail_sync.%'").fetchone()['value']
        self.assertNotIn('geheim', stored)  # no credentials in the sync state

    def test_microsoft_inbox_only_messages_after_activation(self):
        account_id = self.accounts.save({'label': 'Firma', 'provider': 'microsoft', 'email': 'm@firma.example',
                                         'username': 'm@firma.example',
                                         'client_id': '11111111-2222-3333-4444-555555555555'})
        self.accounts.microsoft_token = lambda _: 'token'
        old = {'subject': 'Alt', 'from': {'emailAddress': {'address': 'a@firma.example'}},
               'receivedDateTime': '2026-10-26T08:00:00Z', 'body': {'contentType': 'text', 'content': 'Alt'},
               'toRecipients': [], 'ccRecipients': [], 'internetMessageId': '<alt@firma.example>'}
        graph = FakeGraph({'A1': (old, [])})
        self.importer._graph_get = graph
        self.assertEqual(self.sync.activate(account_id, 15, folder='Anderer Ordner')['mark'],
                         {'after': '2026-10-26T08:00:00Z'})
        self.assertEqual(self.sync.config(account_id)['folder'], 'INBOX')
        new = dict(old, subject='Neu', receivedDateTime='2026-10-26T09:00:00Z', internetMessageId='<neu@firma.example>',
                   body={'contentType': 'text', 'content': 'Neuer Text'})
        graph.messages['B2'] = (new, [graph_attachment('X', 'anlage.pdf', 'application/pdf', PDF)])
        self.clock.now += 15 * 60
        state = self.sync.run_due()[account_id]
        self.assertEqual((state['imported'], state['mark']), (1, {'after': '2026-10-26T09:00:00Z'}))
        self.assertEqual(self.titles(), ['Neu'])
        self.assertEqual(graph.content_requests(), [])  # attachment content never requested
        self.assertTrue(any('receivedDateTime+gt+2026-10-26T08%3A00%3A00Z' in url for url in graph.urls))


if __name__ == '__main__':
    unittest.main()
