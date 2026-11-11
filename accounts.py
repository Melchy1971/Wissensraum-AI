"""Accounts, roles and grants for the multi-user server (Release 3.0, step 10.2).

See docs/sicherheit-3.0.md. One organization per installation. An account is identified by the
Entra ID tenant and object id (never by e-mail). Roles: admin, editor, reader. A grant gives an
account read, write or owner access to one document or one collection; owner is what the importing
account gets. A document without any grant is visible to administrators only, so the stock that
existed before schema 19 belongs to the first administrator. Disabled accounts see nothing.

Changing an account's role, lock or grants ends all its web sessions (step 10.3), so the next
request signs in again with the new rights.

The desktop application does not use this module and keeps its single-user behaviour.
"""

from sessions import end_all as _end_sessions

ROLES = ('admin', 'editor', 'reader')
ACCESS = ('read', 'write', 'owner')
MAX_NAME = 200


from core import Conflict, _revise  # noqa: E402  (one conflict type for every object, step 10.6)


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
        if changed == 1:
            _end_sessions(db, account_id)
    if changed != 1:
        raise Conflict('Das Konto wurde inzwischen geändert.')
    return account(store, account_id)


def _active_admins(store):
    with store.connect() as db:
        return int(db.execute("SELECT count(*) AS n FROM accounts WHERE role='admin' AND disabled=0").fetchone()['n'])


def grant(store, account_id, access, document_id=None, collection_id=None, end_sessions=True, revision=None):
    """Give ``access`` on exactly one document or collection; an existing grant there is changed.

    ``end_sessions=False`` only for the owner grant of a document the account has just created: that
    adds nothing to what it could see before and must not sign it out. ``revision`` is the revision of
    the grant the caller read, 0 when it saw none; anything else raises ``Conflict``.
    """
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
        # ponytail: one grant per account and target is kept by this code; a partial unique index
        # would enforce it in the database (upgrade path).
        existing = db.execute(f'SELECT id FROM grants WHERE account_id=? AND {column}=?',
                              (account_id, target)).fetchone()
        if end_sessions:
            _end_sessions(db, account_id)
        if existing is not None:  # revision 0 ("saw none") never matches an existing grant
            _revise(db, 'grants', existing['id'], revision, 'Die Freigabe')
            db.execute('UPDATE grants SET access=? WHERE id=?', (access, existing['id']))
            return existing['id']
        if revision not in (None, 0):
            raise Conflict('Die Freigabe wurde inzwischen entfernt. Bitte den aktuellen Stand laden.')
        return db.execute(f'INSERT INTO grants(account_id,{column},access) VALUES(?,?,?) RETURNING id',
                          (account_id, target, access)).fetchone()['id']


def inherit_grants(store, previous_id, document_id):
    """Copy the read and write grants of ``previous_id`` to its new version ``document_id``.

    Owner grants are not copied: the new version belongs to whoever uploaded it. Sessions stay.
    """
    with store.connect() as db:
        db.execute("INSERT INTO grants(account_id,document_id,access) SELECT g.account_id,?,g.access FROM grants g "
                   "WHERE g.document_id=? AND g.access IN ('read','write') AND NOT EXISTS (SELECT 1 FROM grants h "
                   'WHERE h.account_id=g.account_id AND h.document_id=?)', (document_id, previous_id, document_id))


def revoke(store, account_id, document_id=None, collection_id=None, revision=None):
    if (document_id is None) == (collection_id is None):
        raise ValueError('Eine Freigabe gilt genau für ein Dokument oder eine Sammlung.')
    column, target = ('document_id', document_id) if document_id is not None else ('collection_id', collection_id)
    with store.connect() as db:
        _end_sessions(db, account_id)
        if revision is None:
            return db.execute(f'DELETE FROM grants WHERE account_id=? AND {column}=?', (account_id, target)).rowcount
        if db.execute(f'DELETE FROM grants WHERE account_id=? AND {column}=? AND revision=?',
                      (account_id, target, int(revision))).rowcount != 1:
            raise Conflict('Die Freigabe wurde inzwischen geändert. Bitte den aktuellen Stand laden.')
        return 1


def grants(store, account_id):
    with store.connect() as db:
        return [dict(row) for row in db.execute(
            'SELECT id,document_id,collection_id,access,revision FROM grants WHERE account_id=? ORDER BY id',
            (account_id,))]


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


def visible_collections(acting, alias='c'):
    """SQL condition for collections ``acting`` may see: administrators all, others those granted to them."""
    if acting['disabled']:
        return '1=0', ()
    if acting['role'] == 'admin':
        return '1=1', ()
    return (f'{alias}.id IN (SELECT g.collection_id FROM grants g WHERE g.account_id=? AND g.collection_id IS NOT NULL)',
            (acting['id'],))


def visible_document_ids(store, acting):
    condition, params = visible_documents(acting)
    with store.connect() as db:
        return [row['id'] for row in db.execute(f'SELECT d.id FROM documents d WHERE {condition} ORDER BY d.id',
                                                params)]


def can_write_collection(store, acting, collection_id):
    """Whether ``acting`` may change a collection: admin, or editor with write or owner access to it."""
    if acting['disabled'] or acting['role'] == 'reader':
        return False
    if acting['role'] == 'admin':
        return True
    with store.connect() as db:
        return db.execute("SELECT 1 FROM grants WHERE account_id=? AND collection_id=? AND access IN ('write','owner')",
                          (acting['id'], collection_id)).fetchone() is not None


def can_delete(store, acting, document_id):
    """Whether ``acting`` may delete a document: admin, or an editor with owner access to exactly this
    document. Write access, or ownership through a collection, is not enough (decision 11.11.2026).
    """
    if acting['disabled'] or acting['role'] == 'reader':
        return False
    if acting['role'] == 'admin':
        return True
    with store.connect() as db:
        return db.execute("SELECT 1 FROM grants WHERE account_id=? AND document_id=? AND access='owner'",
                          (acting['id'], document_id)).fetchone() is not None


def can_delete_collection(store, acting, collection_id):
    """Like ``can_delete`` for a collection: admin, or owner access to exactly this collection."""
    if acting['disabled'] or acting['role'] == 'reader':
        return False
    if acting['role'] == 'admin':
        return True
    with store.connect() as db:
        return db.execute("SELECT 1 FROM grants WHERE account_id=? AND collection_id=? AND access='owner'",
                          (acting['id'], collection_id)).fetchone() is not None


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
