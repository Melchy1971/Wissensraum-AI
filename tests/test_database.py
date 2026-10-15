import json
from pathlib import Path
import tempfile
import unittest
import sys
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

    def test_postgres_adapter_translates_parameters(self):
        calls=[]
        fake=type('Connection',(),{'execute':lambda _,query,parameters:calls.append((query,parameters))})()
        _PostgresConnection(fake).execute('SELECT * FROM settings WHERE name=?',('active_provider',))
        self.assertEqual(calls,[('SELECT * FROM settings WHERE name=%s',('active_provider',))])


if __name__=='__main__': unittest.main()
