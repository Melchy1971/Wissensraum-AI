"""AI graph suggestions on the server (parity roadmap phase 7): requested on a page, run by the worker.

One request per document, stored as setting ``graph.suggest.<id>`` with its state (pending, running, done,
failed), the requesting account and the counts of ``graph_suggest.suggest``. Server and worker change the
state only by compare-and-set, so a request is never run twice. The worker checks again before running
that the account may still change the document (decision 11.11.2026: write access to the evidence).
Only the local Ollama is used (D-023); suggestions stay ``proposed`` until someone confirms them.
"""
import json

PREFIX = 'graph.suggest.'
STALE = 6 * 3600  # a running request older than this lost its worker and counts as failed


def state(store, document_id):
    with store.connect() as db:
        row = db.execute('SELECT value FROM settings WHERE name=?', (f'{PREFIX}{int(document_id)}',)).fetchone()
    try:
        value = json.loads(row['value']) if row else {}
    except ValueError:
        value = {}
    return value if isinstance(value, dict) else {}


def _swap(db, document_id, old, new):
    name = f'{PREFIX}{int(document_id)}'
    if old is None:
        if db.execute('SELECT 1 FROM settings WHERE name=?', (name,)).fetchone():
            return False
        db.execute('INSERT INTO settings(name,value) VALUES(?,?)', (name, json.dumps(new)))
        return True
    return db.execute('UPDATE settings SET value=? WHERE name=? AND value=?',
                      (json.dumps(new), name, json.dumps(old))).rowcount == 1


def request(store, account_id, document_id, now):
    """Queue a run for the document; refused while one is open. A finished state is replaced."""
    current = state(store, document_id)
    if current.get('status') in ('pending', 'running'):
        raise ValueError('Für dieses Dokument laufen schon KI-Vorschläge.')
    new = {'status': 'pending', 'account_id': int(account_id), 'requested': int(now)}
    with store.connect() as db:
        if not _swap(db, document_id, current or None, new):
            raise ValueError('Für dieses Dokument laufen schon KI-Vorschläge.')


def pending(store):
    """Document ids with a pending or running request, oldest request first."""
    with store.connect() as db:
        rows = db.execute('SELECT name,value FROM settings WHERE name LIKE ?', (PREFIX + '%',)).fetchall()
    found = []
    for row in rows:
        try:
            value = json.loads(row['value'])
            found.append((value.get('requested', 0), int(row['name'][len(PREFIX):]), value.get('status')))
        except (ValueError, AttributeError):
            continue
    return [doc for _, doc, status in sorted(found) if status in ('pending', 'running')]


def run_next(store, now, model, suggest=None):
    """Worker: run the oldest request; returns (document_id, state) or None when nothing waits."""
    import accounts
    if suggest is None:
        from graph_suggest import suggest
    for document_id in pending(store):
        current = state(store, document_id)
        if current.get('status') == 'running':
            if now - current.get('started', now) < STALE:
                continue  # another round of this worker is on it (restart: resumed after STALE)
        running = {**current, 'status': 'running', 'started': int(now)}
        with store.connect() as db:
            if not _swap(db, document_id, current, running):
                continue
        try:
            account = accounts.account(store, running['account_id'])
            if not accounts.can_write(store, account, document_id):
                raise PermissionError('rights')
            counts = suggest(store, document_id, model)
            finished = {**running, 'status': 'done', 'counts': counts, 'finished': int(now)}
        except Exception as error:  # the state shows the class, never content
            finished = {**running, 'status': 'failed', 'error': type(error).__name__, 'finished': int(now)}
        with store.connect() as db:
            _swap(db, document_id, running, finished)
        return document_id, finished
    return None
