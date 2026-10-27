"""Read Outlook PST files with libpff (package ``libpff-python``, module ``pypff``, LGPL-3.0-or-later).

Replaces ``libratom.lib.pff.PffArchive``: libratom 0.7.1 (last release 2022) pins dozens of old
packages, among them numpy 1.23.5, which has no wheels for Python 3.12 and 3.13 and cannot be built
there. Only the part of libratom that the import used is kept here, with the same interface:
``messages()``, ``get_message_by_id()``, ``format_message()``, ``get_attachment_metadata()`` and use as
a context manager. Attachment content is read only by the import job, never here.
"""
from __future__ import annotations

import mimetypes
from collections import deque, namedtuple
from pathlib import Path

AttachmentMetadata = namedtuple('AttachmentMetadata', 'name mime_type size')
DROPPED_HEADERS = {'content-type', 'content-transfer-encoding', 'mime-version'}


def attachment_name(attachment):
    """File name of an attachment: ``long_filename`` in libpff-python, ``name`` in libratom's fork."""
    for field in ('name', 'long_filename'):
        try:
            value = getattr(attachment, field, None)
        except OSError:  # libpff raises when the record is missing
            value = None
        if value:
            return _text(value)
    return ''


def _fallback_headers(message, headers):
    """Subject, sender and date from the PST record when transport headers lack them.

    Messages written in Outlook itself (sent items, drafts) usually have no transport headers.
    """
    from email.header import Header
    from email.utils import format_datetime
    present = {name.lower() for name in headers.keys()} if headers else set()
    lines = ''
    for field, attribute in (('Subject', 'subject'), ('From', 'sender_name')):
        try:
            value = _text(getattr(message, attribute, None)).strip()
        except OSError:
            value = ''
        if value and field.lower() not in present:
            lines += f'{field}: {Header(value, "utf-8").encode()}\r\n'
    if 'date' not in present:
        for attribute in ('delivery_time', 'client_submit_time'):
            try:
                value = getattr(message, attribute, None)
            except (OSError, ValueError):
                value = None
            if value is not None and hasattr(value, 'year') and value.year > 1601:
                lines += f'Date: {format_datetime(value)}\r\n'
                break
    return lines


def _text(value):
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='replace')
    return value or ''


class PstArchive:
    def __init__(self, path, pff=None):
        if pff is None:
            import pypff as pff
        self.filepath = str(path)
        self._file = pff.file()
        self._file.open(str(Path(path)), 'rb')
        self._index = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        try:
            self._file.close()
        except Exception:
            pass

    def folders(self):
        """All folders breadth first, starting at the root folder."""
        pending = deque([self._file.root_folder])
        while pending:
            folder = pending.popleft()
            yield folder
            try:
                pending.extend(folder.sub_folders)
            except OSError:
                pass  # damaged folder index: skip its sub folders

    def messages(self):
        """E-mails only: contacts, distribution lists and other items in mail folders are skipped."""
        for folder in self.folders():
            try:
                items = list(folder.sub_messages)
            except OSError:
                continue  # damaged message index in this folder
            yield from (item for item in items if hasattr(item, 'transport_headers'))

    def get_message_by_id(self, identifier):
        if self._index is None:
            self._index = {message.identifier: message for message in self.messages()}
        return self._index.get(int(identifier))

    @staticmethod
    def format_message(message, with_headers=True):
        """RFC 822 text from the transport headers and the plain, RTF or HTML body.

        libpff returns the already decoded body, so the original MIME structure headers no longer
        apply: Content-Type, Content-Transfer-Encoding and MIME-Version are replaced by a single
        UTF-8 text part. RTF is converted to plain text, HTML stays HTML. (libratom kept the original
        headers, garbled umlauts and joined the last header with its own "Body-Type" line.)
        """
        from email.parser import Parser
        headers = Parser().parsestr(_text(message.transport_headers).strip() + '\r\n\r\n', headersonly=True) \
            if with_headers and message.transport_headers else None
        lines = ''.join(f'{name}: {value}\r\n' for name, value in (headers.items() if headers else ())
                        if name.lower() not in DROPPED_HEADERS)
        if with_headers:
            lines += _fallback_headers(message, headers)
        if message.plain_text_body:
            kind, body = 'plain', _text(message.plain_text_body)
        elif message.rtf_body:
            from striprtf.striprtf import rtf_to_text
            kind, body = 'plain', rtf_to_text(_text(message.rtf_body), errors='replace')
        elif message.html_body:
            kind, body = 'html', _text(message.html_body)
        else:
            return lines
        return (f'{lines}MIME-Version: 1.0\r\nContent-Type: text/{kind}; charset=utf-8\r\n'
                f'Content-Transfer-Encoding: 8bit\r\n\r\n{body.strip()}')

    @staticmethod
    def get_attachment_metadata(message):
        """Name, guessed type and size of each named attachment; the content is not read."""
        result = []
        for attachment in message.attachments:
            name = attachment_name(attachment)
            if name:
                result.append(AttachmentMetadata(name, mimetypes.guess_type(name, strict=False)[0], attachment.size))
        return result
