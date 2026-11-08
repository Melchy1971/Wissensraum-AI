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
GRAPH_OBJECTS = {'entity': 'entities', 'mention': 'entity_mentions', 'relation': 'graph_relations'}


def _sqlite(store):
    return getattr(store, 'backend', 'sqlite') != 'postgresql'


def _tables():
    from migrations import SQLITE_TABLE_VERSIONS
    return sorted(set(SQLITE_TABLE_VERSIONS) - DERIVED - EPHEMERAL)


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


def erase(store, ids):
    """Remove the documents and everything derived from them, in one transaction."""
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
    if sqlite:
        with store.connect() as db:
            db.execute("INSERT INTO search_index(search_index) VALUES('optimize')")
        with store.connect() as db:
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        import backups
        counts['sicherungen_mit_daten'] = len(backups.existing(backups.folder(store)))
    return counts
