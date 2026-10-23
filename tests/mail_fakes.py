"""Recording fakes for IMAP, Microsoft Graph and PST used by the attachment tests (Phase 6.3).

FakeImapServer answers UID FETCH like a real server: BODYSTRUCTURE is generated from the
MIME tree, sections are served byte-exact, and every fetch specification is recorded.
"""
import base64
import re
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser


def raw_payload(part):
    """The transfer-encoded body bytes exactly as they are in the message."""
    body = part.get_payload()
    if not isinstance(body, str):
        return bytes(body)
    try:
        return body.encode('ascii', 'surrogateescape')
    except UnicodeEncodeError:  # compat32 decodes 8bit text parts with their charset
        return body.encode(part.get_content_charset() or 'utf-8')


def quoted(value):
    if value is None:
        return 'NIL'
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'


def plist(params):
    if not params:
        return 'NIL'
    return '(' + ' '.join(quoted(k) + ' ' + quoted(v) for k, v in params) + ')'


class FakeImapServer:
    """One mailbox with numbered messages; created per test."""

    def __init__(self, messages, literal_names=False):
        self.raw = {str(uid): data for uid, data in messages.items()}
        self.fetches = []  # (uid, spec)
        self.literal_names = literal_names

    def client(self, host, port):
        return FakeImapClient(self)

    # --- structure -------------------------------------------------------------------
    @staticmethod
    def _parse(raw):
        return BytesParser(policy=policy.compat32).parsebytes(raw)

    def bodystructure(self, part):
        if part.is_multipart():
            children = ''.join(self.bodystructure(child) for child in part.get_payload())
            return f'({children} {quoted(part.get_content_subtype().upper())} {plist([("BOUNDARY", part.get_boundary())])} NIL NIL NIL)'
        maintype, subtype = part.get_content_maintype().upper(), part.get_content_subtype().upper()
        params = [(k.upper(), v) for k, v in part.get_params(header='content-type')[1:]] if part.get_params() else []
        encoding = (part.get('Content-Transfer-Encoding') or '7BIT').upper()
        body = part.get_payload()
        size = len(raw_payload(part))
        disposition = 'NIL'
        if part.get('Content-Disposition'):
            kind = part.get_content_disposition().upper()
            dparams = [(k.upper(), v) for k, v in part.get_params(header='content-disposition')[1:]]
            disposition = f'({quoted(kind)} {plist(dparams)})'
        head = f'{quoted(maintype)} {quoted(subtype)} {plist(params)} NIL NIL {quoted(encoding)} {size}'
        if maintype == 'TEXT':
            return f'({head} {body.count(chr(10)) + 1} NIL {disposition} NIL NIL)'
        return f'({head} NIL {disposition} NIL NIL)'

    def section(self, message, section):
        raw = self.raw[message]
        root = self._parse(raw)
        if section == 'HEADER':
            return raw.split(b'\r\n\r\n', 1)[0] + b'\r\n\r\n'
        if section == 'TEXT':
            return raw.split(b'\r\n\r\n', 1)[1]
        if section.startswith('HEADER.FIELDS'):
            wanted = re.findall(r'[A-Z-]+', section.split('(', 1)[1].upper())
            return b''.join(f'{k}: {v}\r\n'.encode() for k, v in root.items() if k.upper() in wanted) + b'\r\n'
        mime = section.endswith('.MIME')
        numbers = section[:-5].split('.') if mime else section.split('.')
        part = root
        for number in numbers:
            if part.is_multipart():
                part = part.get_payload()[int(number) - 1]
            elif number != '1':
                raise AssertionError(section)
        if mime:
            return b''.join(f'{k}: {v}\r\n'.encode() for k, v in part.items()) + b'\r\n'
        return raw_payload(part)

    def respond(self, uid, spec):
        self.fetches.append((uid, spec))
        items = re.findall(r'BODYSTRUCTURE|BODY\.PEEK\[[^\]]*\]', spec)
        if not items:
            raise AssertionError('unexpected fetch ' + spec)
        root = self._parse(self.raw[uid])
        prefix = f'1 (UID {uid}'
        out = []
        for item in items:
            if item == 'BODYSTRUCTURE':
                structure = self.bodystructure(root)
                if self.literal_names and '"' in structure:
                    # serve the first quoted file name as a literal, like some servers do
                    match = re.search(r'"(FILENAME|NAME)" "([^"]+)"', structure)
                    if match:
                        literal = match.group(2).encode()
                        before = structure[:match.start(2) - 1]
                        after = structure[match.end(2) + 1:]
                        out.append((f'{prefix} BODYSTRUCTURE {before}{{{len(literal)}}}'.encode(), literal))
                        prefix = after
                        continue
                prefix += ' BODYSTRUCTURE ' + structure
                continue
            section = item[len('BODY.PEEK['):-1]
            data = self.section(uid, section)
            out.append((f'{prefix} BODY[{section}] {{{len(data)}}}'.encode(), data))
            prefix = ''
        out.append((prefix + ')').encode())
        return out


class FakeImapClient:
    def __init__(self, server):
        self.server = server

    def login(self, user, password):
        return 'OK', []

    def select(self, folder, readonly=False):
        assert readonly, 'mailbox must be opened read-only'
        return 'OK', [str(len(self.server.raw)).encode()]

    def uid(self, command, *args):
        if command == 'search':
            return 'OK', [' '.join(self.server.raw).encode()]
        if command == 'fetch':
            uid = args[0].decode() if isinstance(args[0], bytes) else str(args[0])
            return 'OK', self.server.respond(uid, args[1])
        if command == 'store':
            raise AssertionError('mailbox must stay unchanged')
        raise AssertionError(command)

    def logout(self):
        return 'BYE', []


def mail(subject, body, attachments=(), html=None, uid_marker=''):
    """Build an RFC 822 message; attachments are (name, mimetype, bytes)."""
    message = EmailMessage()
    message['From'] = 'absender@example.test'
    message['To'] = 'markus@example.test'
    message['Subject'] = subject
    message['Date'] = 'Fri, 23 Oct 2026 10:00:00 +0000'
    message['Message-ID'] = f'<{abs(hash(subject)) % 10**9}{uid_marker}@example.test>'
    if body is not None:
        message.set_content(body)
    if html:
        message.add_alternative(html, subtype='html')
    for name, mimetype, data in attachments:
        maintype, subtype = mimetype.split('/')
        if body is None and not message.is_multipart() and not message.get_payload():
            message.make_mixed()
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return message.as_bytes(policy=policy.SMTP)


class FakeGraph:
    """Recording Graph transport with messages {id: (detail, [attachments])}."""

    def __init__(self, messages):
        self.messages = messages
        self.urls = []

    def __call__(self, url, token):
        self.urls.append(url)
        path = url.split('graph.microsoft.com/v1.0', 1)[1].split('?', 1)[0]
        if path == '/me/mailFolders/inbox/messages':
            return {'value': [{'id': key, 'subject': detail['subject'], 'from': detail['from'],
                               'receivedDateTime': detail['receivedDateTime'],
                               'hasAttachments': bool(attachments)}
                              for key, (detail, attachments) in self.messages.items()]}
        parts = path.split('/')
        from urllib.parse import unquote
        message_id = unquote(parts[3])
        detail, attachments = self.messages[message_id]
        if len(parts) == 4:
            return dict(detail, id=message_id, hasAttachments=bool(attachments))
        if len(parts) == 5 and parts[4] == 'attachments':
            assert 'contentBytes' not in url, 'metadata request must not select content'
            return {'value': [{key: value for key, value in item.items() if key != 'contentBytes'}
                              for item in attachments]}
        attachment_id = unquote(parts[5])
        return next(item for item in attachments if item['id'] == attachment_id)

    def content_requests(self):
        return [url for url in self.urls if re.search(r'/attachments/[^?]+', url)]


def graph_attachment(attachment_id, name, mimetype, data, kind='#microsoft.graph.fileAttachment'):
    item = {'id': attachment_id, 'name': name, 'contentType': mimetype, 'size': len(data),
            'isInline': False, '@odata.type': kind}
    if kind == '#microsoft.graph.fileAttachment':
        item['contentBytes'] = base64.b64encode(data).decode()
    return item


class FakePstAttachment:
    def __init__(self, name, data, log):
        self.name, self._data, self._log = name, data, log
        self.size = len(data)

    def read_buffer(self, size):
        self._log.append(self.name)
        return self._data


class FakePstMessage:
    def __init__(self, raw, attachments, log, identifier=0):
        self.raw = raw
        self.identifier = identifier
        parsed = BytesParser(policy=policy.default).parsebytes(raw)
        self.subject = str(parsed.get('Subject') or '')
        self.sender_name = str(parsed.get('From') or '')
        self.delivery_time = '2026-10-23 10:00:00'
        self.attachments = [FakePstAttachment(name, data, log) for name, _, data in attachments]
        self.meta = [type('Meta', (), {'name': name, 'mime_type': mimetype, 'size': len(data)})()
                     for name, mimetype, data in attachments]


class FakePstArchive:
    """PST archive like libratom's PffArchive; ``read_log`` lists every attachment content read."""

    def __init__(self, messages):
        self.read_log = []
        self.items = [FakePstMessage(raw, attachments, self.read_log, 2100 + index)
                      for index, (raw, attachments) in enumerate(messages)]

    def get_message_by_id(self, identifier):
        return next((item for item in self.items if item.identifier == identifier), None)

    def __call__(self, path):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def messages(self):
        return iter(self.items)

    def format_message(self, message, with_headers=True):
        return message.raw.decode()

    def get_attachment_metadata(self, message):
        return message.meta
