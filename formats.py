"""Text extraction for HTML, RTF, XLSX, PPTX and EML files (Release 2.0.1, step S1).

Shared with the mailbox import: ``html_text``, ``decoded``, ``email_body`` and
``attachment_line`` live here so that core and importer use the same code.
Every function raises ``core.ImportRejected`` with a quarantine reason for files it
cannot read; nothing here writes to the database.
"""
from __future__ import annotations

import io
import warnings
import zipfile
from email import policy
from email.header import decode_header, make_header
from email.parser import BytesParser
from html.parser import HTMLParser

# Header of the OLE compound file format: password-protected Office files (and old .doc/.xls).
CFB_SIGNATURE = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'
MAX_SPREADSHEET_CELLS = 1_000_000
MAX_SLIDES = 2000
OOXML_MAIN_PART = {'.docx': 'word/document.xml', '.xlsx': 'xl/workbook.xml', '.pptx': 'ppt/presentation.xml'}


def _rejected(reason, message):
    from core import ImportRejected
    return ImportRejected(reason, message)


# --- shared with the mailbox import ------------------------------------------------------
class _HTMLText(HTMLParser):
    SKIP = {'script', 'style', 'noscript', 'template', 'head'}
    BLOCK = {'p', 'div', 'br', 'li', 'tr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'table', 'section', 'article'}

    def __init__(self):
        super().__init__()
        self.parts = []
        self.skipping = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skipping += 1
        elif tag in self.BLOCK:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skipping:
            self.skipping -= 1

    def handle_data(self, data):
        if not self.skipping and data.strip():
            self.parts.append(data.strip())


def html_text(value):
    parser = _HTMLText()
    parser.feed(value or '')
    lines = ' '.join(parser.parts).split('\n')
    return '\n'.join(' '.join(line.split()) for line in lines if line.strip())


def decoded(value):
    try:
        return str(make_header(decode_header(value or ''))).strip()
    except (LookupError, UnicodeError):
        return str(value or '').strip()


def email_body(message):
    plain = []
    html = []
    parts = message.walk() if message.is_multipart() else (message,)
    for part in parts:
        if part.get_content_disposition() == 'attachment':
            continue
        kind = part.get_content_type()
        if kind not in ('text/plain', 'text/html'):
            continue
        try:
            value = part.get_content()
        except (LookupError, UnicodeError):
            payload = part.get_payload(decode=True) or b''
            value = payload.decode(part.get_content_charset() or 'utf-8', errors='replace')
        (plain if kind == 'text/plain' else html).append(str(value))
    text = '\n\n'.join(plain).strip()
    return text or html_text('\n'.join(html))


def attachment_line(attachments):
    """Readable list of attachment names for the e-mail text; content is never included."""
    if not attachments:
        return ''
    return 'Anhänge: ' + '; '.join(f'{item["name"]} ({item.get("content_type") or "unbekannt"}, '
                                    f'{item.get("size") or 0} Bytes)' for item in attachments)


# --- plain text ----------------------------------------------------------------------------
def decode_text(data, label='Textdatei'):
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        try:
            return data.decode('utf-16')
        except UnicodeDecodeError:
            raise _rejected('damaged', 'Textkodierung ist nicht lesbar.') from None
    if b'\x00' in data:
        raise _rejected('signature', f'{label} enthält Binärdaten.')
    try:
        return data.decode('utf-8-sig')
    except UnicodeDecodeError:
        try:
            return data.decode('cp1252')
        except UnicodeDecodeError:
            raise _rejected('damaged', 'Textkodierung ist nicht lesbar.') from None


# --- HTML and RTF ----------------------------------------------------------------------------
def html_document(data):
    text = decode_text(data, 'HTML-Datei')
    if '<' not in text[:4096]:
        raise _rejected('signature', 'Datei enthält kein HTML.')
    title = ''
    lower = text.lower()
    start = lower.find('<title')
    if start >= 0:
        start = lower.find('>', start) + 1
        end = lower.find('</title>', start)
        if 0 < start <= end:
            title = ' '.join(html_text(text[start:end]).split())
    return title, html_text(text)


def rtf_document(data):
    if not data.lstrip()[:5] == b'{\\rtf':
        raise _rejected('signature', 'Datei ist kein RTF-Dokument.')
    from striprtf.striprtf import rtf_to_text
    try:
        return rtf_to_text(data.decode('latin-1'), encoding='cp1252', errors='replace')
    except Exception:
        raise _rejected('damaged', 'RTF-Datei ist beschädigt oder nicht lesbar.') from None


# --- Office Open XML containers ------------------------------------------------------------
def office_container(data, suffix):
    """Signature, password protection, decompression limits and main part of DOCX/XLSX/PPTX."""
    label = suffix.lstrip('.').upper()
    if data.startswith(CFB_SIGNATURE):
        raise _rejected('encrypted', f'{label}-Datei ist mit Kennwort geschützt oder hat ein altes Office-Format.')
    if not data.startswith(b'PK\x03\x04'):
        raise _rejected('signature', f'Datei ist kein {label}-Dokument.')
    from core import _zip_within_limits
    _zip_within_limits(data, label)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
    except zipfile.BadZipFile:
        raise _rejected('damaged', f'{label}-Datei ist beschädigt.') from None
    if OOXML_MAIN_PART[suffix] not in names:
        if names & set(OOXML_MAIN_PART.values()):  # another Office format under the wrong extension
            raise _rejected('signature', f'Datei ist kein {label}-Dokument.')
        raise _rejected('damaged', f'{label}-Datei ist beschädigt oder unvollständig.')


def _cell_text(value):
    if value is None:
        return ''
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def xlsx_document(data):
    office_container(data, '.xlsx')
    from openpyxl import load_workbook
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:
        raise _rejected('damaged', 'XLSX-Datei ist beschädigt oder nicht lesbar.') from None
    sections, cells = [], 0
    try:
        for sheet in workbook.worksheets:
            lines = []
            for row in sheet.iter_rows(values_only=True):
                cells += len(row)
                if cells > MAX_SPREADSHEET_CELLS:
                    raise _rejected('limits', f'Tabelle hat mehr als {MAX_SPREADSHEET_CELLS} Zellen.')
                values = [_cell_text(value) for value in row]
                if any(values):
                    lines.append(' | '.join(values).rstrip(' |'))
            if lines:
                sections.append(f'[Tabelle {sheet.title}]\n' + '\n'.join(lines))
    except Exception as error:
        from core import ImportRejected
        if isinstance(error, ImportRejected):
            raise
        raise _rejected('damaged', 'XLSX-Datei ist beschädigt oder nicht lesbar.') from None
    finally:
        workbook.close()
    return '\n\n'.join(sections)


def _shape_texts(shapes):
    for shape in shapes:
        if getattr(shape, 'shape_type', None) == 6 or hasattr(shape, 'shapes'):  # group
            yield from _shape_texts(shape.shapes)
            continue
        if getattr(shape, 'has_text_frame', False) and shape.text_frame.text.strip():
            yield shape.text_frame.text.strip()
        if getattr(shape, 'has_table', False):
            for row in shape.table.rows:
                values = [cell.text.strip() for cell in row.cells]
                if any(values):
                    yield ' | '.join(values)


def pptx_document(data):
    office_container(data, '.pptx')
    from pptx import Presentation
    try:
        presentation = Presentation(io.BytesIO(data))
        slides = list(presentation.slides)
    except Exception:
        raise _rejected('damaged', 'PPTX-Datei ist beschädigt oder nicht lesbar.') from None
    if len(slides) > MAX_SLIDES:
        raise _rejected('limits', f'Präsentation hat mehr als {MAX_SLIDES} Folien.')
    sections = []
    try:
        for number, slide in enumerate(slides, start=1):
            lines = list(_shape_texts(slide.shapes))
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
                notes = slide.notes_slide.notes_text_frame.text.strip()
                if notes:
                    lines.append('Notizen: ' + notes)
            if lines:
                sections.append(f'[Folie {number}]\n' + '\n'.join(lines))
    except Exception:
        raise _rejected('damaged', 'PPTX-Datei ist beschädigt oder nicht lesbar.') from None
    return '\n\n'.join(sections)


# --- EML -------------------------------------------------------------------------------------
def eml_document(data):
    """Text of a saved e-mail; attachments are listed by name and left out of the stored original."""
    message = BytesParser(policy=policy.default).parsebytes(data)
    if b'\x00' in data[:4096] or not any(message.get(name) for name in ('From', 'Subject', 'Date', 'Message-ID')):
        raise _rejected('signature', 'Datei ist keine E-Mail.')
    try:
        attachments = [{'name': part.get_filename() or 'Anhang', 'content_type': part.get_content_type(),
                        'size': len(part.get_payload(decode=True) or b'')} for part in message.iter_attachments()]
        body = email_body(message)
    except Exception:
        raise _rejected('damaged', 'E-Mail-Datei ist beschädigt oder nicht lesbar.') from None
    subject = decoded(message.get('Subject')) or ''
    lines = [('Betreff: ' + subject) if subject else '',
             'Von: ' + decoded(message.get('From')) if message.get('From') else '',
             'An: ' + decoded(message.get('To')) if message.get('To') else '',
             'Datum: ' + decoded(message.get('Date')) if message.get('Date') else '',
             'Nachrichten-ID: ' + decoded(message.get('Message-ID')) if message.get('Message-ID') else '',
             attachment_line(attachments), '', body or '(Kein Nachrichtentext)']
    if not body and not attachments:
        raise _rejected('no_text', 'E-Mail enthält keinen lesbaren Nachrichtentext.')
    from mail_cleanup import strip_message
    stripped, _ = strip_message(data)
    return subject, '\n'.join(filter(None, lines)), stripped or data
