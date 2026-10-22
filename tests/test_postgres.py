"""Real PostgreSQL contract tests; destructive runs require explicit opt-in."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import os
import time
import tempfile
import unittest
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import PostgresStore, _PostgresConnection
from configuration import (ConfigurationTransfer, ConfigurationTransferError,
                           recover_pending_configuration)
from database import DatabaseSettings
from design import DesignSettings
from importer import MailAccounts
from migrations import CURRENT_SCHEMA_VERSION, migrate_postgres
from settings import Settings
from test_settings import FakeVault

MIGRATION_LOCK_ID = 87031904
TEST_SUITE_LOCK_ID = 87031905


def _disposable_database_name(environ):
    database = str(environ.get('PGTEST_DATABASE') or '')
    if environ.get('PGTEST_ALLOW_DESTRUCTIVE') != '1':
        raise RuntimeError('PGTEST_ALLOW_DESTRUCTIVE=1 is required for destructive PostgreSQL tests')
    if not database or not database.lower().endswith('_test'):
        raise RuntimeError('PGTEST_DATABASE must explicitly name a disposable database ending in _test')
    return database


class PostgresDestructiveGuardTests(unittest.TestCase):
    def test_guard_requires_explicit_opt_in(self):
        with self.assertRaisesRegex(RuntimeError, 'PGTEST_ALLOW_DESTRUCTIVE'):
            _disposable_database_name({'PGTEST_DATABASE': 'wissensraum_test'})

    def test_guard_rejects_non_test_database(self):
        with self.assertRaisesRegex(RuntimeError, 'ending in _test'):
            _disposable_database_name({
                'PGTEST_ALLOW_DESTRUCTIVE': '1', 'PGTEST_DATABASE': 'wissensraum'})

    def test_guard_accepts_explicit_disposable_database(self):
        self.assertEqual(_disposable_database_name({
            'PGTEST_ALLOW_DESTRUCTIVE': '1', 'PGTEST_DATABASE': 'wissensraum_test',
        }), 'wissensraum_test')


@unittest.skipUnless(os.environ.get('PGTEST_PASSWORD'),
                     'PostgreSQL-Testdienst nicht konfiguriert')
class PostgresStoreTests(unittest.TestCase):
    @classmethod
    def _raw_connect(cls):
        import psycopg
        return psycopg.connect(host=cls.profile['host'], port=cls.profile['port'],
                               dbname=cls.profile['database'], user=cls.profile['username'],
                               password=cls.password, connect_timeout=10)

    @classmethod
    def _reset_schema(cls):
        with cls._raw_connect() as connection:
            with connection.transaction():
                connection.execute("SET LOCAL lock_timeout = '10s'")
                connection.execute('SELECT pg_advisory_xact_lock(%s)', (MIGRATION_LOCK_ID,))
                connection.execute('DROP SCHEMA IF EXISTS wissensraum CASCADE')

    @classmethod
    def setUpClass(cls):
        database = _disposable_database_name(os.environ)
        cls.profile = {'host': os.environ.get('PGTEST_HOST', '127.0.0.1'),
                       'port': int(os.environ.get('PGTEST_PORT', '5432')),
                       'database': database,
                       'username': os.environ.get('PGTEST_USERNAME', 'wissensraum_test'),
                       'sslmode': 'disable'}
        cls.password = os.environ['PGTEST_PASSWORD']
        cls._suite_lock = cls._raw_connect()
        cls._suite_lock.autocommit = True
        deadline = time.monotonic() + 10
        while not cls._suite_lock.execute(
                'SELECT pg_try_advisory_lock(%s)', (TEST_SUITE_LOCK_ID,)).fetchone()[0]:
            if time.monotonic() >= deadline:
                cls._suite_lock.close()
                raise RuntimeError('Timed out acquiring PostgreSQL test-suite lock')
            time.sleep(0.1)
        cls._reset_schema()

    @classmethod
    def tearDownClass(cls):
        try:
            cls._reset_schema()
        finally:
            cls._suite_lock.execute('SELECT pg_advisory_unlock(%s)', (TEST_SUITE_LOCK_ID,))
            cls._suite_lock.close()

    def setUp(self):
        self._reset_schema()

    def tearDown(self):
        self._reset_schema()

    def test_atomic_reset_rolls_back_when_recreation_fails(self):
        vault = FakeVault()
        settings = DatabaseSettings(Path.cwd(), vault, environ={})
        settings.reset_postgresql(self.profile, self.password, self.profile['database'])
        store = PostgresStore(self.profile, self.password)
        document_id, _ = store.add('Vor Reset', 'Muss einen fehlgeschlagenen Reset überleben.')
        broken = ((1, ('CREATE TABLE partial_reset(value TEXT)', 'NOT VALID POSTGRES SQL')),)
        with patch('migrations.POSTGRES_MIGRATIONS', broken), \
             self.assertRaisesRegex(ValueError, 'zurückgesetzt'):
            settings.reset_postgresql(self.profile, self.password, self.profile['database'])
        self.assertEqual(store.get(document_id)['title'], 'Vor Reset')

    def test_malformed_legacy_schema_is_rejected_without_stamp(self):
        with self._raw_connect() as connection:
            with connection.transaction():
                connection.execute('SELECT pg_advisory_xact_lock(%s)', (MIGRATION_LOCK_ID,))
                connection.execute('CREATE SCHEMA wissensraum')
                for table in ('documents', 'chunks', 'ai_runs', 'settings',
                              'email_accounts', 'email_imports'):
                    connection.execute(f'CREATE TABLE wissensraum.{table}(wrong_column INTEGER)')
        with self.assertRaisesRegex(ValueError, 'Legacy'):
            PostgresStore(self.profile, self.password)
        with self._raw_connect() as connection:
            row = connection.execute("""SELECT to_regclass(
                'wissensraum.schema_migrations'), to_regclass('wissensraum.documents')""").fetchone()
            self.assertIsNone(row[0])
            self.assertIsNotNone(row[1])

    def test_unversioned_unexpected_table_sets_are_rejected_without_metadata(self):
        table_sets = (
            ('partial', ('documents', 'chunks')),
            ('legacy_plus_extra', ('documents', 'chunks', 'ai_runs', 'settings',
                                   'email_accounts', 'email_imports', 'unexpected')),
            ('unrelated', ('unrelated',)),
        )
        for label, tables in table_sets:
            with self.subTest(label=label):
                self._reset_schema()
                with self._raw_connect() as connection:
                    with connection.transaction():
                        connection.execute('SELECT pg_advisory_xact_lock(%s)',
                                           (MIGRATION_LOCK_ID,))
                        connection.execute('CREATE SCHEMA wissensraum')
                        for table in tables:
                            connection.execute(
                                f'CREATE TABLE wissensraum.{table}(marker INTEGER)')
                with self.assertRaisesRegex(ValueError, '[Uu]nversioniert'):
                    PostgresStore(self.profile, self.password)
                with self._raw_connect() as connection:
                    metadata, actual = connection.execute("""SELECT
                        to_regclass('wissensraum.schema_migrations'),
                        ARRAY(SELECT table_name::text FROM information_schema.tables
                              WHERE table_schema='wissensraum' ORDER BY table_name)""").fetchone()
                    self.assertIsNone(metadata)
                    self.assertEqual(actual, sorted(tables))

    def test_startup_waits_for_migration_advisory_lock(self):
        first = self._raw_connect()
        second = self._raw_connect()
        started = False
        try:
            second_pid = second.info.backend_pid

            def migrate_on_second_connection():
                nonlocal started
                started = True
                with second.transaction():
                    second.execute("SET LOCAL statement_timeout = '10s'")
                    migrate_postgres(_PostgresConnection(second))

            with ThreadPoolExecutor(max_workers=1) as pool:
                with first.transaction():
                    first.execute('SELECT pg_advisory_xact_lock(%s)', (MIGRATION_LOCK_ID,))
                    future = pool.submit(migrate_on_second_connection)
                    deadline = time.monotonic() + 5
                    waiting = False
                    while time.monotonic() < deadline:
                        waiting = first.execute("""SELECT EXISTS (
                            SELECT 1 FROM pg_locks
                            WHERE locktype='advisory' AND pid=%s AND NOT granted)""",
                                                (second_pid,)).fetchone()[0]
                        if waiting:
                            break
                        time.sleep(0.05)
                    self.assertTrue(started)
                    self.assertTrue(waiting, 'second connection did not wait on migration lock')
                future.result(timeout=10)
        finally:
            first.close()
            second.close()
        with PostgresStore(self.profile, self.password).connect() as db:
            self.assertEqual(db.execute(
                'SELECT MAX(version) AS version FROM schema_migrations').fetchone()['version'], CURRENT_SCHEMA_VERSION)

    def test_postgres_store_contract(self):
        store=PostgresStore(self.profile,self.password)
        vault=FakeVault()
        authority_temp=tempfile.TemporaryDirectory()
        self.addCleanup(authority_temp.cleanup)
        settings=Settings(store,Path(authority_temp.name),vault,environ={})
        settings.save('ollama','qwen3:8b')
        self.assertEqual(settings.active(),'ollama')
        with store.connect() as db:
            sequences={table:db.execute(
                "SELECT pg_get_serial_sequence(%s,'id') AS name",(f'wissensraum.{table}',)
            ).fetchone()['name'] for table in ('documents','chunks','ai_runs')}
            sequence_values={table:db.execute(
                'SELECT last_value FROM '+name
            ).fetchone()['last_value'] for table,name in sequences.items()}
        self.assertEqual(store.health(),{'backend':'PostgreSQL'})
        self.assertEqual(settings.active(),'ollama')
        with store.connect() as db:
            self.assertEqual(sequence_values,{table:db.execute(
                'SELECT last_value FROM '+name
            ).fetchone()['last_value'] for table,name in sequences.items()})
            self.assertFalse(db.execute(
                "SELECT to_regclass('pg_temp.wissensraum_health_probe') AS probe").fetchone()['probe'])
            residue_columns={
                'documents':'title', 'chunks':'content', 'ai_runs':'question', 'settings':'name',
                'email_accounts':'id', 'email_imports':'remote_id',
            }
            for table,column in residue_columns.items():
                with self.subTest(table=table):
                    self.assertFalse(db.execute(
                        f"SELECT 1 FROM wissensraum.{table} "
                        f"WHERE {column} LIKE ? LIMIT 1",
                        ('.wissensraum_health_probe_%',)).fetchone())

        document_id, created = store.add(
            'PostgreSQL Test', 'Freigabe für den PostgreSQL Integrationslauf.',
            'Tests', 'test.txt', b'synthetic')
        self.assertTrue(created)
        self.assertEqual(store.add(
            'Doppelt', 'Freigabe für den PostgreSQL Integrationslauf.')[1], False)
        self.assertEqual(store.search('Freigabe')[0]['document_id'], document_id)
        store.log('question', 'Freigabe?', 'Erteilt [Q1]',
                  [{'document_id': document_id, 'title': 'PostgreSQL Test',
                    'ordinal': 1, 'content': 'Freigabe'}], 'ollama/qwen3:8b')
        self.assertEqual(store.history()[0]['answer'], 'Erteilt [Q1]')

        accounts=MailAccounts(store,vault,Path(authority_temp.name))
        account_id=accounts.save({'label':'Testkonto','provider':'gmail','email':'test@example.com',
            'username':'test@example.com','host':'imap.gmail.com','port':'993','client_id':'','tenant':'common'},
            'synthetic-app-password')
        with store.connect() as db:
            db.execute('INSERT INTO email_imports(account_id,remote_id,document_id) VALUES(?,?,?)',
                       (account_id, 'imap:INBOX:1', document_id))
        store.delete(document_id)
        with store.connect() as db:
            self.assertEqual(db.execute(
                'SELECT COUNT(*) AS count FROM email_imports').fetchone()['count'], 0)
        self.assertEqual(accounts.get(account_id)['label'], 'Testkonto')
        with self.assertRaises(ValueError):
            store.backup('not-used.db')
        reset = DatabaseSettings(Path.cwd(), vault, environ={}).reset_postgresql(
            self.profile, self.password, self.profile['database'])
        self.assertEqual(reset['tables'], [
            'ai_runs', 'chunks', 'documents', 'email_accounts', 'email_imports',
            'schema_migrations', 'settings'])
        with PostgresStore(self.profile, self.password).connect() as db:
            data_tables = [table for table in reset['tables'] if table != 'schema_migrations']
            self.assertTrue(all(db.execute(
                'SELECT count(*) AS count FROM ' + table).fetchone()['count'] == 0
                                for table in data_tables))
            self.assertEqual(db.execute(
                'SELECT MAX(version) AS version FROM schema_migrations').fetchone()['version'], CURRENT_SCHEMA_VERSION)

    def test_configuration_transfer_contract_on_real_postgresql(self):
        store=PostgresStore(self.profile,self.password)
        vault=FakeVault()
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary)
            database=DatabaseSettings(base,vault,environ={})
            database._write({'backend':'postgresql',**self.profile})
            design=DesignSettings(base)
            transfer=ConfigurationTransfer(store,base,database,design)
            with store.connect() as db:
                db.execute('DELETE FROM email_accounts')
                db.execute("INSERT INTO settings(name,value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                           ('active_provider','ollama'))
                db.execute('''INSERT INTO email_accounts
                    (id,label,provider,email,username,host,port,client_id,tenant)
                    VALUES(?,?,?,?,?,?,?,?,?)''',
                    ('pg-extra','Extra','gmail','extra@example.test','extra',
                     'imap.example.test',993,'','common'))
            document=transfer.export_document()
            self.assertEqual(document['configuration']['ai']['active_provider'],'ollama')
            replacement=dict(document)
            replacement['configuration']=dict(document['configuration'])
            replacement['configuration']['email_accounts']=[]
            transfer.import_document(replacement)
            with store.connect() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) AS count FROM email_accounts').fetchone()['count'],0)

            invalid=json.loads(json.dumps(document))
            invalid['configuration']['design']['theme']='invalid'
            before=transfer.export_document()
            with self.assertRaises(ConfigurationTransferError):
                transfer.import_document(invalid)
            self.assertEqual(transfer.export_document(),before)

            changed=json.loads(json.dumps(before))
            changed['configuration']['ai']['active_provider']='openai'
            real_write=transfer._write_json_atomic
            calls=[]
            def fail_second(path,value):
                calls.append(path)
                if len(calls)==2:
                    raise OSError('injected')
                return real_write(path,value)
            with patch.object(transfer,'_write_json_atomic',side_effect=fail_second):
                with self.assertRaises(ConfigurationTransferError):
                    transfer.import_document(changed)
            self.assertEqual(transfer.export_document(),before)

            # A crash after commit must recover through the recorded PostgreSQL origin even
            # after the imported profile has selected SQLite for the next start.
            switch=json.loads(json.dumps(before))
            switch['configuration']['database'].update(
                {'backend':'sqlite','host':'','database':'','username':'','sslmode':'require'})
            with patch.object(transfer,'_verify_applied',side_effect=OSError('injected crash')), \
                 patch.object(transfer,'_recover_pending',
                              side_effect=['no_pending',OSError('simulate restart')]):
                with self.assertRaises(ConfigurationTransferError):
                    transfer.import_document(switch)
            vault.values['database:postgresql']=self.password
            self.assertEqual(recover_pending_configuration(
                base,database,base/'wissen.db'),'committed_finalized')


if __name__ == '__main__':
    unittest.main()
