"""Versioned, transactional database migrations for supported backends."""
from __future__ import annotations

import sqlite3
import os
import shutil
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

CURRENT_SCHEMA_VERSION = 5
# Unversioned PostgreSQL schemas predate schema_migrations and match version 4.
POSTGRES_LEGACY_SCHEMA_VERSION = 4
# ponytail: source_type is validated in application code (core.SOURCE_TYPES), not by a
# CHECK constraint, because the SQLite contract check cannot read CHECK clauses.
POSTGRES_MIGRATION_LOCK_ID = 87031904
POSTGRES_LOCK_TIMEOUT_SECONDS = 20.0
WINDOWS_SQLITE_MIGRATION_LOCK_OFFSET = 0x7FFFFFFFFFFFFFFE


SQLITE_MIGRATIONS = (
    (1, (
        """CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY, title TEXT NOT NULL, category TEXT NOT NULL,
            filename TEXT, original BLOB, content TEXT NOT NULL,
            sha256 TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY,
            document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL, content TEXT NOT NULL,
            UNIQUE(document_id, ordinal)
        )""",
        """CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
            title, category, content, content='chunks_search', content_rowid='id',
            tokenize='unicode61 remove_diacritics 2'
        )""",
        """CREATE VIEW IF NOT EXISTS chunks_search AS
            SELECT c.id, d.title, d.category, c.content
            FROM chunks c JOIN documents d ON d.id=c.document_id""",
        """CREATE TRIGGER IF NOT EXISTS chunk_insert AFTER INSERT ON chunks BEGIN
            INSERT INTO search_index(rowid,title,category,content)
            SELECT NEW.id,title,category,NEW.content FROM documents WHERE id=NEW.document_id;
        END""",
        """CREATE TRIGGER IF NOT EXISTS chunk_delete AFTER DELETE ON chunks BEGIN
            INSERT INTO search_index(search_index,rowid,title,category,content)
            SELECT 'delete',OLD.id,title,category,OLD.content FROM documents WHERE id=OLD.document_id;
        END""",
        """CREATE TABLE IF NOT EXISTS ai_runs (
            id INTEGER PRIMARY KEY, kind TEXT NOT NULL, question TEXT NOT NULL,
            answer TEXT NOT NULL, sources_json TEXT NOT NULL, model TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
    )),
    (2, (
        "CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL)",
    )),
    (3, (
        """CREATE TABLE IF NOT EXISTS email_accounts (
            id TEXT PRIMARY KEY, label TEXT NOT NULL, provider TEXT NOT NULL,
            email TEXT NOT NULL, username TEXT NOT NULL, host TEXT NOT NULL DEFAULT '',
            port INTEGER NOT NULL DEFAULT 0, client_id TEXT NOT NULL DEFAULT '',
            tenant TEXT NOT NULL DEFAULT 'common',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
    )),
    (4, (
        """CREATE TABLE IF NOT EXISTS email_imports (
            account_id TEXT NOT NULL REFERENCES email_accounts(id) ON DELETE CASCADE,
            remote_id TEXT NOT NULL,
            document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(account_id,remote_id)
        )""",
    )),
    (5, (
        """CREATE TABLE IF NOT EXISTS sources (
            document_id INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
            source_type TEXT NOT NULL, account_id TEXT NOT NULL DEFAULT '',
            locator TEXT NOT NULL DEFAULT '',
            recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        """INSERT INTO sources(document_id, source_type, account_id, locator)
            SELECT id,
                CASE WHEN filename IS NULL THEN 'note'
                     WHEN remote_id IS NOT NULL AND substr(remote_id, 1, 6) = 'graph:' THEN 'graph'
                     WHEN remote_id IS NOT NULL THEN 'imap'
                     WHEN substr(category, 1, 15) = 'E-Mail · PST · ' THEN 'pst'
                     WHEN substr(category, 1, 9) = 'E-Mail · '
                          AND substr(filename, length(filename) - 4) = '.json' THEN 'graph'
                     WHEN substr(category, 1, 9) = 'E-Mail · ' THEN 'imap'
                     ELSE 'file' END,
                COALESCE(account_id, ''),
                CASE WHEN filename IS NULL THEN ''
                     WHEN remote_id IS NOT NULL THEN remote_id
                     WHEN substr(category, 1, 15) = 'E-Mail · PST · ' THEN substr(category, 16)
                     WHEN substr(category, 1, 9) = 'E-Mail · ' THEN ''
                     ELSE filename END
            FROM (SELECT d.id, d.filename, d.category,
                    (SELECT e.remote_id FROM email_imports e WHERE e.document_id = d.id
                     ORDER BY e.imported_at, e.account_id, e.remote_id LIMIT 1) AS remote_id,
                    (SELECT e.account_id FROM email_imports e WHERE e.document_id = d.id
                     ORDER BY e.imported_at, e.account_id, e.remote_id LIMIT 1) AS account_id
                  FROM documents d) AS origin""",
    )),
)


POSTGRES_MIGRATIONS = (
    (1, (
        """CREATE TABLE IF NOT EXISTS documents (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            title TEXT NOT NULL, category TEXT NOT NULL, filename TEXT, original BYTEA,
            content TEXT NOT NULL, sha256 TEXT NOT NULL UNIQUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS chunks (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            document_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL, content TEXT NOT NULL,
            UNIQUE(document_id, ordinal)
        )""",
        "CREATE INDEX IF NOT EXISTS chunks_document_id_idx ON chunks(document_id)",
        "CREATE INDEX IF NOT EXISTS chunks_search_idx ON chunks USING GIN (to_tsvector('simple',content))",
        """CREATE TABLE IF NOT EXISTS ai_runs (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            kind TEXT NOT NULL, question TEXT NOT NULL, answer TEXT NOT NULL,
            sources_json TEXT NOT NULL, model TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
    )),
    (2, (
        "CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL)",
    )),
    (3, (
        """CREATE TABLE IF NOT EXISTS email_accounts (
            id TEXT PRIMARY KEY, label TEXT NOT NULL, provider TEXT NOT NULL,
            email TEXT NOT NULL, username TEXT NOT NULL, host TEXT NOT NULL DEFAULT '',
            port INTEGER NOT NULL DEFAULT 0, client_id TEXT NOT NULL DEFAULT '',
            tenant TEXT NOT NULL DEFAULT 'common',
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
    )),
    (4, (
        """CREATE TABLE IF NOT EXISTS email_imports (
            account_id TEXT NOT NULL REFERENCES email_accounts(id) ON DELETE CASCADE,
            remote_id TEXT NOT NULL,
            document_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            imported_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(account_id,remote_id)
        )""",
    )),
    (5, (
        """CREATE TABLE IF NOT EXISTS sources (
            document_id BIGINT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
            source_type TEXT NOT NULL, account_id TEXT NOT NULL DEFAULT '',
            locator TEXT NOT NULL DEFAULT '',
            recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        """INSERT INTO sources(document_id, source_type, account_id, locator)
            SELECT id,
                CASE WHEN filename IS NULL THEN 'note'
                     WHEN remote_id IS NOT NULL AND substr(remote_id, 1, 6) = 'graph:' THEN 'graph'
                     WHEN remote_id IS NOT NULL THEN 'imap'
                     WHEN substr(category, 1, 15) = 'E-Mail · PST · ' THEN 'pst'
                     WHEN substr(category, 1, 9) = 'E-Mail · '
                          AND substr(filename, length(filename) - 4) = '.json' THEN 'graph'
                     WHEN substr(category, 1, 9) = 'E-Mail · ' THEN 'imap'
                     ELSE 'file' END,
                COALESCE(account_id, ''),
                CASE WHEN filename IS NULL THEN ''
                     WHEN remote_id IS NOT NULL THEN remote_id
                     WHEN substr(category, 1, 15) = 'E-Mail · PST · ' THEN substr(category, 16)
                     WHEN substr(category, 1, 9) = 'E-Mail · ' THEN ''
                     ELSE filename END
            FROM (SELECT d.id, d.filename, d.category,
                    (SELECT e.remote_id FROM email_imports e WHERE e.document_id = d.id
                     ORDER BY e.imported_at, e.account_id, e.remote_id LIMIT 1) AS remote_id,
                    (SELECT e.account_id FROM email_imports e WHERE e.document_id = d.id
                     ORDER BY e.imported_at, e.account_id, e.remote_id LIMIT 1) AS account_id
                  FROM documents d) AS origin""",
    )),
)

POSTGRES_APPLICATION_TABLES = {
    'documents', 'chunks', 'ai_runs', 'settings', 'email_accounts', 'email_imports'
}

SQLITE_TABLE_VERSIONS = {
    'documents': 1, 'chunks': 1, 'ai_runs': 1, 'settings': 2,
    'email_accounts': 3, 'email_imports': 4, 'sources': 5,
}

SQLITE_COLUMNS = {
    'documents': (('id','INTEGER',0,None,1), ('title','TEXT',1,None,0),
                  ('category','TEXT',1,None,0), ('filename','TEXT',0,None,0),
                  ('original','BLOB',0,None,0), ('content','TEXT',1,None,0),
                  ('sha256','TEXT',1,None,0),
                  ('created_at','TEXT',1,'CURRENT_TIMESTAMP',0)),
    'chunks': (('id','INTEGER',0,None,1), ('document_id','INTEGER',1,None,0),
               ('ordinal','INTEGER',1,None,0), ('content','TEXT',1,None,0)),
    'ai_runs': (('id','INTEGER',0,None,1), ('kind','TEXT',1,None,0),
                ('question','TEXT',1,None,0), ('answer','TEXT',1,None,0),
                ('sources_json','TEXT',1,None,0), ('model','TEXT',1,None,0),
                ('created_at','TEXT',1,'CURRENT_TIMESTAMP',0)),
    'settings': (('name','TEXT',0,None,1), ('value','TEXT',1,None,0)),
    'email_accounts': (('id','TEXT',0,None,1), ('label','TEXT',1,None,0),
                       ('provider','TEXT',1,None,0), ('email','TEXT',1,None,0),
                       ('username','TEXT',1,None,0), ('host','TEXT',1,"''",0),
                       ('port','INTEGER',1,'0',0), ('client_id','TEXT',1,"''",0),
                       ('tenant','TEXT',1,"'common'",0),
                       ('created_at','TEXT',1,'CURRENT_TIMESTAMP',0),
                       ('updated_at','TEXT',1,'CURRENT_TIMESTAMP',0)),
    'email_imports': (('account_id','TEXT',1,None,1),
                      ('remote_id','TEXT',1,None,2),
                      ('document_id','INTEGER',1,None,0),
                      ('imported_at','TEXT',1,'CURRENT_TIMESTAMP',0)),
    'sources': (('document_id','INTEGER',0,None,1), ('source_type','TEXT',1,None,0),
                ('account_id','TEXT',1,"''",0), ('locator','TEXT',1,"''",0),
                ('recorded_at','TEXT',1,'CURRENT_TIMESTAMP',0)),
}

SQLITE_UNIQUE_CONSTRAINTS = {
    'documents': {('sha256',)}, 'chunks': {('document_id','ordinal')},
    'ai_runs': set(), 'settings': set(), 'email_accounts': set(),
    'email_imports': set(), 'sources': set(),
}

SQLITE_FOREIGN_KEYS = {
    'documents': set(),
    'chunks': {('document_id','documents','id','NO ACTION','CASCADE')},
    'ai_runs': set(), 'settings': set(), 'email_accounts': set(),
    'email_imports': {
        ('account_id','email_accounts','id','NO ACTION','CASCADE'),
        ('document_id','documents','id','NO ACTION','CASCADE'),
    },
    'sources': {('document_id','documents','id','NO ACTION','CASCADE')},
}

POSTGRES_LEGACY_COLUMNS = {
    ('documents','id','bigint','NO','YES',None,'ALWAYS'),
    ('documents','title','text','NO','NO',None,None),
    ('documents','category','text','NO','NO',None,None),
    ('documents','filename','text','YES','NO',None,None),
    ('documents','original','bytea','YES','NO',None,None),
    ('documents','content','text','NO','NO',None,None),
    ('documents','sha256','text','NO','NO',None,None),
    ('documents','created_at','timestamp with time zone','NO','NO','CURRENT_TIMESTAMP',None),
    ('chunks','id','bigint','NO','YES',None,'ALWAYS'),
    ('chunks','document_id','bigint','NO','NO',None,None),
    ('chunks','ordinal','integer','NO','NO',None,None),
    ('chunks','content','text','NO','NO',None,None),
    ('ai_runs','id','bigint','NO','YES',None,'ALWAYS'),
    ('ai_runs','kind','text','NO','NO',None,None),
    ('ai_runs','question','text','NO','NO',None,None),
    ('ai_runs','answer','text','NO','NO',None,None),
    ('ai_runs','sources_json','text','NO','NO',None,None),
    ('ai_runs','model','text','NO','NO',None,None),
    ('ai_runs','created_at','timestamp with time zone','NO','NO','CURRENT_TIMESTAMP',None),
    ('settings','name','text','NO','NO',None,None),
    ('settings','value','text','NO','NO',None,None),
    ('email_accounts','id','text','NO','NO',None,None),
    ('email_accounts','label','text','NO','NO',None,None),
    ('email_accounts','provider','text','NO','NO',None,None),
    ('email_accounts','email','text','NO','NO',None,None),
    ('email_accounts','username','text','NO','NO',None,None),
    ('email_accounts','host','text','NO','NO',"''::text",None),
    ('email_accounts','port','integer','NO','NO','0',None),
    ('email_accounts','client_id','text','NO','NO',"''::text",None),
    ('email_accounts','tenant','text','NO','NO',"'common'::text",None),
    ('email_accounts','created_at','timestamp with time zone','NO','NO','CURRENT_TIMESTAMP',None),
    ('email_accounts','updated_at','timestamp with time zone','NO','NO','CURRENT_TIMESTAMP',None),
    ('email_imports','account_id','text','NO','NO',None,None),
    ('email_imports','remote_id','text','NO','NO',None,None),
    ('email_imports','document_id','bigint','NO','NO',None,None),
    ('email_imports','imported_at','timestamp with time zone','NO','NO','CURRENT_TIMESTAMP',None),
    ('sources','document_id','bigint','NO','NO',None,None),
    ('sources','source_type','text','NO','NO',None,None),
    ('sources','account_id','text','NO','NO',"''::text",None),
    ('sources','locator','text','NO','NO',"''::text",None),
    ('sources','recorded_at','timestamp with time zone','NO','NO','CURRENT_TIMESTAMP',None),
}

POSTGRES_LEGACY_CONSTRAINTS = {
    ('documents', 'p', ('id',), None, (), ' '),
    ('documents', 'u', ('sha256',), None, (), ' '),
    ('chunks', 'p', ('id',), None, (), ' '),
    ('chunks', 'u', ('document_id', 'ordinal'), None, (), ' '),
    ('chunks', 'f', ('document_id',), 'documents', ('id',), 'c'),
    ('ai_runs', 'p', ('id',), None, (), ' '),
    ('settings', 'p', ('name',), None, (), ' '),
    ('email_accounts', 'p', ('id',), None, (), ' '),
    ('email_imports', 'p', ('account_id', 'remote_id'), None, (), ' '),
    ('email_imports', 'f', ('account_id',), 'email_accounts', ('id',), 'c'),
    ('email_imports', 'f', ('document_id',), 'documents', ('id',), 'c'),
    ('sources', 'p', ('document_id',), None, (), ' '),
    ('sources', 'f', ('document_id',), 'documents', ('id',), 'c'),
}


def _sqlite_version(db):
    return db.execute('PRAGMA user_version').fetchone()[0]


def _lock_file_until(file, deadline, offset=0):
    while True:
        try:
            file.seek(offset)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except (OSError, BlockingIOError):
            if time.monotonic() >= deadline:
                raise TimeoutError('Zeitüberschreitung beim Sperren der Datenbankmigration.') from None
            time.sleep(0.05)


def _unlock_file(file, offset=0):
    file.seek(offset)
    if os.name == 'nt':
        import msvcrt
        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(file.fileno(), fcntl.LOCK_UN)


@contextmanager
def _database_file_lock(path, timeout: float = 20):
    """Hold path and file-identity locks; the OS releases them after crashes."""
    canonical = Path(os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path)))))
    path_lock = open(canonical.with_name(canonical.name + '.migration.lock'), 'a+b')
    database_lock = None
    locked = []
    deadline = time.monotonic() + timeout
    try:
        _lock_file_until(path_lock, deadline)
        locked.append((path_lock, 0))
        try:
            database_lock = open(canonical, 'r+b')
        except FileNotFoundError:
            pass
        else:
            offset = WINDOWS_SQLITE_MIGRATION_LOCK_OFFSET if os.name == 'nt' else 0
            _lock_file_until(database_lock, deadline, offset)
            locked.append((database_lock, offset))
        yield
    finally:
        for file, offset in reversed(locked):
            try:
                _unlock_file(file, offset)
            finally:
                file.close()
        locked_files = {file for file, _ in locked}
        if database_lock is not None and database_lock not in locked_files:
            database_lock.close()
        if path_lock not in locked_files:
            path_lock.close()


def _verified_sqlite_backup(db, path, version, suffix=None):
    backup = path.with_name(path.name + (suffix or f'.pre-migration-v{version}.bak'))
    temporary = backup.with_name(backup.name + f'.{os.getpid()}.{uuid.uuid4().hex}.tmp')
    try:
        with closing(sqlite3.connect(temporary)) as destination:
            db.backup(destination)
        with closing(sqlite3.connect(temporary)) as check:
            if check.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Sicherung vor der Datenbankmigration ist beschädigt.')
            if _sqlite_version(check) != version:
                raise ValueError('Sicherung vor der Datenbankmigration hat eine falsche Version.')
        temporary.replace(backup)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return backup


def _apply_sqlite_migration(db, version, statements):
    db.execute('BEGIN IMMEDIATE')
    try:
        for statement in statements:
            db.execute(statement)
        validate_sqlite(db, version)
        db.execute(f'PRAGMA user_version = {version}')
    except Exception:
        db.rollback()
        raise
    else:
        db.commit()


def _canonical_sql(sql):
    return ''.join(str(sql or '').lower().split()).replace('ifnotexists', '')


def validate_sqlite(db, version=CURRENT_SCHEMA_VERSION):
    """Validate the complete SQLite contract for one recorded schema version."""
    expected_tables = {name for name, introduced in SQLITE_TABLE_VERSIONS.items()
                       if introduced <= version}
    rows = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    actual_tables = {row[0] for row in rows
                     if not row[0].startswith('sqlite_')
                     and not row[0].startswith('search_index')}
    if actual_tables != expected_tables:
        raise ValueError('SQLite-Schema ist unvollständig oder inkompatibel.')

    for table in expected_tables:
        columns = tuple((row[1], str(row[2]).upper(), row[3], row[4], row[5])
                        for row in db.execute(f'PRAGMA table_info({table})'))
        if columns != SQLITE_COLUMNS[table]:
            raise ValueError('SQLite-Schema ist unvollständig oder inkompatibel.')

        unique = set()
        for index in db.execute(f'PRAGMA index_list({table})'):
            if index[2] and index[3] != 'pk':
                unique.add(tuple(row[2] for row in db.execute(
                    f'PRAGMA index_info("{index[1]}")')))
        if unique != SQLITE_UNIQUE_CONSTRAINTS[table]:
            raise ValueError('SQLite-Schema ist unvollständig oder inkompatibel.')

        foreign_keys = {(row[3], row[2], row[4], row[5], row[6])
                        for row in db.execute(f'PRAGMA foreign_key_list({table})')}
        if foreign_keys != SQLITE_FOREIGN_KEYS[table]:
            raise ValueError('SQLite-Schema ist unvollständig oder inkompatibel.')

    violations = db.execute('PRAGMA foreign_key_check').fetchall()
    if violations:
        raise ValueError('Datenbank enthält ungültige Fremdschlüssel.')
    if version < 1:
        return

    expected_fts = {
        'search_index': ('table', SQLITE_MIGRATIONS[0][1][2]),
        'chunks_search': ('view', SQLITE_MIGRATIONS[0][1][3]),
        'chunk_insert': ('trigger', SQLITE_MIGRATIONS[0][1][4]),
        'chunk_delete': ('trigger', SQLITE_MIGRATIONS[0][1][5]),
    }
    rows = db.execute("""SELECT type,name,sql FROM sqlite_master
        WHERE name IN ('search_index','chunks_search','chunk_insert','chunk_delete')""").fetchall()
    actual_fts = {row[1]: (row[0], _canonical_sql(row[2])) for row in rows}
    expected_fts = {name: (kind, _canonical_sql(sql))
                    for name, (kind, sql) in expected_fts.items()}
    if actual_fts != expected_fts:
        raise ValueError('Volltext-Suchstrukturen sind unvollständig oder inkompatibel.')
    db.execute("INSERT INTO search_index(search_index,rank) VALUES('integrity-check',1)")
    db.execute('SELECT rowid FROM search_index LIMIT 1').fetchall()


def migrate_sqlite(path):
    """Upgrade a SQLite database one committed version at a time."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _database_file_lock(path):
        existed = path.exists()
        with closing(sqlite3.connect(path, timeout=20)) as db:
            db.execute('PRAGMA foreign_keys=ON')
            version = _sqlite_version(db)
            if version > CURRENT_SCHEMA_VERSION:
                raise ValueError('Datenbankversion ist neuer als diese Anwendung.')
            if existed:
                validate_sqlite(db, version)
                db.rollback()  # FTS integrity-check opens a read/write transaction.
            if existed and version < CURRENT_SCHEMA_VERSION:
                _verified_sqlite_backup(db, path, version)
            for target, statements in SQLITE_MIGRATIONS:
                if target > version:
                    _apply_sqlite_migration(db, target, statements)
            validate_sqlite(db, CURRENT_SCHEMA_VERSION)


def _document_hashes(db):
    return db.execute('SELECT id, sha256 FROM documents ORDER BY id').fetchall()


def _check_restored_sqlite(db, version, hashes):
    if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        raise ValueError('Wiederhergestellte Datenbank ist beschädigt.')
    if _sqlite_version(db) != version:
        raise ValueError('Wiederhergestellte Datenbank hat eine falsche Version.')
    validate_sqlite(db, version)
    db.rollback()  # FTS integrity-check opens a read/write transaction.
    if _document_hashes(db) != hashes:
        raise ValueError('Dokument-Prüfsummen weichen von der Sicherung ab.')


def restore_sqlite(path, backup):
    """Replace the SQLite database at path with a validated backup.

    The current database is first copied to a verified pre-restore backup.
    Older schema versions are restored as-is and migrated on the next start.
    """
    path, backup = Path(path), Path(backup)
    if not backup.is_file():
        raise ValueError('Sicherungsdatei wurde nicht gefunden.')
    if path.exists() and os.path.samefile(path, backup):
        raise ValueError('Sicherung und Zieldatenbank müssen verschiedene Dateien sein.')
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(f'{path.name}.restore.{os.getpid()}.{uuid.uuid4().hex}.tmp')
    try:
        try:
            # Copy bytes instead of opening the backup: a read-only SQLite
            # connection to a WAL-mode file leaves -wal/-shm files beside it,
            # and validate_sqlite writes an FTS check. A WAL sidecar is copied
            # too so committed pages stored there are not lost.
            shutil.copyfile(backup, staging)
            if Path(str(backup) + '-wal').is_file():
                shutil.copyfile(str(backup) + '-wal', str(staging) + '-wal')
            with closing(sqlite3.connect(staging)) as check:
                version = _sqlite_version(check)
                if version < 1 or version > CURRENT_SCHEMA_VERSION:
                    raise ValueError('Sicherung hat keine unterstützte Schemaversion.')
                if check.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('Sicherung ist beschädigt.')
                validate_sqlite(check, version)
                check.rollback()
                hashes = _document_hashes(check)
        except sqlite3.DatabaseError:
            raise ValueError('Sicherung ist keine lesbare SQLite-Datenbank.') from None
        with _database_file_lock(path):
            existed = path.exists()
            with closing(sqlite3.connect(path, timeout=20)) as target:
                safety = None
                if existed:
                    try:
                        safety = _verified_sqlite_backup(
                            target, path, _sqlite_version(target),
                            f'.pre-restore-{time.strftime("%Y%m%d-%H%M%S")}'
                            f'-{uuid.uuid4().hex[:8]}.bak')
                    except (sqlite3.DatabaseError, ValueError):
                        # ponytail: a damaged target is not overwritten; the user
                        # renames it and restores into a fresh path instead.
                        raise ValueError('Aktuelle Datenbank ist nicht lesbar und wird nicht '
                                         'überschrieben. Datei umbenennen und Wiederherstellung '
                                         'erneut ausführen.') from None
                try:
                    with closing(sqlite3.connect(staging)) as source:
                        source.backup(target)
                    _check_restored_sqlite(target, version, hashes)
                except Exception:
                    if safety is not None:
                        with closing(sqlite3.connect(safety)) as previous:
                            previous.backup(target)
                    raise
    finally:
        for suffix in ('', '-wal', '-shm', '-journal'):
            Path(str(staging) + suffix).unlink(missing_ok=True)
    return {'version': version, 'documents': len(hashes),
            'safety_backup': str(safety) if safety else None}


def _value(row, name, index=0):
    if row is None:
        return None
    return row[name] if isinstance(row, dict) else row[index]


def _postgres_expected_tables(version):
    return {name for name, introduced in SQLITE_TABLE_VERSIONS.items()
            if introduced <= version}


def _validate_postgres_schema(db, version, legacy=False):
    expected_tables = _postgres_expected_tables(version)
    table_rows = db.execute("""SELECT table_name FROM information_schema.tables
        WHERE table_schema='wissensraum'""").fetchall()
    actual_tables = {_value(row, 'table_name') for row in table_rows}
    required_tables = expected_tables if legacy else expected_tables | {'schema_migrations'}
    if actual_tables != required_tables:
        prefix = 'Legacy-' if legacy else ''
        raise ValueError(f'{prefix}PostgreSQL-Schema ist unvollständig oder inkompatibel.')
    columns = db.execute("""SELECT table_name, column_name, data_type, is_nullable,
+        is_identity, column_default, identity_generation
+        FROM information_schema.columns
+        WHERE table_schema='wissensraum'
+          AND table_name IN ('documents','chunks','ai_runs','settings',
+                             'email_accounts','email_imports','sources')
+        ORDER BY table_name, ordinal_position""".replace('\n+', '\n')).fetchall()
    actual_columns = {
        (_value(row, 'table_name'), _value(row, 'column_name', 1),
         _value(row, 'data_type', 2), _value(row, 'is_nullable', 3),
         _value(row, 'is_identity', 4), _value(row, 'column_default', 5),
         _value(row, 'identity_generation', 6))
        for row in columns
    }
    expected_columns = {column for column in POSTGRES_LEGACY_COLUMNS
                        if column[0] in expected_tables}

    constraints = db.execute("""SELECT rel.relname AS table_name, con.contype AS constraint_type,
        ARRAY(SELECT att.attname FROM unnest(con.conkey) WITH ORDINALITY AS key(attnum, ord)
              JOIN pg_catalog.pg_attribute att ON att.attrelid=con.conrelid AND att.attnum=key.attnum
              ORDER BY key.ord) AS columns,
        referenced.relname AS referenced_table,
        ARRAY(SELECT att.attname FROM unnest(con.confkey) WITH ORDINALITY AS key(attnum, ord)
              JOIN pg_catalog.pg_attribute att ON att.attrelid=con.confrelid AND att.attnum=key.attnum
              ORDER BY key.ord) AS referenced_columns,
        con.confdeltype AS delete_action
        FROM pg_catalog.pg_constraint con
        JOIN pg_catalog.pg_class rel ON rel.oid=con.conrelid
        JOIN pg_catalog.pg_namespace ns ON ns.oid=rel.relnamespace
        LEFT JOIN pg_catalog.pg_class referenced ON referenced.oid=con.confrelid
        WHERE ns.nspname='wissensraum' AND con.contype IN ('p','u','f','c','x')
          AND rel.relname IN ('documents','chunks','ai_runs','settings',
                              'email_accounts','email_imports','sources')""").fetchall()
    actual_constraints = {
        (_value(row, 'table_name'), _value(row, 'constraint_type', 1),
         tuple(_value(row, 'columns', 2) or ()), _value(row, 'referenced_table', 3),
         tuple(_value(row, 'referenced_columns', 4) or ()),
         _value(row, 'delete_action', 5) or ' ')
        for row in constraints
    }
    expected_constraints = {constraint for constraint in POSTGRES_LEGACY_CONSTRAINTS
                            if constraint[0] in expected_tables}

    indexes = db.execute("""SELECT tablename AS table_name, indexname AS index_name,
        indexdef AS index_definition FROM pg_catalog.pg_indexes
        WHERE schemaname='wissensraum' AND tablename='chunks'""").fetchall()
    index_definitions = {
        _value(row, 'index_name', 1): str(_value(row, 'index_definition', 2) or '').lower()
        for row in indexes
    }
    document_index = ''.join(index_definitions.get('chunks_document_id_idx', '').split())
    search_index = index_definitions.get('chunks_search_idx', '')
    indexes_valid = (version < 1 or
                     ('(document_id)' in document_index
                      and 'using gin' in search_index and 'to_tsvector' in search_index))
    if (actual_columns != expected_columns
            or actual_constraints != expected_constraints
            or not indexes_valid):
        prefix = 'Legacy-' if legacy else ''
        raise ValueError(f'{prefix}PostgreSQL-Schema ist unvollständig oder inkompatibel.')


def _acquire_postgres_migration_lock(db, timeout_seconds=POSTGRES_LOCK_TIMEOUT_SECONDS):
    try:
        milliseconds = max(1, int(float(timeout_seconds) * 1000))
    except (TypeError, ValueError, OverflowError):
        raise ValueError('Ungültige Zeitgrenze für die PostgreSQL-Migrationssperre.') from None
    try:
        db.execute(f"SET LOCAL lock_timeout = '{milliseconds}ms'")
        db.execute(f'SELECT pg_advisory_xact_lock({POSTGRES_MIGRATION_LOCK_ID})')
    except Exception as error:
        sqlstate = getattr(error, 'sqlstate', None)
        if sqlstate == '55P03' or 'lock timeout' in str(error).lower():
            raise ValueError('Zeitüberschreitung beim Sperren der PostgreSQL-Migration.') from None
        raise


def migrate_postgres(db, lock_timeout_seconds=POSTGRES_LOCK_TIMEOUT_SECONDS, *, acquire_lock=True):
    """Migrate PostgreSQL inside the caller's transaction with a bounded lock wait."""
    if acquire_lock:
        _acquire_postgres_migration_lock(db, lock_timeout_seconds)
    db.execute('CREATE SCHEMA IF NOT EXISTS wissensraum')
    db.execute('SET search_path TO wissensraum, public')
    rows = db.execute("""SELECT table_name FROM information_schema.tables
        WHERE table_schema='wissensraum'""").fetchall()
    tables = {_value(row, 'table_name') for row in rows}
    if 'schema_migrations' not in tables:
        if tables == POSTGRES_APPLICATION_TABLES:
            _validate_postgres_schema(db, POSTGRES_LEGACY_SCHEMA_VERSION, legacy=True)
            db.execute("""CREATE TABLE wissensraum.schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            for target in range(1, POSTGRES_LEGACY_SCHEMA_VERSION + 1):
                db.execute('INSERT INTO wissensraum.schema_migrations(version) VALUES(?)',
                           (target,))
            version = POSTGRES_LEGACY_SCHEMA_VERSION
        elif tables:
            raise ValueError('Unversioniertes PostgreSQL-Schema enthält unerwartete Tabellen.')
        else:
            version = 0
            db.execute("""CREATE TABLE wissensraum.schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
    else:
        history_rows = db.execute(
            'SELECT version FROM wissensraum.schema_migrations ORDER BY version').fetchall()
        history = [_value(row, 'version') for row in history_rows]
        if not history:
            raise ValueError('PostgreSQL-Migrationshistorie ist leer oder beschädigt.')
        if history[-1] > CURRENT_SCHEMA_VERSION:
            raise ValueError('Datenbankversion ist neuer als diese Anwendung.')
        if history != list(range(1, history[-1] + 1)):
            raise ValueError('PostgreSQL-Migrationshistorie ist nicht lückenlos oder beschädigt.')
        version = history[-1]
        _validate_postgres_schema(db, version)

    for target, statements in POSTGRES_MIGRATIONS:
        if target > version:
            for statement in statements:
                db.execute(statement)
            db.execute('INSERT INTO wissensraum.schema_migrations(version) VALUES(?)', (target,))
    _validate_postgres_schema(db, CURRENT_SCHEMA_VERSION)

