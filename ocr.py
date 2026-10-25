"""Optional local text recognition with Tesseract (Phase 6.4, decision D-009).

OCR is used only when pytesseract, pypdfium2 and the Tesseract program are installed.
Without them, images and scans stay in quarantine ("kein lesbarer Text") as before.
Everything runs locally: images go to the Tesseract program on this computer, never
to a network service. ``WISSENSRAUM_OCR=0`` switches OCR off; ``WISSENSRAUM_TESSERACT``
names the program when it is not on the search path.
"""
from __future__ import annotations

import io
import os
import shutil
from functools import lru_cache
from pathlib import Path

WANTED_LANGUAGES = ('deu', 'eng')
OCR_SUFFIXES = frozenset({'.png', '.jpg', '.jpeg', '.tif', '.tiff', '.bmp', '.gif', '.webp'})
MAX_OCR_PAGES = 200
OCR_PAGE_TIMEOUT_SECONDS = 120
MAX_OCR_PIXELS = 60_000_000  # about A3 at 600 dpi; larger images are rejected before decoding
RENDER_DPI = 300
MARKER = '[OCR · Tesseract]'
WINDOWS_DEFAULT = Path(r'C:\Program Files\Tesseract-OCR\tesseract.exe')


def _program():
    configured = os.environ.get('WISSENSRAUM_TESSERACT', '').strip()
    if configured:
        return configured if Path(configured).is_file() else None
    found = shutil.which('tesseract')
    if found:
        return found
    return str(WINDOWS_DEFAULT) if os.name == 'nt' and WINDOWS_DEFAULT.is_file() else None


@lru_cache(maxsize=4)
def _status(switch, program_hint):
    if switch == '0':
        return {'available': False, 'reason': 'disabled', 'message': 'OCR ist mit WISSENSRAUM_OCR=0 abgeschaltet.',
                'version': '', 'languages': []}
    try:
        import pytesseract
        import pypdfium2  # noqa: F401  (needed for scanned PDFs)
    except ImportError:
        return {'available': False, 'reason': 'packages', 'version': '', 'languages': [],
                'message': 'OCR-Erweiterung nicht installiert; install-ocr-support.bat ausführen (optional).'}
    program = _program()
    if not program:
        return {'available': False, 'reason': 'program_missing', 'version': '', 'languages': [],
                'message': 'Tesseract-Programm nicht gefunden; Tesseract installieren (optional).'}
    pytesseract.pytesseract.tesseract_cmd = program
    try:
        version = str(pytesseract.get_tesseract_version())
        languages = sorted(set(pytesseract.get_languages(config='')) & set(WANTED_LANGUAGES))
    except Exception:
        return {'available': False, 'reason': 'program', 'version': '', 'languages': [],
                'message': 'Tesseract konnte nicht gestartet werden.'}
    if not languages:
        return {'available': False, 'reason': 'languages', 'version': version, 'languages': [],
                'message': 'Tesseract ohne Sprachdaten für Deutsch oder Englisch.'}
    missing = [language for language in WANTED_LANGUAGES if language not in languages]
    message = f'OCR verfügbar (Tesseract {version}, Sprachen {"+".join(languages)}).'
    if missing:
        message += ' Fehlende Sprache: ' + ', '.join(missing) + '.'
    return {'available': True, 'reason': 'ok' if not missing else 'languages', 'message': message,
            'version': version, 'languages': languages}


def status():
    """Availability, Tesseract version and installed languages; cached per configuration."""
    return dict(_status(os.environ.get('WISSENSRAUM_OCR', 'auto').strip(), _program()))


def available():
    return status()['available']


def _rejected(reason, message):
    from core import ImportRejected
    return ImportRejected(reason, message)


def _recognize(image):
    import pytesseract
    current = status()
    if image.width * image.height > MAX_OCR_PIXELS:
        raise _rejected('limits', 'Bild ist für die Texterkennung zu groß.')
    try:
        return pytesseract.image_to_string(image.convert('L'), lang='+'.join(current['languages']),
                                           timeout=OCR_PAGE_TIMEOUT_SECONDS)
    except RuntimeError as error:  # pytesseract raises RuntimeError on timeout
        raise _rejected('limits', 'Texterkennung hat die Zeitgrenze je Seite überschritten.') from error


def _labelled(pages):
    pages = [text.strip() for text in pages]
    if not any(pages):
        raise _rejected('no_text', 'Die Texterkennung (OCR) hat keine Schrift gefunden.')
    if len(pages) == 1:
        return MARKER + '\n' + pages[0]
    return MARKER + '\n' + '\n\n'.join(f'[Seite {number}]\n{text}' for number, text in enumerate(pages, start=1))


def image_text(data):
    """Recognize text in an image file (multi-page TIFF included)."""
    from PIL import Image, ImageSequence, UnidentifiedImageError
    try:
        image = Image.open(io.BytesIO(data))
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise _rejected('damaged', 'Bild ist beschädigt oder nicht lesbar.') from None
    if image.width * image.height > MAX_OCR_PIXELS:  # checked before the pixels are decoded
        raise _rejected('limits', 'Bild ist für die Texterkennung zu groß.')
    pages = []
    try:
        for number, frame in enumerate(ImageSequence.Iterator(image), start=1):
            if number > MAX_OCR_PAGES:
                raise _rejected('limits', f'Bild hat mehr als {MAX_OCR_PAGES} Seiten.')
            pages.append(_recognize(frame))
    except Exception as error:
        from core import ImportRejected
        if isinstance(error, ImportRejected):  # our own limits, not a damaged file
            raise
        if isinstance(error, (OSError, ValueError, Image.DecompressionBombError)):
            raise _rejected('damaged', 'Bild ist beschädigt oder nicht lesbar.') from None
        raise
    return _labelled(pages)


def image_pages(data):
    """Number of pages (frames) without decoding pixels; damaged images raise like image_text."""
    from PIL import Image, UnidentifiedImageError
    try:
        image = Image.open(io.BytesIO(data))
        return max(1, int(getattr(image, 'n_frames', 1)))
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise _rejected('damaged', 'Bild ist beschädigt oder nicht lesbar.') from None


def pdf_text(data):
    """Render each page of a PDF without text layer and recognize it."""
    import pypdfium2
    try:
        document = pypdfium2.PdfDocument(data)
    except Exception:
        raise _rejected('damaged', 'PDF ist beschädigt oder nicht lesbar.') from None
    try:
        if len(document) > MAX_OCR_PAGES:
            raise _rejected('limits', f'PDF hat für die Texterkennung mehr als {MAX_OCR_PAGES} Seiten.')
        pages = []
        for index in range(len(document)):
            page = document[index]
            width, height = page.get_size()
            scale = RENDER_DPI / 72
            if width * scale * height * scale > MAX_OCR_PIXELS:
                raise _rejected('limits', 'PDF-Seite ist für die Texterkennung zu groß.')
            pages.append(_recognize(page.render(scale=scale).to_pil()))
        return _labelled(pages)
    finally:
        document.close()
