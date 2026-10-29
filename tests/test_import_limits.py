"""Content limits and quarantine reasons of core.import_document (Phase 6.1)."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import (MAX_DOCX_ENTRIES, MAX_PDF_PAGES, QUARANTINE_REASONS, ImportRejected,
                  import_document)
import import_fixtures as fx


class ImportLimitTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def write(self, name, data):
        path = self.folder / name
        path.write_bytes(data)
        return path

    def assertRejected(self, reason, name, data):
        with self.assertRaises(ImportRejected) as raised:
            import_document(self.write(name, data))
        self.assertEqual(raised.exception.reason, reason)
        self.assertIn(reason, QUARANTINE_REASONS)
        self.assertIsInstance(raised.exception, ValueError)  # existing callers keep working

    def test_supported_formats_are_read(self):
        cases = {'a.txt': 'Freigabe'.encode('utf-8'), 'b.md': '# Titel\nInhalt'.encode('utf-8'),
                 'c.txt': '﻿UTF-16 Text'.encode('utf-16'),
                 'd.pdf': fx.pdf_with_text('PDF Freigabe'), 'e.docx': fx.docx_with_text('DOCX Freigabe')}
        for name, data in cases.items():
            with self.subTest(name):
                title, text, filename, original = import_document(self.write(name, data))
                self.assertEqual((filename, original), (name, data))
                self.assertTrue(text.strip())
        self.assertIn('UTF-16 Text', import_document(self.folder / 'c.txt')[1])

    def test_each_quarantine_reason(self):
        self.assertRejected('unsupported', 'x.exe', b'MZ')
        self.assertRejected('signature', 'x.pdf', b'Kein PDF')
        self.assertRejected('signature', 'x.docx', b'Kein ZIP')
        self.assertRejected('signature', 'x.txt', b'bin\x00\x01')
        self.assertRejected('encrypted', 'x.pdf', fx.encrypted_pdf())
        self.assertRejected('damaged', 'y.pdf', b'%PDF-1.4\nkaputt')
        self.assertRejected('damaged', 'y.docx', fx.not_a_word_zip())
        self.assertRejected('damaged', 'z.docx', b'PK\x03\x04abgeschnitten')
        self.assertRejected('no_text', 'leer.pdf', fx.blank_pdf())
        self.assertRejected('no_text', 'leer.txt', b'   \n')
        self.assertRejected('too_large', 'lang.txt', b'a' * 2_000_001)

    def test_file_size_limit_is_checked_before_reading(self):
        path = self.folder / 'gross.pdf'
        with path.open('wb') as file:
            file.truncate(50 * 1024 * 1024 + 1)  # sparse, never read
        with self.assertRaises(ImportRejected) as raised:
            import_document(path)
        self.assertEqual(raised.exception.reason, 'too_large')
        self.assertEqual(str(raised.exception), 'Datei überschreitet das Limit von 50 MB.')
        # A file below the limit is read and judged by its content, not rejected for its size.
        path.write_bytes(b'%PDF-1.4 kaputt' + b'\0' * (36 * 1024 * 1024))
        with self.assertRaises(ImportRejected) as raised:
            import_document(path)
        self.assertEqual(raised.exception.reason, 'damaged')

    def test_decompression_and_page_limits(self):
        self.assertRejected('limits', 'bombe.docx', fx.zip_bomb_docx())
        self.assertRejected('limits', 'viele.docx', fx.many_entries_docx(MAX_DOCX_ENTRIES + 1))
        self.assertRejected('limits', 'seiten.pdf', fx.blank_pdf(MAX_PDF_PAGES + 1))


if __name__ == '__main__':
    unittest.main()
