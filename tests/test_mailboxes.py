"""Parity roadmap phase 8: mailboxes of server accounts (D-030, docs/adr-postfach-zugaenge.md).

Credentials are AES-256-GCM ciphertext bound to mailbox and account, the key never enters the database. A
mailbox belongs to one account; foreign ones look missing (I3). Microsoft 365 is connected through the Entra
app with Mail.Read, only for the signed-in person. The preview reads headers only; the worker imports the
chosen messages and released attachments and gives the account the owner grant. No real mail service is
called: IMAP, Graph and Entra are local fakes.
"""
import base64
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import accounts
import audit
import privacy
import server
import worker
from core import AI, Store
from import_jobs import ImportJobs
from mail_fakes import FakeGraph, FakeImapServer, graph_attachment, mail
from mail_vault import MailKeyMissing, MailVault, key_id, load_key
from server_mail import PERSONAL, ServerMailboxes, refresher
import test_rights
from test_rights import AUTHORITY, BASE, CLIENT_ID, CLIENT_SECRET, TENANT, fill, people
from test_server import TestIdentityProvider

KEY, OLD_KEY = bytes(range(32)), bytes(range(1, 33))
MAIL_CALLBACK_TENANT = '/postfaecher/microsoft/rueckruf'
PASSWORD = 'geheimes-imap-kennwort'
ATTACHMENT = 'Rechnungsbetrag 120 Euro, fällig im Dezember.'.encode()


def check_mailbox_contract(case, store):
    """Encryption, ownership and export rules alike on both backends."""
    ids = fill(store)
    admin, editor, reader = people(store, ids)
    mine = ServerMailboxes(store, MailVault(store, KEY, owner=editor), owner=editor)
    other = ServerMailboxes(store, MailVault(store, KEY, owner=admin), owner=admin)
    mailbox = mine.save_imap({'provider': 'imap', 'label': 'Büro', 'email': 'buero@example.test',
                              'host': 'imap.example.test', 'port': '993'}, PASSWORD)
    with store.connect() as db:
        row = dict(db.execute('SELECT * FROM account_mailboxes WHERE mailbox_id=?', (mailbox,)).fetchone())
    case.assertNotIn(PASSWORD.encode(), bytes(row['secret']))
    case.assertEqual((row['account_id'], row['key_id']), (editor, key_id(KEY)))
    case.assertEqual(mine.vault.get(mine.secret_name(mailbox)), PASSWORD)
    case.assertEqual([m['id'] for m in mine.list()], [mailbox])
    case.assertEqual(other.list(), [])
    with case.assertRaises(ValueError):
        other.get(mailbox)
    case.assertIsNone(other.vault.get(other.secret_name(mailbox)))
    with case.assertRaises(ValueError):  # a foreign row cannot be overwritten
        other.vault.set(other.secret_name(mailbox), 'fremd')
    with case.assertRaises(ValueError):
        MailVault(store, OLD_KEY).get(mine.secret_name(mailbox))  # unknown key
    # Key change: the old key stays readable, rewrap moves everything to the new one.
    rotated = MailVault(store, OLD_KEY, previous=KEY)
    case.assertEqual(rotated.get(mine.secret_name(mailbox)), PASSWORD)
    case.assertEqual(rotated.rewrap(), 1)
    case.assertEqual(MailVault(store, OLD_KEY).get(mine.secret_name(mailbox)), PASSWORD)
    # A ciphertext copied to another mailbox does not decrypt (mailbox and account are associated data).
    second = mine.save_imap({'provider': 'imap', 'label': 'Zweit', 'email': 'zwei@example.test',
                             'host': 'imap.example.test', 'port': '993'}, 'anderes-kennwort')
    with store.connect() as db:
        db.execute('UPDATE account_mailboxes SET secret=(SELECT secret FROM account_mailboxes WHERE mailbox_id=?),'
                   'nonce=(SELECT nonce FROM account_mailboxes WHERE mailbox_id=?),'
                   'key_id=(SELECT key_id FROM account_mailboxes WHERE mailbox_id=?) WHERE mailbox_id=?',
                   (mailbox, mailbox, mailbox, second))
    with case.assertRaisesRegex(ValueError, 'beschädigt'):
        MailVault(store, OLD_KEY).get(mine.secret_name(second))
    case.assertNotIn('account_mailboxes', privacy._tables())
    mine.delete(second)
    with store.connect() as db:
        case.assertIsNone(db.execute('SELECT 1 FROM account_mailboxes WHERE mailbox_id=?', (second,)).fetchone())


class VaultTests(unittest.TestCase):
    def test_contract(self):
        with tempfile.TemporaryDirectory() as folder:
            check_mailbox_contract(self, Store(Path(folder) / 'wissen.db'))

    def test_key_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'mail_key'
            self.assertIsNone(load_key({}))
            path.write_text(base64.b64encode(KEY).decode() + '\n')
            self.assertEqual(load_key({'WR_MAIL_KEY_FILE': str(path)}), KEY)
            path.write_text(base64.b64encode(b'kurz').decode())
            with self.assertRaises(ValueError):
                load_key({'WR_MAIL_KEY_FILE': str(path)})
        with self.assertRaises(MailKeyMissing):
            MailVault(None, None)
        self.assertNotIn(KEY.hex(), repr(MailVault(None, KEY)))


class PersonalSettingTests(unittest.TestCase):
    def test_off_unless_switched_on(self):
        with tempfile.TemporaryDirectory() as folder:
            secret = Path(folder) / 'secret'
            secret.write_text('geheim\n')
            env = {'WR_OIDC_CLIENT_ID': 'app', 'WR_OIDC_CLIENT_SECRET_FILE': str(secret)}
            self.assertIsNone(server.mail_personal_provider(env))
            self.assertIsNone(server.mail_personal_provider({**env, 'WR_MAIL_PERSONAL_ACCOUNTS': 'ja'}))
            self.assertIsNone(server.mail_personal_provider({'WR_MAIL_PERSONAL_ACCOUNTS': '1'}))  # no app
            provider = server.mail_personal_provider({**env, 'WR_MAIL_PERSONAL_ACCOUNTS': '1'})
            self.assertEqual((provider.authority, provider.tenant_id, provider.client_id),
                             ('https://login.microsoftonline.com/consumers/v2.0', server.PERSONAL_TENANT, 'app'))
            self.assertNotIn('geheim', repr(provider))


class MailIdentityProvider(TestIdentityProvider):
    """Adds the mailbox consent (other redirect, refresh token) and the refresh grant."""

    def __init__(self):
        super().__init__()
        self.refreshes, self.scopes = [], []

    def authorize(self, url, oid):
        import urllib.parse
        self.scopes.append(dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))['scope'])
        return super().authorize(url, oid)

    def http(self, url, form=None):
        if url == AUTHORITY + '/token' and form.get('grant_type') == 'refresh_token':
            self.refreshes.append((form['refresh_token'], form['scope']))
            if form['refresh_token'] == 'widerrufen':
                raise ValueError('invalid_grant')
            return {'access_token': 'graph-token', 'refresh_token': 'refresh-2'}
        if url == AUTHORITY + '/token' and form.get('redirect_uri') == BASE + '/postfaecher/microsoft/rueckruf':
            entry = self.codes.pop(form['code'])
            return {'id_token': self.token(entry['nonce'], entry['oid']), 'refresh_token': 'refresh-1',
                    'access_token': 'graph-token'}
        return super().http(url, form)


PERSONAL_AUTHORITY = 'https://login.personal.example.test/consumers/v2.0'


class PersonalIdentityProvider(MailIdentityProvider):
    """The consumers authority for Outlook.com: own discovery and token endpoint, Microsoft's personal tenant."""

    def __init__(self):
        super().__init__()
        self.personal_refreshes = []

    def http(self, url, form=None):
        if not url.startswith(PERSONAL_AUTHORITY):
            return super().http(url, form)
        path = url[len(PERSONAL_AUTHORITY):]
        if path == '/.well-known/openid-configuration':
            return {'issuer': PERSONAL_AUTHORITY, 'authorization_endpoint': PERSONAL_AUTHORITY + '/authorize',
                    'token_endpoint': PERSONAL_AUTHORITY + '/token', 'jwks_uri': PERSONAL_AUTHORITY + '/keys'}
        if path == '/keys':
            return super().http(AUTHORITY + '/keys')
        if path == '/token' and form.get('grant_type') == 'refresh_token':
            self.personal_refreshes.append((form['refresh_token'], form['scope']))
            return {'access_token': 'graph-token-privat', 'refresh_token': 'privat-2'}
        if path == '/token' and form.get('redirect_uri') == BASE + '/postfaecher/microsoft/privat/rueckruf':
            entry = self.codes.pop(form['code'])
            previous, self.overrides = self.overrides, {**self.overrides, 'iss': PERSONAL_AUTHORITY,
                                                        'tid': server.PERSONAL_TENANT}
            try:
                return {'id_token': self.token(entry['nonce'], entry['oid']), 'refresh_token': 'privat-1'}
            finally:
                self.overrides = previous
        raise OSError('unknown endpoint')


class MailboxPageTests(test_rights.EndpointTests):
    locals().update({name: None for name in dir(test_rights.EndpointTests) if name.startswith('test_')})

    def setUp(self):
        super().setUp()
        self.idp = MailIdentityProvider()
        self.rebuild(mail_keys=(KEY, None))
        self.heartbeat = Path(self.enterContext(tempfile.TemporaryDirectory())) / 'alive'
        self.server = FakeImapServer({11: mail('Rechnung', 'Bitte zahlen.', [('rechnung.txt', 'text/plain', ATTACHMENT)]),
                                      12: mail('Termin', 'Treffen am Montag.')})
        self.enterContext(patch('imaplib.IMAP4_SSL', self.server.client))

    def rebuild(self, **options):
        self.provider = test_rights.Provider(AUTHORITY, TENANT, CLIENT_ID, CLIENT_SECRET, BASE + '/auth/callback',
                                             http=self.idp.http)
        ai = lambda scoped: AI(scoped, 'test-key', transport=test_rights.answer(self.payloads))  # noqa: E731
        self.app = server.create_app(self.store, self.provider, BASE, ai=ai, **options)
        self.clients = {oid: self.sign_in(oid) for oid in ('oid-admin', 'oid-editor', 'oid-reader')}

    def importer(self):
        return worker.mail_importer(self.store, (KEY, None), self.provider)

    def work(self):
        return worker.import_once(self.store, threading.Event(), self.heartbeat, importer=self.importer())

    def add(self, client, **values):
        data = {'provider': 'imap', 'label': 'Büro', 'email': 'buero@example.test', 'host': 'imap.example.test',
                'port': '993', 'secret': PASSWORD, **values}
        response = client.post('/postfaecher', data=data)
        self.assertEqual(response.status_code, 303, response.text[-300:])
        return response.headers['location'].rsplit('/', 1)[1]

    def test_only_importers_and_only_own_mailboxes(self):
        editor = self.clients['oid-editor']
        self.assertEqual(self.clients['oid-reader'].get('/postfaecher').status_code, 403)
        mailbox = self.add(editor)
        self.assertIn('Büro', editor.get('/postfaecher').text)
        admin = self.clients['oid-admin']
        self.assertNotIn('Büro', admin.get('/postfaecher').text)  # not even administrators see it
        for path in (f'/postfaecher/{mailbox}', f'/postfaecher/{mailbox}?vorschau=1'):
            with self.subTest(path):
                self.assertEqual(admin.get(path).status_code, 404)
                self.assertEqual(admin.get(path).text, admin.get('/postfaecher/fehlt').text)
        self.assertEqual(admin.post(f'/postfaecher/{mailbox}/loeschen').status_code, 404)
        self.assertEqual(editor.post('/postfaecher', data={'provider': 'gmail', 'label': 'G', 'email': 'g@example.test',
                                                           'secret': 'normales-passwort'}).status_code, 422)
        details = [e['details'] for e in audit.entries(self.store, 100) if e['event'] == 'mailbox_changed']
        self.assertNotIn('buero@example.test', ''.join(details))
        self.assertNotIn(PASSWORD, ''.join(details))
        self.rebuild()  # without WR_MAIL_KEY_FILE
        self.assertIn('nicht eingerichtet', self.clients['oid-editor'].get('/postfaecher').text)
        self.assertEqual(self.clients['oid-editor'].get(f'/postfaecher/{mailbox}').status_code, 503)

    def test_preview_selection_and_import_by_the_worker(self):
        editor, reader = self.clients['oid-editor'], self.clients['oid-reader']
        mailbox = self.add(editor)
        page = editor.get(f'/postfaecher/{mailbox}', params={'vorschau': 1}).text
        self.assertIn('Rechnung', page)
        self.assertIn('rechnung.txt', page)
        self.assertFalse(any('BODY.PEEK[]' in spec or 'BODY[]' in spec for _, spec in self.server.fetches))
        chosen = editor.post(f'/postfaecher/{mailbox}/import', data={'message': ['11'], 'subject:11': 'Rechnung',
                                                                      'attachment': ['11#2', '12#9'], 'folder': 'INBOX'})
        self.assertEqual(chosen.status_code, 303)
        job = int(chosen.headers['location'].rsplit('/', 1)[1])
        self.assertIn(f'/importe/{job}', editor.get('/importe').text)
        summary = self.work()
        self.assertEqual((summary['status'], summary['imported']), ('completed', 2))
        documents = [i['document_id'] for i in ImportJobs(self.store).items(job)]
        for doc in documents:
            self.assertEqual(editor.get(f'/dokument/{doc}').status_code, 200)
            self.assertEqual(reader.get(f'/dokument/{doc}').status_code, 404)
        self.assertNotIn('Treffen', ''.join(str(self.store.get(d)['content']) for d in documents))  # not chosen
        created = [e for e in audit.entries(self.store, 100) if e['event'] == 'document_created']
        self.assertEqual(len(created), 2)
        self.assertIn('schon importiert', editor.get(f'/postfaecher/{mailbox}', params={'vorschau': 1}).text)

    def test_job_stops_when_the_account_may_no_longer_import(self):
        editor = self.clients['oid-editor']
        mailbox = self.add(editor)
        job = int(editor.post(f'/postfaecher/{mailbox}/import', data={'message': ['12'], 'folder': 'INBOX'})
                  .headers['location'].rsplit('/', 1)[1])
        current = accounts.account(self.store, self.editor)
        accounts.update_account(self.store, self.editor, current['version'], role='reader', disabled=False)
        with self.assertRaises(PermissionError):
            self.work()
        self.assertEqual(ImportJobs(self.store).job(job)['status'], 'cancelled')
        self.assertEqual(ImportJobs(self.store).summary(job)['imported'], 0)

    def test_microsoft_only_for_the_signed_in_person(self):
        editor = self.clients['oid-editor']
        start = editor.get('/postfaecher/microsoft/verbinden')
        self.assertEqual(start.status_code, 303)
        self.idp.overrides = {'preferred_username': 'bearbeiter@example.test'}
        code, state = self.idp.authorize(start.headers['location'], 'oid-editor')
        self.assertIn('Mail.Read', self.idp.scopes[-1])
        self.assertIn('offline_access', self.idp.scopes[-1])
        done = editor.get('/postfaecher/microsoft/rueckruf', params={'code': code, 'state': state})
        self.assertEqual(done.status_code, 303, done.text[-300:])
        mailbox = done.headers['location'].rsplit('/', 1)[1]
        boxes = ServerMailboxes(self.store, MailVault(self.store, KEY), refresh=refresher(self.provider))
        self.assertEqual(boxes.vault.get(boxes.secret_name(mailbox)), 'refresh-1')
        self.assertEqual(boxes.microsoft_token(mailbox), 'graph-token')
        self.assertEqual(boxes.vault.get(boxes.secret_name(mailbox)), 'refresh-2')  # rotated token stored
        # Someone else's consent in this browser is refused: the mailbox must be the own one.
        start = editor.get('/postfaecher/microsoft/verbinden')
        code, state = self.idp.authorize(start.headers['location'], 'oid-admin')
        self.assertEqual(editor.get('/postfaecher/microsoft/rueckruf', params={'code': code, 'state': state}).status_code, 422)
        self.assertEqual(len(boxes.list()), 1)
        # The answer must come back to the browser that started the consent.
        start = editor.get('/postfaecher/microsoft/verbinden')
        code, state = self.idp.authorize(start.headers['location'], 'oid-editor')
        elsewhere = self.sign_in('oid-editor')
        self.assertEqual(elsewhere.get('/postfaecher/microsoft/rueckruf', params={'code': code, 'state': state}).status_code, 422)
        # A replayed or foreign state is refused.
        self.assertEqual(editor.get('/postfaecher/microsoft/rueckruf', params={'code': code, 'state': state}).status_code, 422)
        graph = FakeGraph({'G=1': ({'subject': 'Angebot', 'from': {'emailAddress': {'address': 'a@example.test'}},
                                    'receivedDateTime': '2026-11-10T08:00:00Z', 'toRecipients': [], 'ccRecipients': [],
                                    'body': {'contentType': 'text', 'content': 'Angebot über Pumpen.'},
                                    'internetMessageId': '<g1@example.test>'},
                                   [graph_attachment('A1', 'angebot.txt', 'text/plain', ATTACHMENT)])})
        with patch('importer.Importer._graph_get', lambda self, url, token: graph(url, token)):
            self.assertIn('Angebot', editor.get(f'/postfaecher/{mailbox}', params={'vorschau': 1}).text)
            editor.post(f'/postfaecher/{mailbox}/import', data={'message': ['G=1'], 'attachment': ['G=1#A1']})
            self.assertEqual(self.work()['imported'], 2)
        MailVault(self.store, KEY).set(boxes.secret_name(mailbox), 'widerrufen')
        with self.assertRaisesRegex(ValueError, 'neu verbinden'):
            boxes.microsoft_token(mailbox)

    def test_personal_microsoft_account_only_when_switched_on(self):
        """Outlook.com (D-032): consumers authority, no tid/oid binding, token renewed at consumers."""
        editor = self.clients['oid-editor']
        self.assertEqual(editor.get('/postfaecher/microsoft/privat/verbinden').status_code, 404)
        self.assertNotIn('/privat/verbinden', editor.get('/postfaecher').text)
        self.idp = PersonalIdentityProvider()
        personal = test_rights.Provider(PERSONAL_AUTHORITY, server.PERSONAL_TENANT, CLIENT_ID, CLIENT_SECRET, '',
                                        http=self.idp.http)
        self.rebuild(mail_keys=(KEY, None), mail_personal=personal)
        editor = self.clients['oid-editor']
        self.assertIn('/postfaecher/microsoft/privat/verbinden', editor.get('/postfaecher').text)
        start = editor.get('/postfaecher/microsoft/privat/verbinden')
        self.assertEqual(start.status_code, 303)
        self.assertTrue(start.headers['location'].startswith(PERSONAL_AUTHORITY + '/authorize?'))
        # The personal identity is not the guest account in the tenant; it is accepted all the same.
        self.idp.overrides = {'preferred_username': 'markus@hotmail.example.test'}
        code, state = self.idp.authorize(start.headers['location'], 'msa-person')
        done = editor.get('/postfaecher/microsoft/privat/rueckruf', params={'code': code, 'state': state})
        self.assertEqual(done.status_code, 303, done.text[-300:])
        mailbox = done.headers['location'].rsplit('/', 1)[1]
        boxes = ServerMailboxes(self.store, MailVault(self.store, KEY), refresh=refresher(self.provider, personal))
        row = boxes.get(mailbox)
        self.assertEqual((row['tenant'], row['email'], row['label']),
                         (PERSONAL, 'markus@hotmail.example.test', 'Outlook.com · markus@hotmail.example.test'))
        with self.store.connect() as db:
            owner = db.execute("SELECT id FROM accounts WHERE object_id='oid-editor'").fetchone()[0]
        self.assertEqual(row['account_id'], owner)
        self.assertEqual(boxes.microsoft_token(mailbox), 'graph-token-privat')
        self.assertEqual(self.idp.personal_refreshes, [('privat-1', 'offline_access https://graph.microsoft.com/Mail.Read')])
        self.assertEqual(self.idp.refreshes, [])  # never at the tenant
        self.assertEqual(boxes.vault.get(boxes.secret_name(mailbox)), 'privat-2')
        # The worker renews at the same place.
        mailboxes = worker.mail_importer(self.store, (KEY, None), self.provider, personal).accounts
        self.assertEqual(mailboxes.microsoft_token(mailbox), 'graph-token-privat')
        self.assertEqual(len(self.idp.personal_refreshes), 2)
        # Without the personal provider the worker refuses this mailbox instead of asking the tenant.
        with self.assertRaisesRegex(ValueError, 'neu verbinden'):
            worker.mail_importer(self.store, (KEY, None), self.provider).accounts.microsoft_token(mailbox)
        self.assertEqual(self.idp.refreshes, [])
        # The state of a personal consent does not finish at the work-account callback: the code belongs to
        # the consumers authority, the tenant refuses it.
        start = editor.get('/postfaecher/microsoft/privat/verbinden')
        code, state = self.idp.authorize(start.headers['location'], 'msa-person')
        self.assertEqual(editor.get(MAIL_CALLBACK_TENANT, params={'code': code, 'state': state}).status_code, 422)
        self.assertEqual(len(boxes.list()), 1)
        # The consent goes into the audit trail with its own provider name.
        details = [e['details'] for e in audit.entries(self.store, 100) if e['event'] == 'mailbox_changed']
        self.assertIn('"provider":"microsoft_personal"', ''.join(details))

    def test_sync_only_after_switching_on(self):
        editor = self.clients['oid-editor']
        mailbox = self.add(editor)
        self.assertEqual(worker.sync_once(self.store, self.importer()), {})
        self.assertEqual(editor.post(f'/postfaecher/{mailbox}/synchronisation',
                                     data={'enabled': '1', 'interval': '15', 'folder': 'INBOX'}).status_code, 303)
        self.server.raw['13'] = mail('Neu', 'Neue Nachricht nach dem Einschalten.')
        synced = worker.sync_once(self.store, self.importer(), clock=lambda: 10 ** 10)
        self.assertEqual(synced[mailbox]['imported'], 1)
        jobs = ImportJobs(self.store).upload_jobs(self.editor)
        self.assertTrue(any(j['kind'] == 'imap' for j in jobs))
        new = [d for d in self.store.list() if d['title'] == 'Neu']
        self.assertEqual(self.clients['oid-editor'].get(f'/dokument/{new[0]["id"]}').status_code, 200)
        self.assertFalse(any('BODY.PEEK[2]' in spec for _, spec in self.server.fetches))  # no attachments


if __name__ == '__main__':
    unittest.main()
