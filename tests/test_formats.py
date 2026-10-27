"""HTML, RTF, XLSX, PPTX and EML import (Release 2.0.1, step S1)."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import formats
import import_fixtures as fx
from core import ImportRejected, extract_document


class FormatTests(unittest.TestCase):
    def rejected(self, name, data):
        with self.assertRaises(ImportRejected) as raised:
            extract_document(name, data)
        return raised.exception.reason

    def test_html_text_without_script_and_style_and_with_page_title(self):
        title, text, name, data = extract_document('seite.html', fx.html_doc('Lieferung am Montag', 'Projektseite'))
        self.assertEqual(title, 'Projektseite')
        self.assertIn('Lieferung am Montag', text)
        self.assertIn('Punkt zwei', text)
        self.assertNotIn('Skriptinhalt', text)
        self.assertNotIn('color:red', text)
        self.assertEqual(extract_document('alt.htm', fx.html_doc('Alte Endung'))[1].count('Alte Endung'), 1)

    def test_rtf_text_with_umlauts(self):
        _, text, _, _ = extract_document('brief.rtf', fx.rtf_doc('Prüfung der Mängel'))
        self.assertIn('Prüfung der Mängel', text)
        self.assertIn('Zweite Zeile', text)

    def test_xlsx_text_per_sheet_with_values(self):
        _, text, _, _ = extract_document('umsatz.xlsx', fx.xlsx_with_text('Sonderposten'))
        self.assertIn('[Tabelle Umsatz]', text)
        self.assertIn('Monat 1 | 1000 | Sonderposten', text)
        self.assertIn('[Tabelle Kontakte]', text)
        self.assertIn('Frau Weiß | Köln', text)

    def test_pptx_text_per_slide_with_table_and_notes(self):
        _, text, _, _ = extract_document('vortrag.pptx', fx.pptx_with_text('Kernaussage des Vortrags'))
        self.assertIn('[Folie 1]', text)
        self.assertIn('Folientitel 1', text)
        self.assertIn('Kernaussage des Vortrags', text)
        self.assertIn('Kennzahl | Wert', text)
        self.assertIn('Notizen: Sprechernotiz zur ersten Folie', text)
        self.assertIn('[Folie 2]', text)

    def test_eml_lists_attachments_and_stores_the_original_without_them(self):
        raw = fx.eml_with_attachment('Bitte beachten Sie die Anlage.')
        title, text, name, stored = extract_document('nachricht.eml', raw)
        self.assertEqual(title, 'Gespeicherte Nachricht')
        self.assertIn('Von: absender@example.test', text)
        self.assertIn('Anhänge: anlage.pdf (application/pdf', text)
        self.assertIn('Bitte beachten Sie die Anlage.', text)
        self.assertNotIn(b'ANHANGSINHALT-EML', stored)
        import base64
        self.assertNotIn(base64.b64encode(b'%PDF-1.4 ANHANGSINHALT-EML')[:20], stored)
        self.assertIn(b'X-Wissensraum-Anhaenge-nicht-gespeichert: 1', stored)
        plain = fx.eml_with_attachment('Nur Text', attachment=None)
        self.assertEqual(extract_document('nur.eml', plain)[3], plain)  # nothing to remove: unchanged

    def test_wrong_extension_is_a_signature_mismatch(self):
        for name, data in (('falsch.xlsx', fx.docx_with_text('Word')), ('falsch.pptx', fx.xlsx_with_text('x')),
                           ('falsch.docx', fx.pptx_with_text('x')), ('falsch.rtf', b'Kein RTF'),
                           ('falsch.html', b'\x00\x01binaer'), ('falsch.htm', b'nur Text ohne Auszeichnung'),
                           ('falsch.eml', b'\x89PNG\r\n\x1a\n\x00\x00'), ('falsch.xlsx', b'Kein Archiv')):
            with self.subTest(name=name):
                self.assertEqual(self.rejected(name, data), 'signature')

    def test_damaged_files(self):
        for name, data in (('kaputt.xlsx', fx.xlsx_with_text('x')[:400]), ('kaputt.pptx', fx.pptx_with_text('x')[:600]),
                           ('ohne-inhalt.xlsx', fx.not_a_word_zip()), ('ohne-inhalt.pptx', fx.not_a_word_zip()),
                           ('ohne-inhalt.docx', fx.not_a_word_zip())):
            with self.subTest(name=name):
                self.assertEqual(self.rejected(name, data), 'damaged')

    def test_decompression_bombs(self):
        for suffix in ('.xlsx', '.pptx', '.docx'):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.rejected('bombe' + suffix, fx.office_bomb(suffix)), 'limits')

    def test_password_protected_office_files(self):
        for suffix in ('.xlsx', '.pptx', '.docx'):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.rejected('geschuetzt' + suffix, fx.office_cfb()), 'encrypted')

    def test_cell_and_slide_limits(self):
        with patch.object(formats, 'MAX_SPREADSHEET_CELLS', 5):
            self.assertEqual(self.rejected('gross.xlsx', fx.xlsx_with_text('x', rows=10)), 'limits')
        with patch.object(formats, 'MAX_SLIDES', 2):
            self.assertEqual(self.rejected('lang.pptx', fx.pptx_with_text('x', slides=3)), 'limits')

    def test_empty_documents_have_no_text(self):
        from openpyxl import Workbook
        import io
        empty = io.BytesIO(); Workbook().save(empty)
        self.assertEqual(self.rejected('leer.xlsx', empty.getvalue()), 'no_text')
        self.assertEqual(self.rejected('leer.html', b'<html><head><title>T</title></head><body></body></html>'), 'no_text')
        self.assertEqual(self.rejected('leer.eml', b'Subject: Leer\r\n\r\n'), 'no_text')

    def test_unsupported_message_names_all_formats(self):
        with self.assertRaises(ImportRejected) as raised:
            extract_document('tabelle.ods', b'x')
        self.assertIn('HTML, RTF, XLSX, PPTX und EML', str(raised.exception))


if __name__ == '__main__':
    unittest.main()
