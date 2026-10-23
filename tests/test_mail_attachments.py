"""E-mail attachments only with explicit release (Phase 6.3a).

Every request to IMAP, Graph and PST is recorded. Without a release, no attachment
content is requested and nothing of it is stored.
"""
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import import_fixtures as fx
import mail_structure
from core import Store
from importer import Importer, MailAccounts
from mail_fakes import (FakeGraph, FakeImapServer, FakePstArchive, graph_attachment, mail)
from test_settings import FakeVault

SECRET = b'GEHEIMER-ANHANGSINHALT-'  # marker that must never reach the database unreleased
PDF = fx.pdf_with_text('Vertrag Anhangstext Lieferung')
DOCX = fx.docx_with_text('Protokoll aus dem Anhang')
PNG = b'\x89PNG\r\n\x1a\n' + SECRET + b'bild'


def stored_bytes(store):
    """All BLOB and text content in documents, to search for attachment bytes."""
    with store.connect() as db:
        rows = db.execute('SELECT content, original FROM documents').fetchall()
    return b''.join((row['content'] or '').encode() + bytes(row['original'] or b'') for row in rows)


class MailStructureTests(unittest.TestCase):
    def test_parser_reads_nested_lists_quoted_strings_literals_and_nil(self):
        payload = [(b'1 (UID 7 BODYSTRUCTURE (("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "7BIT" 5 1 NIL NIL NIL NIL)'
                    b'("APPLICATION" "PDF" ("NAME" {9}', b'a "b".pdf'),
                   b') NIL NIL "BASE64" 100 NIL ("ATTACHMENT" ("FILENAME" "x.pdf")) NIL NIL) "MIXED" ("BOUNDARY" "q") NIL NIL NIL))']
        fields = mail_structure.fetch_items(payload)
        parts = mail_structure.parts(fields['BODYSTRUCTURE'])
        self.assertEqual([(p['section'], p['content_type'], p['attachment']) for p in parts],
                         [('1', 'text/plain', False), ('2', 'application/pdf', True)])
        self.assertEqual(parts[1]['name'], 'x.pdf')  # disposition file name wins over type name

    def test_rfc2231_and_encoded_word_file_names(self):
        structure = mail_structure.parse(
            b'(("TEXT" "PLAIN" NIL NIL NIL "7BIT" 1 1 NIL NIL NIL NIL)'
            b'("APPLICATION" "PDF" NIL NIL NIL "BASE64" 1 NIL ("ATTACHMENT" ("FILENAME*" "utf-8\'\'Pr%C3%BCfung.pdf")) NIL NIL)'
            b'("APPLICATION" "PDF" ("NAME" "=?utf-8?q?Gr=C3=BC=C3=9Fe.pdf?=") NIL NIL "BASE64" 1 NIL NIL NIL NIL)'
            b'("APPLICATION" "PDF" NIL NIL NIL "BASE64" 1 NIL ("ATTACHMENT" ("FILENAME*0*" "utf-8\'\'Teil%20" "FILENAME*1*" "zwei.pdf")) NIL NIL)'
            b' "MIXED" NIL NIL NIL)')[0]
        names = [part['name'] for part in mail_structure.parts(structure)[1:]]
        self.assertEqual(names, ['Prüfung.pdf', 'Grüße.pdf', 'Teil zwei.pdf'])

    def test_inline_text_without_name_is_body_and_forwarded_mail_is_one_attachment(self):
        structure = mail_structure.parse(
            b'((("TEXT" "PLAIN" NIL NIL NIL "7BIT" 1 1 NIL NIL NIL NIL)("TEXT" "HTML" NIL NIL NIL "7BIT" 1 1 NIL NIL NIL NIL) "ALTERNATIVE" NIL NIL NIL)'
            b'("MESSAGE" "RFC822" NIL NIL NIL "7BIT" 9 NIL ("TEXT" "PLAIN" NIL NIL NIL "7BIT" 1 1 NIL NIL NIL NIL) 3 NIL ("ATTACHMENT" NIL) NIL NIL)'
            b'("TEXT" "PLAIN" ("NAME" "notiz.txt") NIL NIL "7BIT" 4 1 NIL ("INLINE" ("FILENAME" "notiz.txt")) NIL NIL)'
            b' "MIXED" NIL NIL NIL)')[0]
        parts = mail_structure.parts(structure)
        self.assertEqual([(p['section'], p['attachment']) for p in parts],
                         [('1.1', False), ('1.2', False), ('2', True), ('3', True)])
        self.assertEqual(mail_structure.attachment_name(parts[2]), 'Anhang-2.eml')

    def test_rebuilt_message_keeps_header_and_text_and_notes_left_out_attachments(self):
        rebuilt = mail_structure.rebuild_message(
            b'Subject: Test\r\nContent-Type: multipart/mixed;\r\n boundary="alt"\r\nFrom: a@example.test\r\n\r\n',
            [(b'Content-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: base64\r\n',
              b'SGFsbG8gV2VsdA==')], 2)
        from email import policy
        from email.parser import BytesParser
        message = BytesParser(policy=policy.default).parsebytes(rebuilt)
        self.assertEqual(message['From'], 'a@example.test')
        self.assertEqual(message['X-Wissensraum-Anhaenge-nicht-gespeichert'], '2')
        self.assertEqual(message.get_body(('plain',)).get_content().strip(), 'Hallo Welt')
        self.assertNotIn(b'boundary="alt"', rebuilt)


class AttachmentReleaseTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(self.base / 'wissen.db')
        self.vault = FakeVault()
        self.accounts = MailAccounts(self.store, self.vault, self.base)
        self.importer = Importer(self.store, self.accounts)

    # --- IMAP ------------------------------------------------------------------------
    def imap_account(self):
        account_id = self.accounts.save({'label': 'Arbeit', 'provider': 'imap', 'email': 'markus@example.test',
                                         'username': 'markus', 'host': 'imap.example.test', 'port': 993},
                                        'imap-geheim')
        return account_id

    def imap_server(self, literal_names=False):
        return FakeImapServer({
            101: mail('Vertrag', 'Bitte Vertrag prüfen.', [('vertrag.pdf', 'application/pdf', PDF),
                                                         ('foto.png', 'image/png', PNG)]),
            102: mail('Nur Text', 'Kein Anhang hier.'),
            103: mail('Protokoll', 'Siehe Anlage.', [('protokoll.docx',
                      'application/vnd.openxmlformats-officedocument.wordprocessingml.document', DOCX)],
                      html='<p>Siehe <b>Anlage</b>.</p>'),
        }, literal_names=literal_names)

    def attachment_fetches(self, server):
        """Fetch specs that touch an attachment section or the full message."""
        attachment_sections = {('101', '2'), ('101', '3'), ('103', '2')}
        bad = []
        for uid, spec in server.fetches:
            if 'BODY.PEEK[]' in spec or 'RFC822' in spec:
                bad.append((uid, spec))
            for section in re.findall(r'BODY\.PEEK\[([\d.]+)(?:\.MIME)?\]', spec):
                if (uid, section) in attachment_sections:
                    bad.append((uid, spec))
        return bad

    def test_imap_preview_lists_attachments_from_structure_only(self):
        account_id = self.imap_account()
        server = self.imap_server(literal_names=True)
        items = {item['id']: item for item in self.importer.preview_imap(account_id, client_factory=server.client)}
        self.assertEqual([(a['id'], a['name'], a['content_type']) for a in items['101']['attachments']],
                         [('2', 'vertrag.pdf', 'application/pdf'), ('3', 'foto.png', 'image/png')])
        self.assertGreater(items['101']['attachments'][0]['size'], len(PDF))  # encoded size
        self.assertEqual(items['102']['attachments'], [])
        self.assertEqual([a['name'] for a in items['103']['attachments']], ['protokoll.docx'])
        self.assertFalse(items['101']['attachments'][0]['imported'])
        self.assertEqual({spec for _, spec in server.fetches},
                         {'(BODYSTRUCTURE BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE MESSAGE-ID)])'})
        self.assertEqual(self.store.list(), [])

    def test_imap_import_without_release_never_requests_or_stores_attachment_content(self):
        account_id = self.imap_account()
        server = self.imap_server()
        report = self.importer.import_imap_ids(account_id, 'INBOX', ['101', '102', '103'], server.client)
        self.assertEqual((report['imported'], report['errors'], report['attachments_imported']), (3, 0, 0))
        self.assertEqual(self.attachment_fetches(server), [])
        self.assertNotIn(SECRET, stored_bytes(self.store))
        self.assertNotIn(b'%PDF', stored_bytes(self.store))
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM email_attachments').fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM sources WHERE source_type='attachment'").fetchone()[0], 0)
        contract = next(row for row in self.store.list(all_versions=True) if row['title'] == 'Vertrag')
        content = self.store.get(contract['id'])['content']
        self.assertIn('Bitte Vertrag prüfen.', content)
        self.assertIn('Anhänge: vertrag.pdf (application/pdf', content)
        self.assertIn(b'X-Wissensraum-Anhaenge-nicht-gespeichert: 2', self.store.get(contract['id'])['original'])
        protocol = next(row for row in self.store.list() if row['title'] == 'Protokoll')
        self.assertIn('Siehe Anlage.', self.store.get(protocol['id'])['content'])

    def test_imap_release_downloads_only_released_attachment_and_links_it(self):
        account_id = self.imap_account()
        server = self.imap_server()
        report = self.importer.import_imap_ids(account_id, 'INBOX', ['101'], server.client,
                                               attachments={'101': ['2']})
        self.assertEqual((report['imported'], report['attachments_imported']), (1, 1))
        fetched_sections = [spec for uid, spec in server.fetches if '[2' in spec or '[3' in spec]
        self.assertEqual(fetched_sections, ['(BODY.PEEK[2.MIME] BODY.PEEK[2])'])
        self.assertNotIn(SECRET, stored_bytes(self.store))  # foto.png was not released
        with self.store.connect() as db:
            link = db.execute('SELECT * FROM email_attachments').fetchone()
            source = db.execute("SELECT * FROM sources WHERE source_type='attachment'").fetchone()
        email_id = next(row['id'] for row in self.store.list() if row['title'] == 'Vertrag')
        attachment = self.store.get(link['document_id'])
        self.assertEqual((link['email_document_id'], link['name'], link['content_type'], link['size']),
                         (email_id, 'vertrag.pdf', 'application/pdf', len(PDF)))
        self.assertEqual(attachment['original'], PDF)
        self.assertIn('Vertrag Anhangstext Lieferung', attachment['content'])
        self.assertEqual((source['account_id'], source['locator']), (account_id, 'imap:INBOX:101#2'))
        items = {item['id']: item for item in self.importer.preview_imap(account_id, client_factory=server.client)}
        self.assertEqual([a['imported'] for a in items['101']['attachments']], [True, False])

    def test_attachment_of_an_earlier_imported_message_can_be_released_later(self):
        account_id = self.imap_account()
        server = self.imap_server()
        self.importer.import_imap_ids(account_id, 'INBOX', ['103'], server.client)
        report = self.importer.import_imap_ids(account_id, 'INBOX', [], server.client, attachments={'103': ['2']})
        self.assertEqual((report['found'], report['imported'], report['attachments_imported']), (0, 0, 1))
        self.assertEqual(len([row for row in self.store.list() if row['title'] == 'Protokoll']), 1)
        with self.store.connect() as db:
            link = db.execute('SELECT email_document_id FROM email_attachments').fetchone()
        self.assertEqual(link['email_document_id'],
                         next(row['id'] for row in self.store.list() if row['title'] == 'Protokoll'))

    def test_released_image_is_quarantined_until_ocr_and_stores_nothing(self):
        account_id = self.imap_account()
        server = self.imap_server()
        report = self.importer.import_imap_ids(account_id, 'INBOX', ['101'], server.client,
                                               attachments={'101': ['3']})
        self.assertEqual((report['attachments_imported'], report['attachments_quarantined']), (0, 1))
        self.assertEqual(report['quarantine'], [{'name': 'foto.png', 'reason': 'no_text'}])
        self.assertNotIn(SECRET, stored_bytes(self.store))

    def test_oversized_attachment_is_rejected_before_download(self):
        account_id = self.imap_account()
        big = b'%PDF-1.4\n' + b'0' * (21 * 1024 * 1024)
        server = FakeImapServer({7: mail('Groß', 'Text', [('gross.pdf', 'application/pdf', big)])})
        report = self.importer.import_imap_ids(account_id, 'INBOX', ['7'], server.client, attachments={'7': ['2']})
        self.assertEqual(report['quarantine'], [{'name': 'gross.pdf', 'reason': 'too_large'}])
        self.assertFalse(any('[2]' in spec for _, spec in server.fetches))

    def test_unknown_attachment_and_invalid_selection_are_rejected(self):
        account_id = self.imap_account()
        server = self.imap_server()
        report = self.importer.import_imap_ids(account_id, 'INBOX', [], server.client, attachments={'102': ['2']})
        self.assertEqual(report['attachments_errors'], 1)
        with self.assertRaises(ValueError):
            self.importer.import_imap_ids(account_id, 'INBOX', [], server.client, attachments={'101': ['2]x']})

    def test_deleting_the_email_keeps_the_attachment_document(self):
        account_id = self.imap_account()
        server = self.imap_server()
        self.importer.import_imap_ids(account_id, 'INBOX', ['101'], server.client, attachments={'101': ['2']})
        email_id = next(row['id'] for row in self.store.list() if row['title'] == 'Vertrag')
        self.store.delete(email_id)
        with self.store.connect() as db:
            link = db.execute('SELECT document_id,email_document_id FROM email_attachments').fetchone()
        self.assertIsNone(link['email_document_id'])
        self.assertEqual(self.store.get(link['document_id'])['title'], 'vertrag')

    def test_message_that_is_only_an_attachment_imports_headers_without_content(self):
        account_id = self.imap_account()
        raw = (b'From: a@example.test\r\nSubject: Nur PDF\r\nMIME-Version: 1.0\r\n'
               b'Content-Type: application/pdf; name="x.pdf"\r\nContent-Disposition: attachment; filename="x.pdf"\r\n'
               b'Content-Transfer-Encoding: base64\r\n\r\n' + __import__('base64').b64encode(PDF) + b'\r\n')
        server = FakeImapServer({9: raw})
        report = self.importer.import_imap_ids(account_id, 'INBOX', ['9'], server.client)
        self.assertEqual(report['imported'], 1)
        self.assertEqual([spec for _, spec in server.fetches], ['(BODYSTRUCTURE)', '(BODY.PEEK[HEADER])'])
        self.assertIn('(Kein Nachrichtentext)', self.store.get(self.store.list()[0]['id'])['content'])
        report = self.importer.import_imap_ids(account_id, 'INBOX', [], server.client, attachments={'9': ['1']})
        self.assertEqual(report['attachments_imported'], 1)

    # --- Microsoft Graph -------------------------------------------------------------
    def graph(self):
        detail = lambda subject, body: {'subject': subject, 'from': {'emailAddress': {'address': 'a@example.test'}},
                                        'toRecipients': [], 'receivedDateTime': '2026-10-23T10:00:00Z',
                                        'body': {'contentType': 'text', 'content': body},
                                        'internetMessageId': '<' + subject + '@example.test>'}
        return FakeGraph({
            'AAA=': (detail('Angebot', 'Angebot anbei.'), [
                graph_attachment('ATT/1', 'angebot.pdf', 'application/pdf', PDF),
                graph_attachment('ATT/2', 'bild.png', 'image/png', PNG),
                graph_attachment('ATT/3', 'Weitergeleitet', 'message/rfc822', b'', '#microsoft.graph.itemAttachment')]),
            'BBB=': (detail('Ohne', 'Ohne Anhang.'), []),
        })

    def graph_account(self):
        account_id = self.accounts.save({'label': 'M365', 'provider': 'microsoft', 'email': 'm@example.test',
                                         'username': 'm@example.test',
                                         'client_id': '11111111-2222-3333-4444-555555555555'})
        self.accounts.microsoft_token = lambda _: 'token'
        return account_id

    def test_graph_preview_lists_attachment_metadata_without_content(self):
        account_id = self.graph_account()
        graph = self.graph()
        items = {item['id']: item for item in self.importer.preview_microsoft(account_id, transport=graph)}
        self.assertEqual([(a['id'], a['name'], a['size']) for a in items['AAA=']['attachments']],
                         [('ATT/1', 'angebot.pdf', len(PDF)), ('ATT/2', 'bild.png', len(PNG)),
                          ('ATT/3', 'Weitergeleitet', 0)])
        self.assertEqual(items['BBB=']['attachments'], [])
        self.assertEqual(graph.content_requests(), [])
        self.assertTrue(all('contentBytes' not in url for url in graph.urls))

    def test_graph_import_downloads_only_released_attachments(self):
        account_id = self.graph_account()
        graph = self.graph()
        report = self.importer.import_microsoft_ids(account_id, ['AAA=', 'BBB='], transport=graph)
        self.assertEqual((report['imported'], report['attachments_imported']), (2, 0))
        self.assertEqual(graph.content_requests(), [])
        self.assertNotIn(SECRET, stored_bytes(self.store))
        offer = next(row for row in self.store.list() if row['title'] == 'Angebot')
        self.assertIn('Anhänge: angebot.pdf (application/pdf', self.store.get(offer['id'])['content'])
        report = self.importer.import_microsoft_ids(account_id, [], transport=graph,
                                                    attachments={'AAA=': ['ATT/1', 'ATT/3']})
        self.assertEqual((report['attachments_imported'], report['attachments_quarantined']), (1, 1))
        self.assertEqual(report['quarantine'], [{'name': 'Weitergeleitet', 'reason': 'unsupported'}])
        self.assertEqual([url.split('/attachments/')[1] for url in graph.content_requests()], ['ATT%2F1', 'ATT%2F3'])
        self.assertNotIn(SECRET, stored_bytes(self.store))
        with self.store.connect() as db:
            link = db.execute('SELECT * FROM email_attachments').fetchone()
        self.assertEqual(link['email_document_id'], offer['id'])
        self.assertEqual(self.store.get(link['document_id'])['original'], PDF)

    # --- PST -------------------------------------------------------------------------
    def test_pst_import_lists_attachment_names_but_never_reads_their_content(self):
        archive = FakePstArchive([
            (mail('PST Vertrag', 'Vertrag im Archiv.'), [('vertrag.pdf', 'application/pdf', PDF),
                                                         ('foto.png', 'image/png', PNG)]),
            (mail('PST Nur Text', 'Kein Anhang.'), []),
        ])
        pst = self.base / 'archiv.pst'; pst.write_bytes(b'!BDN')
        report = self.importer.import_pst(pst, archive_factory=archive)
        self.assertEqual((report['imported'], report['errors']), (2, 0))
        self.assertEqual(archive.read_log, [])
        self.assertNotIn(SECRET, stored_bytes(self.store))
        contract = next(row for row in self.store.list() if row['title'] == 'PST Vertrag')
        self.assertIn('Anhänge: vertrag.pdf (application/pdf', self.store.get(contract['id'])['content'])


if __name__ == '__main__':
    unittest.main()
