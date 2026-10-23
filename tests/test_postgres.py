"""Real PostgreSQL contract tests; destructive runs require explicit opt-in."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import os
import shutil
import subprocess
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
from migrations import CURRENT_SCHEMA_VERSION, POSTGRES_MIGRATIONS, migrate_postgres
from test_migrations import SOURCE_FIXTURE, fixture_sources, insert_source_fixture
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

    def _create_version_4_with_source_fixture(self, versioned):
        with self._raw_connect() as connection:
            with connection.transaction():
                connection.execute('SELECT pg_advisory_xact_lock(%s)', (MIGRATION_LOCK_ID,))
                connection.execute('CREATE SCHEMA wissensraum')
                connection.execute('SET LOCAL search_path TO wissensraum')
                for target, statements in POSTGRES_MIGRATIONS:
                    if target <= 4:
                        for statement in statements:
                            connection.execute(statement)
                if versioned:
                    connection.execute('''CREATE TABLE schema_migrations (
                        version INTEGER PRIMARY KEY,
                        applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
                    for target in range(1, 5):
                        connection.execute('INSERT INTO schema_migrations(version) VALUES(%s)',
                                           (target,))
                connection.execute("INSERT INTO email_accounts(id,label,provider,email,username) "
                                   "VALUES('konto','Alt','imap','alt@example.test','alt@example.test')")
                insert_source_fixture(_PostgresConnection(connection))

    def _assert_source_fixture_backfilled(self):
        with self._raw_connect() as connection:
            connection.execute('SET search_path TO wissensraum')
            sources, (documents, rows) = fixture_sources(connection)
            history = [row[0] for row in connection.execute(
                'SELECT version FROM schema_migrations ORDER BY version')]
        self.assertEqual(sources, {title: source for title, *_, source in SOURCE_FIXTURE})
        self.assertEqual(documents, rows)
        self.assertEqual(history, list(range(1, CURRENT_SCHEMA_VERSION + 1)))

    def test_version_4_schema_backfills_sources_on_real_postgresql(self):
        self._create_version_4_with_source_fixture(versioned=True)
        PostgresStore(self.profile, self.password)
        self._assert_source_fixture_backfilled()

    def test_unversioned_legacy_schema_is_adopted_then_backfilled(self):
        self._create_version_4_with_source_fixture(versioned=False)
        PostgresStore(self.profile, self.password)
        self._assert_source_fixture_backfilled()

    def test_add_records_one_source_per_document_on_real_postgresql(self):
        store = PostgresStore(self.profile, self.password)
        note, _ = store.add('Notiz', 'PG Notiz')
        file, _ = store.add('Datei', 'PG Datei', filename='ordner/a.md', original=b'a')
        mail, _ = store.add('Mail', 'PG Mail', 'E-Mail · Arbeit', 'Mail.eml', b'm',
                            ('imap', 'konto', 'imap:INBOX:1'))
        self.assertEqual(store.add('Kopie', 'PG Notiz', filename='b.md'), (note, False))
        with self.assertRaises(ValueError):
            store.add('Falsch', 'PG Falsch', source=('ftp', '', ''))
        with store.connect() as db:
            rows = {row['document_id']: (row['source_type'], row['account_id'], row['locator'])
                    for row in db.execute('SELECT * FROM sources')}
        self.assertEqual(rows, {note: ('note', '', ''), file: ('file', '', 'ordner/a.md'),
                                mail: ('imap', 'konto', 'imap:INBOX:1')})
        store.delete(file)
        with store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) AS total FROM sources').fetchone()['total'], 2)

    def _backup_snapshot(self):
        with self._raw_connect() as connection:
            connection.execute('SET search_path TO wissensraum')
            query = lambda sql: [tuple(row) for row in connection.execute(sql)]
            return {
                'documents': query('SELECT id, sha256, original FROM documents ORDER BY id'),
                'chunks': query('SELECT document_id, ordinal, content FROM chunks ORDER BY id'),
                'sources': query('SELECT document_id, source_type, account_id, locator '
                                 'FROM sources ORDER BY document_id'),
                'ai_runs': query('SELECT question, answer, sources_json FROM ai_runs ORDER BY id'),
                'settings': query('SELECT name, value FROM settings ORDER BY name'),
                'history': query('SELECT version FROM schema_migrations ORDER BY version'),
            }

    def test_documented_pg_dump_restore_round_trip_preserves_knowledge_base(self):
        # Runs exactly the commands documented in README.md (PostgreSQL-Sicherung).
        tools = {name: shutil.which(name, path=os.environ.get('PGTEST_PG_BIN') or None)
                 for name in ('pg_dump', 'pg_restore', 'psql')}
        if not all(tools.values()):
            if os.environ.get('PGTEST_REQUIRE_PG_TOOLS') == '1':
                self.fail('pg_dump/pg_restore/psql fehlen, obwohl PGTEST_REQUIRE_PG_TOOLS=1 gesetzt ist')
            self.skipTest('pg_dump/pg_restore/psql nicht verfügbar')
        store = PostgresStore(self.profile, self.password)
        store.add('Notiz', 'Sicherung Notiz')
        store.add('Datei', 'Sicherung Datei', filename='ordner/a.pdf', original=b'%PDF\x00\xff')
        store.add('Mail', 'Sicherung Mail', 'E-Mail · Arbeit', 'Mail.eml', b'raw',
                  ('imap', 'konto', 'imap:INBOX:1'))
        store.log('ask', 'Frage', 'Antwort [Q1]', [{'title': 'Notiz'}], 'modell')
        with store.connect() as db:
            db.execute("INSERT INTO settings(name,value) VALUES('active_provider','ollama')")
        before = self._backup_snapshot()
        connection = ['--host', self.profile['host'], '--port', str(self.profile['port']),
                      '--username', self.profile['username'], '--dbname', self.profile['database']]
        environment = {**os.environ, 'PGPASSWORD': self.password}
        with tempfile.TemporaryDirectory() as folder:
            dump = str(Path(folder) / 'wissensraum.dump')
            run = lambda *args: subprocess.run(args, env=environment, check=True,
                                               capture_output=True, text=True, timeout=120)
            run(tools['pg_dump'], *connection, '--schema=wissensraum', '--format=custom',
                '--file', dump)
            self.assertIn('TABLE DATA wissensraum documents', run(tools['pg_restore'], '--list', dump).stdout)
            run(tools['psql'], *connection, '-c', 'DROP SCHEMA wissensraum CASCADE')
            run(tools['pg_restore'], *connection, '--single-transaction', '--exit-on-error',
                '--no-owner', dump)
        restored = PostgresStore(self.profile, self.password)
        restored.health()
        self.assertEqual(self._backup_snapshot(), before)
        self.assertEqual({hit['title'] for hit in restored.search('Sicherung')}, {'Notiz', 'Datei', 'Mail'})
        new_id, created = restored.add('Neu', 'Nach Wiederherstellung')
        self.assertTrue(created)
        self.assertGreater(new_id, max(row[0] for row in before['documents']))

    def test_import_job_with_cancel_resume_and_crash_recovery_on_real_postgresql(self):
        import threading
        import import_fixtures as fx
        from import_jobs import ImportJobs
        store = PostgresStore(self.profile, self.password)
        jobs = ImportJobs(store)
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            for name, data in {'a.txt': b'PG Auftrag eins', 'b.pdf': fx.pdf_with_text('PG Auftrag zwei'),
                               'c.docx': fx.docx_with_text('PG Auftrag drei'), 'd.exe': b'MZ',
                               'e.txt': b'PG Auftrag eins'}.items():
                (folder / name).write_bytes(data)
            job_id = jobs.create_for_folder(folder)
            cancel = threading.Event()
            self.assertEqual(jobs.run(job_id, cancel, lambda done, total, _: cancel.set())['status'], 'cancelled')
            real_add = store.add
            def add_then_crash(*args, **kwargs):
                real_add(*args, **kwargs)
                raise KeyboardInterrupt
            with patch.object(store, 'add', side_effect=add_then_crash):
                with self.assertRaises(KeyboardInterrupt):
                    jobs.run(job_id)
            summary = jobs.run(job_id)
            report = jobs.write_report(job_id, folder / 'bericht.csv').read_text(encoding='utf-8-sig')
        self.assertEqual((summary['status'], summary['imported'], summary['duplicate'], summary['quarantined']),
                         ('completed', 3, 1, 1))
        with store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) AS total FROM documents').fetchone()['total'], 3)
            self.assertEqual(db.execute('SELECT count(*) AS total FROM sources').fetchone()['total'], 3)
        self.assertIn('Dateityp nicht unterstützt', report)
        first = jobs.items(job_id)[0]
        store.delete(first['document_id'])
        self.assertIsNone(jobs.items(job_id)[0]['document_id'])

    def test_document_versions_preview_and_current_search_on_real_postgresql(self):
        from import_jobs import ImportJobs
        store = PostgresStore(self.profile, self.password)
        jobs = ImportJobs(store)
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            (folder / 'plan.txt').write_text('Plan Altwert Budget', encoding='utf-8')
            first_job = jobs.create_for_folder(folder)
            self.assertEqual(jobs.run(first_job)['imported'], 1)
            (folder / 'plan.txt').write_text('Plan Neuwert Budget', encoding='utf-8')
            with store.connect() as db:
                before = db.execute('SELECT (SELECT count(*) FROM documents) AS d,'
                                    '(SELECT count(*) FROM document_versions) AS v').fetchone()
            preview = jobs.preview_folder(folder)
            with store.connect() as db:
                after = db.execute('SELECT (SELECT count(*) FROM documents) AS d,'
                                   '(SELECT count(*) FROM document_versions) AS v').fetchone()
            self.assertEqual(after, before)
            self.assertEqual((preview[0]['outcome'], preview[0]['version'], preview[0]['type']), ('version', 2, 'txt'))
            self.assertEqual(jobs.run(jobs.create_for_folder(folder))['versioned'], 1)
        current = store.list()
        self.assertEqual([row['version'] for row in current], [2])
        versions = store.versions(current[0]['id'])
        self.assertEqual([row['version'] for row in versions], [2, 1])
        self.assertEqual(store.get(versions[1]['id'])['content'], 'Plan Altwert Budget')
        self.assertEqual({row['document_id'] for row in store.search('Budget')}, {current[0]['id']})
        self.assertEqual(store.search('Altwert'), [])
        self.assertEqual([row['document_id'] for row in store.search('Altwert', all_versions=True)],
                         [versions[1]['id']])
        store.delete(current[0]['id'])
        self.assertEqual([row['id'] for row in store.list()], [versions[1]['id']])

    def test_released_mail_attachment_is_linked_on_real_postgresql(self):
        import import_fixtures as fx
        from importer import Importer, MailAccounts
        from mail_fakes import FakeGraph, graph_attachment
        from test_settings import FakeVault
        store = PostgresStore(self.profile, self.password)
        with tempfile.TemporaryDirectory() as folder:
            accounts = MailAccounts(store, FakeVault(), Path(folder))
            account_id = accounts.save({'label': 'M365', 'provider': 'microsoft', 'email': 'm@example.test',
                                        'username': 'm@example.test',
                                        'client_id': '11111111-2222-3333-4444-555555555555'})
            accounts.microsoft_token = lambda _: 'token'
            pdf = fx.pdf_with_text('PG Anhang Lieferschein')
            graph = FakeGraph({'MSG=': ({'subject': 'PG Lieferung', 'from': {'emailAddress': {'address': 'a@example.test'}},
                                         'toRecipients': [], 'receivedDateTime': '2026-10-23T10:00:00Z',
                                         'body': {'contentType': 'text', 'content': 'Lieferung anbei.'}},
                                        [graph_attachment('A1', 'lieferschein.pdf', 'application/pdf', pdf)])})
            importer = Importer(store, accounts)
            self.assertEqual(importer.import_microsoft_ids(account_id, ['MSG='], transport=graph)['attachments_imported'], 0)
            self.assertEqual(graph.content_requests(), [])
            first = importer.import_microsoft_ids(account_id, [], transport=graph, attachments={'MSG=': ['A1']})
            again = importer.import_microsoft_ids(account_id, [], transport=graph, attachments={'MSG=': ['A1']})
        self.assertEqual((first['attachments_imported'], again['attachments_duplicates']), (1, 1))
        with store.connect() as db:
            link = db.execute('SELECT * FROM email_attachments').fetchall()
            email_id = db.execute("SELECT document_id FROM sources WHERE source_type='graph'").fetchone()['document_id']
        self.assertEqual([(row['email_document_id'], row['name'], row['size']) for row in link],
                         [(email_id, 'lieferschein.pdf', len(pdf))])
        self.assertEqual(store.get(link[0]['document_id'])['original'], pdf)
        self.assertTrue(importer.preview_microsoft(account_id, transport=graph)[0]['attachments'][0]['imported'])
        store.delete(email_id)
        with store.connect() as db:
            self.assertIsNone(db.execute('SELECT email_document_id FROM email_attachments').fetchone()['email_document_id'])

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
            'ai_runs', 'chunks', 'document_versions', 'documents', 'email_accounts', 'email_attachments',
            'email_imports',
            'import_item_errors', 'import_items', 'import_jobs',
            'schema_migrations', 'settings', 'sources'])
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
