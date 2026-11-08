"""Server-side web sessions and pending sign-ins (Release 3.0, step 10.3, docs/sicherheit-3.0.md).

A session id has 256 bits of randomness and is stored only as its SHA-256. A session ends after
30 minutes without a request or 8 hours after sign-in. A pending sign-in keeps the hashes of
``state`` and ``nonce`` plus the PKCE verifier for at most 10 minutes and can be used once. Times are
Unix seconds. The desktop application does not use this module.
"""
import hashlib
import secrets
import time

IDLE_SECONDS = 30 * 60
ABSOLUTE_SECONDS = 8 * 60 * 60
LOGIN_SECONDS = 10 * 60
MAX_TOKEN = 200


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _now(now):
    return int(time.time() if now is None else now)


def _usable(value):
    return isinstance(value, str) and 0 < len(value) <= MAX_TOKEN


def start_login(store, now=None):
    """A new pending sign-in; returns ``(state, nonce, verifier)``."""
    now = _now(now)
    state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    with store.connect() as db:
        db.execute('DELETE FROM login_requests WHERE created_at<?', (now - LOGIN_SECONDS,))
        db.execute('INSERT INTO login_requests(state,nonce,verifier,created_at) VALUES(?,?,?,?)',
                   (digest(state), digest(nonce), verifier, now))
    return state, nonce, verifier


def finish_login(store, state, now=None):
    """Use a pending sign-in once; returns ``(nonce_digest, verifier)`` or None if unknown or expired."""
    if not _usable(state):
        return None
    now = _now(now)
    with store.connect() as db:
        row = db.execute('DELETE FROM login_requests WHERE state=? RETURNING nonce,verifier,created_at',
                         (digest(state),)).fetchone()
    if row is None or row['created_at'] < now - LOGIN_SECONDS:
        return None
    return row['nonce'], row['verifier']


def create(store, account_id, now=None):
    """A new session for an account; returns ``(session_id, csrf_token)``."""
    now = _now(now)
    session_id, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with store.connect() as db:
        db.execute('INSERT INTO web_sessions(id,account_id,csrf_token,created_at,last_seen) VALUES(?,?,?,?,?)',
                   (digest(session_id), account_id, csrf, now, now))
    return session_id, csrf


def lookup(store, session_id, now=None):
    """The live session as ``{'account_id', 'csrf_token'}``, or None; an expired one is removed."""
    if not _usable(session_id):
        return None
    now, key = _now(now), digest(session_id)
    with store.connect() as db:
        row = db.execute('SELECT account_id,csrf_token,created_at,last_seen FROM web_sessions WHERE id=?',
                         (key,)).fetchone()
        if row is None:
            return None
        if row['last_seen'] < now - IDLE_SECONDS or row['created_at'] < now - ABSOLUTE_SECONDS:
            db.execute('DELETE FROM web_sessions WHERE id=?', (key,))
            return None
        db.execute('UPDATE web_sessions SET last_seen=? WHERE id=?', (now, key))
    return {'account_id': row['account_id'], 'csrf_token': row['csrf_token']}


def end(store, session_id):
    if _usable(session_id):
        with store.connect() as db:
            db.execute('DELETE FROM web_sessions WHERE id=?', (digest(session_id),))


def end_all(db, account_id):
    """Remove every session of an account inside the caller's transaction."""
    return db.execute('DELETE FROM web_sessions WHERE account_id=?', (account_id,)).rowcount
