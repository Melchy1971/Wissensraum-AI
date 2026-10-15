"""Local document, PST and mailbox imports with credentials outside SQLite."""
from __future__ import annotations

from email import policy
from email.header import decode_header, make_header
from email.parser import BytesParser
from html.parser import HTMLParser
import imaplib
import json
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid


MAIL_PROVIDERS = {
    'microsoft': {'name':'Microsoft 365 / Outlook', 'host':'', 'port':0, 'auth':'oauth'},
    'gmail': {'name':'Gmail', 'host':'imap.gmail.com', 'port':993, 'auth':'password'},
    'imap': {'name':'Sonstiges IMAP-Konto', 'host':'', 'port':993, 'auth':'password'},
}
MS_SCOPES = ['Mail.Read']


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts=[]
    def handle_data(self,data):
        if data.strip():
            self.parts.append(data.strip())


def html_text(value):
    parser=_HTMLText()
    parser.feed(value or '')
    return '\n'.join(parser.parts)


def decoded(value):
    try:
        return str(make_header(decode_header(value or ''))).strip()
    except (LookupError,UnicodeError):
        return str(value or '').strip()


def email_body(message):
    plain=[]
    html=[]
    parts=message.walk() if message.is_multipart() else (message,)
    for part in parts:
        if part.get_content_disposition()=='attachment':
            continue
        kind=part.get_content_type()
        if kind not in ('text/plain','text/html'):
            continue
        try:
            value=part.get_content()
        except (LookupError,UnicodeError):
            payload=part.get_payload(decode=True) or b''
            value=payload.decode(part.get_content_charset() or 'utf-8',errors='replace')
        (plain if kind=='text/plain' else html).append(str(value))
    text='\n\n'.join(plain).strip()
    return text or html_text('\n'.join(html))


class MailAccounts:
    def __init__(self,store,vault):
        self.store,self.vault=store,vault

    @staticmethod
    def secret_name(account_id):
        return 'mail:'+account_id

    def list(self):
        with self.store.connect() as db:
            rows=[dict(r) for r in db.execute('SELECT * FROM email_accounts ORDER BY label COLLATE NOCASE')]
        for row in rows:
            row['auth_status']='Verbunden' if self.vault.get(self.secret_name(row['id'])) else 'Nicht verbunden'
        return rows

    def get(self,account_id):
        with self.store.connect() as db:
            row=db.execute('SELECT * FROM email_accounts WHERE id=?',(account_id,)).fetchone()
        if not row:
            raise ValueError('E-Mail-Konto wurde nicht gefunden.')
        result=dict(row)
        result['auth_status']='Verbunden' if self.vault.get(self.secret_name(account_id)) else 'Nicht verbunden'
        return result

    def save(self,values,secret=None):
        provider=str(values.get('provider','')).strip()
        if provider not in MAIL_PROVIDERS:
            raise ValueError('Unbekannter E-Mail-Anbieter.')
        account_id=str(values.get('id') or uuid.uuid4().hex)
        label=str(values.get('label','')).strip()
        address=str(values.get('email','')).strip()
        if not label or len(label)>120 or any(ord(c)<32 for c in label) or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',address):
            raise ValueError('Kontoname und gültige E-Mail-Adresse eingeben.')
        username=str(values.get('username') or address).strip()
        host=str(values.get('host') or MAIL_PROVIDERS[provider]['host']).strip()
        try:
            port=int(values.get('port') or MAIL_PROVIDERS[provider]['port'] or 0)
        except (TypeError,ValueError):
            raise ValueError('Gültigen IMAP-Port eingeben.') from None
        client_id=str(values.get('client_id','')).strip()
        tenant=str(values.get('tenant') or 'common').strip()
        if provider=='microsoft':
            try:
                client_id=str(uuid.UUID(client_id))
            except ValueError:
                raise ValueError('Für Microsoft die Client-ID einer öffentlichen Entra-Anwendung eingeben.')
            if not re.fullmatch(r'[A-Za-z0-9.-]{1,255}',tenant):
                raise ValueError('Gültigen Microsoft-Mandanten eingeben.')
            host,port,username='',0,address
        elif not re.fullmatch(r'[A-Za-z0-9.-]{1,253}',host) or not 1 <= port <= 65535 or not username or any(ord(c)<32 for c in username):
            raise ValueError('IMAP-Server, Port und Benutzername vollständig eingeben.')
        existing=None
        try:
            existing=self.get(account_id)
        except ValueError:
            pass
        with self.store.connect() as db:
            duplicate=db.execute('SELECT id FROM email_accounts WHERE provider=? AND lower(email)=lower(?) AND id<>?',
                                 (provider,address,account_id)).fetchone()
        if duplicate:
            raise ValueError('Dieses E-Mail-Konto ist bereits angelegt.')
        secret=(secret or '').strip()
        if provider=='gmail':
            secret=secret.replace(' ','')
        same_provider=existing and existing['provider']==provider
        if provider!='microsoft' and not secret and not (same_provider and self.vault.get(self.secret_name(account_id))):
            raise ValueError('Kennwort oder App-Passwort eingeben.')
        if existing and not same_provider and self.vault.get(self.secret_name(account_id)):
            self.vault.delete(self.secret_name(account_id))
        if secret:
            if len(secret)>4096 or any(ord(c)<32 for c in secret):
                raise ValueError('Ungültiges Kennwort oder App-Passwort.')
            self.vault.set(self.secret_name(account_id),secret)
        with self.store.connect() as db:
            db.execute('''INSERT INTO email_accounts(id,label,provider,email,username,host,port,client_id,tenant)
                          VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                          label=excluded.label,provider=excluded.provider,email=excluded.email,
                          username=excluded.username,host=excluded.host,port=excluded.port,
                          client_id=excluded.client_id,tenant=excluded.tenant,updated_at=CURRENT_TIMESTAMP''',
                       (account_id,label,provider,address,username,host,port,client_id,tenant))
        return account_id

    def delete(self,account_id):
        self.get(account_id)
        if self.vault.get(self.secret_name(account_id)):
            self.vault.delete(self.secret_name(account_id))
        with self.store.connect() as db:
            db.execute('DELETE FROM email_accounts WHERE id=?',(account_id,))

    def begin_microsoft(self,account_id):
        account=self.get(account_id)
        if account['provider']!='microsoft':
            raise ValueError('OAuth-Anmeldung ist nur für Microsoft-Konten erforderlich.')
        try:
            import msal
        except ImportError:
            raise ValueError('Microsoft-Anmeldung ist nicht installiert. Abhängigkeiten erneut installieren.') from None
        cache=msal.SerializableTokenCache()
        saved=self.vault.get(self.secret_name(account_id))
        if saved:
            cache.deserialize(saved)
        app=msal.PublicClientApplication(account['client_id'],authority='https://login.microsoftonline.com/'+account['tenant'],token_cache=cache)
        flow=app.initiate_device_flow(scopes=MS_SCOPES)
        if 'user_code' not in flow:
            raise ValueError('Microsoft-Anmeldung konnte nicht gestartet werden.')
        return {'app':app,'cache':cache,'flow':flow,'account_id':account_id,'message':flow.get('message','')}

    def finish_microsoft(self,context):
        result=context['app'].acquire_token_by_device_flow(context['flow'])
        if 'access_token' not in result:
            raise ValueError('Microsoft-Anmeldung fehlgeschlagen oder abgebrochen.')
        self.vault.set(self.secret_name(context['account_id']),context['cache'].serialize())
        return True

    def microsoft_token(self,account_id):
        account=self.get(account_id)
        saved=self.vault.get(self.secret_name(account_id))
        if not saved:
            raise ValueError('Microsoft-Konto zuerst unter Einstellungen verbinden.')
        import msal
        cache=msal.SerializableTokenCache()
        cache.deserialize(saved)
        app=msal.PublicClientApplication(account['client_id'],authority='https://login.microsoftonline.com/'+account['tenant'],token_cache=cache)
        accounts=app.get_accounts(username=account['email']) or app.get_accounts()
        result=app.acquire_token_silent(MS_SCOPES,account=accounts[0] if accounts else None)
        if not result or 'access_token' not in result:
            raise ValueError('Microsoft-Anmeldung abgelaufen. Konto unter Einstellungen neu verbinden.')
        if cache.has_state_changed:
            self.vault.set(self.secret_name(account_id),cache.serialize())
        return result['access_token']


class Importer:
    def __init__(self,store,accounts):
        self.store,self.accounts=store,accounts

    @staticmethod
    def _limit(value,maximum=10000):
        value=int(value)
        if not 1 <= value <= maximum:
            raise ValueError(f'Anzahl muss zwischen 1 und {maximum} liegen.')
        return value

    def _add_email(self,raw,label='E-Mail'):
        message=BytesParser(policy=policy.default).parsebytes(raw)
        subject=decoded(message.get('Subject')) or '(Ohne Betreff)'
        sender=decoded(message.get('From'))
        body=email_body(message)
        if not body:
            raise ValueError('E-Mail enthält keinen lesbaren Nachrichtentext.')
        content='\n'.join(filter(None,[
            'Betreff: '+subject,
            'Von: '+sender if sender else '',
            'An: '+decoded(message.get('To')) if message.get('To') else '',
            'Datum: '+decoded(message.get('Date')) if message.get('Date') else '',
            'Nachrichten-ID: '+decoded(message.get('Message-ID')) if message.get('Message-ID') else '',
            '',body]))
        return self.store.add(subject,content,'E-Mail · '+label,subject+'.eml',raw)

    def import_imap(self,account_id,folder='INBOX',limit=100,client_factory=None):
        account=self.accounts.get(account_id)
        if account['provider']=='microsoft':
            return self.import_microsoft(account_id,limit)
        secret=self.accounts.vault.get(self.accounts.secret_name(account_id))
        if not secret:
            raise ValueError('Für dieses Konto fehlt das Kennwort oder App-Passwort.')
        limit=self._limit(limit,2000)
        factory=client_factory or imaplib.IMAP4_SSL
        report={'found':0,'imported':0,'duplicates':0,'errors':0}
        client=None
        try:
            client=factory(account['host'],account['port'])
            client.login(account['username'],secret)
            status,_=client.select(folder or 'INBOX',readonly=True)
            if status!='OK':
                raise ValueError('E-Mail-Ordner wurde nicht gefunden.')
            status,data=client.search(None,'ALL')
            if status!='OK':
                raise ValueError('E-Mails konnten nicht aufgelistet werden.')
            ids=(data[0].split() if data and data[0] else [])[-limit:]
            report['found']=len(ids)
            for message_id in reversed(ids):
                try:
                    status,payload=client.fetch(message_id,'(BODY.PEEK[])')
                    raw=next((item[1] for item in payload if isinstance(item,tuple) and isinstance(item[1],bytes)),None)
                    if status!='OK' or not raw or len(raw)>25*1024*1024:
                        raise ValueError('E-Mail konnte nicht gelesen werden oder ist größer als 25 MB.')
                    _,created=self._add_email(raw,account['label'])
                    report['imported' if created else 'duplicates']+=1
                except Exception:
                    report['errors']+=1
        except imaplib.IMAP4.error:
            raise ValueError('IMAP-Anmeldung fehlgeschlagen. Zugangsdaten und IMAP-Freigabe prüfen.') from None
        finally:
            if client:
                try: client.logout()
                except Exception: pass
        return report

    def _graph_get(self,url,token):
        parsed=urllib.parse.urlparse(url)
        if parsed.scheme!='https' or parsed.netloc!='graph.microsoft.com':
            raise ValueError('Ungültige Microsoft-Graph-Adresse.')
        request=urllib.request.Request(url,headers={'Authorization':'Bearer '+token,'Accept':'application/json'})
        try:
            with urllib.request.urlopen(request,timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code in (401,403):
                raise ValueError('Microsoft-Zugriff abgelehnt. Konto neu verbinden und Mail.Read freigeben.') from None
            raise ValueError('Microsoft Graph meldet HTTP '+str(exc.code)+'.') from None
        except (urllib.error.URLError,TimeoutError,OSError):
            raise ValueError('Microsoft Graph ist nicht erreichbar.') from None

    def import_microsoft(self,account_id,limit=100,transport=None):
        account=self.accounts.get(account_id)
        limit=self._limit(limit,1000)
        token=self.accounts.microsoft_token(account_id)
        get=transport or self._graph_get
        fields='id,subject,from,toRecipients,ccRecipients,receivedDateTime,body,internetMessageId'
        url='https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages?'+urllib.parse.urlencode({'$top':min(limit,100),'$select':fields,'$orderby':'receivedDateTime desc'})
        report={'found':0,'imported':0,'duplicates':0,'errors':0}
        while url and report['found']<limit:
            page=get(url,token)
            for item in page.get('value',[]):
                if report['found']>=limit: break
                report['found']+=1
                try:
                    sender=item.get('from',{}).get('emailAddress',{}).get('address','')
                    recipients=', '.join(x.get('emailAddress',{}).get('address','') for x in item.get('toRecipients',[]))
                    body=item.get('body',{}).get('content','')
                    if item.get('body',{}).get('contentType','').lower()=='html': body=html_text(body)
                    subject=item.get('subject') or '(Ohne Betreff)'
                    content='\n'.join(filter(None,['Betreff: '+subject,'Von: '+sender,'An: '+recipients,
                        'Datum: '+item.get('receivedDateTime',''),'Nachrichten-ID: '+item.get('internetMessageId',''),'',body]))
                    if not body.strip(): raise ValueError('Kein Nachrichtentext')
                    raw=json.dumps(item,ensure_ascii=False).encode()
                    _,created=self.store.add(subject,content,'E-Mail · '+account['label'],subject+'.json',raw)
                    report['imported' if created else 'duplicates']+=1
                except Exception:
                    report['errors']+=1
            url=page.get('@odata.nextLink')
        return report

    def import_pst(self,path,limit=500,archive_factory=None):
        path=Path(path)
        if path.suffix.lower()!='.pst' or not path.is_file():
            raise ValueError('Eine vorhandene Outlook-PST-Datei auswählen.')
        limit=self._limit(limit)
        if archive_factory is None:
            try:
                from libratom.lib.pff import PffArchive
                archive_factory=PffArchive
            except ImportError:
                raise ValueError('PST-Unterstützung fehlt. install-pst-support.bat im Programmordner ausführen.') from None
        report={'found':0,'imported':0,'duplicates':0,'errors':0}
        with archive_factory(path) as archive:
            for message in archive.messages():
                if report['found']>=limit: break
                report['found']+=1
                try:
                    raw=archive.format_message(message,with_headers=True).encode('utf-8',errors='replace')
                    _,created=self._add_email(raw,'PST · '+path.stem)
                    report['imported' if created else 'duplicates']+=1
                except Exception:
                    report['errors']+=1
        return report
