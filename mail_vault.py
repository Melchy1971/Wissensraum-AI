"""Encrypted mailbox credentials of the server (parity roadmap phase 8, D-030, docs/adr-postfach-zugaenge.md).

The desktop keeps credentials in the system key store (``settings.SystemVault``). The server container has
none, so a mailbox's IMAP password or Microsoft refresh token is stored as AES-256-GCM ciphertext in
``account_mailboxes``, next to the account the mailbox belongs to. The key is a Docker secret
(``WR_MAIL_KEY_FILE``, 32 random bytes as base64) that only server and worker read; it never enters the
database, a backup, the log, the audit trail or an export. Mailbox and account are the associated data of
the encryption, so a ciphertext copied to another row does not decrypt. ``WR_MAIL_KEY_PREVIOUS_FILE`` keeps
an old key readable during a key change; every write uses the current key.

``MailVault`` has the interface of ``SystemVault`` (``get``, ``set``, ``delete`` by secret name
``mail:<mailbox id>``), so ``importer.Importer`` and ``mail_sync.MailSync`` work unchanged on the server.
"""
import base64
import hashlib
import os
from pathlib import Path

PREFIX = 'mail:'


class MailKeyMissing(ValueError):
    """The server has no mail key: mailboxes cannot be added or used."""


def load_key(environ=None, name='WR_MAIL_KEY'):
    """The 32-byte key from ``<name>_FILE`` (Docker secret), or None when it is not configured."""
    environ = os.environ if environ is None else environ
    path = environ.get(f'{name}_FILE', '').strip()
    if not path:
        return None
    try:
        raw = Path(path).read_text(encoding='ascii').strip()
        key = base64.b64decode(raw, validate=True)
    except (OSError, ValueError):
        raise ValueError(f'{name}_FILE ist nicht lesbar oder kein Base64.') from None
    if len(key) != 32:
        raise ValueError(f'{name}_FILE muss 32 Bytes enthalten (base64, zum Beispiel aus openssl rand -base64 32).')
    return key


def key_id(key):
    """Short fingerprint of a key; it names the key in the table without revealing it."""
    return hashlib.sha256(b'wissensraum-mail-key\0' + key).hexdigest()[:16]


class MailVault:
    def __init__(self, store, key, previous=None, owner=None):
        """``owner`` restricts the vault to the mailboxes of one account (web requests); None: the worker."""
        if key is None:
            raise MailKeyMissing('Für Postfächer ist kein Schlüssel eingerichtet (WR_MAIL_KEY_FILE).')
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        self.store, self.owner = store, owner
        self._keys = {key_id(key): AESGCM(key)}
        if previous is not None:
            self._keys.setdefault(key_id(previous), AESGCM(previous))
        self.current = key_id(key)

    def __repr__(self):  # never the key
        return f'MailVault(key_id={self.current!r}, owner={self.owner!r})'

    @staticmethod
    def _mailbox(name):
        if not isinstance(name, str) or not name.startswith(PREFIX) or not name[len(PREFIX):]:
            raise ValueError('Unbekannter Geheimnisname.')
        return name[len(PREFIX):]

    def _row(self, db, mailbox_id):
        row = db.execute('SELECT account_id,key_id,nonce,secret FROM account_mailboxes WHERE mailbox_id=?',
                         (mailbox_id,)).fetchone()
        if row is None or (self.owner is not None and row['account_id'] != self.owner):
            return None
        return row

    @staticmethod
    def _associated(mailbox_id, account_id):
        return f'{mailbox_id}\0{int(account_id)}'.encode('utf-8')

    def get(self, name):
        mailbox_id = self._mailbox(name)
        with self.store.connect() as db:
            row = self._row(db, mailbox_id)
        if row is None:
            return None
        cipher = self._keys.get(row['key_id'])
        if cipher is None:
            raise ValueError('Das Postfach ist mit einem nicht mehr vorhandenen Schlüssel gespeichert. Neu verbinden.')
        from cryptography.exceptions import InvalidTag
        try:
            return cipher.decrypt(bytes(row['nonce']), bytes(row['secret']),
                                  self._associated(mailbox_id, row['account_id'])).decode('utf-8')
        except InvalidTag:
            raise ValueError('Der gespeicherte Zugang ist beschädigt. Postfach neu verbinden.') from None

    def set(self, name, value, account_id=None):
        """Store ``value`` for an existing mailbox row; a new row needs ``account_id`` (its owner)."""
        mailbox_id = self._mailbox(name)
        with self.store.connect() as db:
            row = db.execute('SELECT account_id FROM account_mailboxes WHERE mailbox_id=?', (mailbox_id,)).fetchone()
            owner = row['account_id'] if row is not None else (account_id if account_id is not None else self.owner)
            if owner is None or (self.owner is not None and owner != self.owner):
                raise ValueError('Postfach wurde nicht gefunden.')
            nonce = os.urandom(12)
            secret = self._keys[self.current].encrypt(nonce, str(value).encode('utf-8'),
                                                      self._associated(mailbox_id, owner))
            if row is None:
                db.execute('INSERT INTO account_mailboxes(mailbox_id,account_id,key_id,nonce,secret) VALUES(?,?,?,?,?)',
                           (mailbox_id, owner, self.current, nonce, secret))
            else:
                db.execute('UPDATE account_mailboxes SET key_id=?,nonce=?,secret=?,updated_at=CURRENT_TIMESTAMP '
                           'WHERE mailbox_id=?', (self.current, nonce, secret, mailbox_id))

    def delete(self, name):
        mailbox_id = self._mailbox(name)
        with self.store.connect() as db:
            if self._row(db, mailbox_id) is not None:
                db.execute('DELETE FROM account_mailboxes WHERE mailbox_id=?', (mailbox_id,))

    def rewrap(self):
        """Re-encrypt every stored credential with the current key (after a key change); returns the count."""
        with self.store.connect() as db:
            names = [PREFIX + row['mailbox_id'] for row in db.execute(
                'SELECT mailbox_id FROM account_mailboxes WHERE key_id<>?', (self.current,))]
        for name in names:
            self.set(name, self.get(name))
        return len(names)
