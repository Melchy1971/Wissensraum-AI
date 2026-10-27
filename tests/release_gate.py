"""Reproduzierbarer Lauf der manuellen Release-Gate-Prüfungen aus masterplan.md (Phase 4 und 5).

Aufruf aus dem Projektordner:

    python tests/release_gate.py
    python tests/release_gate.py --sqlite "C:\\Pfad\\zur\\Kopie\\wissen.db"

Alle Prüfungen laufen an Wegwerfkopien in einem temporären Ordner. data/wissen.db wird nie
geöffnet. Eine mit --sqlite angegebene Datenbank wird nur lesend kopiert. PostgreSQL-Prüfungen
laufen nur mit PGTEST_DATABASE=*_test und PGTEST_ALLOW_DESTRUCTIVE=1 (wie tests/test_postgres.py).
Das Ergebnis steht in release-gate-<Zeitstempel>.md; Exit-Code 1 bei einem Fehlschlag.
"""
from __future__ import annotations

import argparse
import multiprocessing
import os
import platform
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]

import migrations  # noqa: E402
from core import SOURCE_TYPES, Store  # noqa: E402
from migrations import CURRENT_SCHEMA_VERSION, migrate_sqlite, restore_sqlite  # noqa: E402
from test_migrations import (SOURCE_FIXTURE, SQLiteMigrationTests,  # noqa: E402
                             fixture_sources, insert_source_fixture)

create_version_fixture = SQLiteMigrationTests.create_version_fixture
RESULTS = []


def record(check_id, title):
    """Run a check; record PASS, SKIP (via Skip) or FAIL with the exception text."""
    def decorator(function):
        def run(*args):
            started = time.monotonic()
            try:
                detail = function(*args) or ''
                status = 'PASS'
            except Skip as skip:
                status, detail = 'SKIP', str(skip)
            except Exception as error:
                status, detail = 'FAIL', f'{type(error).__name__}: {error}'
            RESULTS.append((check_id, title, status, detail, time.monotonic() - started))
            print(f'[{status}] {check_id} {title}' + (f': {detail}' if detail else ''))
        return run
    return decorator


class Skip(Exception):
    pass


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def has_table(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()


def snapshot(path):
    """Version and user data that a migration or restore must preserve."""
    with closing(sqlite3.connect(path)) as db:
        state = {
            'version': db.execute('PRAGMA user_version').fetchone()[0],
            'documents': db.execute('SELECT id, sha256, original FROM documents ORDER BY id').fetchall(),
            'chunks': db.execute('SELECT document_id, ordinal, content FROM chunks ORDER BY id').fetchall(),
        }
        for table, query in (('settings', 'SELECT name, value FROM settings ORDER BY name'),
                             ('email_accounts', 'SELECT id, label, email FROM email_accounts ORDER BY id'),
                             ('email_imports', 'SELECT account_id, remote_id, document_id '
                                               'FROM email_imports ORDER BY account_id, remote_id')):
            state[table] = db.execute(query).fetchall() if has_table(db, table) else None
    return state


def check_sources(path):
    with closing(sqlite3.connect(path)) as db:
        documents = db.execute('SELECT count(*) FROM documents').fetchone()[0]
        rows = db.execute('SELECT source_type, count(*) FROM sources GROUP BY source_type').fetchall()
    counts = dict(rows)
    require(sum(counts.values()) == documents,
            f'{documents} Dokumente, aber {sum(counts.values())} Quellen')
    require(set(counts) <= SOURCE_TYPES, f'unbekannte Quelltypen {set(counts) - SOURCE_TYPES}')
    return counts


@record('M1', 'SQLite-Schemaversionen 1 bis 4 auf aktuelle Version migrieren')
def migrate_each_version(work):
    details = []
    for version in range(1, CURRENT_SCHEMA_VERSION):
        path = work / f'm1-v{version}' / 'wissen.db'
        path.parent.mkdir()
        create_version_fixture(path, version)
        if version == 4:
            with closing(sqlite3.connect(path)) as db:
                insert_source_fixture(db)
                db.commit()
        before = snapshot(path)
        Store(path)  # same code path as the desktop and CLI start
        after = snapshot(path)
        require(after['version'] == CURRENT_SCHEMA_VERSION, f'v{version}: Version {after["version"]}')
        for key in ('documents', 'chunks', 'settings', 'email_accounts', 'email_imports'):
            if before[key] is not None:
                require(after[key] == before[key], f'v{version}: {key} verändert')
        backup = path.with_name(path.name + f'.pre-migration-v{version}.bak')
        require(backup.is_file(), f'v{version}: Sicherung {backup.name} fehlt')
        with closing(sqlite3.connect(backup)) as db:
            require(db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok',
                    f'v{version}: Sicherung beschädigt')
            require(db.execute('PRAGMA user_version').fetchone()[0] == version,
                    f'v{version}: Sicherung hat falsche Version')
        require(snapshot(backup)['documents'] == before['documents'],
                f'v{version}: Sicherung enthält andere Dokumente')
        store = Store(path)
        require(store.search('Migrationsbeleg'), f'v{version}: Suchtreffer fehlt nach Migration')
        counts = check_sources(path)
        if version == 4:
            with closing(sqlite3.connect(path)) as db:
                sources, _ = fixture_sources(db)
            expected = {title: source for title, *_, source in SOURCE_FIXTURE}
            expected['Alt'] = ('file', '', 'alt.bin')
            require(sources == expected, f'v4: Quellen weichen ab: {sources}')
        details.append(f'v{version}: {len(after["documents"])} Dokumente, Quellen {counts}')
    return '; '.join(details)


@record('M2', 'Abgebrochene Migration rollt vollständig zurück')
def aborted_migration(work):
    path = work / 'm2' / 'wissen.db'
    path.parent.mkdir()
    create_version_fixture(path, CURRENT_SCHEMA_VERSION - 1)
    before = snapshot(path)
    target, statements = migrations.SQLITE_MIGRATIONS[-1]
    broken = (*migrations.SQLITE_MIGRATIONS[:-1],
              (target, (statements[0], 'SELECT abbruch FROM absichtlich_fehlende_tabelle')))
    with patch.object(migrations, 'SQLITE_MIGRATIONS', broken):
        try:
            migrate_sqlite(path)
        except sqlite3.Error:
            pass
        else:
            raise AssertionError('Testmigration ist nicht gescheitert')
    require(snapshot(path) == before, 'Daten oder Version nach Abbruch verändert')
    with closing(sqlite3.connect(path)) as db:
        require(not has_table(db, 'sources'), 'Teilschema (Tabelle sources) nach Abbruch vorhanden')
    return f'Version bleibt {before["version"]}, kein Teilschema'


def _migrate_in_process(path, start, result):
    start.wait(30)
    try:
        migrate_sqlite(path)
        result.put('ok')
    except Exception as error:
        result.put(f'{type(error).__name__}: {error}')


@record('M3', 'Zwei gleichzeitige Starts migrieren genau einmal')
def concurrent_start(work):
    path = work / 'm3' / 'wissen.db'
    path.parent.mkdir()
    create_version_fixture(path, 3)
    before = snapshot(path)
    context = multiprocessing.get_context('spawn')
    start, result = context.Event(), context.Queue()
    processes = [context.Process(target=_migrate_in_process, args=(str(path), start, result))
                 for _ in range(2)]
    for process in processes:
        process.start()
    start.set()
    outcomes = [result.get(timeout=60) for _ in processes]
    for process in processes:
        process.join(30)
    require(outcomes == ['ok', 'ok'], f'Prozessergebnisse {outcomes}')
    after = snapshot(path)
    require(after['version'] == CURRENT_SCHEMA_VERSION, f'Version {after["version"]}')
    require(after['documents'] == before['documents'], 'Dokumente verändert')
    backups = sorted(p.name for p in path.parent.glob('wissen.db.pre-migration-*.bak'))
    require(backups == ['wissen.db.pre-migration-v3.bak'], f'Sicherungen {backups}')
    return 'beide Prozesse erfolgreich, genau eine Vor-Migrationssicherung'


@record('M4', 'Kopie echter Daten migrieren (--sqlite)')
def real_data_copy(work, source):
    if source is None:
        raise Skip('keine Datenbank mit --sqlite angegeben')
    source = Path(source).resolve()
    if source == (ROOT / 'data' / 'wissen.db').resolve():
        raise Skip('data/wissen.db wird nie direkt verwendet; bitte zuerst eine Kopie anlegen')
    require(source.is_file(), f'{source} nicht gefunden')
    copy = work / 'm4' / 'wissen.db'
    copy.parent.mkdir()
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as original, \
            closing(sqlite3.connect(copy)) as target:
        original.backup(target)
    before = snapshot(copy)
    Store(copy)
    after = snapshot(copy)
    require(after['documents'] == before['documents'], 'Dokument-Prüfsummen oder Originale verändert')
    for key in ('chunks', 'settings', 'email_accounts', 'email_imports'):
        if before[key] is not None:
            require(after[key] == before[key], f'{key} verändert')
    counts = check_sources(copy)
    return (f'Version {before["version"]} auf {after["version"]}, {len(after["documents"])} Dokumente '
            f'unverändert, Quellen {counts}')


@record('R1', 'SQLite-Wiederherstellung mit identischen Dokument-Hashes')
def sqlite_restore(work):
    path, backup = work / 'r1' / 'wissen.db', work / 'r1' / 'sicherung.db'
    path.parent.mkdir()
    store = Store(path)
    store.add('Vor Sicherung', 'Wiederherstellungsprobe eins')
    store.add('Datei', 'Wiederherstellungsprobe zwei', filename='a.md', original=b'\x00\xff')
    store.backup(backup)
    expected = snapshot(backup)
    store.add('Nach Sicherung', 'Wiederherstellungsprobe drei')
    result = restore_sqlite(path, backup)
    require(snapshot(path)['documents'] == expected['documents'], 'Dokument-Hashes weichen ab')
    require(Path(result['safety_backup']).is_file(), 'Sicherung des vorherigen Stands fehlt')
    require(len(snapshot(result['safety_backup'])['documents']) == 3, 'vorheriger Stand unvollständig')
    check_sources(path)
    return f'{result["documents"]} Dokumente wiederhergestellt, vorheriger Stand gesichert'


@record('P1', 'PostgreSQL: Migration, Reset und pg_dump/pg_restore gegen *_test')
def postgres_contract(_work):
    database = os.environ.get('PGTEST_DATABASE', '')
    if not (os.environ.get('PGTEST_PASSWORD') and os.environ.get('PGTEST_ALLOW_DESTRUCTIVE') == '1'
            and database.lower().endswith('_test')):
        raise Skip('PGTEST_PASSWORD, PGTEST_DATABASE=*_test und PGTEST_ALLOW_DESTRUCTIVE=1 nicht gesetzt')
    run = subprocess.run([sys.executable, '-m', 'unittest', 'tests.test_postgres', '-v'], cwd=ROOT,
                         capture_output=True, text=True, timeout=600)
    output = run.stderr
    total = re.search(r'Ran (\d+) tests', output)
    skipped = re.search(r'skipped=(\d+)', output)
    summary = f'{total.group(1) if total else "?"} Tests, {skipped.group(1) if skipped else 0} übersprungen'
    require(run.returncode == 0, f'{summary}; Ausgabe: {output.strip().splitlines()[-1:]}')
    require(not skipped, f'{summary}; übersprungene Tests gelten nicht als bestanden')
    return f'{summary}, Datenbank {database}'


MANUAL_CHECKS = (
    'Neuinstallation auf sauberem Windows-System: `start_windows.bat` zweimal starten, keine Reparaturschleife',
    'Startdiagnose: `python cli.py doctor` ohne FAIL',
    'Einstellungen → Datenbank → „Systemstatus prüfen“: Dialog in Hell- und Dunkelmodus lesbar, keine Schlüssel sichtbar',
    'Einstellungen → Datenbank → „Sicherung wiederherstellen“ mit einer Testdatenbank (`WISSEN_DB` auf eine Kopie setzen), '
    'Bestätigung mit falschem und richtigem Dateinamen',
    'Konfigurationsexport und -import auf dem sauberen System ohne Schlüssel in der Exportdatei',
    'GitHub Actions: Link zum Lauf mit allen sieben grünen Jobs eintragen',
)


def git_state():
    try:
        commit = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], cwd=ROOT, capture_output=True,
                                text=True, timeout=10).stdout.strip()
        dirty = subprocess.run(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=ROOT,
                               capture_output=True, text=True, timeout=30).stdout.strip()
        return f'{commit} (mit lokalen Änderungen)' if dirty else commit
    except Exception:
        return 'unbekannt'


def write_protocol(target, sqlite_source):
    lines = [
        '# Release-Gate-Protokoll Wissensraum-AI', '',
        f'- Zeitpunkt: {time.strftime("%d.%m.%Y %H:%M:%S")}',
        f'- Repository-Stand: {git_state()}',
        f'- System: {platform.platform()}, Python {platform.python_version()}, SQLite {sqlite3.sqlite_version}',
        f'- Aktuelle Schemaversion: {CURRENT_SCHEMA_VERSION}',
        f'- Echte Datenkopie: {"ja" if sqlite_source else "nein"}', '',
        '## Automatisch geprüfte Schritte', '',
        '| ID | Prüfung | Ergebnis | Nachweis | Dauer |', '|---|---|---|---|---:|',
    ]
    for check_id, title, status, detail, seconds in RESULTS:
        lines.append(f'| {check_id} | {title} | {status} | {detail.replace("|", "/")} | {seconds:.1f} s |')
    lines += ['', 'SKIP ist kein bestandener Schritt.', '', '## Manuelle Prüfungen', '']
    lines += [f'- [ ] {item}' for item in MANUAL_CHECKS]
    lines += ['', 'Geprüft von: ____________________  Datum: __________', '']
    target.write_text('\n'.join(lines), encoding='utf-8')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--sqlite', help='Kopie einer echten SQLite-Datenbank, wird nur gelesen')
    parser.add_argument('--protocol', type=Path,
                        default=ROOT / f'release-gate-{time.strftime("%Y%m%d-%H%M%S")}.md')
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix='wissensraum-gate-') as folder:
        work = Path(folder)
        migrate_each_version(work)
        aborted_migration(work)
        concurrent_start(work)
        real_data_copy(work, args.sqlite)
        sqlite_restore(work)
        postgres_contract(work)
    write_protocol(args.protocol, args.sqlite)
    print(f'Protokoll: {args.protocol}')
    return 1 if any(status == 'FAIL' for _, _, status, _, _ in RESULTS) else 0


if __name__ == '__main__':
    raise SystemExit(main())
