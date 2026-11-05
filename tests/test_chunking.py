"""Chunk strategy per document type (Release 2.1, Phase 7): default unchanged, re-split only on change."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import DEFAULT_CHUNKING, Store, chunks, document_type

MODEL = 'test-embedding'
TEXT = ' '.join(f'Wort{number:04d}' for number in range(900))  # 8,999 characters


def sections(store, document):
    with store.connect() as db:
        return [dict(row) for row in db.execute('SELECT id,ordinal,content FROM chunks WHERE document_id=? '
                                                'ORDER BY ordinal', (document,)).fetchall()]


def check_chunking_contract(case, store):
    """Same behaviour on SQLite and PostgreSQL; ``store`` must be empty."""
    case.assertEqual(store.chunk_strategies(), {})
    note, _ = store.add('Notiz', TEXT)
    markdown, _ = store.add('Plan', TEXT + ' Markdown', filename='plan.md')
    case.assertEqual([s['content'] for s in sections(store, markdown)],
                     list(chunks(TEXT + ' Markdown', *DEFAULT_CHUNKING)))  # default unchanged
    note_ids = [s['id'] for s in sections(store, note)]
    store.save_vectors(MODEL, {s['id']: [1.0, 0.0] for s in sections(store, note) + sections(store, markdown)})

    case.assertEqual(store.set_chunk_strategy('.MD', 600, 100), 1)  # suffix spelling is normalised
    case.assertEqual(store.chunk_strategies(), {'md': (600, 100)})
    case.assertEqual([s['content'] for s in sections(store, markdown)], list(chunks(TEXT + ' Markdown', 600, 100)))
    case.assertEqual([s['id'] for s in sections(store, note)], note_ids)  # another type stays as it was
    case.assertEqual(store.vector_counts(MODEL), (len(note_ids), len(note_ids) + len(sections(store, markdown))))
    case.assertEqual({row['document_id'] for row in store.search('Markdown')}, {markdown})  # full text follows
    new, _ = store.add('Neu', TEXT + ' neu', filename='neu.md')
    case.assertEqual(len(sections(store, new)), len(list(chunks(TEXT + ' neu', 600, 100))))
    case.assertEqual(store.set_chunk_strategy('md', 600, 100), 0)  # nothing to do
    case.assertEqual(store.set_chunk_strategy('md'), 2)  # reset: back to the default
    case.assertEqual(store.chunk_strategies(), {})
    case.assertEqual([s['content'] for s in sections(store, markdown)],
                     list(chunks(TEXT + ' Markdown', *DEFAULT_CHUNKING)))
    for bad in [('md', 200, 0), ('md', 9000, 0), ('md', 1000, 600), ('../x', 1000, 0), ('', 1000, 0)]:
        with case.assertRaises(ValueError):
            store.set_chunk_strategy(*bad)
    case.assertEqual(store.chunk_strategies(), {})


class ChunkingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.store = Store(self.folder / 'wissen.db')

    def tearDown(self):
        self.temp.cleanup()

    def test_contract(self):
        check_chunking_contract(self, self.store)

    def test_document_type_is_suffix_for_files_else_source(self):
        self.assertEqual(document_type('Bericht.PDF', 'file'), 'pdf')
        self.assertEqual(document_type('anhang.docx', 'attachment'), 'docx')
        self.assertEqual(document_type(None, 'note'), 'note')
        self.assertEqual(document_type('mail.eml', 'pst'), 'pst')
        self.assertEqual(document_type('ohne_endung', 'file'), 'file')

    def test_mail_sources_use_their_own_type(self):
        self.store.set_chunk_strategy('imap', 500, 0)
        mail, _ = self.store.add('Mail', TEXT, source=('imap', 'konto', 'INBOX/1'))
        self.assertEqual(len(sections(self.store, mail)), len(list(chunks(TEXT, 500, 0))))

    def test_cli_lists_sets_and_resets(self):
        import cli
        db = self.folder / 'wissen.db'
        self.store.add('Plan', TEXT, filename='plan.md')

        def run(*argv):
            out = io.StringIO()
            with patch.object(cli, 'BASE', self.folder), patch.dict(os.environ, {}, clear=True), \
                    redirect_stdout(out), redirect_stderr(io.StringIO()):
                code = cli.main(['--db', str(db), 'chunking', *argv])
            return code, out.getvalue()
        code, out = run()
        self.assertEqual((code, json.loads(out)['standard']), (0, {'zeichen': 1800, 'ueberlappung': 200}))
        code, out = run('md', '--size', '900', '--overlap', '100')
        self.assertEqual((code, json.loads(out)['neu_zerlegt']), (0, 1))
        self.assertEqual(json.loads(run()[1])['typen'], {'md': {'zeichen': 900, 'ueberlappung': 100}})
        self.assertEqual(json.loads(run('md', '--reset')[1])['neu_zerlegt'], 1)
        with self.assertRaises(SystemExit):  # invalid length: fixed message, exit code 1
            run('md', '--size', '50')


if __name__ == '__main__':
    unittest.main()
