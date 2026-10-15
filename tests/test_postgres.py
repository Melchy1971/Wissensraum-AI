"""Real PostgreSQL contract test; skipped when no CI service is configured."""
from pathlib import Path
import os
import unittest
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parent))

from core import PostgresStore
from database import DatabaseSettings
from importer import MailAccounts
from settings import Settings
from test_settings import FakeVault


@unittest.skipUnless(os.environ.get('PGTEST_PASSWORD'),'PostgreSQL-Testdienst nicht konfiguriert')
class PostgresStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profile={'host':os.environ.get('PGTEST_HOST','127.0.0.1'),
                     'port':int(os.environ.get('PGTEST_PORT','5432')),
                     'database':os.environ.get('PGTEST_DATABASE','wissensraum_test'),
                     'username':os.environ.get('PGTEST_USERNAME','wissensraum_test'),
                     'sslmode':'disable'}
        cls.password=os.environ['PGTEST_PASSWORD']
        import psycopg
        with psycopg.connect(host=cls.profile['host'],port=cls.profile['port'],dbname=cls.profile['database'],
                             user=cls.profile['username'],password=cls.password) as connection:
            connection.execute('DROP SCHEMA IF EXISTS wissensraum CASCADE')

    def test_postgres_store_contract(self):
        store=PostgresStore(self.profile,self.password)
        vault=FakeVault()
        settings=Settings(store,Path.cwd(),vault,environ={})
        settings.save('ollama','qwen3:8b')
        self.assertEqual(settings.active(),'ollama')

        document_id,created=store.add('PostgreSQL Test','Freigabe für den PostgreSQL Integrationslauf.',
                                      'Tests','test.txt',b'synthetic')
        self.assertTrue(created)
        self.assertEqual(store.add('Doppelt','Freigabe für den PostgreSQL Integrationslauf.')[1],False)
        self.assertEqual(store.search('Freigabe')[0]['document_id'],document_id)
        store.log('question','Freigabe?','Erteilt [Q1]',[{'document_id':document_id,'title':'PostgreSQL Test',
                  'ordinal':1,'content':'Freigabe'}],'ollama/qwen3:8b')
        self.assertEqual(store.history()[0]['answer'],'Erteilt [Q1]')

        accounts=MailAccounts(store,vault)
        account_id=accounts.save({'label':'Testkonto','provider':'gmail','email':'test@example.com',
            'username':'test@example.com','host':'imap.gmail.com','port':'993','client_id':'','tenant':'common'},
            'synthetic-app-password')
        with store.connect() as db:
            db.execute('INSERT INTO email_imports(account_id,remote_id,document_id) VALUES(?,?,?)',
                       (account_id,'imap:INBOX:1',document_id))
        store.delete(document_id)
        with store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) AS count FROM email_imports').fetchone()['count'],0)
        self.assertEqual(accounts.get(account_id)['label'],'Testkonto')
        with self.assertRaises(ValueError): store.backup('not-used.db')
        reset=DatabaseSettings(Path.cwd(),vault,environ={}).reset_postgresql(
            self.profile,self.password,self.profile['database'])
        self.assertEqual(reset['tables'],['ai_runs','chunks','documents','email_accounts','email_imports','settings'])
        with PostgresStore(self.profile,self.password).connect() as db:
            self.assertTrue(all(db.execute('SELECT count(*) AS count FROM '+table).fetchone()['count']==0
                                for table in reset['tables']))


if __name__=='__main__': unittest.main()
