"""Mailboxes of server accounts (parity roadmap phase 8, D-030).

``ServerMailboxes`` offers what ``importer.Importer`` and ``mail_sync.MailSync`` need from the desktop's
``MailAccounts`` (``get``, ``list``, ``vault``, ``secret_name``, ``microsoft_token``), but every mailbox
belongs to one account (``account_mailboxes``) and its credential is encrypted (``mail_vault.MailVault``).
There is no configuration file on the server, so mailboxes are written directly to ``email_accounts``.

With ``owner`` set (web requests) only that account's mailboxes exist; a foreign mailbox looks like a
missing one. The worker uses ``owner=None`` and sees every server mailbox. Microsoft 365 mailboxes are
connected through the server's Entra app with delegated ``Mail.Read`` (``web`` connect flow); the refresh
token is the stored credential and is exchanged for a Graph token on use.
"""
import re
import uuid

from importer import GMAIL_APP_PASSWORD, MAIL_PROVIDERS, MailLoginError
from mail_vault import PREFIX

GRAPH_SCOPE = 'offline_access https://graph.microsoft.com/Mail.Read'
COLUMNS = 'e.id,e.label,e.provider,e.email,e.username,e.host,e.port,e.client_id,e.tenant,e.created_at,m.account_id'


class ServerMailboxes:
    def __init__(self, store, vault, owner=None, refresh=None):
        """``refresh(refresh_token)`` returns the token response of the Entra app (``oidc.Provider.refresh``)."""
        self.store, self.vault, self.owner, self.refresh = store, vault, owner, refresh

    @staticmethod
    def secret_name(mailbox_id):
        return PREFIX + mailbox_id

    def _rows(self, mailbox_id=None):
        conditions, params = [], []
        if self.owner is not None:
            conditions.append('m.account_id=?'); params.append(self.owner)
        if mailbox_id is not None:
            conditions.append('e.id=?'); params.append(mailbox_id)
        where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
        with self.store.connect() as db:
            return [dict(row, auth_status='Verbunden') for row in db.execute(
                f'SELECT {COLUMNS} FROM email_accounts e JOIN account_mailboxes m ON m.mailbox_id=e.id{where} '
                'ORDER BY lower(e.label),e.id', tuple(params))]

    def list(self):
        return self._rows()

    def get(self, mailbox_id):
        rows = self._rows(str(mailbox_id))
        if not rows:
            raise ValueError('E-Mail-Konto wurde nicht gefunden.')
        return rows[0]

    def owner_of(self, mailbox_id):
        return self.get(mailbox_id)['account_id']

    def _clean(self, values, provider):
        label = ' '.join(str(values.get('label', '')).split())
        address = str(values.get('email', '')).strip()
        if not label or len(label) > 120 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', address) or len(address) > 254:
            raise ValueError('Kontoname und gültige E-Mail-Adresse eingeben.')
        return label, address

    def save_imap(self, values, secret):
        """New IMAP or Gmail mailbox of ``owner`` with its password; returns the mailbox id. Nothing is read yet."""
        provider = str(values.get('provider', '')).strip()
        if provider not in ('imap', 'gmail') or self.owner is None:
            raise ValueError('Unbekannter E-Mail-Anbieter.')
        label, address = self._clean(values, provider)
        host = str(values.get('host') or MAIL_PROVIDERS[provider]['host']).strip()
        try:
            port = int(values.get('port') or MAIL_PROVIDERS[provider]['port'])
        except (TypeError, ValueError):
            raise ValueError('Gültigen IMAP-Port eingeben.') from None
        username = address if provider == 'gmail' else str(values.get('username') or address).strip()
        if not re.fullmatch(r'[A-Za-z0-9.-]{1,253}', host) or not 1 <= port <= 65535 or not username \
                or any(ord(c) < 32 for c in username):
            raise ValueError('IMAP-Server, Port und Benutzername vollständig eingeben.')
        secret = (secret or '').strip()
        if provider == 'gmail':
            secret = secret.replace(' ', '')
            if not GMAIL_APP_PASSWORD.fullmatch(secret):
                raise ValueError('Gmail verlangt ein App-Passwort aus 16 Kleinbuchstaben (Google-Konto, Sicherheit, '
                                 'App-Passwörter). Das normale Google-Passwort funktioniert für IMAP nicht.')
        if not secret or len(secret) > 4096 or any(ord(c) < 32 for c in secret):
            raise ValueError('Kennwort oder App-Passwort eingeben.')
        return self._insert(provider, label, address, username, host, port, secret)

    def save_microsoft(self, address, label, refresh_token):
        """Microsoft 365 mailbox of ``owner`` after the Entra consent; an existing one gets the new token."""
        if self.owner is None:
            raise ValueError('Unbekanntes Konto.')
        label, address = self._clean({'label': label, 'email': address}, 'microsoft')
        existing = next((row for row in self._rows() if row['provider'] == 'microsoft'
                         and row['email'].lower() == address.lower()), None)
        if existing:
            self.vault.set(self.secret_name(existing['id']), refresh_token)
            return existing['id']
        return self._insert('microsoft', label, address, address, '', 0, refresh_token)

    def _insert(self, provider, label, address, username, host, port, secret):
        if any(row['provider'] == provider and row['email'].lower() == address.lower() for row in self._rows()):
            raise ValueError('Dieses E-Mail-Konto ist bereits angelegt.')
        mailbox_id = uuid.uuid4().hex
        with self.store.transaction():
            with self.store.connect() as db:
                db.execute('INSERT INTO email_accounts(id,label,provider,email,username,host,port,client_id,tenant) '
                           "VALUES(?,?,?,?,?,?,?,'','common')", (mailbox_id, label, provider, address, username, host, port))
            self.vault.set(self.secret_name(mailbox_id), secret, account_id=self.owner)
        return mailbox_id

    def delete(self, mailbox_id):
        """Remove the mailbox and its credential; imported e-mails stay as documents of the account."""
        self.get(mailbox_id)
        with self.store.transaction():
            with self.store.connect() as db:
                db.execute('DELETE FROM settings WHERE name=?', ('mail_sync.' + mailbox_id,))
                db.execute('DELETE FROM email_accounts WHERE id=?', (mailbox_id,))  # credential and marks cascade

    def microsoft_token(self, mailbox_id):
        """A Graph access token from the stored refresh token; a rotated refresh token is stored again."""
        mailbox = self.get(mailbox_id)
        if mailbox['provider'] != 'microsoft':
            raise ValueError('OAuth-Anmeldung ist nur für Microsoft-Konten erforderlich.')
        saved = self.vault.get(self.secret_name(mailbox_id))
        if not saved or self.refresh is None:
            raise MailLoginError('Microsoft-Konto zuerst verbinden.')
        try:
            tokens = self.refresh(saved)
        except Exception:  # refused or unreachable: the reason is never the provider's text
            raise MailLoginError('Microsoft-Anmeldung abgelaufen oder widerrufen. Postfach neu verbinden.') from None
        if not isinstance(tokens, dict) or not isinstance(tokens.get('access_token'), str):
            raise MailLoginError('Microsoft-Anmeldung abgelaufen oder widerrufen. Postfach neu verbinden.')
        if isinstance(tokens.get('refresh_token'), str) and tokens['refresh_token'] != saved:
            self.vault.set(self.secret_name(mailbox_id), tokens['refresh_token'])
        return tokens['access_token']
