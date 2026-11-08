"""Accounts, roles and grants for the multi-user server (Release 3.0, step 10.2).

See docs/sicherheit-3.0.md. One organization per installation. An account is identified by the
Entra ID tenant and object id (never by e-mail). Roles: admin, editor, reader. A grant gives an
account read, write or owner access to one document or one collection; owner is what the importing
account gets. A document without any grant is visible to administrators only, so the stock that
existed before schema 19 belongs to the first administrator. Disabled accounts see nothing.

The desktop application does not use this module and keeps its single-user behaviour.
"""

ROLES = ('admin', 'editor', 'reader')
ACCESS = ('read', 'write', 'owner')
MAX_NAME = 200


class Conflict(ValueError):
    """The record changed since it was read (step 10.6 builds on this)."""


def _organization(db):
    return db.execute('SELECT min(id) AS id FROM organizations').fetchone()['id']


def account(store, account_id):
    with store.connect() as db:
        row = db.execute('SELECT id,tenant_id,object_id,display_name,role,disabled,version FROM accounts WHERE id=?',
                         (account_id,)).fetchone()
    if row is None:
        raise ValueError('Konto nicht gefunden.')
    return dict(row)


def by_identity(store, tenant_id, object_id):
    """The account for an Entra ID identity, or None; the login never creates one."""
    with store.connect() as db:
        row = db.execute('SELECT id,tenant_id,object_id,display_name,role,disabled,version FROM accounts '
                         'WHERE tenant_id=? AND object_id=?', (tenant_id, object_id)).fetchone()
    return dict(row) if row else None


def accounts(store):
    with store.connect() as db:
        return [dict(row) for row in db.execute(
            'SELECT id,tenant_id,object_id,display_name,role,disabled,version FROM accounts ORDER BY id')]


def _check_identity(tenant_id, object_id):
    for value in (tenant_id, object_id):
        if not isinstance(value, str) or not value.strip() or len(value) > MAX_NAME:
            raise ValueError('Mandanten- und Objekt-ID sind Pflicht.')


def create_account(store, tenant_id, object_id, role, display_name=''):
    """Create an account; only administrators call this (and the bootstrap command for the first)."""
    _check_identity(tenant_id, object_id)
    if role not in ROLES:
        raise ValueError('Unbekannte Rolle.')
    if len(display_name) > MAX_NAME:
        raise ValueError('Anzeigename ist zu lang.')
    if by_identity(store, tenant_id.strip(), object_id.strip()):
        raise ValueError('Dieses Konto gibt es schon.')
    with store.connect() as db:
        return db.execute('INSERT INTO accounts(organization_id,tenant_id,object_id,display_name,role) '
                          'VALUES(?,?,?,?,?) RETURNING id',
                          (_organization(db), tenant_id.strip(), object_id.strip(), display_name.strip(), role)
                          ).fetchone()['id']


def update_account(store, account_id, version, role=None, disabled=None, display_name=None):
    """Change role, lock state or name; ``version`` must be the one read, else ``Conflict``."""
    if role is not None and role not in ROLES:
        raise ValueError('Unbekannte Rolle.')
    if display_name is not None and len(display_name) > MAX_NAME:
        raise ValueError('Anzeigename ist zu lang.')
    current = account(store, account_id)
    if current['version'] != version:
        raise Conflict('Das Konto wurde inzwischen geändert.')
    new_role = current['role'] if role is None else role
    new_disabled = current['disabled'] if disabled is None else int(bool(disabled))
    if current['role'] == 'admin' and (new_role != 'admin' or new_disabled) and _active_admins(store) == 1:
        raise ValueError('Der letzte aktive Administrator kann nicht herabgestuft oder gesperrt werden.')
    with store.connect() as db:
        changed = db.execute('UPDATE accounts SET role=?,disabled=?,display_name=?,version=version+1 '
                             'WHERE id=? AND version=?',
                             (new_role, new_disabled,
                              current['display_name'] if display_name is None else display_name.strip(),
                              account_id, version)).rowcount
    if changed != 1:
        raise Conflict('Das Konto wurde inzwischen geändert.')
    return account(store, account_id)


def _active_admins(store):
    with store.connect() as db:
        return int(db.execute("SELECT count(*) AS n FROM accounts WHERE role='admin' AND disabled=0").fetchone()['n'])


def grant(store, account_id, access, document_id=None, collection_id=None):
    """Give ``access`` on exactly one document or collection; an existing grant there is replaced."""
    if access not in ACCESS:
        raise ValueError('Unbekannte Freigabe.')
    if (document_id is None) == (collection_id is None):
        raise ValueError('Eine Freigabe gilt genau für ein Dokument oder eine Sammlung.')
    account(store, account_id)
    with store.connect() as db:
        table, target = ('documents', document_id) if document_id is not None else ('collections', collection_id)
        if db.execute(f'SELECT 1 FROM {table} WHERE id=?', (target,)).fetchone() is None:
            raise ValueError('Ziel der Freigabe nicht gefunden.')
        column = 'document_id' if document_id is not None else 'collection_id'
        # ponytail: one grant per account and target is kept by delete-then-insert in one
        # transaction; a partial unique index would enforce it in the database (upgrade path).
        db.execute(f'DELETE FROM grants WHERE account_id=? AND {column}=?', (account_id, target))
        return db.execute(f'INSERT INTO grants(account_id,{column},access) VALUES(?,?,?) RETURNING id',
                          (account_id, target, access)).fetchone()['id']


def revoke(store, account_id, document_id=None, collection_id=None):
    if (document_id is None) == (collection_id is None):
        raise ValueError('Eine Freigabe gilt genau für ein Dokument oder eine Sammlung.')
    column, target = ('document_id', document_id) if document_id is not None else ('collection_id', collection_id)
    with store.connect() as db:
        return db.execute(f'DELETE FROM grants WHERE account_id=? AND {column}=?', (account_id, target)).rowcount


def grants(store, account_id):
    with store.connect() as db:
        return [dict(row) for row in db.execute(
            'SELECT id,document_id,collection_id,access FROM grants WHERE account_id=? ORDER BY id', (account_id,))]


def visible_documents(acting, alias='d'):
    """SQL condition and parameters that keep only documents ``acting`` may see.

    Used inside the query before ordering and limits (docs/sicherheit-3.0.md, Rechte), never to
    filter results afterwards. ``acting`` is an account dict as returned by ``account``.
    """
    if acting['disabled']:
        return '1=0', ()
    if acting['role'] == 'admin':
        return '1=1', ()
    return (f'({alias}.id IN (SELECT g.document_id FROM grants g WHERE g.account_id=? AND g.document_id IS NOT NULL) '
            f'OR {alias}.id IN (SELECT cd.document_id FROM collection_documents cd JOIN grants g '
            f'ON g.collection_id=cd.collection_id WHERE g.account_id=?))', (acting['id'], acting['id']))


def visible_document_ids(store, acting):
    condition, params = visible_documents(acting)
    with store.connect() as db:
        return [row['id'] for row in db.execute(f'SELECT d.id FROM documents d WHERE {condition} ORDER BY d.id',
                                                params)]


def can_write(store, acting, document_id):
    """Whether ``acting`` may change a document: admin, or editor with write or owner access."""
    if acting['disabled'] or acting['role'] == 'reader':
        return False
    if acting['role'] == 'admin':
        return True
    with store.connect() as db:
        return db.execute(
            "SELECT 1 FROM grants g WHERE g.account_id=? AND g.access IN ('write','owner') AND "
            '(g.document_id=? OR g.collection_id IN (SELECT collection_id FROM collection_documents '
            'WHERE document_id=?)) LIMIT 1', (acting['id'], document_id, document_id)).fetchone() is not None
