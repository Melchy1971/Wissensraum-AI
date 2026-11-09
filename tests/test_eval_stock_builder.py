import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent / 'eval'))

import build_own_stock as builder
from core import Store


def short_name(path):
    """The 8.3 alias of an existing Windows path, or None if the volume has none for it."""
    import ctypes
    size = ctypes.windll.kernel32.GetShortPathNameW(str(path), None, 0)
    if not size:
        return None
    buffer = ctypes.create_unicode_buffer(size)
    if not ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, size):
        return None
    return None if os.path.normcase(buffer.value) == os.path.normcase(str(path)) else buffer.value


def other_spelling(path):
    """Another spelling of an existing file's path as an import job might report it.

    Windows: the 8.3 alias if there is one, in swapped case; POSIX: the path relative to the
    working directory (a case change would name another file there).
    """
    path = Path(path).resolve()
    if os.name == 'nt':
        return (short_name(path) or str(path)).swapcase()
    return os.path.relpath(path)


class FakeEmbedder:
    def __init__(self, provider, model):
        self.model = model

    def embed(self, texts):
        return [[1.0, float(index + 1)] for index, _ in enumerate(texts)]


class BrokenEmbedder(FakeEmbedder):
    def embed(self, texts):
        raise ValueError('synthetic failure')


class EvaluationStockBuilderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / 'data'
        self.data.mkdir()
        self.old_data = builder.DATA
        builder.DATA = self.data
        self.base = self.root / 'base.db'
        store = Store(self.base)
        store.add('Basis', 'Unver\u00e4nderter Basistext', filename='basis.md', original=b'original')
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'expected.md').write_text('Erwartete Aussage.', encoding='utf-8')
        self.questions = self.data / 'eval-eigene-fragen.json'
        self.write_questions([{'id': 'e01', 'question': 'Privater Fragetext', 'expected': ['expected.md']},
                              {'id': 'e02', 'question': 'Ausschluss', 'expected': []}])
        self.manifest = self.data / 'eval-messbestand.json'
        self.write_manifest(files=[], folders=[str(self.source)])
        self.output = self.data / 'messbestand-phase7.db'

    def tearDown(self):
        builder.DATA = self.old_data
        self.temp.cleanup()

    def write_manifest(self, **changes):
        data = {'format': builder.MANIFEST_FORMAT, 'version': 1, 'base': str(self.base),
                'files': [], 'folders': [],
                'exclude_names': sorted(builder.REQUIRED_EXCLUDES)}
        data.update(changes)
        self.manifest.write_text(json.dumps(data), encoding='utf-8')

    def write_questions(self, questions):
        self.questions.write_text(json.dumps(questions), encoding='utf-8')

    def build(self, **options):
        return builder.build(self.base, self.manifest, self.questions, self.output, 'test-model',
                             embedder_factory=options.pop('embedder_factory', FakeEmbedder), **options)

    def test_base_stays_identical_backup_creates_output_and_preserves_original_source_and_version(self):
        before = builder.sha256_file(self.base)
        report = self.build()
        self.assertEqual(builder.sha256_file(self.base), before)
        self.assertTrue(report['ready'])
        store = Store(self.output)
        self.assertEqual(store.get(1)['original'], b'original')
        with store.connect() as db:
            self.assertEqual(tuple(db.execute('SELECT source_type,locator FROM sources WHERE document_id=1').fetchone()),
                             ('file', 'basis.md'))
            self.assertEqual(tuple(db.execute('SELECT lineage_id,version FROM document_versions WHERE document_id=1').fetchone()),
                             (1, 1))

    def test_existing_output_requires_replace_and_replace_only_changes_exact_target(self):
        sibling = self.data / 'keep.db'
        sibling.write_bytes(b'keep')
        self.output.write_bytes(b'old')
        with self.assertRaises(FileExistsError):
            self.build()
        self.build(replace=True)
        self.assertEqual(sibling.read_bytes(), b'keep')
        self.assertEqual(len(list(self.data.glob('messbestand-phase7-*-*.db'))), 1)

    def test_manifest_rejects_unknown_fields_empty_and_duplicate_paths(self):
        for change in ({'unknown': True}, {'base': ''}, {'files': ['']},
                       {'exclude_names': ['build', 'BUILD']}):
            self.write_manifest(**change)
            with self.assertRaises(ValueError):
                builder.load_manifest(self.manifest)

    def test_manifest_base_must_match_command_line_base(self):
        self.write_manifest(base=str(self.root / 'other.db'))
        manifest = builder.load_manifest(self.manifest)
        with self.assertRaisesRegex(ValueError, '--base'):
            builder.manifest_entries(manifest, self.base, self.output)

    def test_manifest_rejects_database_as_input_and_paths_outside_data_for_control_files(self):
        self.write_manifest(files=[str(self.base)])
        manifest = builder.load_manifest(self.manifest)
        with self.assertRaisesRegex(ValueError, 'gesch\u00fctzte'):
            builder.manifest_entries(manifest, self.base, self.output)
        with self.assertRaisesRegex(ValueError, 'data'):
            builder.dry_run(self.base, self.manifest, self.questions, self.root / 'outside.db')

    def test_recursive_excludes_and_order_are_deterministic(self):
        hidden = self.source / 'node_modules'
        hidden.mkdir()
        (hidden / 'ignored.md').write_text('ignored', encoding='utf-8')
        (self.source / 'z.md').write_text('z', encoding='utf-8')
        (self.source / 'a.md').write_text('a', encoding='utf-8')
        manifest = builder.load_manifest(self.manifest)
        first = builder.manifest_entries(manifest, self.base, self.output)
        second = builder.manifest_entries(manifest, self.base, self.output)
        self.assertEqual(first, second)
        self.assertEqual([row[0].name for row in first.entries], ['a.md', 'expected.md', 'z.md'])

    def test_folder_discovery_filters_unsupported_suffixes_case_insensitively(self):
        for name in ('image.png', 'archive.zip', 'program.exe', 'extensionless'):
            (self.source / name).write_bytes(b'ignored')
        (self.source / 'manual.PDF').write_bytes(b'pdf')
        (self.source / 'notes.MD').write_text('Text', encoding='utf-8')
        discovery = builder.manifest_entries(builder.load_manifest(self.manifest), self.base, self.output)
        self.assertEqual({row[0].name for row in discovery.entries},
                         {'expected.md', 'manual.PDF', 'notes.MD'})
        self.assertEqual(discovery.skipped_unsupported, 4)

    def test_explicit_unsupported_file_is_rejected(self):
        image = self.source / 'private.png'
        image.write_bytes(b'image')
        self.write_manifest(files=[str(image)], folders=[])
        with self.assertRaisesRegex(ValueError, 'Dokumentformat'):
            builder.manifest_entries(builder.load_manifest(self.manifest), self.base, self.output)

    def test_duplicate_file_and_overlapping_folders_are_discovered_once(self):
        child = self.source / 'child'
        child.mkdir()
        nested = child / 'nested.md'
        nested.write_text('Nested', encoding='utf-8')
        self.write_manifest(files=[str(self.source / 'expected.md')],
                            folders=[str(self.source), str(child)])
        discovery = builder.manifest_entries(builder.load_manifest(self.manifest), self.base, self.output)
        self.assertEqual({row[0] for row in discovery.entries},
                         {(self.source / 'expected.md').resolve(), nested.resolve()})
        self.assertEqual(discovery.duplicate_manifest_entries, 2)

    def test_unknown_import_reason_is_private_safe_error(self):
        returned = other_spelling(self.source / 'expected.md')

        class Jobs:
            def __init__(self, store):
                pass

            def create_selection(self, *args):
                return 1

            def run(self, job):
                pass

            def items(self, job):  # the job reports the file under another spelling of its path
                return [{'path': returned, 'status': 'failed',
                         'reason': 'private failure at C:\\secret\\document.md'}]

        with mock.patch.object(builder, 'ImportJobs', Jobs):
            report = self.build()
        self.assertEqual(report['import_errors'],
                         [{'source': 'D0001-000001', 'status': 'quarantined', 'reason': 'error'}])
        report_text = self.output.with_suffix('.report.json').read_text(encoding='utf-8')
        self.assertNotIn('secret', report_text)
        self.assertNotIn(str(self.root), report_text)

    def test_unmatched_import_item_fails_without_private_path(self):
        class Jobs:
            def __init__(self, store):
                pass

            def create_selection(self, *args):
                return 1

            def run(self, job):
                pass

            def items(self, job):
                return [{'path': str(Path(self_root) / 'private' / 'unknown.md'), 'status': 'imported',
                         'reason': None}]

        self_root = self.root
        store = Store(self.root / 'probe.db')
        entries = [((self.source / 'expected.md').resolve(), 'expected.md', 'F0001')]
        with mock.patch.object(builder, 'ImportJobs', Jobs), self.assertRaises(ValueError) as caught:
            builder.import_all(store, entries)
        self.assertNotIn(str(self.root), str(caught.exception))
        self.assertNotIn('unknown', str(caught.exception))

    def test_path_identity_joins_relative_and_absolute_spellings(self):
        expected = self.source / 'expected.md'
        relative = Path(os.path.relpath(expected)) if Path.cwd().drive == expected.drive else None
        if relative is not None:
            self.assertEqual(builder.path_identity(relative), builder.path_identity(expected))
        self.assertEqual(builder.path_identity(expected), builder.path_identity(expected.resolve()))
        self.assertEqual(builder.path_identity(other_spelling(expected)), builder.path_identity(expected))
        tracked = Path('tests') / 'test_eval_stock_builder.py'  # relative to the repository root
        if tracked.exists():
            self.assertEqual(builder.path_identity(tracked), builder.path_identity(tracked.resolve()))

    def test_path_identity_keeps_different_files_apart(self):
        first, second = self.source / 'Groß.md', self.source / 'groß.md'
        first.write_text('eins', encoding='utf-8')
        second.write_text('zwei', encoding='utf-8')  # same file on a case-insensitive file system
        same_file = first.read_text(encoding='utf-8') == 'zwei'
        self.assertEqual(builder.path_identity(first) == builder.path_identity(second), same_file)
        self.assertNotEqual(builder.path_identity(self.source / 'expected.md'), builder.path_identity(first))
        missing = self.root / 'missing.md'  # no file: the normalised path text identifies it
        self.assertEqual(builder.path_identity(missing), ('path', builder.canonical_path(missing)))
        if os.name == 'nt':
            current = Path.cwd().drive
            other = next(drive for drive in ('C:', 'D:', 'H:', 'Z:') if drive.casefold() != current.casefold())
            self.assertNotEqual(builder.path_identity(Path(current + '\\') / 'identity-missing.md'),
                                builder.path_identity(Path(other + '\\') / 'identity-missing.md'))
            self.assertEqual(builder.path_identity(str(missing).swapcase()), builder.path_identity(missing))

    @unittest.skipUnless(os.name == 'nt', 'Windows 8.3 aliases only')
    def test_path_identity_joins_available_short_name(self):
        long_path = str((self.source / 'expected.md').resolve())
        short_path = short_name(long_path)
        if short_path is None:
            self.skipTest('file has no distinct 8.3 alias')
        self.assertEqual(builder.path_identity(long_path), builder.path_identity(short_path))
        self.assertEqual(builder.path_identity(short_path.swapcase()), builder.path_identity(long_path))

    def test_unsupported_and_duplicate_manifest_entries_do_not_block_ready(self):
        (self.source / 'ignored.exe').write_bytes(b'ignored')
        self.write_manifest(files=[str(self.source / 'expected.md')], folders=[str(self.source)])
        base_before = builder.sha256_file(self.base)
        report = self.build()
        self.assertTrue(report['ready'])
        self.assertEqual((report['imported'], report['skipped_unsupported'],
                          report['duplicate_manifest_entries'], report['failed']), (1, 1, 1, 0))
        self.assertEqual(builder.sha256_file(self.base), base_before)

    def alias(self, expected='alias.md', canonical='expected.md', digest=None):
        return {'expected_title': expected, 'canonical_title': canonical,
                'content_sha256': digest or hashlib.sha256(b'Erwartete Aussage.').hexdigest(),
                'reason': 'content_duplicate'}

    def test_valid_content_alias_satisfies_source_without_creating_content(self):
        self.write_questions([{'id': 'e01', 'question': 'secret', 'expected': ['alias.md']}])
        self.write_manifest(files=[], folders=[str(self.source)], source_aliases=[self.alias()])
        report = self.build()
        self.assertTrue(report['ready'])
        self.assertEqual((report['exact_sources'], report['aliased_sources']), (0, 1))
        with Store(self.output).connect() as database:
            self.assertEqual(database.execute('SELECT COUNT(*) FROM documents').fetchone()[0], 2)
            chunks = database.execute('SELECT COUNT(*) FROM chunks').fetchone()[0]
            vectors = database.execute('SELECT COUNT(*) FROM chunk_vectors').fetchone()[0]
        self.assertEqual((chunks, vectors), (report['chunks'], report['vectors']))

    def test_e16_through_e23_share_exactly_one_valid_alias(self):
        self.write_questions([
            {'id': f'e{number}', 'question': f'secret-{number}', 'expected': ['alias.md']}
            for number in range(16, 24)
        ])
        self.write_manifest(files=[], folders=[str(self.source)], source_aliases=[self.alias()])
        report = self.build()
        self.assertTrue(report['ready'])
        self.assertEqual((report['expected_sources'], report['exact_sources'],
                          report['aliased_sources']), (1, 0, 1))
        self.assertEqual(report['missing_sources'], [])

    def test_invalid_alias_hash_and_unknown_target_stay_missing(self):
        self.write_questions([{'id': 'e01', 'question': 'secret', 'expected': ['alias.md']}])
        cases = [self.alias(digest='0' * 64), self.alias(canonical='unknown.md')]
        for number, alias in enumerate(cases):
            output = self.data / f'invalid-{number}.db'
            self.write_manifest(files=[], folders=[str(self.source)], source_aliases=[alias])
            report = builder.build(self.base, self.manifest, self.questions, output, 'test-model',
                                   embedder_factory=FakeEmbedder)
            self.assertFalse(report['ready'])
            self.assertEqual((len(report['missing_sources']), len(report['invalid_aliases'])), (1, 1))

    def test_conflicting_and_chained_aliases_fail_closed(self):
        other = self.source / 'other.md'
        other.write_text('Andere eindeutige Aussage.', encoding='utf-8')
        other_hash = hashlib.sha256(b'Andere eindeutige Aussage.').hexdigest()
        self.write_questions([{'id': 'e01', 'question': 'secret', 'expected': ['alias.md']}])
        self.write_manifest(files=[], folders=[str(self.source)], source_aliases=[
            self.alias(), self.alias(canonical='other.md', digest=other_hash)])
        report = self.build()
        self.assertFalse(report['ready'])
        self.assertEqual(len(report['conflicting_aliases']), 1)
        self.output.unlink()
        self.write_manifest(files=[], folders=[str(self.source)], source_aliases=[
            self.alias(canonical='next.md'), self.alias(expected='next.md')])
        report = self.build()
        self.assertFalse(report['ready'])
        self.assertEqual(len(report['invalid_aliases']), 1)

    def test_import_duplicate_is_reported_not_stored_twice(self):
        copy = self.source / 'copy.md'
        copy.write_text('Erwartete Aussage.', encoding='utf-8')
        report = self.build()
        self.assertEqual((report['duplicates'], report['documents']), (1, 2))

    def test_missing_and_duplicate_sources_block_ready_with_hashed_identifiers(self):
        self.write_questions([{'id': 'e01', 'question': 'secret', 'expected': ['missing-private.md']}])
        report = self.build()
        self.assertFalse(report['ready'])
        self.assertEqual(report['missing_sources'][0]['question_ids'], ['e01'])
        self.assertNotIn('missing-private.md', json.dumps(report))
        self.output.unlink()
        other = self.source / 'other'
        other.mkdir()
        (other / 'expected.md').write_text('Anderer Inhalt.', encoding='utf-8')
        self.write_manifest(files=[str(self.source / 'expected.md'), str(other / 'expected.md')], folders=[])
        self.write_questions([{'id': 'e01', 'question': 'secret', 'expected': ['expected.md']}])
        report = self.build()
        self.assertFalse(report['ready'])
        self.assertEqual(report['duplicate_sources'][0]['question_ids'], ['e01'])

    def test_source_without_chunk_and_without_vector_block_ready(self):
        store = Store(self.base)
        with store.connect() as db:
            doc = db.execute("INSERT INTO documents(title,category,filename,original,content,sha256) "
                             "VALUES('Leer','Allgemein','empty.md',NULL,'x',?) RETURNING id",
                             (hashlib.sha256(b'unique-empty').hexdigest(),)).fetchone()['id']
            db.execute("INSERT INTO sources(document_id,source_type,account_id,locator) VALUES(?, 'file','','empty.md')", (doc,))
            db.execute('INSERT INTO document_versions(document_id,lineage_id,version) VALUES(?,?,1)', (doc, doc))
        self.write_questions([{'id': 'e01', 'question': 'secret', 'expected': ['empty.md']}])
        report = self.build(skip_vectors=True)
        self.assertTrue(report['sources_without_chunks'])
        self.assertFalse(report['ready'])
        self.output.unlink()
        self.write_questions([{'id': 'e01', 'question': 'secret', 'expected': ['expected.md']}])
        report = self.build(skip_vectors=True)
        self.assertTrue(report['sources_without_vectors'])
        self.assertFalse(report['ready'])

    def test_exclusion_questions_need_no_source_and_counts_are_reported(self):
        self.write_questions([{'id': 'e01', 'question': 'secret', 'expected': []}])
        report = self.build()
        self.assertTrue(report['ready'])
        self.assertEqual((report['answerable_questions'], report['exclusion_questions'],
                          report['expected_sources']), (0, 1, 0))

    def test_report_contains_neither_questions_document_text_nor_absolute_paths(self):
        report = self.build()
        text = self.output.with_suffix('.report.json').read_text(encoding='utf-8')
        self.assertNotIn('Privater Fragetext', text)
        self.assertNotIn('Erwartete Aussage', text)
        self.assertNotIn(str(self.root), text)
        self.assertEqual(report['format'], builder.REPORT_FORMAT)

    def test_dry_run_changes_nothing_and_creates_no_output(self):
        before = {path: path.read_bytes() for path in self.data.iterdir()}
        result = builder.dry_run(self.base, self.manifest, self.questions, self.output)
        self.assertEqual(result['files'], 1)
        self.assertEqual((result['skipped_unsupported'], result['duplicate_manifest_entries']), (0, 0))
        self.assertFalse(self.output.exists())
        self.assertEqual(before, {path: path.read_bytes() for path in self.data.iterdir()})

    def test_aborted_build_marks_failed_database_and_never_publishes_output(self):
        with self.assertRaises(ValueError):
            self.build(embedder_factory=BrokenEmbedder)
        self.assertFalse(self.output.exists())
        self.assertTrue(Path(str(self.output) + '.failed').exists())

    def test_report_failure_moves_published_database_to_failed(self):
        original = Path.write_text

        def fail_report(path, *args, **kwargs):
            if path.name.endswith('.report.json'):
                raise OSError('synthetic report failure')
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, 'write_text', fail_report), self.assertRaises(OSError):
            self.build()
        self.assertFalse(self.output.exists())
        self.assertTrue(Path(str(self.output) + '.failed').exists())

    def test_old_schema_is_migrated_only_in_output(self):
        with closing(sqlite3.connect(self.base)) as db:
            with db:
                for table in ('metadata_changes', 'collections', 'collection_documents', 'favorites', 'saved_searches', 'document_relations',
                              'graph_changes', 'graph_relations', 'entity_mentions', 'entities', 'entity_types', 'relation_types',
                              'ai_usage', 'audit_log', 'web_sessions', 'login_requests', 'grants', 'accounts', 'organizations'):  # version-11 base
                    db.execute(f'DROP TABLE {table}')
                for column in ('status', 'owner', 'review_date', 'revision'):
                    db.execute(f'ALTER TABLE documents DROP COLUMN {column}')
                db.execute('PRAGMA user_version=11')
        before = builder.sha256_file(self.base)
        report = self.build()
        self.assertEqual(report['schema_version'], 22)
        self.assertEqual(builder.sha256_file(self.base), before)
        with closing(sqlite3.connect(self.base)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 11)

    def test_repeated_build_has_stable_semantic_hash_despite_timestamp_metadata(self):
        first = self.build()['reproducible_content_sha256']
        second_output = self.data / 'second.db'
        second = builder.build(self.base, self.manifest, self.questions, second_output, 'test-model',
                               embedder_factory=FakeEmbedder)['reproducible_content_sha256']
        self.assertEqual(first, second)


if __name__ == '__main__':
    unittest.main()
