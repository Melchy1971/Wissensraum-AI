"""Synthetic import files for limit tests and the 6.1 reference corpus (no personal data)."""
from __future__ import annotations

import io
import zipfile


def pdf_with_text(text: str, pages: int = 1) -> bytes:
    """Minimal valid PDF with `text` on every page (Helvetica, ASCII only)."""
    escaped = text.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
    content = f'BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET'.encode('latin-1')
    kids = ' '.join(f'{4 + 2 * index} 0 R' for index in range(pages))
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               f'<< /Type /Pages /Kids [{kids}] /Count {pages} >>'.encode(),
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    for index in range(pages):
        objects.append(f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] '
                       f'/Contents {5 + 2 * index} 0 R /Resources << /Font << /F1 3 0 R >> >> >>'.encode())
        objects.append(b'<< /Length %d >>\nstream\n' % len(content) + content + b'\nendstream')
    output = io.BytesIO()
    output.write(b'%PDF-1.4\n')
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(output.tell())
        output.write(b'%d 0 obj\n' % number + body + b'\nendobj\n')
    xref = output.tell()
    output.write(b'xref\n0 %d\n0000000000 65535 f \n' % (len(objects) + 1))
    for offset in offsets:
        output.write(b'%010d 00000 n \n' % offset)
    output.write(b'trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n' % (len(objects) + 1, xref))
    return output.getvalue()


def blank_pdf(pages: int = 1) -> bytes:
    from pypdf import PdfWriter
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def encrypted_pdf(text: str = 'Geheim') -> bytes:
    from pypdf import PdfReader, PdfWriter
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf_with_text(text))))
    writer.encrypt('synthetic-user-password')
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def docx_with_text(text: str) -> bytes:
    from docx import Document
    document = Document()
    document.add_paragraph(text)
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def zip_bomb_docx(uncompressed_bytes: int = 10 * 1024 * 1024) -> bytes:
    """Valid ZIP named like a DOCX whose single entry compresses far beyond the ratio limit."""
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('word/document.xml', b'\0' * uncompressed_bytes)
    return output.getvalue()


def many_entries_docx(entries: int) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_STORED) as archive:
        for index in range(entries):
            archive.writestr(f'x/{index}.txt', b'x')
    return output.getvalue()


def not_a_word_zip() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        archive.writestr('readme.txt', 'kein Word-Dokument')
    return output.getvalue()


def build_reference_corpus(folder) -> dict:
    """Write the 6.1 reference corpus of exactly 1,000 files; return {locator: (status, reason)}.

    Deterministic: same names, contents and expected results on every run.
    """
    from pathlib import Path
    folder = Path(folder)
    expected = {}

    def put(locator, data, status, reason=''):
        path = folder / locator
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        expected[locator] = (status, reason)

    for index in range(175):
        put(f'text/notiz-{index:03}.txt', f'Referenztext {index} zur Freigabe'.encode('utf-8'), 'imported')
    # Formats of Release 2.0.1: 25 readable files each (125 instead of 125 plain text files).
    for index in range(25):
        put(f'web/seite-{index:03}.html', html_doc(f'HTML Referenz {index}', f'Seite {index}'), 'imported')
        put(f'rtf/brief-{index:03}.rtf', rtf_doc(f'RTF Referenz {index} Prüfung'), 'imported')
        put(f'tabellen/liste-{index:03}.xlsx', xlsx_with_text(f'XLSX Referenz {index}'), 'imported')
        put(f'folien/vortrag-{index:03}.pptx', pptx_with_text(f'PPTX Referenz {index}', slides=1), 'imported')
        put(f'mail/nachricht-{index:03}.eml', eml_with_attachment(f'EML Referenz {index}', attachment=None), 'imported')
    for index in range(150):
        put(f'text/md/seite-{index:03}.md', f'# Markdown {index}\nInhalt {index}'.encode('utf-8'), 'imported')
    for index in range(200):
        put(f'pdf/bericht-{index:03}.pdf', pdf_with_text(f'PDF Referenz {index}'), 'imported')
    for index in range(200):
        put(f'docx/vorlage-{index:03}.docx', docx_with_text(f'DOCX Referenz {index}'), 'imported')
    for index in range(10):
        put(f'wiederholt/kopie-{index:03}.txt', f'Referenztext {index} zur Freigabe'.encode('utf-8'), 'duplicate')
    blank = blank_pdf()
    for index in range(40):
        put(f'scan/scan-{index:03}.pdf', blank, 'quarantined', 'no_text')
    for index in range(30):
        suffix = ('png', 'exe', 'ods')[index % 3]  # ods: still unsupported
        put(f'fremd/datei-{index:03}.{suffix}', b'\x89PNG' + bytes([index]),
            'quarantined', 'no_text' if suffix == 'png' else 'unsupported')  # images wait for OCR (6.4)
    encrypted = encrypted_pdf()
    for index in range(10):
        put(f'geschuetzt/vertraulich-{index:03}.pdf', encrypted, 'quarantined', 'encrypted')
    for index in range(10):  # password-protected Office files
        put(f'geschuetzt/kennwort-{index:03}.{("xlsx", "pptx")[index % 2]}', office_cfb(), 'quarantined', 'encrypted')
    for index in range(20):
        put(f'falsch/umbenannt-{index:03}.pdf', f'Kein PDF {index}'.encode('utf-8'), 'quarantined', 'signature')
    broken = not_a_word_zip()
    for index in range(20):
        put(f'kaputt/defekt-{index:03}.docx', broken, 'quarantined', 'damaged')
    bomb = zip_bomb_docx()
    for index in range(10):
        put(f'bomben/bombe-{index:03}.docx', bomb, 'quarantined', 'limits')
    assert len(expected) == 1000
    return expected


# --- OCR fixtures (Phase 6.4) ------------------------------------------------------------
OCR_FONT_CANDIDATES = (
    '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
    '/usr/share/fonts/dejavu/DejaVuSans.ttf',
    r'C:\Windows\Fonts\arial.ttf',
    '/Library/Fonts/Arial.ttf',
)
OCR_GERMAN = ('Die Prüfung der Lieferung ergab größere Mängel am Gehäuse.',
              'Übergabe am Freitag, Rückfragen bitte an Frau Weiß im Büro.',
              'Die Rechnung über 1.250,00 Euro ist bis zum 15. November fällig.')
OCR_ENGLISH = ('The quarterly report shows steady growth in all regions.',
               'Please confirm the delivery date and the invoice number by Monday.',
               'Meeting notes: budget approved, next review in December.')


def ocr_font():
    from pathlib import Path
    return next((path for path in OCR_FONT_CANDIDATES if Path(path).is_file()), None)


def text_page(lines, size=42):
    """A white A4 page at 300 dpi with the given lines in a real font (about 10 pt)."""
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype(ocr_font(), size)
    page = Image.new('L', (2480, 3508), 255)
    draw = ImageDraw.Draw(page)
    for index, line in enumerate(lines):
        draw.text((180, 220 + index * 110), line, font=font, fill=0)
    return page


def image_bytes(image, fmt, **options):
    import io
    output = io.BytesIO()
    image.save(output, format=fmt, dpi=(300, 300), **options)
    return output.getvalue()


def scanned_pdf(pages):
    """A PDF that contains only page images, like a scanner produces: no text layer."""
    import io
    output = io.BytesIO()
    pages = [page.convert('RGB') for page in pages]
    pages[0].save(output, format='PDF', resolution=300, save_all=True, append_images=pages[1:])
    return output.getvalue()


def mixed_pdf(text, scanned_page):
    """First page with a text layer, second page only an image (a partly scanned document)."""
    import io
    from pypdf import PdfReader, PdfWriter
    writer = PdfWriter()
    writer.add_page(PdfReader(io.BytesIO(pdf_with_text(text))).pages[0])
    writer.add_page(PdfReader(io.BytesIO(scanned_pdf([scanned_page]))).pages[0])
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def blank_png(width=200, height=120):
    """A valid, readable image without any text."""
    from PIL import Image
    return image_bytes(Image.new('L', (width, height), 255), 'PNG')


def character_error_rate(expected, actual):
    """Levenshtein distance over whitespace-normalized text, divided by the expected length."""
    expected = ' '.join(expected.split())
    actual = ' '.join(actual.split())
    previous = list(range(len(actual) + 1))
    for i, left in enumerate(expected, start=1):
        current = [i]
        for j, right in enumerate(actual, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (left != right)))
        previous = current
    return previous[-1] / max(1, len(expected))


# --- Formats of Release 2.0.1 (S1) ------------------------------------------------------
def html_doc(text, title='Seite'):
    return (f'<!DOCTYPE html><html><head><title>{title}</title><style>.x{{color:red}}</style>'
            f'<script>var geheim = "Skriptinhalt";</script></head><body><h1>{title}</h1>'
            f'<p>{text}</p><ul><li>Punkt eins</li><li>Punkt zwei</li></ul></body></html>').encode('utf-8')


def rtf_doc(text):
    escaped = ''.join(f"\\'{ord(c):02x}" if ord(c) > 127 else c for c in text)
    return ('{\\rtf1\\ansi\\ansicpg1252\\deff0{\\fonttbl{\\f0 Arial;}}\\f0\\fs22 '
            + escaped + '\\par Zweite Zeile\\par}').encode('latin-1')


def xlsx_with_text(text, rows=3):
    import io
    from openpyxl import Workbook
    book = Workbook()
    sheet = book.active
    sheet.title = 'Umsatz'
    sheet.append(['Monat', 'Betrag', 'Notiz'])
    for index in range(1, rows):
        sheet.append([f'Monat {index}', 1000 * index, text if index == 1 else None])
    second = book.create_sheet('Kontakte')
    second.append(['Name', 'Ort'])
    second.append(['Frau Weiß', 'Köln'])
    output = io.BytesIO()
    book.save(output)
    return output.getvalue()


def pptx_with_text(text, slides=2):
    import io
    from pptx import Presentation
    from pptx.util import Inches
    deck = Presentation()
    for index in range(slides):
        slide = deck.slides.add_slide(deck.slide_layouts[1])
        slide.shapes.title.text = f'Folientitel {index + 1}'
        slide.placeholders[1].text = text if index == 0 else f'Inhalt {index + 1}'
        if index == 0:
            table = slide.shapes.add_table(2, 2, Inches(1), Inches(4), Inches(4), Inches(1)).table
            table.cell(0, 0).text, table.cell(0, 1).text = 'Kennzahl', 'Wert'
            table.cell(1, 0).text, table.cell(1, 1).text = 'Quote', '97'
            slide.notes_slide.notes_text_frame.text = 'Sprechernotiz zur ersten Folie'
    output = io.BytesIO()
    deck.save(output)
    return output.getvalue()


def eml_with_attachment(text, attachment=b'%PDF-1.4 ANHANGSINHALT-EML', name='anlage.pdf'):
    from email import policy
    from email.message import EmailMessage
    message = EmailMessage()
    message['From'] = 'absender@example.test'
    message['To'] = 'markus@example.test'
    message['Subject'] = 'Gespeicherte Nachricht'
    message['Date'] = 'Mon, 26 Oct 2026 08:00:00 +0100'
    message['Message-ID'] = '<gespeichert@example.test>'
    message.set_content(text)
    if attachment is not None:
        message.add_attachment(attachment, maintype='application', subtype='pdf', filename=name)
    return message.as_bytes(policy=policy.SMTP)


def office_cfb():
    """Start of an OLE compound file: what Office writes for password-protected documents."""
    return b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1' + b'\x00' * 504 + b'EncryptedPackage'


def office_bomb(suffix):
    import io
    import zipfile
    main = {'.xlsx': 'xl/workbook.xml', '.pptx': 'ppt/presentation.xml', '.docx': 'word/document.xml'}[suffix]
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', '<Types/>')
        archive.writestr(main, '<x/>')
        archive.writestr('fuellung.xml', b'\0' * (10 * 1024 * 1024))
    return output.getvalue()
