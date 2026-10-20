import json
from pathlib import Path
import tempfile
import unittest
import sys
from contextlib import contextmanager
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from core import _PostgresConnection
from database import DatabaseSettings
from test_settings import FakeVault


class FakeCursor:
    def __init__(self): self.query=None
    def __enter__(self): return self
    def __exit__(self,*_): pass
    def execute(self,query): self.query=query
    def fetchone(self): return ('wissen_test','test_user','PostgreSQL 18.4, synthetic')


class FakeConnection:
    def __init__(self): self.closed=False; self.cursor_value=FakeCursor()
    def cursor(self): return self.cursor_value
    def close(self): self.closed=True


class FakeResetConnection:
    def __init__(self, tuple_rows=False):
        self.closed=False
        self.calls=[]
        self.schema='old'
        self.rolled_back=False
        self.tuple_rows=tuple_rows

    @contextmanager
    def transaction(self):
        before=self.schema
        try:
            yield
        except Exception:
            self.schema=before
            self.rolled_back=True
            raise

    def execute(self,query,parameters=()):
        self.calls.append((query,parameters))
        if query.startswith('DROP SCHEMA'):
            self.schema=None
        if 'FROM information_schema.tables' in query:
            tables=['ai_runs','chunks','documents','email_accounts','email_imports',
                    'schema_migrations','settings'] if self.schema == 'new' else []
            rows=[(name,) for name in tables] if self.tuple_rows else [{'table_name':name} for name in tables]
            return type('Result',(),{'fetchall':lambda _:rows})()
        if 'SELECT MAX(version)' in query:
            version=4 if self.schema == 'new' else None
            row=(version,) if self.tuple_rows else {'version':version}
            return type('Result',(),{'fetchone':lambda _:row})()
        return type('Result',(),{'fetchall':lambda _:[]})()

    def close(self): self.closed=True


class DatabaseSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.base=Path(self.temp.name)
        self.vault=FakeVault()
        self.settings=DatabaseSettings(self.base,self.vault,environ={})
        self.values={'host':'db.example.test','port':'5432','database':'wissen_test',
                     'username':'test_user','sslmode':'require'}
    def tearDown(self): self.temp.cleanup()

    def test_profile_is_saved_without_password(self):
        self.settings.save(self.values,'synthetic-db-password')
        saved=self.settings.path.read_text(encoding='utf-8')
        self.assertNotIn('synthetic-db-password',saved)
        self.assertEqual(self.vault.get('database:postgresql'),'synthetic-db-password')
        self.assertEqual(json.loads(saved)['host'],'db.example.test')
        self.assertEqual(self.settings.profile()['backend'],'sqlite')

    def test_connection_test_uses_tls_and_redacts_password(self):
        captured={}
        connection=FakeConnection()
        def connector(**values): captured.update(values); return connection
        result=self.settings.test(self.values,'synthetic-db-password',connector)
        self.assertEqual(result['database'],'wissen_test')
        self.assertEqual(captured['sslmode'],'require')
        self.assertEqual(captured['connect_timeout'],10)
        self.assertTrue(connection.closed)
        self.assertNotIn('synthetic-db-password',str(result))

    def test_activation_initializes_schema_and_requires_restart(self):
        created=[]
        self.settings.activate_postgresql(self.values,'synthetic-db-password',
                                            connector=lambda **_:FakeConnection(),
                                            store_factory=lambda profile,password:created.append((profile,password)))
        self.assertEqual(self.settings.profile()['backend'],'postgresql')
        self.assertEqual(created[0][0]['database'],'wissen_test')
        self.assertEqual(created[0][1],'synthetic-db-password')
        self.settings.activate_sqlite()
        self.assertEqual(self.settings.profile()['backend'],'sqlite')

    def test_invalid_connection_values_are_rejected(self):
        for change in ({'host':'https://db.example.test'},{'port':'70000'},{'sslmode':'disable'}):
            with self.subTest(change=change),self.assertRaises(ValueError):
                self.settings.connection_values({**self.values,**change},'synthetic-db-password')

    def test_reset_requires_exact_database_confirmation(self):
        with self.assertRaisesRegex(ValueError,'nichts gelöscht'):
            self.settings.reset_postgresql(self.values,'synthetic-db-password','falsche_datenbank')

    def test_reset_recreates_and_validates_on_same_locked_transaction(self):
        connection=FakeResetConnection(tuple_rows=True)

        def migrate(adapter):
            self.assertIs(adapter.connection,connection)
            connection.schema='new'

        with patch.object(self.settings,'_connect',return_value=connection), \
             patch('migrations.migrate_postgres',side_effect=migrate) as migration:
            result=self.settings.reset_postgresql(
                self.values,'synthetic-db-password',self.values['database'])
        migration.assert_called_once()
        queries=[query for query,_ in connection.calls]
        self.assertLess(next(i for i,q in enumerate(queries) if 'pg_advisory_xact_lock' in q),
                        next(i for i,q in enumerate(queries) if q.startswith('DROP SCHEMA')))
        self.assertEqual(sum('pg_advisory_xact_lock' in q for q in queries),1)
        self.assertLess(next(i for i,q in enumerate(queries) if q.startswith('SET LOCAL lock_timeout')),
                        next(i for i,q in enumerate(queries) if 'pg_advisory_xact_lock' in q))
        self.assertEqual(result['tables'],['ai_runs','chunks','documents','email_accounts',
                                           'email_imports','schema_migrations','settings'])
        self.assertTrue(connection.closed)

    def test_failed_reset_rolls_back_and_preserves_previous_schema(self):
        connection=FakeResetConnection()

        def fail_after_recreation(adapter):
            adapter.execute('SELECT pg_advisory_xact_lock(87031904)')
            connection.schema='partial-new'
            raise RuntimeError('synthetic migration failure')

        with patch.object(self.settings,'_connect',return_value=connection), \
             patch('migrations.migrate_postgres',side_effect=fail_after_recreation), \
             self.assertRaisesRegex(ValueError,'zurückgesetzt'):
            self.settings.reset_postgresql(
                self.values,'synthetic-db-password',self.values['database'])
        self.assertTrue(connection.rolled_back)
        self.assertEqual(connection.schema,'old')

    def test_postgres_adapter_translates_parameters(self):
        calls=[]
        fake=type('Connection',(),{'execute':lambda _,query,parameters:calls.append((query,parameters))})()
        _PostgresConnection(fake).execute('SELECT * FROM settings WHERE name=?',('active_provider',))
        self.assertEqual(calls,[('SELECT * FROM settings WHERE name=%s',('active_provider',))])


if __name__=='__main__': unittest.main()
