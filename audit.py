"""Audit trail of the multi-user server (Release 3.0, step 10.5, docs/sicherheit-3.0.md).

Append only. Entry ids are gapless (1, 2, 3, ...). Each entry stores the SHA-256 of the previous entry
and its own hash over a canonical JSON form of all its fields, so a changed, removed or swapped entry
breaks the chain. Database triggers refuse UPDATE, DELETE and (PostgreSQL) TRUNCATE. Stored are ids,
time, account, source address and short codes, never document texts, questions or answers.

Limit: removing the newest entries leaves a valid shorter chain. ``verify`` therefore accepts the
count and head hash noted from an earlier check (``expected``) and reports a shortened trail.
"""
import hashlib
import json
import threading
import time

GENESIS = '0' * 64
EVENTS = {
    'sign_in', 'sign_in_refused', 'sign_out', 'account_created', 'account_changed', 'grant_created',
    'grant_revoked', 'document_created', 'document_changed', 'document_deleted', 'collection_export',
    'collection_changed', 'graph_changed',
    'original_download', 'privacy_erase', 'settings_changed', 'ai_question', 'import_started',
}
FIELDS = ('id', 'created_at', 'account_id', 'event', 'target', 'source', 'details', 'previous_hash')
MAX_TEXT = 100
AUDIT_LOCK = 0x57524155  # 'WRAU', one chain head per database
_lock = threading.Lock()  # one chain head per process; PostgreSQL also takes AUDIT_LOCK


def _clean_details(details):
    """Only numbers, flags, None and short codes; a text field here would be a content leak."""
    clean = {}
    for key, value in (details or {}).items():
        if isinstance(value, (list, tuple)) and all(isinstance(v, int) and not isinstance(v, bool) for v in value):
            value = list(value)[:1000]
        elif not (value is None or isinstance(value, (int, bool))
                  or (isinstance(value, str) and len(value) <= MAX_TEXT)):
            raise ValueError(f'Audit-Detail {key!r} ist kein Kennwert.')
        clean[str(key)[:MAX_TEXT]] = value
    return json.dumps(clean, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def entry_hash(entry):
    canonical = json.dumps({field: entry[field] for field in FIELDS}, sort_keys=True,
                           separators=(',', ':'), ensure_ascii=True)
    return hashlib.sha256(canonical.encode('ascii')).hexdigest()


def record(store, event, account_id=None, target='', source='', details=None, now=None):
    """Append one entry; returns its id."""
    if event not in EVENTS:
        raise ValueError('Unbekanntes Audit-Ereignis.')
    entry = {'created_at': int(time.time() if now is None else now),
             'account_id': None if account_id is None else int(account_id), 'event': event,
             'target': str(target)[:MAX_TEXT], 'source': str(source)[:MAX_TEXT], 'details': _clean_details(details)}
    # Inside ``store.transaction()`` (services.py) this joins the caller's transaction. PostgreSQL then
    # holds AUDIT_LOCK until that commit, so ids stay gapless and unique. ponytail: on SQLite the
    # process lock ends before the commit; a writing transaction already holds the database write
    # lock there, so only two audit-only calls racing can collide, and that fails with a duplicate id
    # instead of a broken chain. SQLite is the test and desktop backend, the server runs on PostgreSQL.
    with _lock, store.connect() as db:
        if store.backend == 'postgresql':
            # An advisory lock needs no table right; the runtime role may only insert and read here.
            db.execute('SELECT pg_advisory_xact_lock(?)', (AUDIT_LOCK,))
        last = db.execute('SELECT id,hash FROM audit_log ORDER BY id DESC LIMIT 1').fetchone()
        entry['id'] = last['id'] + 1 if last else 1
        entry['previous_hash'] = last['hash'] if last else GENESIS
        db.execute('INSERT INTO audit_log(id,created_at,account_id,event,target,source,details,previous_hash,hash) '
                   'VALUES(?,?,?,?,?,?,?,?,?)', (*(entry[field] for field in FIELDS), entry_hash(entry)))
    return entry['id']


def entries(store, limit=100):
    with store.connect() as db:
        return [dict(row) for row in db.execute(
            f'SELECT {",".join(FIELDS)},hash FROM audit_log ORDER BY id DESC LIMIT ?', (max(1, min(limit, 10_000)),))]


def verify(store, expected=None):
    """Check the whole chain; ``expected`` is ``(count, head)`` from an earlier check.

    Returns ``{'ok', 'entries', 'head'}`` and, when broken, ``'id'`` of the first bad position and
    ``'reason'``: ``fehlt`` (gap), ``kette`` (previous hash does not match), ``verändert`` (content
    does not match its hash) or ``gekürzt`` (fewer entries or another head than ``expected``).
    """
    previous, count, at_known = GENESIS, 0, None
    known = int(expected[0]) if expected is not None else 0
    with store.connect() as db:
        rows = db.execute(f'SELECT {",".join(FIELDS)},hash FROM audit_log ORDER BY id')
        for row in rows:
            row = dict(row)
            count += 1
            if row['id'] != count:
                return {'ok': False, 'entries': count - 1, 'head': previous, 'id': count, 'reason': 'fehlt'}
            if row['previous_hash'] != previous:
                return {'ok': False, 'entries': count - 1, 'head': previous, 'id': count, 'reason': 'kette'}
            if entry_hash(row) != row['hash']:
                return {'ok': False, 'entries': count - 1, 'head': previous, 'id': count, 'reason': 'verändert'}
            previous = row['hash']
            if count == known:
                at_known = previous
    if expected is not None:
        if known > count or (known and at_known != expected[1]):
            return {'ok': False, 'entries': count, 'head': previous, 'id': min(known, count + 1), 'reason': 'gekürzt'}
    return {'ok': True, 'entries': count, 'head': previous}
