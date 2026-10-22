import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import multiprocessing
import os
import queue
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import PostgresStore, Store
from migrations import (CURRENT_SCHEMA_VERSION, POSTGRES_LEGACY_COLUMNS,
                        SQLITE_MIGRATIONS, _database_file_lock, migrate_postgres,
                        migrate_sqlite)


SQLITE_HISTORICAL_MIGRATIONS = (
    (1, (
        """CREATE TABLE documents (
            id INTEGER PRIMARY KEY, title TEXT NOT NULL, category TEXT NOT NULL,
            filename TEXT, original BLOB, content TEXT NOT NULL,
            sha256 TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE chunks (
            id INTEGER PRIMARY KEY,
            document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL, content TEXT NOT NULL,
            UNIQUE(document_id, ordinal)
        )""",
        """CREATE VIRTUAL TABLE search_index USING fts5(
            title, category, content, content='chunks_search', content_rowid='id',
            tokenize='unicode61 remove_diacritics 2'
        )""",
        """CREATE VIEW chunks_search AS
            SELECT c.id, d.title, d.category, c.content
            FROM chunks c JOIN documents d ON d.id=c.document_id""",
        """CREATE TRIGGER chunk_insert AFTER INSERT ON chunks BEGIN
            INSERT INTO search_index(rowid,title,category,content)
            SELECT NEW.id,title,category,NEW.content FROM documents WHERE id=NEW.document_id;
        END""",
        """CREATE TRIGGER chunk_delete AFTER DELETE ON chunks BEGIN
            INSERT INTO search_index(search_index,rowid,title,category,content)
            SELECT 'delete',OLD.id,title,category,OLD.content FROM documents WHERE id=OLD.document_id;
        END""",
        """CREATE TABLE ai_runs (
            id INTEGER PRIMARY KEY, kind TEXT NOT NULL, question TEXT NOT NULL,
            answer TEXT NOT NULL, sources_json TEXT NOT NULL, model TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
    )),
    (2, ("CREATE TABLE settings (name TEXT PRIMARY KEY, value TEXT NOT NULL)",)),
    (3, (
        """CREATE TABLE email_accounts (
            id TEXT PRIMARY KEY, label TEXT NOT NULL, provider TEXT NOT NULL,
            email TEXT NOT NULL, username TEXT NOT NULL, host TEXT NOT NULL DEFAULT '',
            port INTEGER NOT NULL DEFAULT 0, client_id TEXT NOT NULL DEFAULT '',
            tenant TEXT NOT NULL DEFAULT 'common',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
    )),
)


def _migrate_sqlite_in_spawned_process(path, started, result):
    started.set()
    try:
        migrate_sqlite(path)
    except Exception as error:
        result.put(('error', repr(error)))
    else:
        result.put(('ok', None))


def _migrate_sqlite_alias_in_spawned_process(path, ready, start, entered, release, result):
    import migrations

    original_backup = migrations._verified_sqlite_backup

    def blocking_backup(*args):
        entered.put(path)
        if not release.wait(5):
            raise TimeoutError('test did not release migration backup')
        return original_backup(*args)

    migrations._verified_sqlite_backup = blocking_backup
    ready.put(path)
    start.wait()
    try:
        migrations.migrate_sqlite(path)
    except Exception as error:
        result.put(('error', path, repr(error)))
    else:
        result.put(('ok', path, None))


class FakeResult:
    def __init__(self, one=None, rows=()):
        self.one = one
        self.rows = list(rows)

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.rows


class FakePostgres:
    def __init__(self, tables=(), version=None, columns=(), constraints=(), indexes=(),
                 history=None, lock_error=None, current_tables=None):
        self.tables = set(tables)
        self.version = version
        self.history = list(history if history is not None else (
            range(1, version + 1) if version is not None else ()))
        self.columns = list(columns)
        self.constraints = list(constraints)
        self.indexes = list(indexes)
        self.lock_error = lock_error
        self.current_tables = current_tables
        self.calls = []

    def execute(self, query, parameters=()):
        normalized = ' '.join(query.split())
        self.calls.append((normalized, parameters))
        if 'pg_advisory_xact_lock' in normalized and self.lock_error:
            raise self.lock_error
        if 'SELECT MAX(version)' in normalized:
            return FakeResult({'version': self.version})
        if normalized.startswith('SELECT version FROM wissensraum.schema_migrations'):
            return FakeResult(rows=[{'version': version} for version in self.history])
        if 'FROM information_schema.tables' in normalized:
            tables = self.tables | ({'schema_migrations'} if self.version is not None else set())
            if self.current_tables is not None:
                tables = set(self.current_tables) | {'schema_migrations'}
            elif self.version == CURRENT_SCHEMA_VERSION:
                tables |= {'documents', 'chunks', 'ai_runs', 'settings',
                           'email_accounts', 'email_imports'}
            return FakeResult(rows=[{'table_name': name} for name in sorted(tables)])
        if 'FROM information_schema.columns' in normalized:
            return FakeResult(rows=self.columns)
        if 'FROM pg_catalog.pg_constraint' in normalized:
            return FakeResult(rows=self.constraints)
        if 'FROM pg_catalog.pg_indexes' in normalized:
            return FakeResult(rows=self.indexes)
        if normalized.startswith('INSERT INTO wissensraum.schema_migrations'):
            self.version = parameters[0]
            self.history.append(parameters[0])
        return FakeResult()


class SQLiteMigrationTests(unittest.TestCase):
    @staticmethod
    def create_version_fixture(path, version):
        with closing(sqlite3.connect(path)) as db:
            db.execute('PRAGMA foreign_keys=ON')
            for target, statements in SQLITE_HISTORICAL_MIGRATIONS:
                if target > version:
                    break
                for statement in statements:
                    db.execute(statement)
                db.execute(f'PRAGMA user_version={target}')
            db.execute("INSERT INTO documents(id,title,category,filename,original,content,sha256) "
                       "VALUES(1,'Alt','Archiv','alt.bin',X'00FF','Migrationsbeleg','digest')")
            db.execute("INSERT INTO chunks(id,document_id,ordinal,content) VALUES(1,1,1,'Migrationsbeleg')")
            if version >= 2:
                db.execute("INSERT INTO settings(name,value) VALUES('active_provider','ollama')")
            if version >= 3:
                db.execute("INSERT INTO email_accounts(id,label,provider,email,username) "
                           "VALUES('konto','Alt','imap','alt@example.test','alt@example.test')")
            db.commit()

    def test_empty_database_reaches_current_version(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wissen.db'
            migrate_sqlite(path)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], CURRENT_SCHEMA_VERSION)
                tables = {row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({'documents', 'chunks', 'ai_runs', 'settings',
                             'email_accounts', 'email_imports', 'search_index'} <= tables)

    def test_existing_versions_preserve_data_and_create_verified_backup(self):
        for old_version in range(1, CURRENT_SCHEMA_VERSION):
            with self.subTest(old_version=old_version), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'wissen.db'
                self.create_version_fixture(path, old_version)

                migrate_sqlite(path)

                backup = path.with_name(path.name + f'.pre-migration-v{old_version}.bak')
                self.assertTrue(backup.is_file())
                with closing(sqlite3.connect(backup)) as db:
                    self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                    self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], old_version)
                with closing(sqlite3.connect(path)) as db:
                    self.assertEqual(db.execute('SELECT original FROM documents').fetchone()[0], b'\x00\xff')
                    if old_version >= 2:
                        self.assertEqual(db.execute('SELECT value FROM settings').fetchone()[0], 'ollama')
                    if old_version >= 3:
                        self.assertEqual(db.execute('SELECT label FROM email_accounts').fetchone()[0], 'Alt')
                    self.assertEqual(db.execute(
                        "SELECT count(*) FROM search_index WHERE search_index MATCH 'Migrationsbeleg'").fetchone()[0], 1)

    def test_historical_fixtures_do_not_depend_on_runtime_migrations(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wissen.db'
            with patch('migrations.SQLITE_MIGRATIONS', ()):  # implementation changes cannot alter fixtures
                self.create_version_fixture(path, 3)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 3)
                self.assertEqual(db.execute('SELECT title FROM documents').fetchone()[0], 'Alt')

    def test_concurrent_migrations_of_existing_version_zero_database_are_serialized(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wissen.db'
            with closing(sqlite3.connect(path)):
                pass
            active = 0
            maximum = 0
            guard = threading.Lock()
            from migrations import _verified_sqlite_backup

            def slow_backup(*args):
                nonlocal active, maximum
                with guard:
                    active += 1
                    maximum = max(maximum, active)
                time.sleep(0.15)
                try:
                    return _verified_sqlite_backup(*args)
                finally:
                    with guard:
                        active -= 1

            with patch('migrations._verified_sqlite_backup', side_effect=slow_backup):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(migrate_sqlite, path) for _ in range(2)]
                    for future in futures:
                        future.result(timeout=10)
            self.assertEqual(maximum, 1)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], CURRENT_SCHEMA_VERSION)
            backup = path.with_name(path.name + '.pre-migration-v0.bak')
            self.assertTrue(backup.is_file())

    def test_sqlite_migration_lock_has_bounded_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wissen.db'
            with _database_file_lock(path):
                with self.assertRaisesRegex(TimeoutError, 'Zeitüberschreitung'):
                    with _database_file_lock(path, timeout=0.05):
                        self.fail('contended lock was unexpectedly acquired')

    def test_sqlite_migrations_serialize_across_spawned_processes(self):
        context = multiprocessing.get_context('spawn')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wissen.db'
            started = context.Event()
            result = context.Queue()
            process = context.Process(
                target=_migrate_sqlite_in_spawned_process,
                args=(str(path), started, result),
            )
            try:
                with _database_file_lock(path):
                    process.start()
                    self.assertTrue(started.wait(5), 'child process did not start')
                    time.sleep(0.15)
                    self.assertTrue(process.is_alive(), 'child bypassed the migration lock')
                process.join(10)
                self.assertFalse(process.is_alive(), 'child migration did not finish after lock release')
                self.assertEqual(process.exitcode, 0)
                self.assertEqual(result.get(timeout=2), ('ok', None))
            finally:
                if process.is_alive():
                    process.terminate()
                    process.join(5)
                result.close()
                result.join_thread()
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],
                                 CURRENT_SCHEMA_VERSION)

    def test_sqlite_migrations_serialize_hard_link_aliases_across_spawned_processes(self):
        context = multiprocessing.get_context('spawn')
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / 'wissen.db'
            first_alias = Path(directory) / 'alias-one.db'
            second_alias = Path(directory) / 'alias-two.db'
            with closing(sqlite3.connect(original)):
                pass
            try:
                os.link(original, first_alias)
                os.link(original, second_alias)
            except (OSError, NotImplementedError) as error:
                self.skipTest(f'hard links unavailable in temporary directory: {error}')

            ready = context.Queue()
            entered = context.Queue()
            result = context.Queue()
            start = context.Event()
            release = context.Event()
            processes = [
                context.Process(
                    target=_migrate_sqlite_alias_in_spawned_process,
                    args=(str(path), ready, start, entered, release, result),
                )
                for path in (first_alias, second_alias)
            ]
            try:
                for process in processes:
                    process.start()
                self.assertEqual({ready.get(timeout=5), ready.get(timeout=5)},
                                 {str(first_alias), str(second_alias)})
                start.set()
                entered.get(timeout=5)
                with self.assertRaises(queue.Empty):
                    entered.get(timeout=0.3)
                release.set()
                for process in processes:
                    process.join(10)
                    self.assertFalse(process.is_alive(), 'child migration did not finish')
                    self.assertEqual(process.exitcode, 0)
                outcomes = [result.get(timeout=2), result.get(timeout=2)]
                self.assertTrue(all(outcome[0] == 'ok' for outcome in outcomes), outcomes)
            finally:
                release.set()
                for process in processes:
                    if process.is_alive():
                        process.terminate()
                    process.join(5)
                for channel in (ready, entered, result):
                    channel.close()
                    channel.join_thread()
            with closing(sqlite3.connect(original)) as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],
                                 CURRENT_SCHEMA_VERSION)

    def test_newer_sqlite_schema_is_rejected_without_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wissen.db'
            with closing(sqlite3.connect(path)) as db:
                db.execute('CREATE TABLE marker(value TEXT)')
                db.execute("INSERT INTO marker VALUES('unverändert')")
                db.execute(f'PRAGMA user_version={CURRENT_SCHEMA_VERSION + 1}')
                db.commit()
            with self.assertRaisesRegex(ValueError, 'neuer'):
                migrate_sqlite(path)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], CURRENT_SCHEMA_VERSION + 1)
                self.assertEqual(db.execute('SELECT value FROM marker').fetchone()[0], 'unverändert')

    def test_current_sqlite_schema_missing_table_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wissen.db'
            migrate_sqlite(path)
            with closing(sqlite3.connect(path)) as db:
                db.execute('DROP TABLE settings')
            with self.assertRaisesRegex(ValueError, 'Schema'):
                migrate_sqlite(path)

    def test_current_sqlite_schema_with_wrong_default_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wissen.db'
            migrate_sqlite(path)
            with closing(sqlite3.connect(path)) as db:
                db.execute('ALTER TABLE email_accounts RENAME TO old_email_accounts')
                db.execute("""CREATE TABLE email_accounts (
                    id TEXT PRIMARY KEY, label TEXT NOT NULL, provider TEXT NOT NULL,
                    email TEXT NOT NULL, username TEXT NOT NULL,
                    host TEXT NOT NULL DEFAULT 'wrong', port INTEGER NOT NULL DEFAULT 0,
                    client_id TEXT NOT NULL DEFAULT '', tenant TEXT NOT NULL DEFAULT 'common',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
                db.execute('DROP TABLE old_email_accounts')
            with self.assertRaisesRegex(ValueError, 'Schema'):
                migrate_sqlite(path)

    def test_current_sqlite_schema_with_extra_unique_constraint_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wissen.db'
            migrate_sqlite(path)
            with closing(sqlite3.connect(path)) as db:
                db.execute('CREATE UNIQUE INDEX unexpected_email_unique ON email_accounts(email)')
            with self.assertRaisesRegex(ValueError, 'Schema'):
                migrate_sqlite(path)

    def test_failed_sqlite_migration_rolls_back_schema_and_version(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wissen.db'
            self.create_version_fixture(path, 1)
            # Keep migration 1: validate_sqlite reads the search DDL from it.
            broken = (SQLITE_MIGRATIONS[0],
                      (2, ('CREATE TABLE partial_change(value TEXT)', 'NOT VALID SQL')))
            with patch('migrations.SQLITE_MIGRATIONS', broken), \
                 self.assertRaises(sqlite3.DatabaseError):
                migrate_sqlite(path)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)
                self.assertIsNone(db.execute(
                    "SELECT name FROM sqlite_master WHERE name='partial_change'").fetchone())

    def test_failed_post_migration_validation_rolls_back_target_version(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wissen.db'
            migrate_sqlite(path)
            with closing(sqlite3.connect(path)) as db:
                db.execute('DROP TABLE email_imports')
                db.execute('PRAGMA foreign_keys=OFF')
                db.execute("INSERT INTO chunks(document_id,ordinal,content) VALUES(999,1,'verwaist')")
                db.execute('PRAGMA user_version=3')
                db.commit()
            with self.assertRaisesRegex(ValueError, 'Fremdschlüssel'):
                migrate_sqlite(path)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 3)
                self.assertIsNone(db.execute(
                    "SELECT name FROM sqlite_master WHERE name='email_imports'").fetchone())

    def test_store_initialization_uses_migration_layer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'wissen.db'
            self.create_version_fixture(path, 3)
            Store(path)
            self.assertTrue(path.with_name(path.name + '.pre-migration-v3.bak').is_file())
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], CURRENT_SCHEMA_VERSION)


class PostgresMigrationTests(unittest.TestCase):
    required = frozenset({'documents', 'chunks', 'ai_runs', 'settings',
                          'email_accounts', 'email_imports'})

    @staticmethod
    def legacy_metadata():
        defaults = {
            ('documents', 'created_at'): 'CURRENT_TIMESTAMP',
            ('ai_runs', 'created_at'): 'CURRENT_TIMESTAMP',
            ('email_accounts', 'host'): "''::text",
            ('email_accounts', 'port'): '0',
            ('email_accounts', 'client_id'): "''::text",
            ('email_accounts', 'tenant'): "'common'::text",
            ('email_accounts', 'created_at'): 'CURRENT_TIMESTAMP',
            ('email_accounts', 'updated_at'): 'CURRENT_TIMESTAMP',
            ('email_imports', 'imported_at'): 'CURRENT_TIMESTAMP',
        }
        columns = []
        for item in POSTGRES_LEGACY_COLUMNS:
            table, column, data_type, nullable, identity = item[:5]
            default = item[5] if len(item) > 5 else defaults.get((table, column))
            generation = item[6] if len(item) > 6 else ('ALWAYS' if identity == 'YES' else None)
            columns.append({'table_name':table, 'column_name':column, 'data_type':data_type,
                            'is_nullable':nullable, 'is_identity':identity,
                            'column_default':default, 'identity_generation':generation})
        constraints = [
            {'table_name':'documents','constraint_type':'p','columns':['id'],
             'referenced_table':None,'referenced_columns':[],'delete_action':' '},
            {'table_name':'documents','constraint_type':'u','columns':['sha256'],
             'referenced_table':None,'referenced_columns':[],'delete_action':' '},
            {'table_name':'chunks','constraint_type':'p','columns':['id'],
             'referenced_table':None,'referenced_columns':[],'delete_action':' '},
            {'table_name':'chunks','constraint_type':'u','columns':['document_id','ordinal'],
             'referenced_table':None,'referenced_columns':[],'delete_action':' '},
            {'table_name':'chunks','constraint_type':'f','columns':['document_id'],
             'referenced_table':'documents','referenced_columns':['id'],'delete_action':'c'},
            {'table_name':'settings','constraint_type':'p','columns':['name'],
             'referenced_table':None,'referenced_columns':[],'delete_action':' '},
            {'table_name':'email_accounts','constraint_type':'p','columns':['id'],
             'referenced_table':None,'referenced_columns':[],'delete_action':' '},
            {'table_name':'email_imports','constraint_type':'p','columns':['account_id','remote_id'],
             'referenced_table':None,'referenced_columns':[],'delete_action':' '},
            {'table_name':'email_imports','constraint_type':'f','columns':['account_id'],
             'referenced_table':'email_accounts','referenced_columns':['id'],'delete_action':'c'},
            {'table_name':'email_imports','constraint_type':'f','columns':['document_id'],
             'referenced_table':'documents','referenced_columns':['id'],'delete_action':'c'},
            {'table_name':'ai_runs','constraint_type':'p','columns':['id'],
             'referenced_table':None,'referenced_columns':[],'delete_action':' '},
        ]
        indexes = [
            {'table_name':'chunks','index_name':'chunks_document_id_idx','index_definition':'CREATE INDEX chunks_document_id_idx ON wissensraum.chunks USING btree (document_id)'},
            {'table_name':'chunks','index_name':'chunks_search_idx','index_definition':"CREATE INDEX chunks_search_idx ON wissensraum.chunks USING gin (to_tsvector('simple'::regconfig, content))"},
        ]
        return columns, constraints, indexes

    def valid_legacy(self):
        return FakePostgres(self.required, columns=self.legacy_metadata()[0],
                            constraints=self.legacy_metadata()[1], indexes=self.legacy_metadata()[2])

    def test_existing_six_table_schema_is_adopted_as_version_four_under_lock(self):
        db = self.valid_legacy()
        migrate_postgres(db)
        statements = [query for query, _ in db.calls]
        lock_index = next(i for i, query in enumerate(statements) if 'pg_advisory_xact_lock' in query)
        inspection_index = next(i for i, query in enumerate(statements)
                                if 'FROM information_schema.tables' in query)
        self.assertLess(lock_index, inspection_index)
        self.assertEqual(db.version, CURRENT_SCHEMA_VERSION)
        self.assertTrue(any('CREATE TABLE' in query and 'wissensraum.schema_migrations' in query
                            for query in statements))
        self.assertFalse(any('GENERATED ALWAYS AS IDENTITY' in query for query in statements))

    def test_incompatible_six_table_legacy_schema_is_rejected_without_stamp(self):
        db = self.valid_legacy()
        db.columns = [row for row in db.columns
                      if not (row['table_name'] == 'documents' and row['column_name'] == 'sha256')]
        with self.assertRaisesRegex(ValueError, 'Legacy'):
            migrate_postgres(db)
        self.assertIsNone(db.version)

    def test_legacy_schema_without_required_search_index_is_rejected(self):
        db = self.valid_legacy()
        db.indexes = [row for row in db.indexes if row['index_name'] != 'chunks_search_idx']
        with self.assertRaisesRegex(ValueError, 'Legacy'):
            migrate_postgres(db)
        self.assertIsNone(db.version)

    def test_legacy_schema_with_wrong_type_identity_or_foreign_key_is_rejected(self):
        def wrong_type(db):
            next(row for row in db.columns if row['table_name'] == 'chunks'
                 and row['column_name'] == 'document_id')['data_type'] = 'integer'

        def missing_identity(db):
            next(row for row in db.columns if row['table_name'] == 'documents'
                 and row['column_name'] == 'id')['is_identity'] = 'NO'

        def missing_foreign_key(db):
            db.constraints = [row for row in db.constraints
                              if not (row['constraint_type'] == 'f'
                                      and row['table_name'] == 'email_imports'
                                      and row['columns'] == ['document_id'])]

        for name, corrupt in (('type', wrong_type), ('identity', missing_identity),
                              ('foreign_key', missing_foreign_key)):
            with self.subTest(name=name):
                db = self.valid_legacy()
                corrupt(db)
                with self.assertRaisesRegex(ValueError, 'Legacy'):
                    migrate_postgres(db)
                self.assertIsNone(db.version)

    def test_legacy_schema_with_missing_or_wrong_default_is_rejected(self):
        for column, default in (('host', None), ('tenant', "'wrong'::text"),
                                ('created_at', None)):
            with self.subTest(column=column, default=default):
                db = self.valid_legacy()
                next(row for row in db.columns if row['table_name'] == 'email_accounts'
                     and row['column_name'] == column)['column_default'] = default
                with self.assertRaisesRegex(ValueError, 'Legacy'):
                    migrate_postgres(db)
                self.assertIsNone(db.version)

    def test_legacy_schema_with_wrong_identity_generation_is_rejected(self):
        db = self.valid_legacy()
        next(row for row in db.columns if row['table_name'] == 'documents'
             and row['column_name'] == 'id')['identity_generation'] = 'BY DEFAULT'
        with self.assertRaisesRegex(ValueError, 'Legacy'):
            migrate_postgres(db)
        self.assertIsNone(db.version)

    def test_legacy_schema_with_extra_unique_or_foreign_key_is_rejected(self):
        extras = (
            {'table_name':'email_accounts','constraint_type':'u','columns':['email'],
             'referenced_table':None,'referenced_columns':[],'delete_action':' '},
            {'table_name':'email_accounts','constraint_type':'f','columns':['id'],
             'referenced_table':'email_accounts','referenced_columns':['id'],
             'delete_action':'a'},
        )
        for extra in extras:
            with self.subTest(constraint=extra['constraint_type']):
                db = self.valid_legacy()
                db.constraints.append(extra)
                with self.assertRaisesRegex(ValueError, 'Legacy'):
                    migrate_postgres(db)
                self.assertIsNone(db.version)

    def test_empty_postgres_database_runs_ordered_explicit_migrations(self):
        columns, constraints, indexes = self.legacy_metadata()
        db = FakePostgres(columns=columns, constraints=constraints, indexes=indexes)
        migrate_postgres(db)
        recorded_versions = [parameters[0] for query, parameters in db.calls
                             if query.startswith('INSERT INTO wissensraum.schema_migrations')]
        self.assertEqual(recorded_versions, [1, 2, 3, 4])
        self.assertEqual(db.version, CURRENT_SCHEMA_VERSION)
        self.assertTrue(any('GENERATED ALWAYS AS IDENTITY' in query for query, _ in db.calls))
        self.assertTrue(any("to_tsvector('simple',content)" in query for query, _ in db.calls))

    def test_partial_unversioned_schema_is_rejected_without_metadata_table(self):
        self._assert_unversioned_tables_rejected({'documents', 'chunks'})

    def test_exact_legacy_tables_plus_extra_is_rejected_without_metadata_table(self):
        self._assert_unversioned_tables_rejected(self.required | {'unexpected'})

    def test_unrelated_unversioned_schema_is_rejected_without_metadata_table(self):
        self._assert_unversioned_tables_rejected({'unrelated'})

    def _assert_unversioned_tables_rejected(self, tables):
        db = FakePostgres(tables)
        with self.assertRaisesRegex(ValueError, '[Uu]nversioniert'):
            migrate_postgres(db)
        statements = [query for query, _ in db.calls]
        self.assertFalse(any('CREATE TABLE' in query and 'wissensraum.schema_migrations' in query
                             for query in statements))
        self.assertFalse(any(query.startswith('INSERT INTO wissensraum.schema_migrations')
                             for query in statements))
        self.assertIsNone(db.version)

    def test_newer_postgres_schema_is_rejected_after_lock(self):
        db = FakePostgres(version=CURRENT_SCHEMA_VERSION + 1)
        with self.assertRaisesRegex(ValueError, 'neuer'):
            migrate_postgres(db)
        statements = [query for query, _ in db.calls]
        self.assertLess(next(i for i, query in enumerate(statements)
                             if query.startswith('SET LOCAL lock_timeout')),
                        next(i for i, query in enumerate(statements)
                             if 'pg_advisory_xact_lock' in query))
        self.assertFalse(any(query.startswith('INSERT INTO wissensraum.schema_migrations')
                             for query, _ in db.calls))

    def test_postgres_history_must_be_exactly_contiguous(self):
        for history in ([1, 3, 4], [1, 2, 2, 3, 4], [0, 1, 2, 3, 4]):
            with self.subTest(history=history):
                db = FakePostgres(version=4, history=history)
                with self.assertRaisesRegex(ValueError, 'Migrationshistorie'):
                    migrate_postgres(db)

    def test_current_postgres_schema_missing_table_is_rejected(self):
        columns, constraints, indexes = self.legacy_metadata()
        db = FakePostgres(version=CURRENT_SCHEMA_VERSION, columns=columns,
                          constraints=constraints, indexes=indexes,
                          current_tables=self.required - {'settings'})
        with self.assertRaisesRegex(ValueError, 'Schema'):
            migrate_postgres(db)

    def test_postgres_lock_timeout_is_local_and_precedes_advisory_lock(self):
        db = self.valid_legacy()
        migrate_postgres(db, lock_timeout_seconds=0.25)
        statements = [query for query, _ in db.calls]
        timeout_index = next(i for i, query in enumerate(statements)
                             if query.startswith('SET LOCAL lock_timeout'))
        lock_index = next(i for i, query in enumerate(statements)
                          if 'pg_advisory_xact_lock' in query)
        self.assertLess(timeout_index, lock_index)
        self.assertIn("'250ms'", db.calls[timeout_index][0])
        self.assertEqual(db.calls[timeout_index][1], ())

    def test_postgres_lock_timeout_is_sanitized(self):
        db = FakePostgres(lock_error=RuntimeError(
            'canceling statement due to lock timeout host=secret.internal user=admin'))
        with self.assertRaisesRegex(ValueError, '^Zeitüberschreitung beim Sperren der PostgreSQL-Migration\\.$'):
            migrate_postgres(db, lock_timeout_seconds=0.01)

    def test_postgres_store_initialization_uses_migration_layer(self):
        sentinel = object()

        @contextmanager
        def connection(_store):
            yield sentinel

        profile = {'host': 'db', 'port': 5432, 'database': 'wissen', 'username': 'user'}
        with patch.object(PostgresStore, 'connect', connection), \
             patch('core.migrate_postgres') as migrate:
            PostgresStore(profile, 'password')
        migrate.assert_called_once_with(sentinel)


if __name__ == '__main__':
    unittest.main()
