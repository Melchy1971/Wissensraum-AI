import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Store, AI
from settings import Settings, SystemVault, masked_key, read_env_file, PROVIDERS

class FakeVault:
    def __init__(self):
        self.values={}
    def get(self,provider): return self.values.get(provider)
    def set(self,provider,key): self.values[provider]=key
    def delete(self,provider): self.values.pop(provider,None)

class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.base=Path(self.temp.name)/'app'
        self.base.mkdir()
        self.store=Store(self.base/'wissen.db')
        self.vault=FakeVault()
        self.settings=Settings(self.store,self.base,self.vault,environ={})
    def tearDown(self): self.temp.cleanup()

    def test_existing_file_key_is_detected_without_being_copied(self):
        (self.base.parent/'.env.local').write_text('OPENAI_API_KEY=test-existing-1234567890\n',encoding='utf-8')
        key,source=self.settings.credential('openai')
        self.assertEqual(key,'test-existing-1234567890')
        row=self.settings.overview()[0]
        self.assertNotIn(key,json.dumps(row))
        self.assertTrue(row['masked'].endswith('7890'))
        self.assertEqual(self.vault.values,{})
        self.assertEqual(self.settings.ai().key,key)

    def test_credential_precedence_and_persistence(self):
        self.settings.environ['OPENAI_API_KEY']='env-secret'
        (self.base/'.env').write_text('OPENAI_API_KEY=file-secret',encoding='utf-8')
        self.assertEqual(self.settings.credential('openai')[0],'env-secret')
        self.settings.save('openai','gpt-4.1','new-secret')
        restored=Settings(self.store,self.base,self.vault,environ={})
        self.assertEqual(restored.credential('openai')[0],'new-secret')
        self.assertEqual(restored.model('openai'),'gpt-4.1')
        with self.store.connect() as db:
            rows=db.execute('SELECT * FROM settings').fetchall()
            self.assertNotIn('new-secret',str([tuple(r) for r in rows]))

    def test_switch_to_claude_and_ollama(self):
        self.settings.save('anthropic','claude-sonnet-5-5','claude-test-secret')
        self.assertEqual(self.settings.ai().provider,'anthropic')
        rows={row['provider']:row for row in self.settings.overview()}
        self.assertTrue(rows['anthropic']['active'])
        self.assertFalse(rows['openai']['active'])
        self.settings.save('ollama','qwen3:8b')
        ai=self.settings.ai()
        self.assertEqual(ai.provider,'ollama')
        self.assertEqual(ai.key,'')
        self.assertEqual(self.settings.credential('anthropic')[0],'claude-test-secret')

    def test_remove_disables_external_fallback_until_replaced(self):
        self.settings.environ['OPENAI_API_KEY']='external-key'
        self.settings.save('openai','gpt-4.1','saved-key')
        self.settings.remove_key('openai')
        self.assertEqual(self.vault.values,{})
        self.assertEqual(self.settings.credential('openai'),('','Deaktiviert'))
        with self.assertRaises(ValueError): self.settings.ai()
        self.assertEqual(self.settings.environ['OPENAI_API_KEY'],'external-key')
        self.settings.save('openai','gpt-4.1','replacement-key')
        self.assertEqual(self.settings.credential('openai')[0],'replacement-key')

    def test_save_unchanged_external_key_does_not_require_vault_write(self):
        self.settings.environ['OPENAI_API_KEY']='external-key'
        with patch.object(self.vault,'set',side_effect=AssertionError('must not write')):
            self.settings.save('openai','gpt-4.1','external-key')
        self.assertEqual(self.settings.model('openai'),'gpt-4.1')

    def test_missing_key_and_invalid_model(self):
        with self.assertRaises(ValueError): self.settings.ai()
        with self.assertRaises(ValueError): self.settings.save('openai','model?key=secret','some-key')
        with self.assertRaises(ValueError): self.settings.save('gemini','gemini-3.8-flash','contains space')
        self.assertEqual(self.settings.config(),{})

    def test_import_all_supported_env_names(self):
        p=self.base/'keys.env'
        p.write_text('export OPENAI_API_KEY="test-openai"\nANTHROPIC_API_KEY=test-claude\nGOOGLE_API_KEY=test-google # comment\n',encoding='utf-8')
        self.assertEqual(self.settings.import_keys(p),3)
        self.assertEqual(self.settings.active(),'openai')
        self.assertEqual(self.settings.credential('gemini')[0],'test-google')

    def test_no_plaintext_backend(self):
        class Plaintext:
            __module__='keyrings.alt.file'
        fake=type('KeyringModule',(),{'get_keyring':staticmethod(lambda:Plaintext())})()
        with patch.dict(sys.modules,{'keyring':fake}):
            vault=SystemVault()
            self.assertIsNone(vault.get('openai'))
            with self.assertRaises(ValueError): vault.set('openai','secret')

    def test_vault_failure_does_not_change_preferences(self):
        with patch.object(self.vault,'set',side_effect=ValueError('Vault unavailable')):
            with self.assertRaises(ValueError):
                self.settings.save('anthropic','claude-sonnet-5-5','test-secret')
        self.assertEqual(self.settings.active(),'openai')
        self.assertEqual(self.settings.config(),{})

    def test_schema_upgrade_keeps_documents_and_fts(self):
        self.store.add('Altdokument','Migrationsbeleg')
        with self.store.connect() as db:
            db.execute('DROP TABLE settings')
            db.execute('PRAGMA user_version=1')
        restored=Store(self.store.path)
        self.assertEqual(restored.search('Migrationsbeleg')[0]['title'],'Altdokument')
        self.assertEqual(Settings(restored,self.base,self.vault,{}).active(),'openai')
        with restored.connect() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],2)

    def test_short_secrets_never_show_in_mask(self):
        for key in ('x','secret','12345678'):
            self.assertNotIn(key,masked_key(key))

class AdapterTests(unittest.TestCase):
    def exercise(self,provider,response):
        captured=[]
        def transport(req,timeout):
            captured.append(req)
            return io.BytesIO(json.dumps(response).encode())
        ai=AI(None,'synthetic-test-key',PROVIDERS[provider]['models'][0],transport,provider)
        answer=ai.generate('Was steht in der Quelle?',[{'title':'Test','document_id':1,'ordinal':1,'content':'Testwissen'}])
        self.assertIn('[Q1]',answer)
        return captured[0],json.loads(captured[0].data)

    def test_anthropic(self):
        req,payload=self.exercise('anthropic',{'stop_reason':'end_turn','content':[{'type':'text','text':'Testwissen [Q1]'}]})
        self.assertEqual(req.full_url,'https://api.anthropic.com/v1/messages')
        self.assertEqual(req.get_header('X-api-key'),'synthetic-test-key')
        self.assertIn('system',payload)
        self.assertNotIn('Authorization',req.headers)

    def test_gemini(self):
        req,payload=self.exercise('gemini',{'candidates':[{'finishReason':'STOP','content':{'parts':[{'text':'Testwissen [Q1]'}]}}]})
        self.assertIn(':generateContent',req.full_url)
        self.assertNotIn('synthetic-test-key',req.full_url)
        self.assertEqual(req.get_header('X-goog-api-key'),'synthetic-test-key')
        self.assertIn('systemInstruction',payload)

    def test_ollama_never_sends_cloud_credentials(self):
        req,payload=self.exercise('ollama',{'done':True,'done_reason':'stop','message':{'content':'Testwissen [Q1]'}})
        self.assertEqual(req.full_url,'http://127.0.0.1:11434/api/chat')
        self.assertNotIn('Authorization',req.headers)
        self.assertFalse(payload['stream'])
        self.assertNotIn('synthetic-test-key',json.dumps(dict(req.headers)))

    def test_truncated_and_blocked_answers_are_rejected(self):
        for provider,response in [('anthropic',{'stop_reason':'max_tokens','content':[]}),
                                  ('gemini',{'promptFeedback':{'blockReason':'SAFETY'}}),
                                  ('ollama',{'done':True,'done_reason':'length','message':{'content':'partial [Q1]'}})]:
            with self.subTest(provider=provider):
                with self.assertRaises(ValueError): self.exercise(provider,response)

if __name__=='__main__': unittest.main()
