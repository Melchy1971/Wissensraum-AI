import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from contextlib import contextmanager
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Store
from importer import Importer, MailAccounts
from test_settings import FakeVault

RAW_MAIL=(b'From: sender@example.com\r\nTo: owner@example.com\r\nSubject: Projektstatus\r\n'
          b'Date: Thu, 15 Oct 2026 08:00:00 +0000\r\nMessage-ID: <one@example.com>\r\n'
          b'Content-Type: text/plain; charset=utf-8\r\n\r\nDie Freigabe wurde erteilt.')

class FakeImap:
    def __init__(self,host,port):
        self.host,self.port=host,port
        self.logged_out=False
        self.uid_calls=[]
    def login(self,user,password):
        self.login_values=(user,password); return 'OK',[]
    def select(self,folder,readonly=False):
        self.selected=(folder,readonly); return 'OK',[b'2']
    def uid(self,command,*args):
        self.uid_calls.append((command,args))
        if command=='search': return 'OK',[b'101 202']
        if command=='fetch': return 'OK',[(b'RFC822',RAW_MAIL)]
        raise AssertionError(command)
    def logout(self): self.logged_out=True

class FakeArchive:
    def __init__(self,path): self.path=path
    def __enter__(self): return self
    def __exit__(self,*_): pass
    def messages(self): return iter((object(),object()))
    def format_message(self,message,with_headers=True): return RAW_MAIL.decode()

class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.base=Path(self.temp.name)
        self.store=Store(self.base/'wissen.db')
        self.vault=FakeVault()
        self.accounts=MailAccounts(self.store,self.vault,self.base)
        self.importer=Importer(self.store,self.accounts)
    def tearDown(self): self.temp.cleanup()

    @contextmanager
    def _configuration_operation_owned_by_process(self):
        ready=self.base/'mail-lock-ready'
        release=self.base/'mail-lock-release'
        script=(
            "import pathlib,time; from configuration_authority import configuration_operation_lock; "
            "lock=configuration_operation_lock(pathlib.Path(r'"+str(self.base)+"')); "
            "lock.acquire(); pathlib.Path(r'"+str(ready)+"').write_text('ready'); "
            "\nwhile not pathlib.Path(r'"+str(release)+"').exists(): time.sleep(.01)\n"
            "lock.release()")
        process=subprocess.Popen([sys.executable,'-c',script],
                                 cwd=str(Path(__file__).parents[1]))
        try:
            deadline=time.monotonic()+5
            while not ready.exists() and process.poll() is None and time.monotonic()<deadline:
                time.sleep(.01)
            self.assertTrue(ready.exists())
            yield
        finally:
            release.write_text('release',encoding='utf-8')
            try:
                returncode=process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                self.fail('lock-owner process did not exit after release request')
            self.assertEqual(returncode,0,'lock-owner process exited unexpectedly')

    def _configuration_snapshot(self):
        with self.store.connect() as db:
            rows=[tuple(row) for row in db.execute(
                'SELECT id,label,provider,email,username,host,port,client_id,tenant '
                'FROM email_accounts ORDER BY id')]
        authority=self.base/'data'/'configuration-authority.json'
        return rows,authority.read_bytes() if authority.exists() else None,dict(self.vault.values)

    def test_account_save_busy_lock_precedes_vault_sql_and_authority_mutation(self):
        before=self._configuration_snapshot()
        values={'label':'Privat','provider':'gmail','email':'owner@gmail.com',
                'username':'owner@gmail.com','host':'imap.gmail.com','port':'993',
                'client_id':'','tenant':'common'}
        with patch.object(self.vault,'set',side_effect=AssertionError('vault write attempted')):
            with self._configuration_operation_owned_by_process():
                with self.assertRaisesRegex(
                        ValueError,'^Konfiguration wird gerade geändert; bitte erneut versuchen\\.$'):
                    self.accounts.save(values,'replacement-secret')
        self.assertEqual(self._configuration_snapshot(),before)

    def test_account_delete_busy_lock_precedes_vault_sql_and_authority_mutation(self):
        account_id=self.add_gmail()
        before=self._configuration_snapshot()
        with patch.object(self.vault,'delete',side_effect=AssertionError('vault delete attempted')):
            with self._configuration_operation_owned_by_process():
                with self.assertRaisesRegex(
                        ValueError,'^Konfiguration wird gerade geändert; bitte erneut versuchen\\.$'):
                    self.accounts.delete(account_id)
        self.assertEqual(self._configuration_snapshot(),before)

    def add_gmail(self):
        return self.accounts.save({'label':'Privat','provider':'gmail','email':'owner@gmail.com',
            'username':'owner@gmail.com','host':'imap.gmail.com','port':'993','client_id':'','tenant':'common'},'app-password')

    def test_account_metadata_and_secret_are_separated(self):
        account_id=self.add_gmail()
        row=self.accounts.get(account_id)
        self.assertEqual(row['auth_status'],'Verbunden')
        self.assertEqual(row['host'],'imap.gmail.com')
        with self.store.connect() as db:
            dump=str([tuple(r) for r in db.execute('SELECT * FROM email_accounts')])
            self.assertNotIn('app-password',dump)
        self.assertEqual(self.vault.get('mail:'+account_id),'app-password')
        self.accounts.save({**row,'label':'Privat neu'},secret='')
        self.assertEqual(self.accounts.get(account_id)['label'],'Privat neu')
        self.accounts.delete(account_id)
        self.assertEqual(self.vault.values,{})

    def test_imap_readonly_import_and_deduplication(self):
        account_id=self.add_gmail()
        clients=[]
        def factory(host,port):
            clients.append(FakeImap(host,port)); return clients[-1]
        preview=self.importer.preview_imap(account_id,'INBOX',100,factory)
        self.assertEqual([item['id'] for item in preview],['202','101'])
        report=self.importer.import_imap_ids(account_id,'INBOX',[preview[0]['id']],factory)
        self.assertEqual(report,{'found':1,'imported':1,'duplicates':0,'errors':0})
        self.assertEqual(clients[0].selected,('INBOX',True))
        self.assertEqual(len(clients),2)
        self.assertTrue(all(client.logged_out for client in clients))
        self.assertTrue(all('HEADER.FIELDS' in call[1][1] for call in clients[0].uid_calls if call[0]=='fetch'))
        self.assertEqual(clients[1].uid_calls[-1][1][1],'(BODY.PEEK[])')
        self.assertEqual(self.store.search('Freigabe')[0]['title'],'Projektstatus')
        doc=self.store.get(1)
        self.assertEqual(doc['original'],RAW_MAIL)
        refreshed=self.importer.preview_imap(account_id,'INBOX',100,factory)
        self.assertTrue(next(item for item in refreshed if item['id']=='202')['imported'])
        self.assertFalse(next(item for item in refreshed if item['id']=='101')['imported'])
        other_folder=self.importer.preview_imap(account_id,'Archive',100,factory)
        self.assertTrue(all(not item['imported'] for item in other_folder))
        self.store.delete(doc['id'])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM email_imports').fetchone()[0],0)

    def test_pst_import_uses_local_archive_and_limit(self):
        path=self.base/'archive.pst'; path.write_bytes(b'test')
        report=self.importer.import_pst(path,1,FakeArchive)
        self.assertEqual(report['found'],1)
        self.assertEqual(report['imported'],1)
        self.assertIn('PST',self.store.get(1)['category'])

    def test_microsoft_graph_import(self):
        account_id=self.accounts.save({'label':'Arbeit','provider':'microsoft','email':'owner@example.com',
            'username':'','host':'','port':'0','client_id':'11111111-1111-1111-1111-111111111111','tenant':'common'})
        item={'id':'1','subject':'Graph Nachricht','from':{'emailAddress':{'address':'boss@example.com'}},
              'toRecipients':[{'emailAddress':{'address':'owner@example.com'}}],
              'receivedDateTime':'2026-10-15T08:00:00Z','internetMessageId':'<graph@example.com>',
              'body':{'contentType':'html','content':'<p>Graph Inhalt</p>'}}
        def graph(url,token):
            if '/mailFolders/' in url:
                return {'value':[{k:v for k,v in item.items() if k in ('id','subject','from','receivedDateTime')}]}
            return item
        with patch.object(self.accounts,'microsoft_token',return_value='synthetic-token'):
            report=self.importer.import_microsoft(account_id,10,graph)
            refreshed=self.importer.preview_microsoft(account_id,10,graph)
        self.assertEqual(report['imported'],1)
        self.assertTrue(refreshed[0]['imported'])
        self.assertEqual(self.store.search('Graph Inhalt')[0]['title'],'Graph Nachricht')
        with self.store.connect() as db:
            self.assertNotIn('synthetic-token',str([tuple(r) for r in db.execute('SELECT * FROM email_accounts')]))

    def test_microsoft_requires_client_id(self):
        with self.assertRaises(ValueError):
            self.accounts.save({'label':'MS','provider':'microsoft','email':'owner@example.com','client_id':'','tenant':'common'})

if __name__=='__main__': unittest.main()
