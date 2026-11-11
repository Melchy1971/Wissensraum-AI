import json
import os
import shutil
import sqlite3
from contextlib import closing
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from migrations import CURRENT_SCHEMA_VERSION
from diagnostics import (
    DiagnosticResult,
    check_active_database,
    check_data_directory,
    check_keyring,
    check_pillow,
    check_python,
    check_tcl_tk,
    format_diagnostics,
    health_overview,
    run_diagnostics,
)


def sqlite_schema_snapshot(path):
    connection = sqlite3.connect(path)
    try:
        return (connection.execute('PRAGMA user_version').fetchone()[0], tuple(connection.execute(
            "SELECT type,name,sql FROM sqlite_master ORDER BY type,name")))
    finally:
        connection.close()


def make_v1_sqlite(path):
    from core import Store
    store = Store(path)
    store.add('Altbestand', 'Unveränderte Suche in Schema Version eins')
    with store.connect() as connection:
        for table in ('chunk_vectors', 'rule_run_changes', 'rule_runs', 'rules', 'document_tags', 'tags',
                      'email_attachments', 'document_versions', 'import_item_errors', 'import_items', 'import_jobs',
                      'sources', 'metadata_changes', 'collections', 'collection_documents', 'favorites', 'saved_searches', 'document_relations',
                      'graph_changes', 'graph_relations', 'entity_mentions', 'entities', 'entity_types', 'relation_types'):
            connection.execute(f'DROP TABLE {table}')
        for column in ('status', 'owner', 'review_date', 'revision'):
            connection.execute(f'ALTER TABLE documents DROP COLUMN {column}')
        connection.execute('ALTER TABLE ai_runs DROP COLUMN account_id')
        connection.execute('DROP TABLE email_imports')
        connection.execute('DROP TABLE email_accounts')
        connection.execute('DROP TABLE settings')
        connection.execute('PRAGMA user_version=1')
    return store


def sqlite_content_snapshot(path):
    connection = sqlite3.connect(path)
    try:
        return {
            'documents': tuple(connection.execute('SELECT * FROM documents ORDER BY id')),
            'chunks': tuple(connection.execute('SELECT * FROM chunks ORDER BY id')),
            'search_index': tuple(connection.execute(
                'SELECT rowid,title,category,content FROM search_index ORDER BY rowid')),
            'counts': tuple(connection.execute(
                'SELECT (SELECT count(*) FROM documents), (SELECT count(*) FROM chunks), '
                '(SELECT count(*) FROM search_index)').fetchone()),
        }
    finally:
        connection.close()


def sqlite_application_snapshot(path):
    connection = sqlite3.connect(path)
    try:
        return {
            table: tuple(connection.execute(f'SELECT * FROM {table}').fetchall())
            for table in ('documents', 'chunks', 'ai_runs', 'settings',
                          'email_accounts', 'email_imports')
        }
    finally:
        connection.close()


class DiagnosticTests(unittest.TestCase):
    def _configured_database_result(self, folder, saved, environment):
        from database import DatabaseSettings
        path = Path(folder) / 'data' / 'database.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(saved), encoding='utf-8')
        vault = types.SimpleNamespace(get=lambda *_: 'synthetic-db-password')
        settings = DatabaseSettings(Path(folder), vault, environ=environment)
        with patch('core.PostgresStore.health', return_value={'backend': 'PostgreSQL'}):
            return check_active_database(settings, Path(folder) / 'wissen.db')

    def test_explicit_empty_saved_postgresql_fields_do_not_fallback_to_environment(self):
        environment = {
            'DB_HOST': 'db.example.test', 'DB_PORT': '5432', 'DB_DATABASE': 'wissen_test',
            'DB_USERNAME': 'test_user', 'DB_SSLMODE': 'require',
        }
        for field in ('host', 'port', 'database', 'username', 'sslmode'):
            for value in ('', None):
                with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as folder:
                    result = self._configured_database_result(
                        folder, {'backend': 'postgresql', field: value}, environment)
                    self.assertEqual(result.status, 'FAIL')

    def test_invalid_saved_postgresql_types_and_port_boundaries_fail_closed(self):
        environment = {
            'DB_HOST': 'db.example.test', 'DB_PORT': '5432', 'DB_DATABASE': 'wissen_test',
            'DB_USERNAME': 'test_user', 'DB_SSLMODE': 'require',
        }
        cases = (
            ('host', True), ('database', False), ('username', True), ('sslmode', False),
            ('port', True), ('port', False), ('port', 0), ('port', 65536),
            ('host', ['db.example.test']), ('port', {'value': 5432}),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as folder:
                result = self._configured_database_result(
                    folder, {'backend': 'postgresql', field: value}, environment)
                self.assertEqual(result.status, 'FAIL')

    def test_missing_saved_postgresql_fields_still_fall_back_to_environment(self):
        environment = {
            'DB_HOST': 'db.example.test', 'DB_PORT': '5432', 'DB_DATABASE': 'wissen_test',
            'DB_USERNAME': 'test_user', 'DB_SSLMODE': 'require',
        }
        with tempfile.TemporaryDirectory() as folder:
            result = self._configured_database_result(folder, {'backend': 'postgresql'}, environment)
        self.assertEqual(result.status, 'PASS')

    def test_python_check_has_stable_structured_result(self):
        supported = check_python((3, 11, 9))
        unsupported = check_python((3, 10, 14))
        too_new = check_python((3, 14, 0))

        self.assertEqual(supported, DiagnosticResult('python', 'PASS', 'Python 3.11 wird unterstützt.', {'version': '3.11.9'}))
        self.assertEqual(unsupported.status, 'FAIL')
        self.assertEqual(unsupported.check_id, 'python')
        self.assertEqual(unsupported.details, {'version': '3.10.14', 'supported': '3.11–3.13'})
        self.assertEqual(too_new.status, 'FAIL')
        self.assertEqual(too_new.details, {'version': '3.14.0', 'supported': '3.11–3.13'})

    def test_python_check_rejects_prerelease_and_dev_builds(self):
        for version_info in ((3, 11, 0, 'candidate', 1), (3, 13, 0, 'beta', 2),
                             (3, 12, 0, 'alpha', 4)):
            with self.subTest(version_info=version_info):
                result = check_python(version_info)
                self.assertEqual(result.status, 'FAIL')
                self.assertEqual(result.details['version'], '.'.join(map(str, version_info[:3])))
        self.assertEqual(check_python(sys.version_info).status, 'PASS')

    def test_runtime_library_checks_are_injected_and_keyring_is_not_written(self):
        class Tcl:
            def call(self, *_): return '8.6.14'
        TkModule = types.SimpleNamespace(Tcl=Tcl, TkVersion=8.6)
        class ImageModule:
            __version__ = '12.3.0'
            @staticmethod
            def new(*_): return object()
        ImageTkModule = types.SimpleNamespace(PhotoImage=object)
        class Backend:
            def get_password(self, *_): return None
            def set_password(self, *_): raise AssertionError('must not write a secret')
            def delete_password(self, *_): raise AssertionError('must not delete a secret')
        Backend.__module__ = 'keyring.backends.Windows'
        class Vault:
            def backend(self): return Backend()
            def get(self, *_): raise AssertionError('must not use the error-swallowing vault read')
            def set(self, *_): raise AssertionError('must not write a secret')

        self.assertEqual(check_tcl_tk(lambda: TkModule).details, {'version': '8.6.14'})
        self.assertEqual(check_pillow(lambda: ImageModule, lambda: ImageTkModule).details,
                         {'version': '12.3.0'})
        result = check_keyring(Vault())
        self.assertEqual(result.status, 'PASS')
        self.assertEqual(result.details, {'backend': 'Windows'})

    def test_keyring_backend_read_failure_fails_without_writes_or_deletes(self):
        backend = Mock()
        backend.get_password.side_effect = RuntimeError('backend locked')
        vault = Mock()
        vault.backend.return_value = backend

        result = check_keyring(vault)

        self.assertEqual(result.status, 'FAIL')
        backend.get_password.assert_called_once()
        backend.set_password.assert_not_called()
        backend.delete_password.assert_not_called()
        vault.get.assert_not_called()
        self.assertNotIn('backend locked', str(result))

    def test_tcl_without_tk_metadata_fails(self):
        class Tcl:
            def call(self, *_): return '8.6.14'

        result = check_tcl_tk(lambda: types.SimpleNamespace(Tcl=Tcl))

        self.assertEqual(result.status, 'FAIL')
        self.assertEqual(result.details, {})

    def test_pillow_missing_imagetk_adapter_fails(self):
        class ImageModule:
            __version__ = '12.3.0'
            @staticmethod
            def new(*_): return object()

        result = check_pillow(
            lambda: ImageModule,
            lambda: (_ for _ in ()).throw(ImportError('PIL.ImageTk unavailable')),
        )

        self.assertEqual(result.status, 'FAIL')
        self.assertEqual(result.details, {})
        self.assertNotIn('ImageTk unavailable', str(result))

    def test_data_directory_probe_is_removed(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / 'data'
            data.mkdir()
            before = set(data.iterdir())
            result = check_data_directory(data)
            self.assertEqual(result.status, 'PASS')
            self.assertEqual(set(data.iterdir()), before)
            self.assertEqual(result.details, {'directory': 'data'})

    def test_missing_data_directory_is_checked_without_creating_it(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / 'data'
            result = check_data_directory(data)
            self.assertEqual(result.status, 'PASS')
            self.assertFalse(data.exists())
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_missing_data_directory_preserves_directory_created_concurrently(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / 'data'
            real_mkdir = Path.mkdir
            created = False

            def create_target_during_probe(candidate, *args, **kwargs):
                nonlocal created
                result = real_mkdir(candidate, *args, **kwargs)
                if not created and candidate != data:
                    created = True
                    real_mkdir(data)
                return result

            with patch('diagnostics.Path.mkdir', autospec=True,
                       side_effect=create_target_during_probe):
                result = check_data_directory(data)

            self.assertEqual(result.status, 'PASS')
            self.assertTrue(data.is_dir())

    def test_missing_data_directory_fails_when_file_appears_during_probe(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / 'data'
            real_mkdir = Path.mkdir
            created = False

            def create_file_during_probe(candidate, *args, **kwargs):
                nonlocal created
                result = real_mkdir(candidate, *args, **kwargs)
                if not created and candidate != data:
                    created = True
                    data.write_text('concurrent file', encoding='utf-8')
                return result

            with patch('diagnostics.Path.mkdir', autospec=True,
                       side_effect=create_file_during_probe):
                result = check_data_directory(data)

            self.assertEqual(result.status, 'FAIL')
            self.assertTrue(data.is_file())
            self.assertEqual(data.read_text(encoding='utf-8'), 'concurrent file')

    def test_missing_data_directory_fails_when_directory_creation_is_denied(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / 'data'

            with patch('diagnostics.Path.mkdir', side_effect=PermissionError('synthetic denial')):
                result = check_data_directory(data)

            self.assertEqual(result.status, 'FAIL')
            self.assertFalse(data.exists())
            self.assertNotIn('synthetic denial', str(result))

    def test_missing_sqlite_database_is_a_non_mutating_warning(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'data' / 'wissen.db'
            from database import DatabaseSettings
            settings = DatabaseSettings(Path(folder), types.SimpleNamespace(get=lambda *_: None), environ={})
            result = check_active_database(settings, path)
            self.assertEqual(result.status, 'WARN')
            self.assertFalse(path.exists())
            self.assertFalse(path.parent.exists())
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_interrupted_missing_sqlite_probe_never_creates_the_configured_target(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'data' / 'wissen.db'
            from database import DatabaseSettings
            settings = DatabaseSettings(Path(folder), types.SimpleNamespace(get=lambda *_: None), environ={})
            target_seen_during_probe = []

            def interrupt_during_write(_descriptor, _content):
                target_seen_during_probe.append(path.exists())
                raise KeyboardInterrupt('synthetic interruption')

            with patch('database.os.write', side_effect=interrupt_during_write), self.assertRaises(KeyboardInterrupt):
                settings.health(path)

            self.assertEqual(target_seen_during_probe, [False])
            self.assertFalse(path.exists())
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_missing_sqlite_probe_preserves_directory_created_concurrently(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'data' / 'wissen.db'
            from database import DatabaseSettings
            settings = DatabaseSettings(Path(folder), types.SimpleNamespace(get=lambda *_: None), environ={})
            real_write = __import__('os').write

            def concurrent_directory_creation(descriptor, content):
                path.parent.mkdir(exist_ok=True)
                return real_write(descriptor, content)

            with patch('database.os.write', side_effect=concurrent_directory_creation):
                result = settings.health(path)

            self.assertEqual(result, {'backend': 'SQLite', 'missing': True})
            self.assertTrue(path.parent.is_dir())
            self.assertFalse(path.exists())

    def test_missing_sqlite_database_with_file_as_parent_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            parent = Path(folder) / 'not-a-directory'
            parent.write_text('unchanged',encoding='utf-8')
            path = parent / 'wissen.db'
            from database import DatabaseSettings
            settings = DatabaseSettings(Path(folder), types.SimpleNamespace(get=lambda *_: None), environ={})

            result = check_active_database(settings, path, force_sqlite=True)

            self.assertEqual(result.status, 'FAIL')
            self.assertEqual(parent.read_text(encoding='utf-8'), 'unchanged')

    def test_empty_existing_sqlite_database_fails_schema_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'wissen.db'
            sqlite3.connect(path).close()
            from database import DatabaseSettings
            settings = DatabaseSettings(Path(folder), types.SimpleNamespace(get=lambda *_: None), environ={})

            result = check_active_database(settings, path)

            self.assertEqual(result.status, 'FAIL')
            self.assertEqual(result.details, {'backend': 'SQLite'})

    def test_unusable_sqlite_path_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'wissen.db'
            path.mkdir()
            from database import DatabaseSettings
            settings = DatabaseSettings(Path(folder), types.SimpleNamespace(get=lambda *_: None), environ={})

            result = check_active_database(settings, path)

            self.assertEqual(result.status, 'FAIL')
            self.assertEqual(result.details, {'backend': 'SQLite'})

    def test_valid_sqlite_database_check_rolls_back_probe_data(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'wissen.db'
            from core import Store
            from database import DatabaseSettings
            store = Store(path)
            store.add('Bleibt', 'Unveränderte Nutzerdaten')
            settings = DatabaseSettings(Path(folder), types.SimpleNamespace(get=lambda *_: None), environ={})

            result = check_active_database(settings, path)

            self.assertEqual(result, DiagnosticResult('database', 'PASS', 'Aktive SQLite-Datenbank ist betriebsbereit.',
                                                      {'backend': 'SQLite'}))
            self.assertEqual(store.list()[0]['title'], 'Bleibt')
            with store.connect() as connection:
                self.assertFalse(connection.execute(
                    "SELECT 1 FROM settings WHERE name='.wissensraum_health_probe'").fetchone())

    def test_sqlite_health_writes_every_application_table_then_rolls_back_without_residue(self):
        from core import Store
        from database import DatabaseSettings
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'wissen.db'
            Store(path)
            before = sqlite_application_snapshot(path)
            traced = []
            real_connect = sqlite3.connect

            def traced_connect(*args, **kwargs):
                connection = real_connect(*args, **kwargs)
                connection.set_trace_callback(traced.append)
                return connection

            settings = DatabaseSettings(Path(folder), types.SimpleNamespace(get=lambda *_: None), environ={})
            with patch('core.sqlite3.connect', side_effect=traced_connect):
                result = check_active_database(settings, path)

            self.assertEqual(result.status, 'PASS')
            for table in ('documents', 'chunks', 'ai_runs', 'settings',
                          'email_accounts', 'email_imports'):
                with self.subTest(table=table):
                    self.assertTrue(any(statement.startswith(f'INSERT INTO {table}')
                                        for statement in traced))
            self.assertIn('ROLLBACK', traced)
            self.assertEqual(sqlite_application_snapshot(path), before)
            self.assertEqual(list(path.parent.glob('wissen.db-*')), [])

    def test_sqlite_health_fails_when_any_application_table_write_is_denied(self):
        from core import Store
        from database import DatabaseSettings
        for table in ('documents', 'chunks', 'ai_runs', 'settings',
                      'email_accounts', 'email_imports'):
            with self.subTest(table=table), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'wissen.db'
                Store(path)
                connection = sqlite3.connect(path)
                try:
                    connection.execute(f'''
                        CREATE TRIGGER deny_health_insert BEFORE INSERT ON {table} BEGIN
                          SELECT RAISE(ABORT, 'synthetic {table} write denied');
                        END
                    ''')
                    connection.commit()
                finally:
                    connection.close()
                before = sqlite_application_snapshot(path)
                settings = DatabaseSettings(Path(folder), types.SimpleNamespace(get=lambda *_: None), environ={})

                result = check_active_database(settings, path)

                self.assertEqual(result.status, 'FAIL')
                self.assertNotIn('synthetic', str(result))
                self.assertEqual(sqlite_application_snapshot(path), before)

    def test_legacy_sqlite_versions_warn_without_mutation_then_migrate(self):
        from core import Store
        from database import DatabaseSettings
        for version in (1, 2, 3, 4, 5, 6, 7, 8, 9):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'wissen.db'
                store = Store(path)
                store.add('Altbestand', f'Migration Version {version}')
                with store.connect() as connection:
                    for table in ('metadata_changes', 'collections', 'collection_documents', 'favorites', 'saved_searches', 'document_relations',
                                  'graph_changes', 'graph_relations', 'entity_mentions', 'entities', 'entity_types', 'relation_types',
                                  'ai_usage', 'audit_log', 'web_sessions', 'login_requests', 'account_favorites', 'account_saved_searches',
                                  'grants', 'accounts', 'organizations'):
                        connection.execute(f'DROP TABLE {table}')
                    for column in ('status', 'owner', 'review_date', 'revision'):
                        connection.execute(f'ALTER TABLE documents DROP COLUMN {column}')
                    connection.execute('ALTER TABLE ai_runs DROP COLUMN account_id')
                    connection.execute('ALTER TABLE import_jobs DROP COLUMN account_id')
                    connection.execute('ALTER TABLE import_items DROP COLUMN upload')
                    connection.execute('DROP TABLE chunk_vectors')
                    if version < 9:
                        for table in ('rule_run_changes', 'rule_runs', 'rules', 'document_tags', 'tags'):
                            connection.execute(f'DROP TABLE {table}')
                    if version < 8: connection.execute('DROP TABLE email_attachments')
                    if version < 7: connection.execute('DROP TABLE document_versions')
                    if version < 6:
                        for table in ('import_item_errors', 'import_items', 'import_jobs'):
                            connection.execute(f'DROP TABLE {table}')
                    if version < 5: connection.execute('DROP TABLE sources')
                    if version < 4: connection.execute('DROP TABLE email_imports')
                    if version < 3: connection.execute('DROP TABLE email_accounts')
                    if version < 2: connection.execute('DROP TABLE settings')
                    connection.execute(f'PRAGMA user_version={version}')
                before = sqlite_schema_snapshot(path)
                settings = DatabaseSettings(Path(folder), types.SimpleNamespace(get=lambda *_: None), environ={})

                result = check_active_database(settings, path)

                self.assertEqual(result.status, 'WARN')
                self.assertEqual(sqlite_schema_snapshot(path), before)
                migrated = Store(path)
                with migrated.connect() as connection:
                    self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0],
                                     CURRENT_SCHEMA_VERSION)
                    self.assertEqual(tuple(connection.execute(
                        'SELECT source_type,account_id,locator FROM sources').fetchall()[0]),
                        ('note', '', ''))
                self.assertEqual(migrated.search('Migration')[0]['title'], 'Altbestand')

    def test_sqlite_v1_health_executes_real_write_at_custom_path_and_rolls_it_back(self):
        from database import DatabaseSettings
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            path = base / 'custom-database-location' / 'legacy.db'
            store = make_v1_sqlite(path)
            before = sqlite_content_snapshot(path)
            traced = []
            connect_targets = []
            real_connect = sqlite3.connect

            def traced_connect(*args, **kwargs):
                connect_targets.append(args[0])
                connection = real_connect(*args, **kwargs)
                connection.set_trace_callback(traced.append)
                return connection

            settings = DatabaseSettings(base, types.SimpleNamespace(get=lambda *_: None), environ={})
            with patch('core.sqlite3.connect', side_effect=traced_connect):
                result = check_active_database(settings, path)

            self.assertEqual(result.status, 'WARN')
            self.assertEqual(result.details, {'backend': 'SQLite'})
            self.assertIn(f'Schema {CURRENT_SCHEMA_VERSION}', result.message)
            self.assertIn(path.resolve().as_uri() + '?mode=rw', connect_targets)
            self.assertIn('BEGIN IMMEDIATE', traced)
            self.assertTrue(any(statement.startswith('INSERT INTO documents') for statement in traced))
            self.assertIn('ROLLBACK', traced)
            self.assertEqual(sqlite_content_snapshot(path), before)
            self.assertEqual(list(path.parent.glob('legacy.db-*')), [])  # before our own connection below
            with closing(sqlite3.connect(path)) as connection:  # unmigrated: query the v1 index directly
                self.assertEqual(connection.execute(
                    'SELECT d.title FROM search_index JOIN chunks c ON c.id=search_index.rowid '
                    "JOIN documents d ON d.id=c.document_id WHERE search_index MATCH 'Unveränderte'"
                    ).fetchone()[0], 'Altbestand')

    def test_sqlite_v1_health_fails_closed_when_document_write_is_denied(self):
        from database import DatabaseSettings
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            path = base / 'custom-database-location' / 'legacy.db'
            make_v1_sqlite(path)
            connection = sqlite3.connect(path)
            try:
                connection.execute('''
                    CREATE TRIGGER deny_health_document_insert BEFORE INSERT ON documents BEGIN
                      SELECT RAISE(ABORT, 'synthetic document write denied');
                    END
                ''')
                connection.commit()
            finally:
                connection.close()
            before = sqlite_content_snapshot(path)
            settings = DatabaseSettings(base, types.SimpleNamespace(get=lambda *_: None), environ={})

            result = check_active_database(settings, path)

            self.assertEqual(result.status, 'FAIL')
            self.assertEqual(result.details, {'backend': 'SQLite'})
            self.assertNotIn('synthetic document write denied', str(result))
            self.assertEqual(sqlite_content_snapshot(path), before)

    def test_sqlite_health_rejects_newer_and_incompatible_legacy_schema(self):
        from core import Store
        from database import DatabaseSettings
        for version,corrupt in ((CURRENT_SCHEMA_VERSION + 1,False),(2,True)):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'wissen.db'
                Store(path)
                connection = sqlite3.connect(path)
                try:
                    connection.execute(f'PRAGMA user_version={version}')
                    if corrupt:
                        connection.execute('ALTER TABLE documents DROP COLUMN filename')
                    connection.commit()
                finally:
                    connection.close()
                settings = DatabaseSettings(Path(folder), types.SimpleNamespace(get=lambda *_: None), environ={})
                self.assertEqual(check_active_database(settings, path).status, 'FAIL')

    def test_sqlite_health_rejects_missing_column_constraint_and_search_object(self):
        from core import SCHEMA, Store
        from database import DatabaseSettings
        for corruption in ('missing_column', 'missing_unique', 'missing_trigger'):
            with self.subTest(corruption=corruption), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'wissen.db'
                if corruption == 'missing_unique':
                    connection = sqlite3.connect(path)
                    connection.executescript(SCHEMA.replace(', UNIQUE(document_id, ordinal)', ''))
                    connection.close()
                else:
                    Store(path)
                connection = sqlite3.connect(path)
                try:
                    if corruption == 'missing_column':
                        connection.execute('ALTER TABLE documents DROP COLUMN filename')
                    elif corruption == 'missing_trigger':
                        connection.execute('DROP TRIGGER chunk_insert')
                    connection.commit()
                finally:
                    connection.close()
                settings = DatabaseSettings(Path(folder), types.SimpleNamespace(get=lambda *_: None), environ={})
                self.assertEqual(check_active_database(settings, path).status, 'FAIL')

    def test_sqlite_health_rejects_malformed_column_metadata_and_tokenizer(self):
        from core import SCHEMA
        from database import DatabaseSettings
        corruptions=(
            ('nullable_title', SCHEMA.replace('title TEXT NOT NULL', 'title TEXT', 1)),
            ('wrong_id_type', SCHEMA.replace('id INTEGER PRIMARY KEY', 'id TEXT PRIMARY KEY', 1)),
            ('missing_timestamp_default', SCHEMA.replace(
                'created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP', 'created_at TEXT NOT NULL', 1)),
            ('wrong_tokenizer', SCHEMA.replace("unicode61 remove_diacritics 2", "unicode61 remove_diacritics 1")),
        )
        for corruption,schema in corruptions:
            with self.subTest(corruption=corruption), tempfile.TemporaryDirectory() as folder:
                path=Path(folder)/'wissen.db'
                connection=sqlite3.connect(path)
                try:
                    connection.executescript(schema)
                finally:
                    connection.close()
                settings=DatabaseSettings(Path(folder),types.SimpleNamespace(get=lambda *_:None),environ={})
                self.assertEqual(check_active_database(settings,path).status,'FAIL')

    @unittest.skipUnless(shutil.which('sh'), 'sh nicht im Suchpfad; unter Windows Git Bash in PATH aufnehmen')
    def test_shell_launcher_uses_supported_existing_venv_without_system_python(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            launcher = work / 'start.sh'
            launcher.write_text(
                (root / 'start.sh').read_text(encoding='utf-8').replace('\r\n', '\n'),
                encoding='utf-8',
            )
            venv_python = work / '.venv' / 'bin' / 'python'
            venv_python.parent.mkdir(parents=True)
            venv_python.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$*" >> "$LAUNCHER_LOG"\nexit 0\n',
                encoding='utf-8',
            )
            venv_python.chmod(0o755)
            log = work / 'calls.log'
            environment = os.environ.copy()
            environment.update(PYTHON='system-python-must-not-run', LAUNCHER_LOG=str(log))

            result = subprocess.run(
                ['sh', str(launcher)], cwd=work, env=environment,
                text=True, capture_output=True, check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            calls = log.read_text(encoding='utf-8')
            self.assertIn('sys.version_info', calls)
            self.assertIn('-m pip install -r requirements.txt', calls)
            self.assertIn('app.py', calls)

    @unittest.skipUnless(shutil.which('sh'), 'sh nicht im Suchpfad; unter Windows Git Bash in PATH aufnehmen')
    def test_shell_launcher_gates_venv_creation_on_supported_system_python(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            launcher = work / 'start.sh'
            launcher.write_text(
                (root / 'start.sh').read_text(encoding='utf-8').replace('\r\n', '\n'),
                encoding='utf-8',
            )
            system_python = work / 'unsupported-python'
            system_python.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$*" >> "$LAUNCHER_LOG"\nexit 1\n',
                encoding='utf-8',
            )
            system_python.chmod(0o755)
            log = work / 'calls.log'
            environment = os.environ.copy()
            environment.update(PYTHON=str(system_python), LAUNCHER_LOG=str(log))

            result = subprocess.run(
                ['sh', str(launcher)], cwd=work, env=environment,
                text=True, capture_output=True, check=False,
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn('sys.version_info', log.read_text(encoding='utf-8'))
            self.assertFalse((work / '.venv').exists())

    @unittest.skipUnless(shutil.which('sh'), 'sh nicht im Suchpfad; unter Windows Git Bash in PATH aufnehmen')
    def test_shell_launcher_rejects_unsupported_existing_venv_without_system_fallback(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder)
            launcher = work / 'start.sh'
            launcher.write_text(
                (root / 'start.sh').read_text(encoding='utf-8').replace('\r\n', '\n'),
                encoding='utf-8',
            )
            venv_python = work / '.venv' / 'bin' / 'python'
            venv_python.parent.mkdir(parents=True)
            venv_python.write_text('#!/bin/sh\nexit 1\n', encoding='utf-8')
            venv_python.chmod(0o755)
            environment = os.environ.copy()
            environment['PYTHON'] = 'system-python-must-not-run'

            result = subprocess.run(
                ['sh', str(launcher)], cwd=work, env=environment,
                text=True, capture_output=True, check=False,
            )

            self.assertEqual(result.returncode, 1)
            self.assertNotIn('system-python-must-not-run', result.stderr)
            self.assertIn('virtuelle Umgebung', result.stderr)

    def test_windows_launchers_share_supported_python_selection_contract(self):
        root = Path(__file__).resolve().parents[1]
        launchers = {
            name: (root / name).read_text(encoding='utf-8')
            for name in ('start_windows.bat', 'install-pst-support.bat')
        }
        supported_range = (
            "(3, 11) <= sys.version_info[:2] <= (3, 13) "
            "and sys.version_info.releaselevel == 'final'"
        )

        for name, launcher in launchers.items():
            with self.subTest(launcher=name):
                self.assertIn(supported_range, launcher)
                self.assertIn('for %%V in (3.13 3.12 3.11) do (', launcher)
                self.assertIn('set "PYSEL=%%V"', launcher)
                self.assertIn('py -%PYSEL% -m venv .venv', launcher)
                commands = [line.strip().lower() for line in launcher.splitlines()
                            if line.strip() and not line.strip().lower().startswith('rem ')]
                self.assertFalse(any(line.startswith('py -3 ') for line in commands))

    def test_windows_launchers_validate_before_deleting_or_creating_venv(self):
        root = Path(__file__).resolve().parents[1]
        launchers = {
            name: (root / name).read_text(encoding='utf-8')
            for name in ('start_windows.bat', 'install-pst-support.bat')
        }

        for name, launcher in launchers.items():
            with self.subTest(launcher=name):
                existing = launcher.index('if exist .venv\\Scripts\\python.exe')
                venv_validation = launcher.index('.venv\\Scripts\\python.exe -c', existing)
                system_validation = launcher.index('call :require_system_python')
                creation_command = 'py -%PYSEL% -m venv .venv'
                self.assertIn(creation_command, launcher)
                creation = launcher.index(creation_command)
                self.assertLess(existing, venv_validation)
                self.assertLess(venv_validation, system_validation)
                self.assertLess(system_validation, creation)
                for mutation in ('rmdir /s /q .venv', 'py -%PYSEL% -m venv .venv'):
                    offset = 0
                    while True:
                        mutation_at = launcher.find(mutation, offset)
                        if mutation_at < 0:
                            break
                        validation_at = launcher.rfind('call :require_system_python', 0,
                                                       mutation_at)
                        self.assertGreaterEqual(validation_at, 0)
                        offset = mutation_at + len(mutation)

    def test_windows_launcher_repair_validates_before_removing_venv(self):
        root = Path(__file__).resolve().parents[1]
        windows = (root / 'start_windows.bat').read_text(encoding='utf-8')
        repair = windows.index('echo Beschaedigte Python-Umgebung erkannt.')
        repair_validation = windows.index('call :require_system_python', repair)
        repair_removal = windows.index('rmdir /s /q .venv', repair)
        self.assertLess(repair_validation, repair_removal)

    def test_postgresql_database_failure_is_closed_and_redacted(self):
        secret = 'synthetic-secret-password'
        class Settings:
            def profile(self): return {'backend': 'postgresql'}
            def health(self, *_args, **_kwargs): raise RuntimeError(secret)
            def _connect(self, *_): raise AssertionError('diagnostics must not call private connection APIs')

        result = check_active_database(Settings(), Path('unused.db'))

        self.assertEqual(result.status, 'FAIL')
        self.assertEqual(result.details, {'backend': 'PostgreSQL'})
        self.assertNotIn(secret, str(result))
        self.assertNotIn('secret.internal', str(result))

    def test_database_check_uses_public_health_abstraction(self):
        settings = Mock()
        settings.profile.return_value = {'backend': 'postgresql'}
        settings.health.return_value = {'backend': 'PostgreSQL'}

        result = check_active_database(settings, Path('unused.db'))

        self.assertEqual(result.status, 'PASS')
        settings.health.assert_called_once_with(Path('unused.db'), force_sqlite=False)

    def test_explicit_sqlite_override_ignores_configured_postgresql(self):
        from database import DatabaseSettings
        with tempfile.TemporaryDirectory() as folder:
            settings = DatabaseSettings(Path(folder), types.SimpleNamespace(get=lambda *_: None),
                                        environ={'DB_BACKEND':'postgresql'})
            path = Path(folder)/'alternate.db'

            result = check_active_database(settings, path, force_sqlite=True)

            self.assertEqual(result.status, 'WARN')
            self.assertFalse(path.exists())

    def test_database_profile_error_is_reported_without_raising(self):
        class Settings:
            def profile(self): raise RuntimeError('DB_PASSWORD=synthetic-secret')

        result = check_active_database(Settings(), Path('unused.db'))

        self.assertEqual(result.status, 'FAIL')
        self.assertEqual(result.details, {'backend': 'Unknown'})
        self.assertNotIn('synthetic-secret', str(result))

    def test_runner_and_human_output_have_deterministic_order(self):
        results = run_diagnostics(checks=[
            lambda: DiagnosticResult('python', 'PASS', 'Python ok.', {'version': '3.13.7'}),
            lambda: DiagnosticResult('database', 'FAIL', 'Datenbank prüfen.', {'backend': 'SQLite'}),
        ])
        self.assertEqual([result.check_id for result in results], ['python', 'database'])
        self.assertEqual(format_diagnostics(results),
                         '[PASS] python: Python ok. (version=3.13.7)\n'
                         '[FAIL] database: Datenbank prüfen. (backend=SQLite)\n'
                         'Ergebnis: 1 bestanden, 0 Warnungen, 1 fehlgeschlagen.')


class HealthOverviewTests(unittest.TestCase):
    def setUp(self):
        # The overview includes the optional OCR; its state is fixed here so that these tests do
        # not depend on whether and how Tesseract is installed on the test computer.
        not_installed = {'available': False, 'reason': 'packages', 'version': '', 'languages': [],
                         'message': 'OCR-Erweiterung nicht installiert.'}
        self.enterContext(patch('ocr.status', return_value=not_installed))

    class Vault:
        def __init__(self, secrets=None):
            self.secrets = dict(secrets or {})

        def get(self, name):
            return self.secrets.get(name)

    def build(self, secrets=None, provider='openai'):
        from core import Store
        from importer import MailAccounts
        from settings import Settings
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        store = Store(folder / 'wissen.db')
        store.add('Dokument', 'Inhalt')
        with store.connect() as db:
            db.execute("INSERT INTO settings(name,value) VALUES('active_provider',?)", (provider,))
            db.execute("INSERT INTO email_accounts(id,label,provider,email,username) "
                       "VALUES('a','Arbeit','gmail','a@example.test','a@example.test')")
        vault = self.Vault(secrets)
        return store, Settings(store, folder, vault, environ={}), MailAccounts(store, vault, folder)

    def test_missing_ai_key_and_mail_secret_are_warnings_without_secret_values(self):
        from settings import PROVIDERS
        store, settings, accounts = self.build()
        with patch('urllib.request.urlopen', side_effect=AssertionError('network')), \
                patch('imaplib.IMAP4_SSL', side_effect=AssertionError('network')):
            results = health_overview(store, settings, accounts, PROVIDERS)
        self.assertEqual([(r.check_id, r.status) for r in results[:3]],
                         [('database', 'PASS'), ('ai_provider', 'WARN'), ('mail_accounts', 'WARN')])
        self.assertEqual((results[3].check_id, results[3].status), ('ocr', 'PASS'))  # not installed is valid
        self.assertEqual(results[0].details['documents'], 1)
        self.assertIn('Arbeit', results[2].message)

    def test_configured_state_passes_and_never_exposes_keys(self):
        from settings import PROVIDERS
        secret = 'sk-GEHEIM-1234567890'
        store, settings, accounts = self.build({'openai': secret, 'mail:a': 'app-passwort'})
        text = format_diagnostics(health_overview(store, settings, accounts, PROVIDERS))
        self.assertNotIn('FAIL', text.split('Ergebnis')[0])
        self.assertNotIn('WARN', text.split('Ergebnis')[0])
        self.assertNotIn(secret, text)
        self.assertNotIn('app-passwort', text)

    def test_ollama_needs_no_key_and_broken_store_fails_closed(self):
        from settings import PROVIDERS
        store, settings, accounts = self.build(provider='ollama')
        self.assertEqual(health_overview(store, settings, accounts, PROVIDERS)[1].status, 'PASS')
        with patch.object(store, 'connect', side_effect=OSError('kaputt')):
            results = health_overview(store, settings, accounts, PROVIDERS)
        self.assertEqual(results[0].status, 'FAIL')
        self.assertEqual(results[2].status, 'FAIL')


if __name__ == '__main__':
    unittest.main()
