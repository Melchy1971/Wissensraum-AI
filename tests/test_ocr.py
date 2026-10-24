"""Optional local OCR with Tesseract (Phase 6.4).

Recognition tests need Tesseract with German and English data; they are skipped without it,
unless WISSENSRAUM_REQUIRE_OCR=1 (Linux CI), where a missing OCR fails the run.
"""
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import import_fixtures as fx
import ocr
from core import ImportRejected, Store, extract_document
from diagnostics import check_ocr
from import_jobs import ImportJobs

REQUIRED = os.environ.get('WISSENSRAUM_REQUIRE_OCR') == '1'
MAX_CER = 0.05


def ready():
    state = ocr.status()
    return state['available'] and set(state['languages']) == {'deu', 'eng'} and fx.ocr_font()


class OcrWithoutTesseractTests(unittest.TestCase):
    """Behaviour without OCR: unchanged quarantine; status explains the optional extension."""

    def test_disabled_ocr_keeps_images_and_scans_in_quarantine(self):
        with patch.dict(os.environ, {'WISSENSRAUM_OCR': '0'}):
            self.assertFalse(ocr.available())
            for name, data in (('scan.png', fx.blank_png()), ('scan.pdf', fx.blank_pdf())):
                with self.assertRaises(ImportRejected) as raised:
                    extract_document(name, data)
                self.assertEqual(raised.exception.reason, 'no_text')
                self.assertIn('OCR', str(raised.exception))
            result = check_ocr()
        self.assertEqual(result.status, 'PASS')
        self.assertIn('optional', result.message)

    def test_status_messages_for_each_state(self):
        missing = {'available': False, 'reason': 'packages', 'version': '', 'languages': [],
                   'message': 'OCR-Erweiterung nicht installiert.'}
        broken = dict(missing, reason='program', message='Tesseract konnte nicht gestartet werden.')
        no_program = dict(missing, reason='program_missing', message='Tesseract-Programm nicht gefunden.')
        partial = {'available': True, 'reason': 'languages', 'version': '5.3.0', 'languages': ['eng'],
                   'message': 'OCR verfügbar. Fehlende Sprache: deu.'}
        full = dict(partial, reason='ok', languages=['deu', 'eng'], message='OCR verfügbar.')
        self.assertEqual(check_ocr(lambda: missing).status, 'PASS')
        self.assertEqual(check_ocr(lambda: no_program).status, 'PASS')  # packages only: still a valid setup
        self.assertEqual(check_ocr(lambda: broken).status, 'WARN')
        self.assertEqual(check_ocr(lambda: partial).status, 'WARN')
        result = check_ocr(lambda: full)
        self.assertEqual((result.status, result.details), ('PASS', {'version': '5.3.0', 'sprachen': 'deu+eng'}))

    def test_missing_program_is_reported_not_raised(self):
        with patch.dict(os.environ, {'WISSENSRAUM_OCR': 'auto', 'WISSENSRAUM_TESSERACT': '/nicht/vorhanden/tesseract'}):
            state = ocr.status()
        self.assertFalse(state['available'])
        self.assertIn(state['reason'], ('program_missing', 'packages'))


@unittest.skipUnless(ready() or REQUIRED, 'Tesseract mit Deutsch und Englisch nicht installiert')
class OcrRecognitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not ready():
            raise AssertionError('WISSENSRAUM_REQUIRE_OCR=1, aber Tesseract mit deu+eng oder Schrift fehlt: '
                                 + ocr.status()['message'])

    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(self.base / 'wissen.db')
        self.jobs = ImportJobs(self.store)

    def assertRecognized(self, text, lines):
        body = text.replace(ocr.MARKER, '')
        for marker in ('[Seite 1]', '[Seite 2]'):
            body = body.replace(marker, '')
        rate = fx.character_error_rate(' '.join(lines), body)
        self.assertLessEqual(rate, MAX_CER, f'Zeichenfehlerrate {rate:.1%}:\n{body}')
        return rate

    def test_status_reports_version_and_both_languages(self):
        state = ocr.status()
        self.assertTrue(state['version'])
        self.assertEqual(state['languages'], ['deu', 'eng'])
        self.assertEqual(check_ocr().status, 'PASS')

    def test_german_and_english_images_at_300_dpi_within_5_percent(self):
        lines = fx.OCR_GERMAN + fx.OCR_ENGLISH
        page = fx.text_page(lines)
        for name, data in (('seite.png', fx.image_bytes(page, 'PNG')),
                           ('seite.jpg', fx.image_bytes(page, 'JPEG', quality=85)),
                           ('seite.tif', fx.image_bytes(page, 'TIFF', compression='tiff_lzw'))):
            with self.subTest(name=name):
                title, text, stored_name, stored = extract_document(name, data)
                self.assertTrue(text.startswith(ocr.MARKER))
                self.assertEqual((stored_name, stored), (name, data))
                self.assertRecognized(text, lines)

    def test_scanned_pdf_and_multipage_tiff_are_read_page_by_page(self):
        first, second = fx.text_page(fx.OCR_GERMAN), fx.text_page(fx.OCR_ENGLISH)
        pdf = fx.scanned_pdf([first, second])
        from pypdf import PdfReader
        import io
        self.assertFalse(any((page.extract_text() or '').strip() for page in PdfReader(io.BytesIO(pdf)).pages))
        _, text, _, _ = extract_document('scan.pdf', pdf)
        self.assertIn('[Seite 2]', text)
        self.assertRecognized(text, fx.OCR_GERMAN + fx.OCR_ENGLISH)
        tiff = fx.image_bytes(first, 'TIFF', save_all=True, append_images=[second], compression='tiff_lzw')
        _, text, _, _ = extract_document('scan.tiff', tiff)
        self.assertRecognized(text, fx.OCR_GERMAN + fx.OCR_ENGLISH)

    def test_blank_scan_stays_quarantined_and_broken_image_is_damaged(self):
        for name, data, reason in (('leer.png', fx.blank_png(), 'no_text'), ('leer.pdf', fx.blank_pdf(), 'no_text'),
                                   ('kaputt.png', b'\x89PNG\r\n\x1a\nkaputt', 'damaged')):
            with self.subTest(name=name), self.assertRaises(ImportRejected) as raised:
                extract_document(name, data)
            self.assertEqual(raised.exception.reason, reason)

    def test_limits_for_pixels_pages_and_time(self):
        from PIL import Image
        huge = fx.image_bytes(Image.new('1', (10000, 7000), 1), 'PNG')
        with patch('PIL.Image.Image.load', side_effect=AssertionError('decoded despite the pixel limit')):
            with self.assertRaises(ImportRejected) as raised:
                extract_document('riesig.png', huge)
        self.assertEqual(raised.exception.reason, 'limits')
        page = fx.text_page(fx.OCR_ENGLISH[:1])
        tiff = fx.image_bytes(page, 'TIFF', save_all=True, append_images=[page, page], compression='tiff_lzw')
        with patch.object(ocr, 'MAX_OCR_PAGES', 2), self.assertRaises(ImportRejected) as raised:
            extract_document('drei.tif', tiff)
        self.assertEqual(raised.exception.reason, 'limits')
        with patch.object(ocr, 'OCR_PAGE_TIMEOUT_SECONDS', 0.001), self.assertRaises(ImportRejected) as raised:
            extract_document('seite.png', fx.image_bytes(page, 'PNG'))
        self.assertEqual(raised.exception.reason, 'limits')

    def test_ocr_opens_no_network_connection(self):
        attempts = []
        def refuse(*args, **kwargs):
            attempts.append(args)
            raise AssertionError('network connection during OCR')
        page = fx.text_page(fx.OCR_GERMAN)
        with patch.object(socket.socket, 'connect', refuse), patch.object(socket.socket, 'connect_ex', refuse), \
                patch.object(socket, 'create_connection', refuse), patch.object(socket, 'getaddrinfo', refuse):
            _, text, _, _ = extract_document('seite.png', fx.image_bytes(page, 'PNG'))
            extract_document('scan.pdf', fx.scanned_pdf([page]))
        self.assertEqual(attempts, [])
        self.assertRecognized(text, fx.OCR_GERMAN)

    def test_quarantined_scans_are_retried_once_after_ocr_without_duplicates(self):
        folder = self.base / 'quelle'
        folder.mkdir()
        (folder / 'brief.png').write_bytes(fx.image_bytes(fx.text_page(fx.OCR_GERMAN), 'PNG'))
        (folder / 'scan.pdf').write_bytes(fx.scanned_pdf([fx.text_page(fx.OCR_ENGLISH)]))
        (folder / 'leer.png').write_bytes(fx.blank_png())
        (folder / 'notiz.txt').write_text('Schon lesbar', encoding='utf-8')
        with patch.dict(os.environ, {'WISSENSRAUM_OCR': '0'}):
            first = self.jobs.run(self.jobs.create_for_folder(folder))
        self.assertEqual((first['imported'], first['quarantined']), (1, 3))
        retry_ids = self.jobs.retry_quarantined('no_text')
        self.assertEqual(len(retry_ids), 1)
        self.assertEqual(sorted(item['locator'] for item in self.jobs.items(retry_ids[0])),
                         ['brief.png', 'leer.png', 'scan.pdf'])
        summary = self.jobs.run(retry_ids[0])
        self.assertEqual((summary['imported'], summary['quarantined']), (2, 1))
        documents = {row['title']: self.store.get(row['id'])['content'] for row in self.store.list()}
        self.assertEqual(set(documents), {'notiz', 'brief', 'scan'})
        self.assertTrue(documents['brief'].startswith(ocr.MARKER))
        self.assertEqual(sorted(locator for group in self.jobs.retry_candidates('no_text').values()
                                for _, locator in group), ['leer.png'])
        again = self.jobs.run(self.jobs.retry_quarantined('no_text')[0])
        self.assertEqual((again['total'], again['quarantined']), (1, 1))
        self.assertEqual(len(self.store.list(all_versions=True)), 3)

    def test_released_image_attachment_is_read_with_ocr(self):
        from importer import Importer, MailAccounts
        from mail_fakes import FakeImapServer, mail
        from test_settings import FakeVault
        accounts = MailAccounts(self.store, FakeVault(), self.base)
        account_id = accounts.save({'label': 'Arbeit', 'provider': 'imap', 'email': 'm@example.test',
                                    'username': 'm', 'host': 'imap.example.test', 'port': 993}, 'geheim')
        scan = fx.image_bytes(fx.text_page(fx.OCR_GERMAN), 'PNG')
        server = FakeImapServer({5: mail('Scan', 'Scan anbei.', [('scan.png', 'image/png', scan)])})
        importer = Importer(self.store, accounts)
        report = importer.import_imap_ids(account_id, 'INBOX', ['5'], server.client, attachments={'5': ['2']})
        self.assertEqual(report['attachments_imported'], 1)
        with self.store.connect() as db:
            document_id = db.execute('SELECT document_id FROM email_attachments').fetchone()['document_id']
        self.assertRecognized(self.store.get(document_id)['content'], fx.OCR_GERMAN)


if __name__ == '__main__':
    unittest.main()
