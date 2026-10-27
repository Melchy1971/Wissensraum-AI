"""PST adapter over libpff (replaces libratom)."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pst_archive import PstArchive


def message(identifier, headers='Subject: Hallo\r\nFrom: a@example.test\r\n', plain=b'Text', attachments=()):
    return SimpleNamespace(identifier=identifier, transport_headers=headers, plain_text_body=plain, rtf_body=None,
                           html_body=None, attachments=list(attachments))


class FakeFolder:
    def __init__(self, messages=(), folders=(), broken=False):
        self._messages, self.sub_folders, self.broken = list(messages), list(folders), broken

    @property
    def sub_messages(self):
        if self.broken:
            raise OSError('damaged index')
        return iter(self._messages)


class FakePff:
    def __init__(self, root):
        self.root, self.opened, self.closed = root, None, False

    def file(self):
        pff = self

        class File:
            root_folder = pff.root

            def open(self, path, mode):
                pff.opened = (path, mode)

            def close(self):
                pff.closed = True
        return File()


class PstArchiveTests(unittest.TestCase):
    def archive(self, root):
        pff = FakePff(root)
        return PstArchive('archiv.pst', pff), pff

    def test_messages_of_all_folders_breadth_first_and_lookup_by_id(self):
        deep = FakeFolder([message(4)])
        root = FakeFolder([message(1)], [FakeFolder([message(2)], [deep]), FakeFolder([message(3)])])
        archive, pff = self.archive(root)
        with archive:
            self.assertEqual([m.identifier for m in archive.messages()], [1, 2, 3, 4])
            self.assertEqual(archive.get_message_by_id('4').identifier, 4)
            self.assertIsNone(archive.get_message_by_id(99))
        self.assertEqual(pff.opened, ('archiv.pst', 'rb'))
        self.assertTrue(pff.closed)

    def test_damaged_folder_is_skipped(self):
        archive, _ = self.archive(FakeFolder([message(1)], [FakeFolder(broken=True), FakeFolder([message(2)])]))
        self.assertEqual([m.identifier for m in archive.messages()], [1, 2])

    @staticmethod
    def parsed(msg):
        from email import policy
        from email.parser import BytesParser
        return BytesParser(policy=policy.default).parsebytes(PstArchive.format_message(msg).encode('utf-8'))

    def test_headers_stay_intact_and_umlauts_survive(self):
        original = ('Subject: =?utf-8?q?Gr=C3=BC=C3=9Fe?=\r\nFrom: a@example.test\r\nMIME-Version: 1.0\r\n'
                    'Content-Type: multipart/alternative;\r\n boundary="xyz"\r\nContent-Transfer-Encoding: 7bit\r\n')
        parsed = self.parsed(message(1, headers=original, plain='Schöne Grüße'.encode()))
        self.assertEqual((parsed['From'], parsed['Subject']), ('a@example.test', 'Grüße'))
        self.assertEqual(parsed.get_content_type(), 'text/plain')  # original multipart header replaced
        self.assertEqual(parsed.get_content().strip(), 'Schöne Grüße')
        self.assertEqual(self.parsed(message(2, headers=None, plain=b'Nur Text')).get_content().strip(), 'Nur Text')
        self.assertEqual(PstArchive.format_message(message(3, plain=None)), 'Subject: Hallo\r\nFrom: a@example.test\r\n')

    def test_items_without_transport_headers_keep_subject_sender_and_date(self):
        from datetime import datetime, timezone
        sent = message(1, headers=None, plain=b'Gesendet aus Outlook')
        sent.subject, sent.sender_name = 'Angebot für Müller', 'Markus D'
        sent.delivery_time = datetime(2026, 10, 27, 9, 30, tzinfo=timezone.utc)
        parsed = self.parsed(sent)
        self.assertEqual((parsed['Subject'], parsed['From'], parsed['Date'].datetime.day),
                         ('Angebot für Müller', '"Markus D"', 27))  # display name only, no address
        with_headers = message(2)  # transport headers win
        with_headers.subject, with_headers.sender_name = 'Anderer Betreff', 'X'
        self.assertEqual((self.parsed(with_headers)['Subject'], self.parsed(with_headers)['From']),
                         ('Hallo', 'a@example.test'))

    def test_rtf_becomes_text_and_html_stays_html(self):
        rtf = message(1, plain=None)
        rtf.rtf_body = rb"{\rtf1\ansi Rechnung \'fcber 5 Euro\par}"
        self.assertEqual(self.parsed(rtf).get_content().strip(), 'Rechnung über 5 Euro')
        html = message(2, plain=None)
        html.html_body = '<p>Hallo <b>Welt</b></p>'.encode()
        self.assertEqual(self.parsed(html).get_content_type(), 'text/html')

    def test_import_text_of_a_formatted_message(self):
        import tempfile
        from core import Store
        from importer import Importer
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'wissen.db')
            raw = PstArchive.format_message(message(1, plain='Schöne Grüße'.encode())).encode('utf-8')
            doc_id, _ = Importer(store, None)._add_email(raw, 'PST · archiv', ('pst', '', 'archiv'))
            content = store.get(doc_id)['content']
        self.assertIn('Betreff: Hallo', content)
        self.assertIn('Schöne Grüße', content)

    def test_attachment_metadata_without_reading_content(self):
        def unreadable(_size):
            raise AssertionError('content must not be read')
        class Unnamed:  # libpff raises when the file name record is missing
            size = 5
            read_buffer = staticmethod(unreadable)

            @property
            def long_filename(self):
                raise OSError('missing record')
        # libpff-python names the field long_filename (libratom's fork: name)
        parts = [SimpleNamespace(long_filename='rechnung.pdf', size=1200, read_buffer=unreadable),
                 SimpleNamespace(name='alt.docx', size=7, read_buffer=unreadable), Unnamed()]
        self.assertEqual([tuple(item) for item in PstArchive.get_attachment_metadata(message(1, attachments=parts))],
                         [('rechnung.pdf', 'application/pdf', 1200),
                          ('alt.docx', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', 7)])

    def test_stored_mime_type_first_then_extension_without_reading_content(self):
        def unreadable(*_):
            raise AssertionError('content must not be read')

        class Entry:
            def __init__(self, entry_type, value_type, value):
                self.entry_type, self.value_type, self.value = entry_type, value_type, value

            def get_data_as_string(self):
                if self.entry_type == 0x3701:  # PR_ATTACH_DATA_BIN: the attachment content itself
                    raise AssertionError('attachment data must not be read')
                return self.value

            data = property(lambda self: unreadable())

        def part(name, *entries):
            return SimpleNamespace(long_filename=name, size=10, read_buffer=unreadable,
                                   record_sets=[SimpleNamespace(entries=[Entry(0x3701, 0x0102, b''), *entries])])
        parts = [part('scan.bin', Entry(0x370E, 0x001F, 'Image/PNG')),               # stored type wins
                 part('bericht.pdf', Entry(0x370E, 0x001F, 'application/octet-stream')),  # generic: use extension
                 part('liste.xlsx', Entry(0x370E, 0x001F, 'kein typ')),               # invalid: use extension
                 part('notiz.xyz', Entry(0x370E, 0x0003, 'text/plain')),               # not a string property
                 part('ohne')]                                                         # nothing known
        self.assertEqual([item.mime_type for item in PstArchive.get_attachment_metadata(message(1, attachments=parts))],
                         ['image/png', 'application/pdf',
                          'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                          'application/octet-stream', 'application/octet-stream'])


class ContentTypeTableTests(unittest.TestCase):
    """The same answer on every system: Python's mimetypes (registry, /etc/mime.types) is not used."""

    def test_extensions(self):
        from unittest.mock import patch
        import content_types
        expected = {'a.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                    'a.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    'a.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
                    'a.pdf': 'application/pdf', 'A.PNG': 'image/png', 'a.jpg': 'image/jpeg', 'a.jpeg': 'image/jpeg',
                    'a.xyz': 'application/octet-stream', 'ohne_endung': 'application/octet-stream',
                    '': 'application/octet-stream'}
        with patch('mimetypes.guess_type', side_effect=AssertionError('mimetypes is platform dependent')), \
                patch('mimetypes.guess_extension', side_effect=AssertionError('mimetypes is platform dependent')):
            self.assertEqual({name: content_types.content_type_for(name) for name in expected}, expected)
            self.assertEqual([content_types.extension_for(t) for t in ('application/pdf', 'IMAGE/JPEG; x=1',
                                                                       'message/rfc822', 'application/x-unbekannt')],
                             ['.pdf', '.jpg', '.eml', ''])


if __name__ == '__main__':
    unittest.main()
