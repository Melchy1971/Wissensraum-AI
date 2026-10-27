"""Removing attachment content from IMAP originals stored before step 6.3a."""
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import import_fixtures as fx
import mail_cleanup
from core import Store
from importer import Importer, MailAccounts
from mail_fakes import mail
from test_settings import FakeVault

SECRET = b'VERTRAULICHER-ANHANG-'
PDF = fx.pdf_with_text('Vertrag') + SECRET


def encoded_marker(raw):
    """First line of the attachment as it is stored in the message (base64), to find it in files."""
    from email import policy
    from email.parser import BytesParser
    part = next(part for part in BytesParser(policy=policy.compat32).parsebytes(raw).walk()
                if part.get_filename())
    return part.get_payload().strip().splitlines()[0].encode()


class MailCleanupTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(self.base / 'wissen.db')
        self.accounts = MailAccounts(self.store, FakeVault(), self.base)
        self.importer = Importer(self.store, self.accounts)
        self.account_id = self.accounts.save({'label': 'Arbeit', 'provider': 'imap', 'email': 'm@example.test',
                                              'username': 'm', 'host': 'imap.example.test', 'port': 993}, 'geheim')

    def legacy(self, uid, raw):
        """Store a message the way the IMAP import did until step 6.2: the full raw message."""
        key = f'imap:INBOX:{uid}'
        document_id, _ = self.importer._add_email(raw, 'Arbeit', ('imap', self.account_id, key))
        self.importer._record_import(self.account_id, key, document_id)
        return document_id

    def database_bytes(self):
        data = b''
        for path in self.base.glob('wissen.db*'):
            if '.bak' not in path.name:
                data += path.read_bytes()
        return data

    def test_dry_run_counts_and_changes_nothing(self):
        self.legacy(1, mail('Vertrag', 'Bitte prüfen.', [('vertrag.pdf', 'application/pdf', PDF)]))
        self.legacy(2, mail('Nur Text', 'Kein Anhang.'))
        before = self.store.get(1)['original']
        result = mail_cleanup.strip_stored_attachments(self.store, dry_run=True)
        self.assertEqual((result['checked'], result['changed'], result['attachments'], result['skipped']), (2, 1, 1, 0))
        self.assertEqual(self.store.get(1)['original'], before)
        self.assertEqual(list(self.base.glob('*.bak')), [])

    def test_cleanup_removes_attachments_keeps_text_and_leaves_a_verified_backup(self):
        raw = mail('Vertrag', 'Bitte prüfen.', [('vertrag.pdf', 'application/pdf', PDF)],
                   html='<p>Bitte <b>prüfen</b>.</p>')
        marker = encoded_marker(raw)
        contract = self.legacy(1, raw)
        plain = self.legacy(2, mail('Nur Text', 'Kein Anhang.'))
        document = self.store.get(contract)
        plain_before = self.store.get(plain)['original']
        self.assertIn(marker, self.database_bytes())
        result = mail_cleanup.strip_stored_attachments(self.store)
        self.assertEqual((result['changed'], result['attachments']), (1, 1))
        backup = Path(result['backup'])
        with closing(sqlite3.connect(backup)) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertIn(marker, bytes(db.execute('SELECT original FROM documents WHERE id=?', (contract,)).fetchone()[0]))
        after = self.store.get(contract)
        self.assertNotIn(marker, after['original'])
        self.assertIn(b'X-Wissensraum-Anhaenge-nicht-gespeichert: 1', after['original'])
        self.assertEqual((after['content'], after['sha256']), (document['content'], document['sha256']))
        self.assertEqual(self.store.get(plain)['original'], plain_before)
        self.assertEqual(self.store.search('prüfen')[0]['document_id'], contract)
        self.assertNotIn(marker, self.database_bytes())  # VACUUM removed the freed pages
        again = mail_cleanup.strip_stored_attachments(self.store)
        self.assertEqual((again['changed'], again['backup']), (0, ''))

    def test_message_that_would_lose_text_is_skipped(self):
        raw = (b'From: a@example.test\r\nSubject: Grenzfall\r\nMIME-Version: 1.0\r\n'
               b'Content-Type: multipart/mixed; boundary="b"\r\n\r\n--b\r\n'
               b'Content-Type: text/plain; charset=utf-8\r\n\r\nHaupttext\r\n--b\r\n'
               b'Content-Type: text/plain; name="notiz.txt"\r\nContent-Disposition: inline; filename="notiz.txt"\r\n\r\n'
               b'Inline-Text\r\n--b\r\nContent-Type: application/pdf; name="x.pdf"\r\n'
               b'Content-Disposition: attachment; filename="x.pdf"\r\n\r\n' + SECRET + b'\r\n--b--\r\n')
        document_id = self.legacy(1, raw)
        result = mail_cleanup.strip_stored_attachments(self.store)
        self.assertEqual((result['changed'], result['skipped']), (0, 1))
        self.assertEqual(self.store.get(document_id)['original'], raw)

    def test_only_imap_originals_are_touched(self):
        raw = mail('Datei', 'Text', [('vertrag.pdf', 'application/pdf', PDF)])
        self.store.add('Datei', 'Dateiinhalt', 'Allgemein', 'mail.eml', raw)  # a local file, not an IMAP original
        result = mail_cleanup.strip_stored_attachments(self.store)
        self.assertEqual((result['checked'], result['changed']), (0, 0))

    def test_postgresql_requires_confirmed_pg_dump_backup(self):
        self.legacy(1, mail('Vertrag', 'Bitte prüfen.', [('vertrag.pdf', 'application/pdf', PDF)]))
        before = self.store.get(1)['original']
        with patch.object(self.store, 'backend', 'postgresql'):
            with self.assertRaisesRegex(ValueError, 'pg_dump'):
                mail_cleanup.strip_stored_attachments(self.store)
        self.assertEqual(self.store.get(1)['original'], before)

    def test_cli_dry_run(self):
        import cli
        self.legacy(1, mail('Vertrag', 'Bitte prüfen.', [('vertrag.pdf', 'application/pdf', PDF)]))
        vault = Mock(); vault.get.return_value = None
        output = io.StringIO()
        with patch.object(cli, 'BASE', self.base), patch.object(cli, 'SystemVault', return_value=vault), \
                redirect_stdout(output):
            self.assertEqual(cli.main(['--db', str(self.store.path), 'strip-mail-attachments', '--dry-run']), 0)
        self.assertEqual(json.loads(output.getvalue())['changed'], 1)


if __name__ == '__main__':
    unittest.main()
