"""Provider preferences in SQLite; credentials in OS vault or existing environment files."""
from pathlib import Path
import os
import re
from contextlib import contextmanager
from configuration_authority import (CONFIGURATION_OPERATION_BUSY, ConfigurationAuthority,
                                     configuration_operation_lock, store_identity)

PROVIDERS = {
    'openai': {'name':'OpenAI', 'env':('OPENAI_API_KEY',), 'models':('gpt-4.1-mini','gpt-4.1'),
               'endpoint':'https://api.openai.com/v1/responses'},
    'anthropic': {'name':'Claude (Anthropic)', 'env':('ANTHROPIC_API_KEY',),
                  'models':('claude-sonnet-5-5','claude-haiku-5-5'), 'endpoint':'https://api.anthropic.com/v1/messages'},
    'gemini': {'name':'Gemini (Google)', 'env':('GEMINI_API_KEY','GOOGLE_API_KEY'),
               'models':('gemini-3.8-flash',), 'endpoint':'https://generativelanguage.googleapis.com/v1beta/models'},
    'ollama': {'name':'Ollama (lokal)', 'env':(), 'models':('llama3.2','qwen3:8b'),
               'endpoint':'http://127.0.0.1:11434/api/chat'},
}


def masked_key(key):
    return '•••••••• '+key[-4:] if key and len(key)>8 else ('••••••••' if key else 'Nicht hinterlegt')


def read_env_file(path):
    values = {}
    try:
        text = Path(path).read_text(encoding='utf-8-sig')
    except (OSError,UnicodeError):
        return values
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('export '):
            line = line[7:]
        name,sep,value = line.partition('=')
        value = value.strip()
        if not sep or name.strip().startswith('#'):
            continue
        if value[:1] in ('"', "'") and len(value)>=2 and value[-1]==value[0]:
            value=value[1:-1]
        else:
            value=value.split(' #',1)[0].strip()
        if value:
            values[name.strip()]=value
    return values


class SystemVault:
    service = 'Wissensraum-AI'

    def backend(self):
        try:
            import keyring
            backend = keyring.get_keyring()
            # Permit only native OS stores; never silently use a plaintext file backend.
            module = type(backend).__module__
            if not module.startswith(('keyring.backends.Windows','keyring.backends.macOS',
                                      'keyring.backends.SecretService','keyring.backends.kwallet',
                                      'keyring.backends.libsecret')):
                raise RuntimeError('unsupported backend')
            return backend
        except Exception:
            raise ValueError('Sicherer Betriebssystem-Schlüsselspeicher nicht verfügbar. Unter Linux einen entsperrten Secret-Service-Schlüsselbund verwenden. Vorhandene Umgebungs-/Dateischlüssel bleiben nutzbar.') from None

    def get(self,provider):
        try:
            return self.backend().get_password(self.service,provider)
        except Exception:
            return None

    def set(self,provider,key):
        backend=self.backend()
        try:
            backend.set_password(self.service,provider,key)
        except Exception:
            raise ValueError('Zugangsdaten konnten nicht im Betriebssystem-Schlüsselspeicher gespeichert werden.') from None

    def delete(self,provider):
        backend=self.backend()
        try:
            if backend.get_password(self.service,provider):
                backend.delete_password(self.service,provider)
        except Exception:
            raise ValueError('Zugangsdaten konnten nicht aus dem Betriebssystem-Schlüsselspeicher entfernt werden.') from None


class Settings:
    def __init__(self,store,base,vault=None,environ=None):
        self.store,self.base=store,Path(base)
        self.vault=vault if vault is not None else SystemVault()
        self.environ=os.environ if environ is None else environ

    def config(self):
        with self.store.connect() as db:
            return {row['name']:row['value'] for row in db.execute('SELECT name,value FROM settings').fetchall()}

    @contextmanager
    def _public_mutation(self):
        lock=configuration_operation_lock(self.base)
        try:
            lock.acquire()
        except Exception:
            raise ValueError(CONFIGURATION_OPERATION_BUSY) from None
        try:
            yield
        finally:
            lock.release()

    def _put_owned(self,values):
        authority=ConfigurationAuthority(self.base)
        # Commit the stable store identity before the mutation transaction.
        store_identity(self.store)
        prepared=None
        try:
            with self.store.connect() as db:
                db.executemany('INSERT INTO settings(name,value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value',values.items())
                prepared=authority.synchronize(self.store,db)
            authority.finalize_synchronize(self.store,prepared)
        finally:
            authority.abort_synchronize(prepared)

    def put(self,values):
        with self._public_mutation():
            self._put_owned(values)

    def active(self):
        provider=self.config().get('active_provider','openai')
        return provider if provider in PROVIDERS else 'openai'

    def model(self,provider):
        default=self.environ.get('OPENAI_MODEL') if provider=='openai' else None
        return self.config().get(provider+'.model', default or PROVIDERS[provider]['models'][0])

    def credential(self,provider):
        if provider not in PROVIDERS:
            raise ValueError('Unbekannter KI-Anbieter.')
        if not PROVIDERS[provider]['env']:
            return '', 'Kein Schlüssel erforderlich'
        if self.config().get(provider+'.disabled')=='1':
            return '', 'Deaktiviert'
        key=self.vault.get(provider)
        if key:
            return key,'Betriebssystem-Schlüsselspeicher'
        for name in PROVIDERS[provider]['env']:
            value=self.environ.get(name,'').strip()
            if value:
                return value,'Umgebungsvariable'
        for p in (self.base/'.env.local',self.base/'.env',self.base.parent/'.env.local'):
            values=read_env_file(p)
            for name in PROVIDERS[provider]['env']:
                if values.get(name):
                    return values[name],'Vorhandene Schlüsseldatei'
        return '', 'Nicht hinterlegt'

    def overview(self):
        rows=[]
        active=self.active()
        for provider,info in PROVIDERS.items():
            key,source=self.credential(provider)
            rows.append({'provider':provider,'name':info['name'],
                         'masked':masked_key(key) if info['env'] else 'Nicht erforderlich',
                         'source':source,'model':self.model(provider),'active':provider==active})
        return rows

    def _save_owned(self,provider,model,key=None,activate=True):
        if provider not in PROVIDERS:
            raise ValueError('Unbekannter KI-Anbieter.')
        model=model.strip()
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,119}',model):
            raise ValueError('Gültige Modell-ID mit maximal 120 Zeichen eingeben.')
        values={provider+'.model':model}
        if key is not None and PROVIDERS[provider]['env']:
            key=key.strip()
            if not key or len(key)>4096 or any(c.isspace() for c in key) or any(ord(c)<32 for c in key):
                raise ValueError('API-Schlüssel darf nicht leer sein oder Leerzeichen enthalten.')
            current,_=self.credential(provider)
            if key != current:
                self.vault.set(provider,key)
            values[provider+'.disabled']='0'
        if activate:
            values['active_provider']=provider
        self._put_owned(values)

    def save(self,provider,model,key=None,activate=True):
        with self._public_mutation():
            self._save_owned(provider,model,key,activate)

    def _remove_key_owned(self,provider):
        if not PROVIDERS[provider]['env']:
            return
        # External credentials are disabled for this app; source files/environment remain untouched.
        if self.vault.get(provider):
            self.vault.delete(provider)
        self._put_owned({provider+'.disabled':'1'})

    def remove_key(self,provider):
        with self._public_mutation():
            self._remove_key_owned(provider)

    def import_keys(self,path):
        with self._public_mutation():
            values=read_env_file(path)
            found=0
            for provider,info in PROVIDERS.items():
                value=next((values[n] for n in info['env'] if values.get(n)),None)
                if value:
                    self._save_owned(provider,self.model(provider),value,activate=False)
                    found+=1
            if not found:
                raise ValueError('Keine unterstützten API-Schlüssel in dieser Datei gefunden.')
            return found

    def ai(self,transport=None):
        from core import AI
        provider=self.active()
        key,_=self.credential(provider)
        if PROVIDERS[provider]['env'] and not key:
            raise ValueError('Für '+PROVIDERS[provider]['name']+' fehlt ein API-Schlüssel. Unter Einstellungen hinterlegen.')
        return AI(self.store,key,self.model(provider),transport=transport,provider=provider)
