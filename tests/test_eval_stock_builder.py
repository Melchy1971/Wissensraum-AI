import hashlib
import json
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
        data = {'format': builder.MANIFEST_FORMAT, 'version': 1, 'files': [], 'folders': [],
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
        for change in ({'unknown': True}, {'files': ['']}, {'files': [str(self.source / 'expected.md')] * 2}):
            self.write_manifest(**change)
            with self.assertRaises(ValueError):
                builder.load_manifest(self.manifest)

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
        self.assertEqual([row[0].name for row in first], ['a.md', 'expected.md', 'z.md'])

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
                db.execute('PRAGMA user_version=10')
        before = builder.sha256_file(self.base)
        report = self.build()
        self.assertEqual(report['schema_version'], 11)
        self.assertEqual(builder.sha256_file(self.base), before)
        with closing(sqlite3.connect(self.base)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 10)

    def test_repeated_build_has_stable_semantic_hash_despite_timestamp_metadata(self):
        first = self.build()['reproducible_content_sha256']
        second_output = self.data / 'second.db'
        second = builder.build(self.base, self.manifest, self.questions, second_output, 'test-model',
                               embedder_factory=FakeEmbedder)['reproducible_content_sha256']
        self.assertEqual(first, second)


if __name__ == '__main__':
    unittest.main()
