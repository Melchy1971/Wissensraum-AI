"""Operations overview (Release 2.4, step 9.1): what is stored, what failed, how the AI is used.

Only counts, sizes, status values and error codes are reported; no document text, question,
answer, path or file name, so the overview can be shown or exported without exposing content.
Works on SQLite and PostgreSQL through ``store.connect()``.
"""
import json
from datetime import datetime, timedelta, timezone

DEFAULT_DAYS = 30


def _utc(value):
    """created_at of either backend as an aware UTC datetime."""
    if hasattr(value, 'astimezone'):
        return value.astimezone(timezone.utc)
    return datetime.strptime(str(value)[:19], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)


def _counts(db, sql, params=()):
    return {str(row['key']): int(row['n']) for row in db.execute(sql, params)}


def overview(store, days=DEFAULT_DAYS, now=None):
    """Counts for the last ``days`` days where time matters, totals otherwise."""
    days = max(1, min(int(days), 3650))
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    length = 'octet_length' if getattr(store, 'backend', 'sqlite') == 'postgresql' else 'length'
    with store.connect() as db:
        documents = {
            'anzahl': int(db.execute('SELECT count(*) AS n FROM documents').fetchone()['n']),
            'nach_status': _counts(db, 'SELECT status AS key,count(*) AS n FROM documents GROUP BY status'),
            'nach_quelle': _counts(db, "SELECT COALESCE(s.source_type,'unbekannt') AS key,count(*) AS n FROM documents d "
                                       'LEFT JOIN sources s ON s.document_id=d.id GROUP BY s.source_type'),
            'text_zeichen': int(db.execute('SELECT COALESCE(sum(length(content)),0) AS n FROM documents').fetchone()['n']),
            'originale_bytes': int(db.execute(f'SELECT COALESCE(sum({length}(original)),0) AS n FROM documents')
                                   .fetchone()['n']),
            'wiedervorlage_faellig': int(db.execute(
                "SELECT count(*) AS n FROM documents WHERE review_date IS NOT NULL AND review_date<>'' "
                'AND review_date<=?', (now.date().isoformat(),)).fetchone()['n']),
        }
        chunks = int(db.execute('SELECT count(*) AS n FROM chunks').fetchone()['n'])
        vectors = _counts(db, 'SELECT model AS key,count(*) AS n FROM chunk_vectors GROUP BY model')
        unvectored = int(db.execute('SELECT count(*) AS n FROM chunks c LEFT JOIN chunk_vectors v ON v.chunk_id=c.id '
                                    'WHERE v.chunk_id IS NULL').fetchone()['n'])
        jobs = _counts(db, 'SELECT status AS key,count(*) AS n FROM import_jobs GROUP BY status')
        items = _counts(db, 'SELECT status AS key,count(*) AS n FROM import_items GROUP BY status')
        recent_errors = {}
        for row in db.execute('SELECT reason,created_at FROM import_item_errors'):
            if _utc(row['created_at']) >= since:
                recent_errors[row['reason']] = recent_errors.get(row['reason'], 0) + 1
        runs = {'anfragen': 0, 'nach_art': {}, 'nach_modell': {}, 'nach_status': {}, 'mit_graphkontext': 0}
        for row in db.execute('SELECT kind,model,sources_json,created_at FROM ai_runs'):
            if _utc(row['created_at']) < since:
                continue
            runs['anfragen'] += 1
            for key, value in (('nach_art', row['kind']), ('nach_modell', row['model'])):
                runs[key][value] = runs[key].get(value, 0) + 1
            try:
                meta = json.loads(row['sources_json'])
            except ValueError:
                meta = {}
            status = meta.get('answer_status', 'ohne Status') if isinstance(meta, dict) else 'ohne Status'
            runs['nach_status'][status] = runs['nach_status'].get(status, 0) + 1
            runs['mit_graphkontext'] += bool(isinstance(meta, dict) and meta.get('graph_relations'))
        graph = {kind: _counts(db, f'SELECT status AS key,count(*) AS n FROM {table} WHERE deleted_at IS NULL GROUP BY status')
                 for kind, table in (('entitaeten', 'entities'), ('relationen', 'graph_relations'))}
    import backups, budgets
    safety = backups.status(store, now)
    safety.pop('ordner', None)  # no paths in the overview
    return {'zeitraum_tage': days, 'dokumente': documents,
            'abschnitte': {'anzahl': chunks, 'vektoren_je_modell': vectors, 'ohne_vektor': unvectored},
            'import': {'auftraege_nach_status': jobs, 'eintraege_nach_status': items,
                       'fehler_im_zeitraum_nach_grund': dict(sorted(recent_errors.items()))},
            'ki': runs, 'wissensgraph': graph, 'ki_budget': budgets.statuses(store, now),
            'sicherung': safety}


def report(data):
    """Plain German text for the GUI and the CLI."""
    import backups, budgets
    def join(values):
        return ', '.join(f'{key} {value}' for key, value in sorted(values.items())) or 'keine'
    d, a, i, k, g = data['dokumente'], data['abschnitte'], data['import'], data['ki'], data['wissensgraph']
    return '\n'.join([
        f'Dokumente: {d["anzahl"]} ({join(d["nach_status"])}), Quellen: {join(d["nach_quelle"])}',
        f'Umfang: {d["text_zeichen"]:,} Zeichen Text'.replace(',', '.')
        + f', {d["originale_bytes"] / 1048576:.1f} MiB Originale'.replace('.', ','),
        f'Wiedervorlage fällig: {d["wiedervorlage_faellig"]}',
        f'Abschnitte: {a["anzahl"]}, Vektoren: {join(a["vektoren_je_modell"])}, ohne Vektor: {a["ohne_vektor"]}',
        f'Importaufträge: {join(i["auftraege_nach_status"])}, Einträge: {join(i["eintraege_nach_status"])}',
        f'Importfehler der letzten {data["zeitraum_tage"]} Tage: {join(i["fehler_im_zeitraum_nach_grund"])}',
        f'KI-Anfragen der letzten {data["zeitraum_tage"]} Tage: {k["anfragen"]} (Art: {join(k["nach_art"])}; '
        f'Modell: {join(k["nach_modell"])}; Ergebnis: {join(k["nach_status"])}; mit Graphkontext: {k["mit_graphkontext"]})',
        f'Wissensgraph: Entitäten {join(g["entitaeten"])}, Relationen {join(g["relationen"])}',
        'Sicherung:',
        *('  ' + line for line in (backups.lines(data['sicherung']) if 'sicherung' in data else [])),
        'KI-Budget im laufenden Monat:',
        *('  ' + line for line in budgets.lines(data.get('ki_budget', []))),
    ])
