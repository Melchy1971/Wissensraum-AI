"""Retention rules for AI history and deleted content (Release 2.4, step 9.3).

A rule keeps entries of one category for a number of days; 0 keeps them for good, which is the
default, so nothing is removed until the user sets a rule. Rules are applied only on request,
after a preview of the counts. Categories:

- ``ki_verlauf``: stored AI questions and answers (``ai_runs``)
- ``ki_nutzung``: token counts of the budgets (``ai_usage``); the current month always stays
- ``importfehler``: error entries of import jobs (``import_item_errors``)
- ``graph_geloescht``: entities, mentions and relations of the knowledge graph that were marked as
  deleted, and the change log (``graph_changes``); older changes can no longer be undone

On SQLite the removed rows are overwritten (``secure_delete``) and the write-ahead log is emptied,
so their text does not stay readable in the file. PostgreSQL frees the space with its autovacuum.
"""
import json
from datetime import datetime, timedelta, timezone

SETTING = 'retention'
MAX_DAYS = 36500
CATEGORIES = {
    'ki_verlauf': ('KI-Verlauf (Fragen und Antworten)', (('ai_runs', 'created_at'),)),
    'ki_nutzung': ('KI-Nutzungszählung für Budgets', (('ai_usage', 'created_at'),)),
    'importfehler': ('Fehlereinträge von Importaufträgen', (('import_item_errors', 'created_at'),)),
    'graph_geloescht': ('Gelöschte Objekte und Änderungsprotokoll des Wissensgraphen',
                        (('graph_relations', 'deleted_at'), ('entity_mentions', 'deleted_at'),
                         ('entities', 'deleted_at'), ('graph_changes', 'created_at'))),
}


def _utc(value):
    if hasattr(value, 'astimezone'):
        return value.astimezone(timezone.utc)
    return datetime.strptime(str(value)[:19], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)


def rules(store):
    """Days per category; 0 = keep for good."""
    with store.connect() as db:
        row = db.execute('SELECT value FROM settings WHERE name=?', (SETTING,)).fetchone()
    try:
        stored = json.loads(row['value']) if row else {}
    except ValueError:
        stored = {}
    stored = stored if isinstance(stored, dict) else {}
    return {key: stored.get(key, 0) if isinstance(stored.get(key, 0), int) else 0 for key in CATEGORIES}


def set_rule(store, category, days):
    if category not in CATEGORIES:
        raise ValueError('Unbekannte Kategorie.')
    days = int(days)
    if not 0 <= days <= MAX_DAYS:
        raise ValueError(f'Aufbewahrung zwischen 0 (unbegrenzt) und {MAX_DAYS:,} Tagen.'.replace(',', '.'))
    current = rules(store)
    current[category] = days
    with store.connect() as db:
        db.execute('DELETE FROM settings WHERE name=?', (SETTING,))
        db.execute('INSERT INTO settings(name,value) VALUES(?,?)', (SETTING, json.dumps(current)))
    return current


def _expired(db, category, days, now):
    """Ids per table that the rule removes."""
    cutoff = now - timedelta(days=days)
    if category == 'ki_nutzung':  # the budget counts the current month
        cutoff = min(cutoff, now.replace(day=1, hour=0, minute=0, second=0, microsecond=0))
    found = {}
    for table, column in CATEGORIES[category][1]:
        # ponytail: reads id and time of every candidate row and compares in Python, the same on both
        # backends; enough for a desktop store, a WHERE on an indexed column is the upgrade.
        found[table] = [row['id'] for row in db.execute(
            f'SELECT id,{column} AS stamp FROM {table} WHERE {column} IS NOT NULL')
            if _utc(row['stamp']) < cutoff]
    return found


def preview(store, now=None):
    """Rows each active rule would remove now: {category: {table: count}}."""
    now = now or datetime.now(timezone.utc)
    active = {key: days for key, days in rules(store).items() if days}
    with store.connect() as db:
        return {key: {table: len(ids) for table, ids in _expired(db, key, days, now).items()}
                for key, days in active.items()}


def apply(store, now=None, maintenance=True):
    """Remove what the rules allow, in one transaction; returns the counts like ``preview``.

    ``maintenance`` False leaves out emptying the SQLite WAL, for a caller inside its own transaction.
    """
    now = now or datetime.now(timezone.utc)
    active = {key: days for key, days in rules(store).items() if days}
    sqlite = getattr(store, 'backend', 'sqlite') != 'postgresql'
    removed = {}
    with store.connect() as db:
        if sqlite:
            db.execute('PRAGMA secure_delete=ON')
        for key, days in active.items():
            removed[key] = {}
            for table, ids in _expired(db, key, days, now).items():
                if ids:
                    db.executemany(f'DELETE FROM {table} WHERE id=?', [(i,) for i in ids])
                removed[key][table] = len(ids)
    if sqlite and maintenance and any(n for counts in removed.values() for n in counts.values()):
        with store.connect() as db:
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    return removed


def report(counts):
    """German lines for ``preview`` or ``apply``."""
    if not counts:
        return 'Keine Aufbewahrungsregel aktiv; es wird nichts entfernt.'
    return '\n'.join(f'{CATEGORIES[key][0]}: ' + ', '.join(f'{table} {n}' for table, n in tables.items())
                     for key, tables in counts.items())
