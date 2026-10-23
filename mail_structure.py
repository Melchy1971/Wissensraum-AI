"""IMAP BODYSTRUCTURE parsing: find text parts and attachments without fetching their content.

The importer asks the server for the structure first. Text parts are fetched one by one
(``BODY.PEEK[n.MIME]`` and ``BODY.PEEK[n]``), attachments only after an explicit release.
"""
from __future__ import annotations

import re
import uuid
from email.header import decode_header, make_header
from urllib.parse import unquote

_LITERAL = re.compile(rb'\{(\d+)\}\s*$')


def fetch_bytes(payload):
    """Join an imaplib fetch payload; literals become escaped quoted strings."""
    joined = b''
    for item in payload or ():
        if isinstance(item, tuple):
            prefix, literal = item[0], item[1] if len(item) > 1 else b''
            match = _LITERAL.search(prefix)
            if match:
                prefix = prefix[:match.start()]
            joined += prefix + b'"' + literal.replace(b'\\', b'\\\\').replace(b'"', b'\\"') + b'"'
        elif isinstance(item, bytes):
            joined += item
    return joined


def parse(data):
    """Parse IMAP response bytes into nested lists of bytes and None (NIL)."""
    position = 0
    length = len(data)

    def skip():
        nonlocal position
        while position < length and data[position:position + 1] in b' \r\n\t':
            position += 1

    def value():
        nonlocal position
        skip()
        char = data[position:position + 1]
        if char == b'(':
            position += 1
            items = []
            while True:
                skip()
                if position >= length:
                    raise ValueError('IMAP-Antwort ist unvollständig.')
                if data[position:position + 1] == b')':
                    position += 1
                    return items
                items.append(value())
        if char == b'"':
            position += 1
            out = bytearray()
            while position < length:
                current = data[position:position + 1]
                if current == b'\\':
                    out += data[position + 1:position + 2]
                    position += 2
                    continue
                if current == b'"':
                    position += 1
                    return bytes(out)
                out += current
                position += 1
            raise ValueError('IMAP-Antwort ist unvollständig.')
        start = position
        while position < length and data[position:position + 1] not in b' ()\r\n':
            if data[position:position + 1] == b'[':
                end = data.find(b']', position)
                position = length if end < 0 else end + 1
                continue
            position += 1
        atom = data[start:position]
        return None if atom.upper() == b'NIL' else atom

    items = []
    while True:
        skip()
        if position >= length:
            return items
        items.append(value())


def fetch_items(payload):
    """Return the key/value pairs of one FETCH response as {KEY: value} (keys upper-case)."""
    parsed = parse(fetch_bytes(payload))
    attributes = next((item for item in parsed if isinstance(item, list)), [])
    result = {}
    for index in range(0, len(attributes) - 1, 2):
        key = attributes[index]
        if isinstance(key, bytes):
            result[key.upper().decode('ascii', errors='replace')] = attributes[index + 1]
    return result


def _text(value):
    return value.decode('utf-8', errors='replace') if isinstance(value, bytes) else ''


def _params(values):
    if not isinstance(values, list):
        return {}
    return {_text(values[i]).lower(): _text(values[i + 1]) for i in range(0, len(values) - 1, 2)}


def _decoded(value):
    try:
        return str(make_header(decode_header(value))).strip()
    except (LookupError, UnicodeError, ValueError):
        return value.strip()


def _rfc2231(value):
    """Decode an RFC 2231 extended value such as utf-8''Pr%C3%BCfung.pdf."""
    charset, _, rest = value.partition("'")
    _, _, text = rest.partition("'")
    if not rest:  # no charset given: plain percent-encoding
        return unquote(value)
    try:
        return unquote(text, encoding=charset or 'utf-8', errors='replace')
    except LookupError:
        return unquote(text, encoding='utf-8', errors='replace')


def _filename(params, key):
    """RFC 2231 (name*, name*0*, ...) and encoded-word aware parameter lookup."""
    if params.get(key + '*'):
        return _rfc2231(params[key + '*']).strip()
    pieces = sorted((int(match.group(1)), name.endswith('*'), value) for name, value in params.items()
                    if (match := re.fullmatch(re.escape(key) + r'\*(\d+)\*?', name)))
    if pieces:
        encoded = pieces[0][1]  # charset is declared on the first encoded piece
        joined = ''.join(value for _, _, value in pieces)
        return (_rfc2231(joined) if encoded else joined).strip()
    return _decoded(params.get(key, ''))


def _leaf(node, section):
    kind = _text(node[0]).lower()
    subtype = _text(node[1]).lower()
    params = _params(node[2])
    encoding = _text(node[5]).lower()
    try:
        size = int(node[6])
    except (TypeError, ValueError, IndexError):
        size = 0
    extension = 8 if kind == 'text' else (10 if (kind, subtype) == ('message', 'rfc822') else 7)
    disposition, disposition_params = '', {}
    if len(node) > extension + 1 and isinstance(node[extension + 1], list) and node[extension + 1]:
        disposition = _text(node[extension + 1][0]).lower()
        disposition_params = _params(node[extension + 1][1] if len(node[extension + 1]) > 1 else None)
    name = _filename(disposition_params, 'filename') or _filename(params, 'name')
    content_type = f'{kind}/{subtype}'
    is_body_text = content_type in ('text/plain', 'text/html') and disposition != 'attachment' and not name
    return {'section': section, 'content_type': content_type, 'encoding': encoding, 'size': size,
            'name': name, 'disposition': disposition, 'charset': params.get('charset', ''),
            'attachment': not is_body_text}


def parts(structure):
    """Flatten a BODYSTRUCTURE into leaf parts with IMAP section numbers.

    A nested message/rfc822 is one attachment; its inner parts are not visited.
    """
    leaves = []

    def walk(node, section):
        if node and isinstance(node[0], list):  # multipart: leading child lists, then subtype and extensions
            for index, child in enumerate(node, start=1):
                if not isinstance(child, list):
                    break
                walk(child, f'{section}.{index}' if section else str(index))
            return
        leaves.append(_leaf(node, section or '1'))

    if not isinstance(structure, list) or not structure:
        raise ValueError('E-Mail-Struktur ist nicht lesbar.')
    walk(structure, '')
    return leaves


def is_multipart(structure):
    return bool(structure) and isinstance(structure[0], list)


def attachment_name(part):
    if part['name']:
        return part['name']
    import mimetypes
    extension = mimetypes.guess_extension(part['content_type']) or ''
    if part['content_type'] == 'message/rfc822':
        extension = '.eml'
    return f'Anhang-{part["section"]}{extension}'


_HEADER_LINE = re.compile(rb'^([!-9;-~]+):', re.MULTILINE)


def _without_headers(header, names):
    """Remove the named header fields including folded continuation lines."""
    lines = header.replace(b'\r\n', b'\n').split(b'\n')
    kept, skipping = [], False
    for line in lines:
        if line[:1] in (b' ', b'\t'):
            if not skipping:
                kept.append(line)
            continue
        match = _HEADER_LINE.match(line)
        skipping = bool(match and match.group(1).lower() in names)
        if not skipping:
            kept.append(line)
    return b'\r\n'.join(kept).strip(b'\r\n') + b'\r\n'


def rebuild_message(header, text_parts, attachment_count=0):
    """Build an RFC 822 message from the header and the fetched text parts only.

    ``text_parts`` are (mime_header, body) pairs. Attachments are never part of the result;
    the number left out is recorded in a header so the original stays honest.
    """
    header = _without_headers(header, {b'content-type', b'content-transfer-encoding', b'mime-version'})
    note = f'X-Wissensraum-Anhaenge-nicht-gespeichert: {attachment_count}\r\n'.encode() if attachment_count else b''
    if not text_parts:
        return header + note + b'MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n'
    boundary = ('wissensraum-' + uuid.uuid4().hex).encode()
    body = b''.join(b'--' + boundary + b'\r\n' + mime.strip(b'\r\n') + b'\r\n\r\n' + content + b'\r\n'
                    for mime, content in text_parts)
    return (header + note + b'MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary="' + boundary
            + b'"\r\n\r\n' + body + b'--' + boundary + b'--\r\n')


def part_payload(mime_header, body):
    """Decode one fetched part (MIME header plus transfer-encoded body) to bytes."""
    from email import policy
    from email.parser import BytesParser
    message = BytesParser(policy=policy.default).parsebytes(mime_header.strip(b'\r\n') + b'\r\n\r\n' + body)
    if message.get_content_type() == 'message/rfc822':
        return body
    return message.get_payload(decode=True) or b''
