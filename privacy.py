"""Data protection export and selective erasure (Release 2.4, step 9.6).

``export`` writes everything the application stores about the user into one ZIP file: each table as
JSON (``daten/<tabelle>.json``) and binary values such as originals as files (``dateien/…``). Text
sections and vectors are left out because they are derived from the documents. Access keys are not
in the database (they live in the system key store) and so are never part of the export.

``find`` lists the documents that contain a text; ``impact`` shows what erasing them affects and
``erase`` removes them in one transaction together with everything derived from them: sections,
vectors, versions, tags, relations, mentions in the knowledge graph, entities that are left without
any mention, change-log entries of removed graph objects, stored AI answers that cited the
documents, and import entries pointing to them. On SQLite the removed rows are overwritten, the full
text index is rebuilt without them and the write-ahead log is emptied. Existing backups still
contain the data; ``erase`` reports how many there are.
"""
import json
import os
import re
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

DERIVED = {'chunks', 'chunk_vectors'}
# Server sign-in state (step 10.3): short-lived, no user content, CSRF tokens must not leave the server.
EPHEMERAL = {'web_sessions', 'login_requests'}
# Encrypted mailbox credentials of the server (D-030): never part of an export, not even as ciphertext.
SECRETS = {'account_mailboxes'}
GRAPH_OBJECTS = {'entity': 'entities', 'mention': 'entity_mentions', 'relation': 'graph_relations'}


def _sqlite(store):
    return getattr(store, 'backend', 'sqlite') != 'postgresql'


def _tables():
    from migrations import SQLITE_TABLE_VERSIONS
    return sorted(set(SQLITE_TABLE_VERSIONS) - DERIVED - EPHEMERAL - SECRETS)


def _plain(value):
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    return value


def export(store, target, overwrite=False):
    """Write the complete export; the file appears complete or not at all. Returns counts."""
    from migrations import CURRENT_SCHEMA_VERSION
    target = Path(target)
    if target.exists() and not overwrite:
        raise ValueError('Die Zieldatei gibt es schon.')
    handle, temporary = tempfile.mkstemp(prefix='.datenexport-', suffix='.tmp', dir=target.parent)
    counts, files = {}, 0
    try:
        with os.fdopen(handle, 'wb') as file, zipfile.ZipFile(file, 'w', zipfile.ZIP_DEFLATED) as archive, \
                store.connect() as db:
            for table in _tables():
                rows = []
                for number, row in enumerate(db.execute(f'SELECT * FROM {table}'), 1):
                    item = {}
                    for column in row.keys():
                        value = row[column]
                        if isinstance(value, (bytes, bytearray, memoryview)):
                            key = row['id'] if 'id' in row.keys() else number
                            name = f'dateien/{table}/{key}-{column}.bin'
                            archive.writestr(name, bytes(value))
                            files += 1
                            value = {'datei': name, 'bytes': len(bytes(value))}
                        item[column] = _plain(value)
                    rows.append(item)
                counts[table] = len(rows)
                archive.writestr(f'daten/{table}.json', json.dumps(rows, ensure_ascii=False, indent=1))
            archive.writestr('manifest.json', json.dumps({
                'format': 'wissensraum-datenexport', 'format_version': 1, 'schema_version': CURRENT_SCHEMA_VERSION,
                'exported_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'tabellen': counts,
                'hinweis': 'Abschnitte und Vektoren fehlen, weil sie aus den Dokumenten berechnet werden. '
                           'Zugangsschlüssel liegen im Schlüsselspeicher des Betriebssystems, nicht in der Datenbank.'},
                ensure_ascii=False, indent=2))
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return {'tabellen': counts, 'dateien': files}


def find(store, text, limit=500):
    """Documents whose title, file name or text contains ``text`` (case-insensitive)."""
    text = ' '.join(str(text).split())
    if len(text) < 2:
        raise ValueError('Mindestens zwei Zeichen suchen.')
    pattern = '%' + re.sub(r'([\\%_])', r'\\\1', text.lower()) + '%'
    with store.connect() as db:
        return [dict(row) for row in db.execute(
            "SELECT id,title,category,created_at FROM documents WHERE lower(title) LIKE ? ESCAPE '\\' "
            "OR lower(COALESCE(filename,'')) LIKE ? ESCAPE '\\' OR lower(content) LIKE ? ESCAPE '\\' "
            'ORDER BY id LIMIT ?', (pattern, pattern, pattern, limit))]


def _runs_citing(db, ids):
    from core import run_sources
    found = []
    for row in db.execute('SELECT id,sources_json FROM ai_runs'):
        try:
            sources, _ = run_sources(row['sources_json'])
        except (ValueError, TypeError, AttributeError):
            continue
        if any(isinstance(s, dict) and s.get('document_id') in ids for s in sources):
            found.append(row['id'])
    return found


def _ids_in(db, sql, ids):
    marks = ','.join('?' * len(ids))
    return [row['id'] for row in db.execute(sql.format(marks=marks), tuple(ids))]


def impact(store, ids):
    """What ``erase`` would remove besides the documents themselves."""
    ids = sorted({int(i) for i in ids})
    if not ids:
        return {'dokumente': 0, 'ki_verlauf': 0, 'erwaehnungen': 0, 'relationen': 0, 'importeintraege': 0}
    with store.connect() as db:
        return {'dokumente': len(_ids_in(db, 'SELECT id FROM documents WHERE id IN ({marks})', ids)),
                'ki_verlauf': len(_runs_citing(db, set(ids))),
                'erwaehnungen': len(_ids_in(db, 'SELECT id FROM entity_mentions WHERE document_id IN ({marks})', ids)),
                'relationen': len(_ids_in(db, 'SELECT id FROM graph_relations WHERE document_id IN ({marks})', ids)),
                'importeintraege': len(_ids_in(db, 'SELECT id FROM import_items WHERE document_id IN ({marks})', ids))}


def erase(store, ids, maintenance=True):
    """Remove the documents and everything derived from them, in one transaction.

    ``maintenance`` False leaves out the SQLite clean-up after the commit (index rebuild, empty WAL), for a
    caller that erases inside its own transaction and runs ``maintain`` afterwards.
    """
    from core import _bump_vector_generation
    ids = sorted({int(i) for i in ids})
    if not ids:
        raise ValueError('Keine Dokumente ausgewählt.')
    sqlite = _sqlite(store)
    with store.connect() as db:
        if sqlite:
            db.execute('PRAGMA secure_delete=ON')
        found = _ids_in(db, 'SELECT id FROM documents WHERE id IN ({marks})', ids)
        if len(found) != len(ids):
            raise ValueError('Dokument wurde nicht gefunden.')
        runs = _runs_citing(db, set(ids))
        counts = {'erwaehnungen': len(_ids_in(db, 'SELECT id FROM entity_mentions WHERE document_id IN ({marks})', ids)),
                  'relationen': len(_ids_in(db, 'SELECT id FROM graph_relations WHERE document_id IN ({marks})', ids))}
        marks = ','.join('?' * len(ids))
        counts['importeintraege'] = db.execute(f'DELETE FROM import_items WHERE document_id IN ({marks})',
                                               tuple(ids)).rowcount
        db.executemany('DELETE FROM ai_runs WHERE id=?', [(i,) for i in runs])
        db.execute(f'DELETE FROM chunks WHERE document_id IN ({marks})', tuple(ids))  # FTS trigger needs the document
        db.execute(f'DELETE FROM documents WHERE id IN ({marks})', tuple(ids))
        orphans = [row['id'] for row in db.execute(
            'SELECT e.id FROM entities e WHERE NOT EXISTS (SELECT 1 FROM entity_mentions m WHERE m.entity_id=e.id)')]
        db.executemany('DELETE FROM entities WHERE id=?', [(i,) for i in orphans])
        stale = []
        for kind, table in GRAPH_OBJECTS.items():
            stale += [row['id'] for row in db.execute(
                f'SELECT c.id FROM graph_changes c WHERE c.object_type=? '
                f'AND NOT EXISTS (SELECT 1 FROM {table} o WHERE o.id=c.object_id)', (kind,))]
        db.executemany('DELETE FROM graph_changes WHERE id=?', [(i,) for i in stale])
        _bump_vector_generation(db)
        counts.update(dokumente=len(ids), ki_verlauf=len(runs), entitaeten=len(orphans), protokoll=len(stale))
    if sqlite and maintenance:
        counts.update(maintain(store))
    return counts


def maintain(store):
    """SQLite after an erasure: rebuild the full text index, empty the WAL, count backups that still hold data."""
    if not _sqlite(store):
        return {}
    with store.connect() as db:
        db.execute("INSERT INTO search_index(search_index) VALUES('optimize')")
    with store.connect() as db:
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    import backups
    return {'sicherungen_mit_daten': len(backups.existing(backups.folder(store)))}


# --- server (parity roadmap phase 5, decision 11.11.2026) -------------------------------------------
def account_report(store, account):
    """What the server stores about one account, as it may see it: the data protection disclosure.

    Titles only of documents the account still sees; AI answers that cite documents it no longer sees
    are withheld as on its own history page (S-10). Other accounts appear by id only.
    """
    from import_jobs import ImportJobs
    scoped = store.as_viewer(account)
    visible = {d['id']: d['title'] for d in scoped.list(all_versions=True)}
    collections = {c['id']: c['name'] for c in scoped.collections()}
    with store.connect() as db:
        grants = [dict(r) for r in db.execute('SELECT document_id,collection_id,access,created_at FROM grants '
                                              'WHERE account_id=? ORDER BY id', (account['id'],))]
        favorites = [r['document_id'] for r in db.execute(
            'SELECT document_id FROM account_favorites WHERE account_id=? ORDER BY document_id', (account['id'],))]
        sessions = int(db.execute('SELECT count(*) AS n FROM web_sessions WHERE account_id=?',
                                  (account['id'],)).fetchone()['n'])
        trail = [dict(r) for r in db.execute('SELECT id,created_at,event,target,source,details FROM audit_log '
                                             'WHERE account_id=? ORDER BY id', (account['id'],))]
    for grant in grants:
        grant['titel'] = (visible.get(grant['document_id']) if grant['document_id'] is not None
                          else collections.get(grant['collection_id']))
    history = [{'zeit': _plain(run['created_at']), 'frage': run['question'],
                'antwort': None if run['withheld'] else run['answer'], 'ausgeblendet': run['withheld'],
                'quellen': [s.get('title') for s in run['sources']]} for run in scoped.visible_history()]
    return {'format': 'wissensraum-auskunft', 'format_version': 1,
            'erstellt': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'konto': {key: _plain(account.get(key)) for key in
                      ('id', 'tenant_id', 'object_id', 'display_name', 'role', 'disabled', 'created_at')},
            'freigaben': [{k: _plain(v) for k, v in g.items()} for g in grants],
            'favoriten': [{'dokument': i, 'titel': visible.get(i)} for i in favorites],
            'gespeicherte_suchen': scoped.saved_searches(),
            'ki_verlauf': history,
            'importauftraege': [{k: _plain(v) for k, v in job.items()}
                                for job in ImportJobs(store).upload_jobs(account['id'], limit=1000)],
            'aktive_sitzungen': sessions,
            'audit': [{k: _plain(v) for k, v in entry.items()} for entry in trail],
            'hinweis': 'Dokumentinhalte stehen nicht in der Auskunft. Titel erscheinen nur für Dokumente, '
                       'die das Konto heute sehen darf.'}


EXPORT_SETTING = 'privacy.export'
EXPORT_KEEP = 24 * 3600  # seconds a finished installation export stays on the server
EXPORT_STALE = 6 * 3600  # a running export older than this lost its worker (restart) and counts as failed


def export_state(store):
    """State of the installation export job: {} or status, account, times, file, size, error."""
    with store.connect() as db:
        row = db.execute('SELECT value FROM settings WHERE name=?', (EXPORT_SETTING,)).fetchone()
    try:
        value = json.loads(row['value']) if row else {}
    except ValueError:
        value = {}
    return value if isinstance(value, dict) else {}


def _swap_state(db, old, new):
    """Compare and set: only one caller wins a change of the job state."""
    if old is None:
        if db.execute('SELECT 1 FROM settings WHERE name=?', (EXPORT_SETTING,)).fetchone():
            return False
        db.execute('INSERT INTO settings(name,value) VALUES(?,?)', (EXPORT_SETTING, json.dumps(new)))
        return True
    if new is None:
        return db.execute('DELETE FROM settings WHERE name=? AND value=?', (EXPORT_SETTING, json.dumps(old))).rowcount == 1
    return db.execute('UPDATE settings SET value=? WHERE name=? AND value=?',
                      (json.dumps(new), EXPORT_SETTING, json.dumps(old))).rowcount == 1


def request_export(store, account_id, now):
    """Ask the worker for an installation export; refused while one is open or still kept."""
    current = export_state(store)
    if current:
        raise ValueError('Es gibt schon einen Export. Erst herunterladen oder löschen.')
    with store.connect() as db:
        if not _swap_state(db, None, {'status': 'pending', 'account_id': account_id, 'requested': int(now)}):
            raise ValueError('Es gibt schon einen Export. Erst herunterladen oder löschen.')


def run_export(store, folder, now):
    """Worker: build a requested export in ``folder``, remove one kept longer than a day. Returns the state."""
    current = export_state(store)
    folder = Path(folder)
    if current.get('status') == 'done' and now - current.get('finished', now) >= EXPORT_KEEP:
        discard_export(store, folder)
        return {}
    if current.get('status') == 'running' and now - current.get('started', now) >= EXPORT_STALE:
        with store.connect() as db:
            _swap_state(db, current, {**current, 'status': 'failed', 'error': 'abgebrochen', 'finished': int(now)})
        return export_state(store)
    if current.get('status') != 'pending':
        return current
    running = {**current, 'status': 'running', 'started': int(now)}
    with store.connect() as db:
        if not _swap_state(db, current, running):
            return export_state(store)
    name = f'datenexport-{datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%d-%H%M%S")}.zip'
    try:
        folder.mkdir(parents=True, exist_ok=True)
        export(store, folder / name, overwrite=True)
        finished = {**running, 'status': 'done', 'file': name, 'bytes': (folder / name).stat().st_size,
                    'finished': int(now)}
    except Exception as error:  # the state shows the class, never a path or content
        (folder / name).unlink(missing_ok=True)
        finished = {**running, 'status': 'failed', 'error': type(error).__name__, 'finished': int(now)}
    with store.connect() as db:
        _swap_state(db, running, finished)
    return finished


def export_file(store, folder):
    """Path of the finished export, or None."""
    current = export_state(store)
    if current.get('status') != 'done' or not re.fullmatch(r'datenexport-\d{8}-\d{6}\.zip', current.get('file', '')):
        return None
    path = Path(folder) / current['file']
    return path if path.is_file() else None


def discard_export(store, folder):
    """Delete the file of a finished or failed export and forget the job; an open job stays."""
    current = export_state(store)
    if current.get('status') in ('pending', 'running'):
        raise ValueError('Der Export läuft noch.')
    path = export_file(store, folder)
    if path is not None:
        path.unlink(missing_ok=True)
    if current:
        with store.connect() as db:
            _swap_state(db, current, None)
