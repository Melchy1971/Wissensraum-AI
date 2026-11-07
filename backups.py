"""Integrity check, daily backup and restore drill (Release 2.4, step 9.4).

Targets of the masterplan: recovery point at most 24 hours (RPO), recovery time at most 2 hours
(RTO), a successful restore drill every quarter. For SQLite the application keeps verified daily
copies in its own folder (default ``backups`` beside the database) and can restore the newest one
into a temporary file as a drill, with the time it took. PostgreSQL is backed up with pg_dump
outside the application; here only its integrity is checked.

Results contain counts, times, file names of the backups and document ids, never content.
"""
import hashlib
import json
import re
import shutil
import sqlite3
import tempfile
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

SETTING = 'backup'
DRILL_SETTING = 'backup.drill'
NAME = re.compile(r'^wissen-(\d{8}-\d{6})\.db$')
RPO = timedelta(hours=24)
RTO_SECONDS = 2 * 3600
DRILL_EVERY = timedelta(days=90)
DEFAULTS = {'auto': True, 'ordner': '', 'behalten': 7}
MAX_KEEP = 365


def _sqlite(store):
    return getattr(store, 'backend', 'sqlite') != 'postgresql'


def _read(store, name):
    with store.connect() as db:
        row = db.execute('SELECT value FROM settings WHERE name=?', (name,)).fetchone()
    try:
        value = json.loads(row['value']) if row else {}
    except ValueError:
        value = {}
    return value if isinstance(value, dict) else {}


def _write(store, name, value):
    with store.connect() as db:
        db.execute('DELETE FROM settings WHERE name=?', (name,))
        db.execute('INSERT INTO settings(name,value) VALUES(?,?)', (name, json.dumps(value)))


def settings(store):
    stored = _read(store, SETTING)
    result = dict(DEFAULTS)
    if isinstance(stored.get('auto'), bool):
        result['auto'] = stored['auto']
    if isinstance(stored.get('ordner'), str):
        result['ordner'] = stored['ordner']
    if isinstance(stored.get('behalten'), int) and 1 <= stored['behalten'] <= MAX_KEEP:
        result['behalten'] = stored['behalten']
    return result


def set_settings(store, auto=None, folder=None, keep=None):
    current = settings(store)
    if auto is not None:
        current['auto'] = bool(auto)
    if folder is not None:
        current['ordner'] = str(Path(folder).expanduser().resolve()) if str(folder).strip() else ''
    if keep is not None:
        keep = int(keep)
        if not 1 <= keep <= MAX_KEEP:
            raise ValueError(f'Zwischen 1 und {MAX_KEEP} Sicherungen behalten.')
        current['behalten'] = keep
    _write(store, SETTING, current)
    return current


def folder(store):
    configured = settings(store)['ordner']
    return Path(configured) if configured else Path(store.path).parent / 'backups'


def existing(target):
    """Backups in ``target``, newest first, as (path, UTC time from the name)."""
    found = []
    if Path(target).is_dir():
        for path in Path(target).iterdir():
            match = NAME.match(path.name)
            if match and path.is_file():
                found.append((path, datetime.strptime(match.group(1), '%Y%m%d-%H%M%S').replace(tzinfo=timezone.utc)))
    return sorted(found, key=lambda item: item[1], reverse=True)


def _hash_mismatches(db):
    # ponytail: reads every document once; fine for a desktop store, a sampled check is the upgrade.
    return [row['id'] for row in db.execute('SELECT id,content,sha256 FROM documents ORDER BY id')
            if hashlib.sha256(row['content'].encode()).hexdigest() != row['sha256']]


def _check_sqlite_file(path):
    """(problems, document count) of a SQLite file; nothing is changed (the schema check rolls back)."""
    try:
        return _check_sqlite_contents(path)
    except sqlite3.DatabaseError:
        return ['Datei ist keine lesbare SQLite-Datenbank oder beschädigt'], 0


def _check_sqlite_contents(path):
    from migrations import validate_sqlite, _sqlite_version
    problems = []
    with closing(sqlite3.connect(path, timeout=20)) as db:
        db.row_factory = sqlite3.Row
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            return ['Dateistruktur beschädigt'], 0
        if db.execute('PRAGMA foreign_key_check').fetchone():
            problems.append('ungültige Fremdschlüssel')
        try:
            validate_sqlite(db, _sqlite_version(db))
        except (ValueError, sqlite3.DatabaseError):
            problems.append('Schema weicht ab')
        db.rollback()
        mismatches = _hash_mismatches(db)
        count = int(db.execute('SELECT count(*) FROM documents').fetchone()[0])
    if mismatches:
        problems.append('Prüfsumme weicht ab bei Dokument ' + ', '.join(map(str, mismatches[:20])))
    return problems, count


def integrity(store):
    """Check the active database: structure, schema and document checksums."""
    start = time.monotonic()
    if _sqlite(store):
        problems, count = _check_sqlite_file(store.path)
    else:
        from migrations import CURRENT_SCHEMA_VERSION
        with store.connect() as db:
            problems = []
            version = db.execute('SELECT max(version) AS v FROM schema_migrations').fetchone()['v']
            if version != CURRENT_SCHEMA_VERSION:
                problems.append('Schemaversion weicht ab')
            mismatches = _hash_mismatches(db)
            if mismatches:
                problems.append('Prüfsumme weicht ab bei Dokument ' + ', '.join(map(str, mismatches[:20])))
            count = int(db.execute('SELECT count(*) AS n FROM documents').fetchone()['n'])
    return {'ok': not problems, 'probleme': problems, 'dokumente': count,
            'sekunden': round(time.monotonic() - start, 2)}


def create(store, now=None):
    """Write a verified copy of the SQLite database and keep only the newest ``behalten`` copies."""
    if not _sqlite(store):
        raise ValueError('PostgreSQL wird mit pg_dump auf dem Server gesichert, nicht in der Anwendung.')
    now = now or datetime.now(timezone.utc)
    target = folder(store)
    target.mkdir(parents=True, exist_ok=True)
    size = sum(Path(str(store.path) + suffix).stat().st_size for suffix in ('', '-wal')
               if Path(str(store.path) + suffix).exists())
    if shutil.disk_usage(target).free < 2 * size:
        raise ValueError('Zu wenig freier Speicher im Sicherungsordner (mindestens die doppelte Datenbankgröße).')
    final = target / f'wissen-{now.strftime("%Y%m%d-%H%M%S")}.db'
    staging = target / (final.name + '.tmp')
    try:
        store.backup(staging)
        problems, count = _check_sqlite_file(staging)
        if problems:
            raise ValueError('Sicherung ist fehlerhaft: ' + '; '.join(problems))
        staging.replace(final)
    finally:
        for suffix in ('', '-wal', '-shm', '-journal'):
            Path(str(staging) + suffix).unlink(missing_ok=True)
    for old, _ in existing(target)[settings(store)['behalten']:]:
        old.unlink(missing_ok=True)
    return {'datei': final.name, 'dokumente': count, 'bytes': final.stat().st_size}


def due(store, now=None):
    """Whether the newest backup is older than the RPO of 24 hours (or missing)."""
    now = now or datetime.now(timezone.utc)
    found = existing(folder(store))
    return not found or now - found[0][1] >= RPO


def auto(store, now=None):
    """Daily backup when it is switched on and due; None otherwise."""
    if _sqlite(store) and settings(store)['auto'] and due(store, now):
        return create(store, now)
    return None


def drill(store, now=None):
    """Restore the newest backup into a temporary file, check it and record the time it took."""
    from migrations import restore_sqlite
    if not _sqlite(store):
        raise ValueError('Die Wiederherstellungsübung für PostgreSQL läuft mit pg_restore auf dem Server.')
    now = now or datetime.now(timezone.utc)
    found = existing(folder(store))
    if not found:
        raise ValueError('Keine Sicherung vorhanden. Zuerst eine Sicherung erstellen.')
    source = found[0][0]
    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='wissensraum-uebung-') as scratch:
        restored = Path(scratch) / 'wissen.db'
        restore_sqlite(restored, source)
        problems, count = _check_sqlite_file(restored)
    seconds = round(time.monotonic() - start, 2)
    result = {'zeit': now.strftime('%Y-%m-%d %H:%M:%S'), 'sicherung': source.name, 'dokumente': count,
              'sekunden': seconds, 'ok': not problems and seconds <= RTO_SECONDS, 'probleme': problems}
    _write(store, DRILL_SETTING, result)
    return result


def status(store, now=None):
    """Backup age against the RPO, last drill against the RTO and the quarter."""
    now = now or datetime.now(timezone.utc)
    if not _sqlite(store):
        return {'backend': 'postgresql'}
    found = existing(folder(store))
    last = _read(store, DRILL_SETTING)
    try:
        drilled = datetime.strptime(last['zeit'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
    except (KeyError, TypeError, ValueError):
        drilled = None
    return {'backend': 'sqlite', 'ordner': str(folder(store)), 'automatisch': settings(store)['auto'],
            'anzahl': len(found), 'letzte': found[0][0].name if found else None,
            'alter_stunden': round((now - found[0][1]).total_seconds() / 3600, 1) if found else None,
            'rpo_ok': bool(found) and now - found[0][1] < RPO,
            'letzte_uebung': last if drilled else None,
            'uebung_faellig': drilled is None or now - drilled >= DRILL_EVERY}


def lines(state):
    """German text for ``status``."""
    if state.get('backend') == 'postgresql':
        return ['PostgreSQL: Sicherung und Wiederherstellungsübung mit pg_dump und pg_restore auf dem Server.']
    result = []
    if state['letzte']:
        hours = f'{state["alter_stunden"]:.1f}'.replace('.', ',')
        result.append(f'Letzte Sicherung vor {hours} Stunden ({state["anzahl"]} vorhanden)'
                      + ('' if state['rpo_ok'] else ' · älter als 24 Stunden'))
    else:
        result.append('Noch keine Sicherung im Sicherungsordner · Ziel höchstens 24 Stunden')
    result.append('Tägliche Sicherung: ' + ('an' if state['automatisch'] else 'aus'))
    drill_result = state['letzte_uebung']
    if drill_result:
        seconds = f'{drill_result["sekunden"]:.1f}'.replace('.', ',')
        result.append(f'Letzte Wiederherstellungsübung {drill_result["zeit"][:10]}: '
                      + ('bestanden' if drill_result['ok'] else 'nicht bestanden')
                      + f' in {seconds} Sekunden')
    if state['uebung_faellig']:
        result.append('Wiederherstellungsübung fällig (vierteljährlich)')
    return result
