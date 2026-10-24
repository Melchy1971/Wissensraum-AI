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

    for index in range(300):
        put(f'text/notiz-{index:03}.txt', f'Referenztext {index} zur Freigabe'.encode('utf-8'), 'imported')
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
        suffix = ('png', 'exe', 'xlsx')[index % 3]
        put(f'fremd/datei-{index:03}.{suffix}', b'\x89PNG' + bytes([index]),
            'quarantined', 'no_text' if suffix == 'png' else 'unsupported')  # images wait for OCR (6.4)
    encrypted = encrypted_pdf()
    for index in range(20):
        put(f'geschuetzt/vertraulich-{index:03}.pdf', encrypted, 'quarantined', 'encrypted')
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
