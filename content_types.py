"""Fixed mapping between file extensions and MIME types, identical on every system.

Python's ``mimetypes`` adds entries from the Windows registry or from /etc/mime.types, so the
same file name gave different types on different machines (for example ``.docx`` on a Windows CI
runner without Office: None). The application therefore uses only this table. Unknown extensions
get ``application/octet-stream``.
"""
from __future__ import annotations

from pathlib import PurePath

UNKNOWN = 'application/octet-stream'
_OOXML = 'application/vnd.openxmlformats-officedocument.'
BY_EXTENSION = {
    '.pdf': 'application/pdf',
    '.docx': _OOXML + 'wordprocessingml.document',
    '.xlsx': _OOXML + 'spreadsheetml.sheet',
    '.pptx': _OOXML + 'presentationml.presentation',
    '.doc': 'application/msword',
    '.xls': 'application/vnd.ms-excel',
    '.ppt': 'application/vnd.ms-powerpoint',
    '.odt': 'application/vnd.oasis.opendocument.text',
    '.ods': 'application/vnd.oasis.opendocument.spreadsheet',
    '.odp': 'application/vnd.oasis.opendocument.presentation',
    '.rtf': 'application/rtf',
    '.txt': 'text/plain',
    '.md': 'text/markdown',
    '.csv': 'text/csv',
    '.html': 'text/html',
    '.htm': 'text/html',
    '.xml': 'application/xml',
    '.json': 'application/json',
    '.eml': 'message/rfc822',
    '.msg': 'application/vnd.ms-outlook',
    '.ics': 'text/calendar',
    '.vcf': 'text/vcard',
    '.zip': 'application/zip',
    '.7z': 'application/x-7z-compressed',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.gif': 'image/gif',
    '.bmp': 'image/bmp',
    '.tif': 'image/tiff',
    '.tiff': 'image/tiff',
    '.webp': 'image/webp',
    '.heic': 'image/heic',
    '.svg': 'image/svg+xml',
    '.mp3': 'audio/mpeg',
    '.wav': 'audio/wav',
    '.mp4': 'video/mp4',
}
# First extension listed for a type wins (.htm -> .html, .jpeg -> .jpg).
EXTENSION_FOR = {}
for _extension, _type in BY_EXTENSION.items():
    EXTENSION_FOR.setdefault(_type, _extension)


def content_type_for(name):
    """MIME type from the file extension; ``application/octet-stream`` when unknown."""
    return BY_EXTENSION.get(PurePath(str(name or '')).suffix.lower(), UNKNOWN)


def extension_for(content_type):
    """File extension for a MIME type, or '' when the type is not in the table."""
    return EXTENSION_FOR.get(str(content_type or '').split(';')[0].strip().lower(), '')
