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
import base64
import uuid
from contextlib import contextmanager
from core import MAX_DOCUMENT_BYTES, ImportRejected, extract_document
import mail_structure
from configuration_authority import (CONFIGURATION_OPERATION_BUSY, ConfigurationAuthority,
                                     configuration_operation_lock, store_identity)


MAIL_PROVIDERS = {
    'microsoft': {'name':'Microsoft 365 / Outlook', 'host':'', 'port':0, 'auth':'oauth'},
    'gmail': {'name':'Gmail', 'host':'imap.gmail.com', 'port':993, 'auth':'password'},
    'imap': {'name':'Sonstiges IMAP-Konto', 'host':'', 'port':993, 'auth':'password'},
}
MS_SCOPES = ['Mail.Read']
GMAIL_APP_PASSWORD = re.compile(r'[a-z]{16}')


def imap_refusal(error):
    """Readable reason for a refused IMAP login; never repeats the server text itself."""
    text = error.args[0] if getattr(error, 'args', None) else error
    text = (text.decode('utf-8', 'replace') if isinstance(text, bytes) else str(text)).lower()
    if 'application-specific password' in text or 'app password' in text:
        return 'Der Anbieter verlangt ein App-Passwort.'
    if 'authenticationfailed' in text or 'invalid credentials' in text or 'authentication failed' in text:
        return 'Benutzername oder Passwort wurden abgelehnt.'
    if 'webalert' in text or 'web login required' in text:
        return 'Der Anbieter verlangt zuerst eine Anmeldung im Browser.'
    if 'unavailable' in text or 'try again' in text:
        return 'Der Server ist vorübergehend nicht verfügbar.'
    code = re.search(r'\[([a-z-]+)\]', text)
    return f'Serverantwort {code.group(1).upper()}.' if code else 'Der Server nennt keinen Grund.'
MAX_MESSAGE_TEXT_BYTES = 25*1024*1024
# Transfer encoding (base64) makes a part about a third larger than the decoded file.
MAX_ATTACHMENT_ENCODED_BYTES = MAX_DOCUMENT_BYTES*4//3+4096


def attachment_line(attachments):
    """Readable list of attachment names for the e-mail text; content is never included."""
    if not attachments:
        return ''
    return 'Anhänge: '+'; '.join(f'{item["name"]} ({item.get("content_type") or "unbekannt"}, '
                                  f'{item.get("size") or 0} Bytes)' for item in attachments)


def _report():
    return {'found':0,'imported':0,'duplicates':0,'errors':0,'attachments_imported':0,
            'attachments_duplicates':0,'attachments_quarantined':0,'attachments_errors':0,'quarantine':[]}


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
    def __init__(self,store,vault,base):
        self.store,self.vault=store,vault
        self.authority=ConfigurationAuthority(base)
        self.base=Path(base)

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

    def _change_metadata(self,operation):
        # Commit the stable store identity before the mutation transaction.
        store_identity(self.store)
        prepared=None
        try:
            with self.store.connect() as db:
                result=operation(db)
                prepared=self.authority.synchronize(self.store,db)
            self.authority.finalize_synchronize(self.store,prepared)
            return result
        finally:
            self.authority.abort_synchronize(prepared)

    @staticmethod
    def secret_name(account_id):
        return 'mail:'+account_id

    def list(self):
        with self.store.connect() as db:
            rows=[dict(r) for r in db.execute('SELECT * FROM email_accounts ORDER BY lower(label)')]
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

    def _save_owned(self,values,secret=None):
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
            username=address  # Gmail accepts only the full address as IMAP user name
            secret=secret.replace(' ','')
            if secret and not GMAIL_APP_PASSWORD.fullmatch(secret):
                raise ValueError('Gmail verlangt ein App-Passwort aus 16 Kleinbuchstaben (Google-Konto → '
                                 'Sicherheit → App-Passwörter). Das normale Google-Passwort funktioniert für IMAP nicht.')
        same_provider=existing and existing['provider']==provider
        if provider!='microsoft' and not secret and not (same_provider and self.vault.get(self.secret_name(account_id))):
            raise ValueError('Kennwort oder App-Passwort eingeben.')
        if existing and not same_provider and self.vault.get(self.secret_name(account_id)):
            self.vault.delete(self.secret_name(account_id))
        if secret:
            if len(secret)>4096 or any(ord(c)<32 for c in secret):
                raise ValueError('Ungültiges Kennwort oder App-Passwort.')
            self.vault.set(self.secret_name(account_id),secret)
        def persist(db):
            db.execute('''INSERT INTO email_accounts(id,label,provider,email,username,host,port,client_id,tenant)
                          VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                          label=excluded.label,provider=excluded.provider,email=excluded.email,
                          username=excluded.username,host=excluded.host,port=excluded.port,
                          client_id=excluded.client_id,tenant=excluded.tenant,updated_at=CURRENT_TIMESTAMP''',
                       (account_id,label,provider,address,username,host,port,client_id,tenant))
        self._change_metadata(persist)
        return account_id

    def save(self,values,secret=None):
        with self._public_mutation():
            return self._save_owned(values,secret)

    def _delete_owned(self,account_id):
        self.get(account_id)
        if self.vault.get(self.secret_name(account_id)):
            self.vault.delete(self.secret_name(account_id))
        self._change_metadata(lambda db: db.execute(
            'DELETE FROM email_accounts WHERE id=?',(account_id,)))

    def delete(self,account_id):
        with self._public_mutation():
            self._delete_owned(account_id)

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

    def _add_email(self,raw,label='E-Mail',source=None,attachments=()):
        message=BytesParser(policy=policy.default).parsebytes(raw)
        subject=decoded(message.get('Subject')) or '(Ohne Betreff)'
        sender=decoded(message.get('From'))
        body=email_body(message)
        if not body and not attachments:
            raise ImportRejected('no_text','E-Mail enthält keinen lesbaren Nachrichtentext.')
        body=body or '(Kein Nachrichtentext)'
        content='\n'.join(filter(None,[
            'Betreff: '+subject,
            'Von: '+sender if sender else '',
            'An: '+decoded(message.get('To')) if message.get('To') else '',
            'Datum: '+decoded(message.get('Date')) if message.get('Date') else '',
            'Nachrichten-ID: '+decoded(message.get('Message-ID')) if message.get('Message-ID') else '',
            attachment_line(attachments),
            '',body]))
        return self.store.add(subject,content,'E-Mail · '+label,subject+'.eml',raw,source)

    def _email_document(self,account_id,key):
        with self.store.connect() as db:
            row=db.execute('SELECT document_id FROM email_imports WHERE account_id=? AND remote_id=?',
                           (account_id,key)).fetchone()
        return row['document_id'] if row else None

    def _store_attachment(self,name,data,content_type,account_id,locator,email_document_id,label):
        """Store one released attachment as its own document linked to its e-mail; raises ImportRejected."""
        title,text,name,data=extract_document(name,data)
        document_id,created=self.store.add(title,text,'E-Mail · '+label+' · Anhang',name,data,
                                           ('attachment',account_id,locator))
        with self.store.connect() as db:
            db.execute('INSERT INTO email_attachments(document_id,email_document_id,name,content_type,size) '
                       'VALUES(?,?,?,?,?) ON CONFLICT(document_id) DO NOTHING',
                       (document_id,email_document_id,name,content_type or '',len(data)))
        if account_id:
            self._record_import(account_id,locator,document_id)
        return document_id,created

    def _add_attachment(self,report,name,data,content_type,account_id,locator,email_document_id,label):
        try:
            document_id,created=self._store_attachment(name,data,content_type,account_id,locator,
                                                       email_document_id,label)
        except ImportRejected as rejected:
            report['attachments_quarantined']+=1
            report['quarantine'].append({'name':name,'reason':rejected.reason})
            return None
        report['attachments_imported' if created else 'attachments_duplicates']+=1
        return document_id

    def _source_matches(self,document_id,source):
        """True when the document was stored for exactly this source (crash recovery)."""
        with self.store.connect() as db:
            row=db.execute('SELECT source_type,account_id,locator FROM sources WHERE document_id=?',
                           (document_id,)).fetchone()
        return row is not None and (row['source_type'],row['account_id'],row['locator'])==tuple(source)

    def _imported_ids(self,account_id,remote_ids):
        remote_ids=list(dict.fromkeys(str(value) for value in remote_ids))
        if not remote_ids: return set()
        placeholders=','.join('?' for _ in remote_ids)
        with self.store.connect() as db:
            rows=db.execute(f'SELECT remote_id FROM email_imports WHERE account_id=? AND remote_id IN ({placeholders})',
                            (account_id,*remote_ids)).fetchall()
        return {row['remote_id'] for row in rows}

    def _record_import(self,account_id,remote_id,document_id):
        with self.store.connect() as db:
            db.execute('''INSERT INTO email_imports(account_id,remote_id,document_id) VALUES(?,?,?)
                          ON CONFLICT(account_id,remote_id) DO UPDATE SET
                          document_id=excluded.document_id,imported_at=CURRENT_TIMESTAMP''',
                       (account_id,str(remote_id),document_id))

    @staticmethod
    def _imap_import_key(folder,uid):
        return 'imap:'+str(folder or 'INBOX')+':'+str(uid)

    @staticmethod
    def _microsoft_import_key(message_id):
        return 'graph:'+str(message_id)

    def _imap_login(self,account_id,folder,client_factory=None):
        account=self.accounts.get(account_id)
        secret=self.accounts.vault.get(self.accounts.secret_name(account_id))
        if not secret:
            raise ValueError('Für dieses Konto fehlt das Kennwort oder App-Passwort.')
        factory=client_factory or imaplib.IMAP4_SSL
        client=factory(account['host'],account['port'])
        username=account['email'] if account['provider']=='gmail' else account['username']
        stage='login'
        try:
            client.login(username,secret)
            stage='select'
            status,_=client.select(folder or 'INBOX',readonly=True)
            if status!='OK':
                raise ValueError('E-Mail-Ordner wurde nicht gefunden.')
            return client,account
        except imaplib.IMAP4.error as error:
            try: client.logout()
            except Exception: pass
            if stage=='select':
                raise ValueError('E-Mail-Ordner konnte nicht schreibgeschützt geöffnet werden.') from None
            hint=(' Bei Gmail: Bestätigung in zwei Schritten aktivieren und ein App-Passwort aus 16 Buchstaben eintragen.'
                  if account['provider']=='gmail' else ' Zugangsdaten und IMAP-Freigabe prüfen.')
            raise ValueError('IMAP-Anmeldung fehlgeschlagen: '+imap_refusal(error)+hint) from None
        except (OSError,TimeoutError):
            try: client.logout()
            except Exception: pass
            raise ValueError('IMAP-Server ist nicht erreichbar.') from None

    @staticmethod
    def _imap_raw(payload):
        return next((item[1] for item in payload or [] if isinstance(item,tuple) and isinstance(item[1],bytes)),None)

    def preview_imap(self,account_id,folder='INBOX',limit=100,client_factory=None):
        limit=self._limit(limit,2000)
        client,_=self._imap_login(account_id,folder,client_factory)
        items=[]
        try:
            status,data=client.uid('search',None,'ALL')
            if status!='OK':
                raise ValueError('E-Mails konnten nicht aufgelistet werden.')
            ids=(data[0].split() if data and data[0] else [])[-limit:]
            for uid in reversed(ids):
                try:
                    # Structure and selected header fields only: no message text, no attachment content.
                    status,payload=client.uid('fetch',uid,
                        '(BODYSTRUCTURE BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE MESSAGE-ID)])')
                    if status!='OK': raise ValueError('Kopfzeile nicht lesbar')
                    fields=mail_structure.fetch_items(payload)
                    raw=next((value for key,value in fields.items() if key.startswith('BODY[HEADER')),None)
                    if not isinstance(raw,bytes): raise ValueError('Kopfzeile nicht lesbar')
                    message=BytesParser(policy=policy.default).parsebytes(raw)
                    attachments=[{'id':part['section'],'name':mail_structure.attachment_name(part),
                                  'content_type':part['content_type'],'size':part['size']}
                                 for part in mail_structure.parts(fields.get('BODYSTRUCTURE')) if part['attachment']]
                    items.append({'id':uid.decode('ascii'),'subject':decoded(message.get('Subject')) or '(Ohne Betreff)',
                                  'sender':decoded(message.get('From')),'date':decoded(message.get('Date')),
                                  'attachments':attachments})
                except Exception:
                    items.append({'id':uid.decode('ascii',errors='ignore'),'subject':'(Kopfzeile nicht lesbar)',
                                  'sender':'','date':'','attachments':[]})
        finally:
            try: client.logout()
            except Exception: pass
        self._mark_imported(account_id,items,lambda item:self._imap_import_key(folder,item['id']))
        return items

    def _mark_imported(self,account_id,items,key):
        keys=[key(item) for item in items]
        keys+=[key(item)+'#'+str(part['id']) for item in items for part in item.get('attachments',())]
        imported=self._imported_ids(account_id,keys)
        for item in items:
            item['imported']=key(item) in imported
            for part in item.get('attachments',()):
                part['imported']=key(item)+'#'+str(part['id']) in imported

    @staticmethod
    def _imap_fetch(client,uid,spec):
        status,payload=client.uid('fetch',uid.encode('ascii'),spec)
        if status!='OK':
            raise ValueError('E-Mail konnte nicht gelesen werden.')
        return mail_structure.fetch_items(payload)

    def _imap_message(self,client,uid,structure,leaves):
        """Fetch header and text parts only; return an RFC 822 message without attachment content."""
        attachments=[leaf for leaf in leaves if leaf['attachment']]
        texts=[leaf for leaf in leaves if not leaf['attachment']]
        if sum(leaf['size'] for leaf in texts)>MAX_MESSAGE_TEXT_BYTES:
            raise ValueError('Nachrichtentext ist größer als 25 MB.')
        if not mail_structure.is_multipart(structure):
            if attachments:  # the whole message is one attachment
                header=self._imap_fetch(client,uid,'(BODY.PEEK[HEADER])').get('BODY[HEADER]') or b''
                return mail_structure.rebuild_message(header,[],1)
            fields=self._imap_fetch(client,uid,'(BODY.PEEK[HEADER] BODY.PEEK[TEXT])')
            return (fields.get('BODY[HEADER]') or b'')+(fields.get('BODY[TEXT]') or b'')
        spec=' '.join(['BODY.PEEK[HEADER]']+[f'BODY.PEEK[{leaf["section"]}.MIME] BODY.PEEK[{leaf["section"]}]'
                                               for leaf in texts])
        fields=self._imap_fetch(client,uid,'('+spec+')')
        parts=[(fields.get(f'BODY[{leaf["section"]}.MIME]') or b'',fields.get(f'BODY[{leaf["section"]}]') or b'')
               for leaf in texts]
        return mail_structure.rebuild_message(fields.get('BODY[HEADER]') or b'',parts,len(attachments))

    def _imap_attachment(self,client,uid,structure,leaf):
        if not mail_structure.is_multipart(structure):
            fields=self._imap_fetch(client,uid,'(BODY.PEEK[HEADER] BODY.PEEK[1])')
            return mail_structure.part_payload(fields.get('BODY[HEADER]') or b'',fields.get('BODY[1]') or b'')
        section=leaf['section']
        fields=self._imap_fetch(client,uid,f'(BODY.PEEK[{section}.MIME] BODY.PEEK[{section}])')
        return mail_structure.part_payload(fields.get(f'BODY[{section}.MIME]') or b'',fields.get(f'BODY[{section}]') or b'')

    def import_imap_ids(self,account_id,folder,uids,client_factory=None,attachments=None):
        """Import the selected messages; attachments only when released in ``attachments``.

        ``attachments`` maps a message UID to the released attachment section numbers. It may
        name messages that were imported earlier. The full message (``BODY.PEEK[]``) is never fetched.
        """
        uids=list(dict.fromkeys(str(uid) for uid in uids))
        released={str(uid):list(dict.fromkeys(str(part) for part in parts))
                  for uid,parts in (attachments or {}).items() if parts}
        everything=list(dict.fromkeys(uids+list(released)))
        if (not everything or len(everything)>2000 or any(not re.fullmatch(r'\d+',uid) for uid in everything)
                or any(not re.fullmatch(r'\d+(\.\d+)*',part) for parts in released.values() for part in parts)):
            raise ValueError('Keine gültige E-Mail-Auswahl vorhanden.')
        client,account=self._imap_login(account_id,folder,client_factory)
        report=_report(); report['found']=len(uids)
        try:
            for uid in everything:
                key=self._imap_import_key(folder,uid)
                try:
                    structure=self._imap_fetch(client,uid,'(BODYSTRUCTURE)').get('BODYSTRUCTURE')
                    leaves=mail_structure.parts(structure)
                except Exception:  # without the structure nothing is fetched, not even the text
                    if uid in uids: report['errors']+=1
                    report['attachments_errors']+=len(released.get(uid,()))
                    continue
                if uid in uids:
                    try:
                        raw=self._imap_message(client,uid,structure,leaves)
                        names=[{'name':mail_structure.attachment_name(leaf),'content_type':leaf['content_type'],
                                'size':leaf['size']} for leaf in leaves if leaf['attachment']]
                        document_id,created=self._add_email(raw,account['label'],('imap',account_id,key),names)
                        self._record_import(account_id,key,document_id)
                        report['imported' if created else 'duplicates']+=1
                    except Exception:
                        report['errors']+=1
                email_document_id=self._email_document(account_id,key)
                by_section={leaf['section']:leaf for leaf in leaves if leaf['attachment']}
                for section in released.get(uid,()):
                    leaf=by_section.get(section)
                    try:
                        if leaf is None:
                            raise ValueError('Anhang wurde nicht gefunden.')
                        name=mail_structure.attachment_name(leaf)
                        if leaf['size']>MAX_ATTACHMENT_ENCODED_BYTES:  # rejected before any download
                            report['attachments_quarantined']+=1
                            report['quarantine'].append({'name':name,'reason':'too_large'})
                            continue
                        data=self._imap_attachment(client,uid,structure,leaf)
                        self._add_attachment(report,name,data,leaf['content_type'],account_id,key+'#'+section,
                                             email_document_id,account['label'])
                    except Exception:
                        report['attachments_errors']+=1
        finally:
            try: client.logout()
            except Exception: pass
        return report

    def import_imap(self,account_id,folder='INBOX',limit=100,client_factory=None):
        account=self.accounts.get(account_id)
        if account['provider']=='microsoft':
            return self.import_microsoft(account_id,limit)
        items=self.preview_imap(account_id,folder,limit,client_factory)
        return self.import_imap_ids(account_id,folder,[item['id'] for item in items],client_factory)

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

    def preview_microsoft(self,account_id,limit=100,transport=None):
        limit=self._limit(limit,1000)
        token=self.accounts.microsoft_token(account_id)
        get=transport or self._graph_get
        fields='id,subject,from,receivedDateTime,hasAttachments'
        url='https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages?'+urllib.parse.urlencode({'$top':min(limit,100),'$select':fields,'$orderby':'receivedDateTime desc'})
        items=[]
        while url and len(items)<limit:
            page=get(url,token)
            for item in page.get('value',[]):
                if len(items)>=limit: break
                message_id=str(item.get('id',''))
                if not message_id: continue
                items.append({'id':message_id,'subject':item.get('subject') or '(Ohne Betreff)',
                              'sender':item.get('from',{}).get('emailAddress',{}).get('address',''),
                              'date':item.get('receivedDateTime',''),
                              'attachments':(self._graph_attachments(get,token,message_id)
                                             if item.get('hasAttachments') else [])})
            url=page.get('@odata.nextLink')
        self._mark_imported(account_id,items,lambda item:self._microsoft_import_key(item['id']))
        return items

    @staticmethod
    def _graph_message_url(message_id,suffix=''):
        return 'https://graph.microsoft.com/v1.0/me/messages/'+urllib.parse.quote(message_id,safe='')+suffix

    def _graph_attachments(self,get,token,message_id):
        """Attachment metadata only: contentBytes is never selected here."""
        url=self._graph_message_url(message_id,'/attachments?'+urllib.parse.urlencode(
            {'$select':'id,name,contentType,size,isInline'}))
        try:
            page=get(url,token)
        except Exception:
            return []
        return [{'id':str(item.get('id','')),'name':item.get('name') or 'Anhang','content_type':item.get('contentType') or '',
                 'size':int(item.get('size') or 0)} for item in page.get('value',[]) if item.get('id')]

    def _add_graph_item(self,item,label,source=None,attachments=()):
        sender=item.get('from',{}).get('emailAddress',{}).get('address','')
        recipients=', '.join(x.get('emailAddress',{}).get('address','') for x in item.get('toRecipients',[]))
        body=item.get('body',{}).get('content','')
        if item.get('body',{}).get('contentType','').lower()=='html': body=html_text(body)
        subject=item.get('subject') or '(Ohne Betreff)'
        if not body.strip():
            if not attachments: raise ImportRejected('no_text','E-Mail enthält keinen lesbaren Nachrichtentext.')
            body='(Kein Nachrichtentext)'
        content='\n'.join(filter(None,['Betreff: '+subject,'Von: '+sender,'An: '+recipients,
            'Datum: '+item.get('receivedDateTime',''),'Nachrichten-ID: '+item.get('internetMessageId',''),
            attachment_line(attachments),'',body]))
        raw=json.dumps(item,ensure_ascii=False).encode()
        return self.store.add(subject,content,'E-Mail · '+label,subject+'.json',raw,source)

    def import_microsoft_ids(self,account_id,message_ids,transport=None,attachments=None):
        """Import selected messages; download only attachments released in ``attachments``."""
        account=self.accounts.get(account_id)
        message_ids=list(dict.fromkeys(str(value) for value in message_ids))
        released={str(key):list(dict.fromkeys(str(value) for value in values))
                  for key,values in (attachments or {}).items() if values}
        everything=list(dict.fromkeys(message_ids+list(released)))
        valid=lambda value:value and len(value)<=1000 and not any(ord(c)<32 for c in value)
        if (not everything or len(everything)>1000 or not all(valid(value) for value in everything)
                or not all(valid(value) for values in released.values() for value in values)):
            raise ValueError('Keine gültige E-Mail-Auswahl vorhanden.')
        token=self.accounts.microsoft_token(account_id)
        get=transport or self._graph_get
        fields='id,subject,from,toRecipients,ccRecipients,receivedDateTime,body,internetMessageId,hasAttachments'
        report=_report(); report['found']=len(message_ids)
        for message_id in everything:
            key=self._microsoft_import_key(message_id)
            if message_id in message_ids:
                try:
                    item=get(self._graph_message_url(message_id,'?'+urllib.parse.urlencode({'$select':fields})),token)
                    names=self._graph_attachments(get,token,message_id) if item.get('hasAttachments') else []
                    document_id,created=self._add_graph_item(item,account['label'],('graph',account_id,key),names)
                    self._record_import(account_id,key,document_id)
                    report['imported' if created else 'duplicates']+=1
                except Exception:
                    report['errors']+=1
            email_document_id=self._email_document(account_id,key)
            for attachment_id in released.get(message_id,()):
                try:
                    item=get(self._graph_message_url(message_id,'/attachments/'+urllib.parse.quote(attachment_id,safe='')),token)
                    name=item.get('name') or 'Anhang'
                    if item.get('@odata.type')!='#microsoft.graph.fileAttachment' or 'contentBytes' not in item:
                        report['attachments_quarantined']+=1
                        report['quarantine'].append({'name':name,'reason':'unsupported'})
                        continue
                    if int(item.get('size') or 0)>MAX_ATTACHMENT_ENCODED_BYTES:
                        report['attachments_quarantined']+=1
                        report['quarantine'].append({'name':name,'reason':'too_large'})
                        continue
                    data=base64.b64decode(item['contentBytes'],validate=True)
                    self._add_attachment(report,name,data,item.get('contentType') or '',account_id,
                                         key+'#'+attachment_id,email_document_id,account['label'])
                except Exception:
                    report['attachments_errors']+=1
        return report

    def import_microsoft(self,account_id,limit=100,transport=None):
        items=self.preview_microsoft(account_id,limit,transport)
        return self.import_microsoft_ids(account_id,[item['id'] for item in items],transport)

    def preview_account(self,account_id,folder='INBOX',limit=100):
        account=self.accounts.get(account_id)
        if account['provider']=='microsoft':
            return self.preview_microsoft(account_id,limit)
        return self.preview_imap(account_id,folder,limit)

    def import_selected(self,account_id,folder,items,attachments=None):
        """``items``: messages to import; ``attachments``: {message id: [released attachment ids]}."""
        account=self.accounts.get(account_id)
        ids=[item['id'] for item in items]
        if account['provider']=='microsoft':
            return self.import_microsoft_ids(account_id,ids,attachments=attachments)
        return self.import_imap_ids(account_id,folder,ids,attachments=attachments)

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
                    names=self._pst_attachment_names(archive,message)
                    _,created=self._add_email(raw,'PST · '+path.stem,('pst','',path.stem),names)
                    report['imported' if created else 'duplicates']+=1
                except Exception:
                    report['errors']+=1
        return report

    # --- PST selection (step 6.3b) ---------------------------------------------------
    @staticmethod
    def _pst_archive(path,archive_factory=None):
        path=Path(path)
        if path.suffix.lower()!='.pst' or not path.is_file():
            raise ValueError('Eine vorhandene Outlook-PST-Datei auswählen.')
        if archive_factory is None:
            try:
                from libratom.lib.pff import PffArchive
                archive_factory=PffArchive
            except ImportError:
                raise ValueError('PST-Unterstützung fehlt. install-pst-support.bat im Programmordner ausführen.') from None
        return archive_factory(path)

    @staticmethod
    def _pst_key(path,identifier):
        return 'pst:'+str(Path(path).resolve())+':'+str(identifier)

    @staticmethod
    def _pst_attachment_list(message):
        """Name and size from the attachment records; the content is not read."""
        import mimetypes
        items=[]
        for index,attachment in enumerate(getattr(message,'attachments',None) or ()):
            name=getattr(attachment,'name',None) or f'Anhang-{index+1}'
            items.append({'id':str(index),'name':name,'content_type':mimetypes.guess_type(name)[0] or '',
                          'size':int(getattr(attachment,'size',0) or 0)})
        return items

    def preview_pst(self,path,limit=500,archive_factory=None):
        """List messages and attachment metadata of a PST file; nothing is stored or read beyond metadata."""
        limit=self._limit(limit)
        items=[]
        with self._pst_archive(path,archive_factory) as archive:
            for message in archive.messages():
                if len(items)>=limit: break
                date=getattr(message,'delivery_time',None) or getattr(message,'client_submit_time',None)
                items.append({'id':str(message.identifier),'subject':(getattr(message,'subject',None) or '(Ohne Betreff)'),
                              'sender':getattr(message,'sender_name',None) or '','date':str(date or ''),
                              'attachments':self._pst_attachment_list(message)})
        keys={item['id']:self._pst_key(path,item['id']) for item in items}
        wanted=list(keys.values())+[keys[item['id']]+'#'+part['id'] for item in items for part in item['attachments']]
        with self.store.connect() as db:
            known=set()
            for start in range(0,len(wanted),500):
                chunk=wanted[start:start+500]
                rows=db.execute('SELECT locator FROM sources WHERE source_type IN (?,?) AND account_id=? AND locator IN ('
                                +','.join('?' for _ in chunk)+')',('pst','attachment','',*chunk)).fetchall()
                known|={row['locator'] for row in rows}
        for item in items:
            item['imported']=keys[item['id']] in known
            for part in item['attachments']:
                part['imported']=keys[item['id']]+'#'+part['id'] in known
        return items

    # --- import jobs (step 6.3b) -----------------------------------------------------
    @staticmethod
    def _job_entries(items,attachments):
        """Job items in mailbox order: each message, then its released attachments."""
        released={str(key):[str(value) for value in values] for key,values in (attachments or {}).items() if values}
        known={str(item['id']):item for item in items}
        entries=[]
        order=list(dict.fromkeys([str(item['id']) for item in items]+list(released)))
        for message_id in order:
            item=known.get(message_id) or {}
            subject=item.get('subject') or message_id
            if message_id in known:
                entries.append(('E-Mail: '+subject,message_id))
            names={str(part.get('id')):part.get('name') for part in item.get('attachments',())}
            for part in released.get(message_id,()):
                entries.append(('Anhang: '+(names.get(part) or part)+' (E-Mail: '+subject+')',message_id+'#'+part))
        return entries

    def create_mail_job(self,jobs,account_id,folder,items,attachments=None):
        """Create an import job for selected messages and released attachments of a mailbox."""
        account=self.accounts.get(account_id)
        kind='graph' if account['provider']=='microsoft' else 'imap'
        root=json.dumps({'account_id':account_id,'folder':folder or 'INBOX'},ensure_ascii=False)
        entries=self._job_entries(items,attachments)
        for _,locator in entries:
            message_id,_,part=locator.partition('#')
            if kind=='imap' and (not re.fullmatch(r'\d+',message_id) or (part and not re.fullmatch(r'\d+(\.\d+)*',part))):
                raise ValueError('Keine gültige E-Mail-Auswahl vorhanden.')
            if not message_id or len(locator)>2100 or any(ord(c)<32 for c in locator):
                raise ValueError('Keine gültige E-Mail-Auswahl vorhanden.')
        return jobs.create_entries(kind,root,'E-Mail · '+account['label'],entries)

    def create_pst_job(self,jobs,path,items,attachments=None):
        path=Path(path).resolve()
        entries=self._job_entries(items,attachments)
        if any(not re.fullmatch(r'\d+(#\d+)?',locator) for _,locator in entries):
            raise ValueError('Keine gültige PST-Auswahl vorhanden.')
        return jobs.create_entries('pst',json.dumps({'path':str(path)},ensure_ascii=False),
                                   'E-Mail · PST · '+path.stem,entries)

    @contextmanager
    def job_handler(self,job,client_factory=None,transport=None,archive_factory=None):
        """Open the mailbox or archive once per run and yield ``handle(item, recovering)``.

        ``handle`` returns (status, document_id, stored bytes) or raises ImportRejected (quarantine),
        OSError or another exception (failed, retried on resume).
        """
        settings=json.loads(job['root'])
        if job['kind']=='imap':
            account=self.accounts.get(settings['account_id'])
            folder=settings['folder']
            client,_=self._imap_login(settings['account_id'],folder,client_factory)
            structures={}
            def structure_of(uid):
                if uid not in structures:
                    structure=self._imap_fetch(client,uid,'(BODYSTRUCTURE)').get('BODYSTRUCTURE')
                    structures[uid]=(structure,mail_structure.parts(structure))
                return structures[uid]
            def handle(item,recovering):
                uid,_,section=item['locator'].partition('#')
                structure,leaves=structure_of(uid)
                key=self._imap_import_key(folder,uid)
                if not section:
                    raw=self._imap_message(client,uid,structure,leaves)
                    names=[{'name':mail_structure.attachment_name(leaf),'content_type':leaf['content_type'],
                            'size':leaf['size']} for leaf in leaves if leaf['attachment']]
                    return self._job_email(raw,account,('imap',account['id'],key),names,recovering)
                leaf=next((leaf for leaf in leaves if leaf['attachment'] and leaf['section']==section),None)
                if leaf is None:
                    raise ValueError('Anhang wurde nicht gefunden.')
                name=mail_structure.attachment_name(leaf)
                if leaf['size']>MAX_ATTACHMENT_ENCODED_BYTES:  # rejected before any download
                    raise ImportRejected('too_large','Anhang überschreitet das Limit von 20 MB.')
                data=self._imap_attachment(client,uid,structure,leaf)
                return self._job_attachment(name,data,leaf['content_type'],account['id'],key+'#'+section,
                                            self._email_document(account['id'],key),account['label'],recovering)
            try:
                yield handle
            finally:
                try: client.logout()
                except Exception: pass
        elif job['kind']=='graph':
            account=self.accounts.get(settings['account_id'])
            token=self.accounts.microsoft_token(account['id'])
            get=transport or self._graph_get
            fields='id,subject,from,toRecipients,ccRecipients,receivedDateTime,body,internetMessageId,hasAttachments'
            def handle(item,recovering):
                message_id,_,attachment_id=item['locator'].partition('#')
                key=self._microsoft_import_key(message_id)
                if not attachment_id:
                    detail=get(self._graph_message_url(message_id,'?'+urllib.parse.urlencode({'$select':fields})),token)
                    names=self._graph_attachments(get,token,message_id) if detail.get('hasAttachments') else []
                    raw=json.dumps(detail,ensure_ascii=False).encode()
                    source=('graph',account['id'],key)
                    document_id,created=self._add_graph_item(detail,account['label'],source,names)
                    return self._job_result(document_id,created,source,recovering,raw,account['id'],key)
                detail=get(self._graph_message_url(message_id,'/attachments/'+urllib.parse.quote(attachment_id,safe='')),token)
                name=detail.get('name') or 'Anhang'
                if detail.get('@odata.type')!='#microsoft.graph.fileAttachment' or 'contentBytes' not in detail:
                    raise ImportRejected('unsupported','Weitergeleitete Elemente und Verweise werden nicht unterstützt.')
                if int(detail.get('size') or 0)>MAX_ATTACHMENT_ENCODED_BYTES:
                    raise ImportRejected('too_large','Anhang überschreitet das Limit von 20 MB.')
                data=base64.b64decode(detail['contentBytes'],validate=True)
                return self._job_attachment(name,data,detail.get('contentType') or '',account['id'],
                                            key+'#'+attachment_id,self._email_document(account['id'],key),
                                            account['label'],recovering)
            yield handle
        elif job['kind']=='pst':
            path=Path(settings['path'])
            with self._pst_archive(path,archive_factory) as archive:
                def message_of(identifier):
                    message=archive.get_message_by_id(int(identifier))
                    if message is None:
                        raise ValueError('Nachricht wurde im PST-Archiv nicht gefunden.')
                    return message
                def handle(item,recovering):
                    identifier,_,index=item['locator'].partition('#')
                    message=message_of(identifier)
                    key=self._pst_key(path,identifier)
                    label='PST · '+path.stem
                    if not index:
                        raw=archive.format_message(message,with_headers=True).encode('utf-8',errors='replace')
                        names=self._pst_attachment_list(message)
                        source=('pst','',key)
                        document_id,created=self._add_email(raw,label,source,names)
                        return self._job_result(document_id,created,source,recovering,raw)
                    attachments=list(getattr(message,'attachments',None) or ())
                    if int(index)>=len(attachments):
                        raise ValueError('Anhang wurde nicht gefunden.')
                    attachment=attachments[int(index)]
                    name=getattr(attachment,'name',None) or f'Anhang-{int(index)+1}'
                    size=int(getattr(attachment,'size',0) or 0)
                    if size>MAX_DOCUMENT_BYTES:  # rejected before reading
                        raise ImportRejected('too_large','Anhang überschreitet das Limit von 20 MB.')
                    data=attachment.read_buffer(size) if size else b''
                    email_document_id=self._pst_email_document(key)
                    import mimetypes
                    return self._job_attachment(name,bytes(data),mimetypes.guess_type(name)[0] or '','',
                                                key+'#'+index,email_document_id,label,recovering)
                yield handle
        else:
            raise ValueError('Unbekannte Auftragsart.')

    def _pst_email_document(self,key):
        with self.store.connect() as db:
            row=db.execute("SELECT document_id FROM sources WHERE source_type='pst' AND account_id='' AND locator=?",
                           (key,)).fetchone()
        return row['document_id'] if row else None

    def _job_result(self,document_id,created,source,recovering,raw,account_id=None,key=None):
        if not created and recovering and self._source_matches(document_id,source):
            created=True  # stored by this item before the interruption
        if account_id:
            self._record_import(account_id,key,document_id)
        return ('imported' if created else 'duplicate'),document_id,raw

    def _job_email(self,raw,account,source,names,recovering):
        document_id,created=self._add_email(raw,account['label'],source,names)
        return self._job_result(document_id,created,source,recovering,raw,account['id'],source[2])

    def _job_attachment(self,name,data,content_type,account_id,locator,email_document_id,label,recovering):
        document_id,created=self._store_attachment(name,data,content_type,account_id,locator,
                                                   email_document_id,label)
        if not created and recovering and self._source_matches(document_id,('attachment',account_id,locator)):
            created=True
        return ('imported' if created else 'duplicate'),document_id,data

    @staticmethod
    def _pst_attachment_names(archive,message):
        """Names, types and sizes from libratom metadata; attachment content is not read (step 6.3b)."""
        metadata=getattr(archive,'get_attachment_metadata',None)
        if metadata is None:
            return []
        try:
            return [{'name':item.name,'content_type':item.mime_type or '','size':item.size} for item in metadata(message)]
        except Exception:
            return []
