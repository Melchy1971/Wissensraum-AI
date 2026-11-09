"""Versioned, transactional database migrations for supported backends."""
from __future__ import annotations

import sqlite3
import os
import shutil
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

CURRENT_SCHEMA_VERSION = 23
# Unversioned PostgreSQL schemas predate schema_migrations and match version 4.
POSTGRES_LEGACY_SCHEMA_VERSION = 4
# ponytail: source_type is validated in application code (core.SOURCE_TYPES), not by a
# CHECK constraint, because the SQLite contract check cannot read CHECK clauses.
POSTGRES_MIGRATION_LOCK_ID = 87031904
POSTGRES_LOCK_TIMEOUT_SECONDS = 20.0
WINDOWS_SQLITE_MIGRATION_LOCK_OFFSET = 0x7FFFFFFFFFFFFFFE


SQLITE_AUDIT_TRIGGERS = {
    'audit_log_no_update': """CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
        BEGIN SELECT RAISE(ABORT, 'Der Audit-Verlauf ist unveränderlich.'); END""",
    'audit_log_no_delete': """CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
        BEGIN SELECT RAISE(ABORT, 'Der Audit-Verlauf ist unveränderlich.'); END""",
}


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
    (6, (
        """CREATE TABLE IF NOT EXISTS import_jobs (
            id INTEGER PRIMARY KEY, kind TEXT NOT NULL, root TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT 'Allgemein', status TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS import_items (
            id INTEGER PRIMARY KEY,
            job_id INTEGER NOT NULL REFERENCES import_jobs(id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL, path TEXT NOT NULL, locator TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', reason TEXT NOT NULL DEFAULT '',
            size INTEGER, sha256 TEXT NOT NULL DEFAULT '',
            document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(job_id, ordinal)
        )""",
        """CREATE TABLE IF NOT EXISTS import_item_errors (
            id INTEGER PRIMARY KEY,
            item_id INTEGER NOT NULL REFERENCES import_items(id) ON DELETE CASCADE,
            attempt INTEGER NOT NULL, reason TEXT NOT NULL, message TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
    )),
    (7, (
        """CREATE TABLE IF NOT EXISTS document_versions (
            document_id INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
            lineage_id INTEGER NOT NULL, version INTEGER NOT NULL,
            UNIQUE(lineage_id, version)
        )""",
        "INSERT INTO document_versions(document_id, lineage_id, version) SELECT id, id, 1 FROM documents",
    )),
    (8, (
        """CREATE TABLE IF NOT EXISTS email_attachments (
            document_id INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
            email_document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
            name TEXT NOT NULL, content_type TEXT NOT NULL DEFAULT '', size INTEGER
        )""",
    )),
    (9, (
        """CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, normalized TEXT NOT NULL UNIQUE
        )""",
        """CREATE TABLE IF NOT EXISTS document_tags (
            document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            PRIMARY KEY(document_id, tag_id)
        )""",
        """CREATE TABLE IF NOT EXISTS rules (
            id INTEGER PRIMARY KEY, position INTEGER NOT NULL, name TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1, source_type TEXT NOT NULL DEFAULT '',
            path_pattern TEXT NOT NULL DEFAULT '', sender_domain TEXT NOT NULL DEFAULT '',
            extension TEXT NOT NULL DEFAULT '', keyword TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT '', tags TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS rule_runs (
            id INTEGER PRIMARY KEY, status TEXT NOT NULL DEFAULT 'applied',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, reverted_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS rule_run_changes (
            run_id INTEGER NOT NULL REFERENCES rule_runs(id) ON DELETE CASCADE,
            document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            previous_category TEXT NOT NULL, new_category TEXT NOT NULL,
            added_tags TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(run_id, document_id)
        )""",
    )),
    # Release 2.1: one vector per chunk; model and dimension per row, so a model change
    # makes old vectors stale instead of mixing incompatible spaces.
    (10, (
        """CREATE TABLE IF NOT EXISTS chunk_vectors (
            chunk_id INTEGER PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
            model TEXT NOT NULL, dimension INTEGER NOT NULL, vector BLOB NOT NULL
        )""",
    )),
    # Version 11 changes only PostgreSQL (stored search vectors); SQLite keeps its FTS5 index and
    # takes the version number so that both backends share one schema version.
    (11, ()),
    # Release 2.2, step 8A.1: every change of a document's metadata with old and new value,
    # manual or by a rule run; the content, the original and its hash never change.
    (12, (
        """CREATE TABLE IF NOT EXISTS metadata_changes (
            id INTEGER PRIMARY KEY,
            document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            field TEXT NOT NULL, old_value TEXT NOT NULL DEFAULT '', new_value TEXT NOT NULL DEFAULT '',
            origin TEXT NOT NULL, run_id INTEGER REFERENCES rule_runs(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
    )),
    # Release 2.2, step 8A.2: document status, owner and review date; outdated and archived
    # documents can be kept out of AI answers. Existing documents start as 'new'.
    (13, (
        "ALTER TABLE documents ADD COLUMN status TEXT NOT NULL DEFAULT 'new'",
        "ALTER TABLE documents ADD COLUMN owner TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE documents ADD COLUMN review_date TEXT",
    )),
    # Release 2.2, step 8A.3: collections, favorites and saved searches; documents keep every field.
    (14, (
        """CREATE TABLE IF NOT EXISTS collections (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, normalized TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS collection_documents (
            collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
            document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(collection_id, document_id)
        )""",
        """CREATE TABLE IF NOT EXISTS favorites (
            document_id INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS saved_searches (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, normalized TEXT NOT NULL UNIQUE,
            query TEXT NOT NULL, mode TEXT, tag TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
    )),
    # Release 2.2, step 8A.4: typed relations between two documents; both ends are deleted with
    # their document. A pair carries each kind at most once.
    (15, (
        """CREATE TABLE IF NOT EXISTS document_relations (
            id INTEGER PRIMARY KEY,
            source_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            target_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            kind TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(source_id, target_id, kind)
        )""",
    )),
    # Release 2.3, step 8B.1: knowledge graph. Every relation carries its evidence (document and
    # exact text span); entities, mentions and relations are soft-deleted so that every change can be
    # reverted. Configurable entity and relation types start with a small German catalogue.
    (16, (
        """CREATE TABLE IF NOT EXISTS entity_types (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, normalized TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS relation_types (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, normalized TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS entities (
            id INTEGER PRIMARY KEY, type_id INTEGER NOT NULL REFERENCES entity_types(id),
            name TEXT NOT NULL, normalized TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'confirmed',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, deleted_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS entity_mentions (
            id INTEGER PRIMARY KEY, entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            start_offset INTEGER NOT NULL, end_offset INTEGER NOT NULL, quote TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, deleted_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS graph_relations (
            id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            target_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            type_id INTEGER NOT NULL REFERENCES relation_types(id),
            document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            start_offset INTEGER NOT NULL, end_offset INTEGER NOT NULL, quote TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'confirmed',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, deleted_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS graph_changes (
            id INTEGER PRIMARY KEY, action TEXT NOT NULL, object_type TEXT NOT NULL, object_id INTEGER NOT NULL,
            old_value TEXT NOT NULL DEFAULT '', new_value TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, reverted_at TEXT
        )""",
        "INSERT INTO entity_types(name,normalized) VALUES ('Person','person'), ('Organisation','organisation'), ('Prozess','prozess'), ('System','system'), ('Entscheidung','entscheidung')",
        "INSERT INTO relation_types(name,normalized) VALUES ('arbeitet für','arbeitet für'), ('verantwortet','verantwortet'), ('nutzt','nutzt'), ('entscheidet über','entscheidet über'), ('gehört zu','gehört zu')",
    )),
    # Release 2.3, step 8B.2: AI suggestions; a mention, like entities and relations, is 'proposed'
    # until the user confirms it. Existing mentions were all made by hand.
    (17, (
        "ALTER TABLE entity_mentions ADD COLUMN status TEXT NOT NULL DEFAULT 'confirmed'",
    )),
    # Release 2.4, step 9.2: token numbers per provider response for budgets; no text.
    (18, (
        """CREATE TABLE IF NOT EXISTS ai_usage (
            id INTEGER PRIMARY KEY, provider TEXT NOT NULL, model TEXT NOT NULL,
            input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
    )),
    # Release 3.0, step 10.2: one organization, accounts with a role, grants per collection or
    # document. Ownership is a grant of level 'owner'; a document without any grant is visible
    # to administrators only, so the existing stock belongs to the first administrator.
    (19, (
        """CREATE TABLE IF NOT EXISTS organizations (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY,
            organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            tenant_id TEXT NOT NULL, object_id TEXT NOT NULL, display_name TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL, disabled INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(tenant_id, object_id)
        )""",
        """CREATE TABLE IF NOT EXISTS grants (
            id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            document_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
            collection_id INTEGER REFERENCES collections(id) ON DELETE CASCADE,
            access TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        "INSERT INTO organizations(name) VALUES ('Standard')",
    )),
    # Release 3.0, step 10.3: server-side web sessions and pending sign-ins. Only SHA-256 values
    # of session ids, states and nonces are stored; times are Unix seconds (no time zone issues).
    (20, (
        """CREATE TABLE IF NOT EXISTS web_sessions (
            id TEXT PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            csrf_token TEXT NOT NULL, created_at INTEGER NOT NULL, last_seen INTEGER NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS login_requests (
            state TEXT PRIMARY KEY, nonce TEXT NOT NULL, verifier TEXT NOT NULL, created_at INTEGER NOT NULL
        )""",
    )),
    # Release 3.0, step 10.5: append-only audit trail. Ids are gapless and assigned by audit.py; each
    # entry carries the SHA-256 of the previous one. Triggers refuse every change and deletion.
    (21, (
        """CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY, created_at INTEGER NOT NULL, account_id INTEGER, event TEXT NOT NULL,
            target TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '', details TEXT NOT NULL DEFAULT '{}',
            previous_hash TEXT NOT NULL, hash TEXT NOT NULL
        )""",
        SQLITE_AUDIT_TRIGGERS['audit_log_no_update'],
        SQLITE_AUDIT_TRIGGERS['audit_log_no_delete'],
    )),
    # Release 3.0, step 10.6: a revision per changeable object; a change names the revision it read.
    (22, tuple(f'ALTER TABLE {table} ADD COLUMN revision INTEGER NOT NULL DEFAULT 1'
               for table in ('documents', 'collections', 'entities', 'graph_relations', 'grants'))),
    # Release 3.0, step 10.7: the AI history belongs to the asking account (None: desktop, command line).
    (23, ('ALTER TABLE ai_runs ADD COLUMN account_id INTEGER',)),
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
    (6, (
        """CREATE TABLE IF NOT EXISTS import_jobs (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, kind TEXT NOT NULL,
            root TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT 'Allgemein',
            status TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS import_items (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            job_id BIGINT NOT NULL REFERENCES import_jobs(id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL, path TEXT NOT NULL, locator TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', reason TEXT NOT NULL DEFAULT '',
            size BIGINT, sha256 TEXT NOT NULL DEFAULT '',
            document_id BIGINT REFERENCES documents(id) ON DELETE SET NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(job_id, ordinal)
        )""",
        """CREATE TABLE IF NOT EXISTS import_item_errors (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            item_id BIGINT NOT NULL REFERENCES import_items(id) ON DELETE CASCADE,
            attempt INTEGER NOT NULL, reason TEXT NOT NULL, message TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
    )),
    (7, (
        """CREATE TABLE IF NOT EXISTS document_versions (
            document_id BIGINT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
            lineage_id BIGINT NOT NULL, version INTEGER NOT NULL,
            UNIQUE(lineage_id, version)
        )""",
        "INSERT INTO document_versions(document_id, lineage_id, version) SELECT id, id, 1 FROM documents",
    )),
    (8, (
        """CREATE TABLE IF NOT EXISTS email_attachments (
            document_id BIGINT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
            email_document_id BIGINT REFERENCES documents(id) ON DELETE SET NULL,
            name TEXT NOT NULL, content_type TEXT NOT NULL DEFAULT '', size BIGINT
        )""",
    )),
    (9, (
        """CREATE TABLE IF NOT EXISTS tags (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, name TEXT NOT NULL, normalized TEXT NOT NULL UNIQUE
        )""",
        """CREATE TABLE IF NOT EXISTS document_tags (
            document_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            tag_id BIGINT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            PRIMARY KEY(document_id, tag_id)
        )""",
        """CREATE TABLE IF NOT EXISTS rules (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, position INTEGER NOT NULL, name TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1, source_type TEXT NOT NULL DEFAULT '',
            path_pattern TEXT NOT NULL DEFAULT '', sender_domain TEXT NOT NULL DEFAULT '',
            extension TEXT NOT NULL DEFAULT '', keyword TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT '', tags TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS rule_runs (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, status TEXT NOT NULL DEFAULT 'applied',
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, reverted_at TIMESTAMPTZ
        )""",
        """CREATE TABLE IF NOT EXISTS rule_run_changes (
            run_id BIGINT NOT NULL REFERENCES rule_runs(id) ON DELETE CASCADE,
            document_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            previous_category TEXT NOT NULL, new_category TEXT NOT NULL,
            added_tags TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(run_id, document_id)
        )""",
    )),
    (10, (
        """CREATE TABLE IF NOT EXISTS chunk_vectors (
            chunk_id BIGINT PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
            model TEXT NOT NULL, dimension INTEGER NOT NULL, vector BYTEA NOT NULL
        )""",
    )),
    # Release 2.1, step 7.3: stored search vectors. ts_rank_cd parsed every hit again, about
    # 1.3 s per query at 50,000 chunks; the stored vector is parsed once on write.
    (11, (
        "ALTER TABLE chunks ADD COLUMN search_vector tsvector "
        "GENERATED ALWAYS AS (to_tsvector('simple',content)) STORED",
        "ALTER TABLE documents ADD COLUMN search_vector tsvector "
        "GENERATED ALWAYS AS (to_tsvector('simple',title || ' ' || category)) STORED",
        'DROP INDEX chunks_search_idx',
        'CREATE INDEX chunks_search_idx ON chunks USING GIN (search_vector)',
        'CREATE INDEX documents_search_idx ON documents USING GIN (search_vector)',
    )),
    (12, (
        """CREATE TABLE IF NOT EXISTS metadata_changes (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            document_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            field TEXT NOT NULL, old_value TEXT NOT NULL DEFAULT '', new_value TEXT NOT NULL DEFAULT '',
            origin TEXT NOT NULL, run_id BIGINT REFERENCES rule_runs(id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
    )),
    (13, (
        "ALTER TABLE documents ADD COLUMN status TEXT NOT NULL DEFAULT 'new'",
        "ALTER TABLE documents ADD COLUMN owner TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE documents ADD COLUMN review_date TEXT",
    )),
    (14, (
        """CREATE TABLE IF NOT EXISTS collections (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, name TEXT NOT NULL, normalized TEXT NOT NULL UNIQUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS collection_documents (
            collection_id BIGINT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
            document_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            added_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(collection_id, document_id)
        )""",
        """CREATE TABLE IF NOT EXISTS favorites (
            document_id BIGINT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS saved_searches (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, name TEXT NOT NULL, normalized TEXT NOT NULL UNIQUE,
            query TEXT NOT NULL, mode TEXT, tag TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
    )),
    (15, (
        """CREATE TABLE IF NOT EXISTS document_relations (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            source_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            target_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            kind TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(source_id, target_id, kind)
        )""",
    )),
    (16, (
        """CREATE TABLE IF NOT EXISTS entity_types (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, name TEXT NOT NULL, normalized TEXT NOT NULL UNIQUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS relation_types (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, name TEXT NOT NULL, normalized TEXT NOT NULL UNIQUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS entities (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, type_id BIGINT NOT NULL REFERENCES entity_types(id),
            name TEXT NOT NULL, normalized TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'confirmed',
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, deleted_at TIMESTAMPTZ
        )""",
        """CREATE TABLE IF NOT EXISTS entity_mentions (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, entity_id BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            document_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            start_offset BIGINT NOT NULL, end_offset BIGINT NOT NULL, quote TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, deleted_at TIMESTAMPTZ
        )""",
        """CREATE TABLE IF NOT EXISTS graph_relations (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, source_id BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            target_id BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
            type_id BIGINT NOT NULL REFERENCES relation_types(id),
            document_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            start_offset BIGINT NOT NULL, end_offset BIGINT NOT NULL, quote TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'confirmed',
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, deleted_at TIMESTAMPTZ
        )""",
        """CREATE TABLE IF NOT EXISTS graph_changes (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, action TEXT NOT NULL, object_type TEXT NOT NULL, object_id BIGINT NOT NULL,
            old_value TEXT NOT NULL DEFAULT '', new_value TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, reverted_at TIMESTAMPTZ
        )""",
        "INSERT INTO entity_types(name,normalized) VALUES ('Person','person'), ('Organisation','organisation'), ('Prozess','prozess'), ('System','system'), ('Entscheidung','entscheidung')",
        "INSERT INTO relation_types(name,normalized) VALUES ('arbeitet für','arbeitet für'), ('verantwortet','verantwortet'), ('nutzt','nutzt'), ('entscheidet über','entscheidet über'), ('gehört zu','gehört zu')",
    )),
    (17, (
        "ALTER TABLE entity_mentions ADD COLUMN status TEXT NOT NULL DEFAULT 'confirmed'",
    )),
    (18, (
        """CREATE TABLE IF NOT EXISTS ai_usage (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, provider TEXT NOT NULL, model TEXT NOT NULL,
            input_tokens BIGINT NOT NULL, output_tokens BIGINT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
    )),
    (19, (
        """CREATE TABLE IF NOT EXISTS organizations (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, name TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS accounts (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            organization_id BIGINT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            tenant_id TEXT NOT NULL, object_id TEXT NOT NULL, display_name TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL, disabled INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(tenant_id, object_id)
        )""",
        """CREATE TABLE IF NOT EXISTS grants (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            account_id BIGINT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            document_id BIGINT REFERENCES documents(id) ON DELETE CASCADE,
            collection_id BIGINT REFERENCES collections(id) ON DELETE CASCADE,
            access TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        "INSERT INTO organizations(name) VALUES ('Standard')",
    )),
    (20, (
        """CREATE TABLE IF NOT EXISTS web_sessions (
            id TEXT PRIMARY KEY, account_id BIGINT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            csrf_token TEXT NOT NULL, created_at BIGINT NOT NULL, last_seen BIGINT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS login_requests (
            state TEXT PRIMARY KEY, nonce TEXT NOT NULL, verifier TEXT NOT NULL, created_at BIGINT NOT NULL
        )""",
    )),
    (21, (
        """CREATE TABLE IF NOT EXISTS audit_log (
            id BIGINT PRIMARY KEY, created_at BIGINT NOT NULL, account_id BIGINT, event TEXT NOT NULL,
            target TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '', details TEXT NOT NULL DEFAULT '{}',
            previous_hash TEXT NOT NULL, hash TEXT NOT NULL
        )""",
        """CREATE OR REPLACE FUNCTION wissensraum.audit_log_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'Der Audit-Verlauf ist unveränderlich.'; END $$""",
        """CREATE TRIGGER audit_log_no_change BEFORE UPDATE OR DELETE ON audit_log
            FOR EACH ROW EXECUTE FUNCTION wissensraum.audit_log_immutable()""",
        """CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log
            FOR EACH STATEMENT EXECUTE FUNCTION wissensraum.audit_log_immutable()""",
    )),
    (22, tuple(f'ALTER TABLE {table} ADD COLUMN revision INTEGER NOT NULL DEFAULT 1'
               for table in ('documents', 'collections', 'entities', 'graph_relations', 'grants'))),
    # Release 3.0, step 10.7: the AI history belongs to the asking account (None: desktop, command line).
    (23, ('ALTER TABLE ai_runs ADD COLUMN account_id INTEGER',)),
)

# Columns added to an existing table by a later migration: (table, column) -> version.
POSTGRES_COLUMN_VERSIONS = {('chunks', 'search_vector'): 11, ('documents', 'search_vector'): 11,
                            ('documents', 'status'): 13, ('documents', 'owner'): 13,
                            ('documents', 'review_date'): 13, ('entity_mentions', 'status'): 17,
                            **{(table, 'revision'): 22 for table in ('documents', 'collections', 'entities',
                                                                       'graph_relations', 'grants')},
                            ('ai_runs', 'account_id'): 23}
# Columns added to an existing SQLite table by a later migration: (table, column) -> version.
SQLITE_COLUMN_VERSIONS = {('documents', 'status'): 13, ('documents', 'owner'): 13, ('documents', 'review_date'): 13,
                          ('entity_mentions', 'status'): 17,
                          **{(table, 'revision'): 22 for table in ('documents', 'collections', 'entities',
                                                                     'graph_relations', 'grants')},
                          ('ai_runs', 'account_id'): 23}
# Generated columns and their expression as PostgreSQL reports it (whitespace-insensitive).
POSTGRES_GENERATED_COLUMNS = {
    ('chunks', 'search_vector'): "to_tsvector('simple'::regconfig, content)",
    ('documents', 'search_vector'): "to_tsvector('simple'::regconfig, ((title || ' '::text) || category))",
}

POSTGRES_APPLICATION_TABLES = {
    'documents', 'chunks', 'ai_runs', 'settings', 'email_accounts', 'email_imports'
}

SQLITE_TABLE_VERSIONS = {
    'documents': 1, 'chunks': 1, 'ai_runs': 1, 'settings': 2,
    'email_accounts': 3, 'email_imports': 4, 'sources': 5,
    'import_jobs': 6, 'import_items': 6, 'import_item_errors': 6, 'document_versions': 7,
    'email_attachments': 8, 'tags': 9, 'document_tags': 9, 'rules': 9, 'rule_runs': 9,
    'rule_run_changes': 9, 'chunk_vectors': 10, 'metadata_changes': 12,
    'collections': 14, 'collection_documents': 14, 'favorites': 14, 'saved_searches': 14,
    'document_relations': 15,
    'entity_types': 16, 'relation_types': 16, 'entities': 16, 'entity_mentions': 16, 'graph_relations': 16,
    'graph_changes': 16, 'ai_usage': 18, 'organizations': 19, 'accounts': 19, 'grants': 19,
    'web_sessions': 20, 'login_requests': 20, 'audit_log': 21,
}

# Constant SQL list of all application tables, derived from the catalog so that
# schema checks cannot drift from the migrations.
_APPLICATION_TABLE_LIST = ','.join(f"'{name}'" for name in sorted(SQLITE_TABLE_VERSIONS))

SQLITE_COLUMNS = {
    'documents': (('id','INTEGER',0,None,1), ('title','TEXT',1,None,0),
                  ('category','TEXT',1,None,0), ('filename','TEXT',0,None,0),
                  ('original','BLOB',0,None,0), ('content','TEXT',1,None,0),
                  ('sha256','TEXT',1,None,0),
                  ('created_at','TEXT',1,'CURRENT_TIMESTAMP',0), ('status','TEXT',1,"'new'",0),
                  ('owner','TEXT',1,"''",0), ('review_date','TEXT',0,None,0), ('revision','INTEGER',1,'1',0)),
    'chunks': (('id','INTEGER',0,None,1), ('document_id','INTEGER',1,None,0),
               ('ordinal','INTEGER',1,None,0), ('content','TEXT',1,None,0)),
    'ai_runs': (('id','INTEGER',0,None,1), ('kind','TEXT',1,None,0),
                ('question','TEXT',1,None,0), ('answer','TEXT',1,None,0),
                ('sources_json','TEXT',1,None,0), ('model','TEXT',1,None,0),
                ('created_at','TEXT',1,'CURRENT_TIMESTAMP',0), ('account_id','INTEGER',0,None,0)),
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
    'import_jobs': (('id','INTEGER',0,None,1), ('kind','TEXT',1,None,0),
                    ('root','TEXT',1,"''",0), ('category','TEXT',1,"'Allgemein'",0),
                    ('status','TEXT',1,None,0), ('created_at','TEXT',1,'CURRENT_TIMESTAMP',0),
                    ('updated_at','TEXT',1,'CURRENT_TIMESTAMP',0)),
    'import_items': (('id','INTEGER',0,None,1), ('job_id','INTEGER',1,None,0),
                     ('ordinal','INTEGER',1,None,0), ('path','TEXT',1,None,0),
                     ('locator','TEXT',1,None,0), ('status','TEXT',1,"'pending'",0),
                     ('reason','TEXT',1,"''",0), ('size','INTEGER',0,None,0),
                     ('sha256','TEXT',1,"''",0), ('document_id','INTEGER',0,None,0),
                     ('attempts','INTEGER',1,'0',0),
                     ('updated_at','TEXT',1,'CURRENT_TIMESTAMP',0)),
    'import_item_errors': (('id','INTEGER',0,None,1), ('item_id','INTEGER',1,None,0),
                           ('attempt','INTEGER',1,None,0), ('reason','TEXT',1,None,0),
                           ('message','TEXT',1,None,0),
                           ('created_at','TEXT',1,'CURRENT_TIMESTAMP',0)),
    'document_versions': (('document_id','INTEGER',0,None,1), ('lineage_id','INTEGER',1,None,0),
                          ('version','INTEGER',1,None,0)),
    'email_attachments': (('document_id','INTEGER',0,None,1), ('email_document_id','INTEGER',0,None,0),
                          ('name','TEXT',1,None,0), ('content_type','TEXT',1,"''",0),
                          ('size','INTEGER',0,None,0)),
    'tags': (('id','INTEGER',0,None,1), ('name','TEXT',1,None,0), ('normalized','TEXT',1,None,0)),
    'document_tags': (('document_id','INTEGER',1,None,1), ('tag_id','INTEGER',1,None,2)),
    'rules': (('id','INTEGER',0,None,1), ('position','INTEGER',1,None,0), ('name','TEXT',1,None,0),
              ('enabled','INTEGER',1,'1',0), ('source_type','TEXT',1,"''",0),
              ('path_pattern','TEXT',1,"''",0), ('sender_domain','TEXT',1,"''",0),
              ('extension','TEXT',1,"''",0), ('keyword','TEXT',1,"''",0),
              ('category','TEXT',1,"''",0), ('tags','TEXT',1,"''",0),
              ('created_at','TEXT',1,'CURRENT_TIMESTAMP',0)),
    'rule_runs': (('id','INTEGER',0,None,1), ('status','TEXT',1,"'applied'",0),
                  ('created_at','TEXT',1,'CURRENT_TIMESTAMP',0), ('reverted_at','TEXT',0,None,0)),
    'rule_run_changes': (('run_id','INTEGER',1,None,1), ('document_id','INTEGER',1,None,2),
                         ('previous_category','TEXT',1,None,0), ('new_category','TEXT',1,None,0),
                         ('added_tags','TEXT',1,"''",0)),
    'chunk_vectors': (('chunk_id','INTEGER',0,None,1), ('model','TEXT',1,None,0),
                      ('dimension','INTEGER',1,None,0), ('vector','BLOB',1,None,0)),
    'metadata_changes': (('id','INTEGER',0,None,1), ('document_id','INTEGER',1,None,0),
                         ('field','TEXT',1,None,0), ('old_value','TEXT',1,"''",0),
                         ('new_value','TEXT',1,"''",0), ('origin','TEXT',1,None,0),
                         ('run_id','INTEGER',0,None,0), ('created_at','TEXT',1,'CURRENT_TIMESTAMP',0)),
    'collections': (('id','INTEGER',0,None,1), ('name','TEXT',1,None,0), ('normalized','TEXT',1,None,0),
                    ('created_at','TEXT',1,'CURRENT_TIMESTAMP',0), ('revision','INTEGER',1,'1',0)),
    'collection_documents': (('collection_id','INTEGER',1,None,1), ('document_id','INTEGER',1,None,2),
                             ('added_at','TEXT',1,'CURRENT_TIMESTAMP',0)),
    'favorites': (('document_id','INTEGER',0,None,1), ('created_at','TEXT',1,'CURRENT_TIMESTAMP',0)),
    'saved_searches': (('id','INTEGER',0,None,1), ('name','TEXT',1,None,0), ('normalized','TEXT',1,None,0),
                       ('query','TEXT',1,None,0), ('mode','TEXT',0,None,0), ('tag','TEXT',0,None,0),
                       ('created_at','TEXT',1,'CURRENT_TIMESTAMP',0)),
    'document_relations': (('id','INTEGER',0,None,1), ('source_id','INTEGER',1,None,0),
                           ('target_id','INTEGER',1,None,0), ('kind','TEXT',1,None,0),
                           ('note','TEXT',1,"''",0), ('created_at','TEXT',1,'CURRENT_TIMESTAMP',0)),
    'entity_types': (('id','INTEGER',0,None,1), ('name','TEXT',1,None,0), ('normalized','TEXT',1,None,0),
                     ('created_at','TEXT',1,'CURRENT_TIMESTAMP',0)),
    'relation_types': (('id','INTEGER',0,None,1), ('name','TEXT',1,None,0), ('normalized','TEXT',1,None,0),
                       ('created_at','TEXT',1,'CURRENT_TIMESTAMP',0)),
    'entities': (('id','INTEGER',0,None,1), ('type_id','INTEGER',1,None,0), ('name','TEXT',1,None,0),
                 ('normalized','TEXT',1,None,0), ('status','TEXT',1,"'confirmed'",0),
                 ('created_at','TEXT',1,'CURRENT_TIMESTAMP',0), ('deleted_at','TEXT',0,None,0), ('revision','INTEGER',1,'1',0)),
    'entity_mentions': (('id','INTEGER',0,None,1), ('entity_id','INTEGER',1,None,0), ('document_id','INTEGER',1,None,0),
                        ('start_offset','INTEGER',1,None,0), ('end_offset','INTEGER',1,None,0),
                        ('quote','TEXT',1,None,0), ('created_at','TEXT',1,'CURRENT_TIMESTAMP',0),
                        ('deleted_at','TEXT',0,None,0), ('status','TEXT',1,"'confirmed'",0)),
    'graph_relations': (('id','INTEGER',0,None,1), ('source_id','INTEGER',1,None,0), ('target_id','INTEGER',1,None,0),
                        ('type_id','INTEGER',1,None,0), ('document_id','INTEGER',1,None,0),
                        ('start_offset','INTEGER',1,None,0), ('end_offset','INTEGER',1,None,0),
                        ('quote','TEXT',1,None,0), ('status','TEXT',1,"'confirmed'",0),
                        ('created_at','TEXT',1,'CURRENT_TIMESTAMP',0), ('deleted_at','TEXT',0,None,0), ('revision','INTEGER',1,'1',0)),
    'graph_changes': (('id','INTEGER',0,None,1), ('action','TEXT',1,None,0), ('object_type','TEXT',1,None,0),
                      ('object_id','INTEGER',1,None,0), ('old_value','TEXT',1,"''",0), ('new_value','TEXT',1,"''",0),
                      ('created_at','TEXT',1,'CURRENT_TIMESTAMP',0), ('reverted_at','TEXT',0,None,0)),
    'ai_usage': (('id','INTEGER',0,None,1), ('provider','TEXT',1,None,0), ('model','TEXT',1,None,0),
                 ('input_tokens','INTEGER',1,None,0), ('output_tokens','INTEGER',1,None,0),
                 ('created_at','TEXT',1,'CURRENT_TIMESTAMP',0)),
    'organizations': (('id','INTEGER',0,None,1), ('name','TEXT',1,None,0), ('created_at','TEXT',1,'CURRENT_TIMESTAMP',0)),
    'accounts': (('id','INTEGER',0,None,1), ('organization_id','INTEGER',1,None,0), ('tenant_id','TEXT',1,None,0),
                 ('object_id','TEXT',1,None,0), ('display_name','TEXT',1,"''",0), ('role','TEXT',1,None,0),
                 ('disabled','INTEGER',1,'0',0), ('version','INTEGER',1,'1',0),
                 ('created_at','TEXT',1,'CURRENT_TIMESTAMP',0)),
    'grants': (('id','INTEGER',0,None,1), ('account_id','INTEGER',1,None,0), ('document_id','INTEGER',0,None,0),
               ('collection_id','INTEGER',0,None,0), ('access','TEXT',1,None,0),
               ('created_at','TEXT',1,'CURRENT_TIMESTAMP',0), ('revision','INTEGER',1,'1',0)),
    'web_sessions': (('id','TEXT',0,None,1), ('account_id','INTEGER',1,None,0), ('csrf_token','TEXT',1,None,0),
                     ('created_at','INTEGER',1,None,0), ('last_seen','INTEGER',1,None,0)),
    'login_requests': (('state','TEXT',0,None,1), ('nonce','TEXT',1,None,0), ('verifier','TEXT',1,None,0),
                       ('created_at','INTEGER',1,None,0)),
    'audit_log': (('id','INTEGER',0,None,1), ('created_at','INTEGER',1,None,0), ('account_id','INTEGER',0,None,0),
                  ('event','TEXT',1,None,0), ('target','TEXT',1,"''",0), ('source','TEXT',1,"''",0),
                  ('details','TEXT',1,"'{}'",0), ('previous_hash','TEXT',1,None,0), ('hash','TEXT',1,None,0)),
}

SQLITE_UNIQUE_CONSTRAINTS = {
    'documents': {('sha256',)}, 'chunks': {('document_id','ordinal')},
    'ai_runs': set(), 'settings': set(), 'email_accounts': set(),
    'email_imports': set(), 'sources': set(),
    'import_jobs': set(), 'import_items': {('job_id','ordinal')}, 'import_item_errors': set(),
    'document_versions': {('lineage_id','version')}, 'email_attachments': set(),
    'tags': {('normalized',)}, 'document_tags': set(), 'rules': set(), 'rule_runs': set(),
    'rule_run_changes': set(), 'chunk_vectors': set(), 'metadata_changes': set(),
    'collections': {('normalized',)}, 'collection_documents': set(), 'favorites': set(),
    'saved_searches': {('normalized',)},
    'document_relations': {('source_id','target_id','kind')},
    'entity_types': {('normalized',)}, 'relation_types': {('normalized',)}, 'entities': set(),
    'entity_mentions': set(), 'graph_relations': set(), 'graph_changes': set(), 'ai_usage': set(),
    'organizations': set(), 'accounts': {('tenant_id', 'object_id')}, 'grants': set(),
    'web_sessions': set(), 'login_requests': set(), 'audit_log': set(),
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
    'import_jobs': set(),
    'import_items': {
        ('job_id','import_jobs','id','NO ACTION','CASCADE'),
        ('document_id','documents','id','NO ACTION','SET NULL'),
    },
    'import_item_errors': {('item_id','import_items','id','NO ACTION','CASCADE')},
    'document_versions': {('document_id','documents','id','NO ACTION','CASCADE')},
    'email_attachments': {('document_id','documents','id','NO ACTION','CASCADE'),
                          ('email_document_id','documents','id','NO ACTION','SET NULL')},
    'tags': set(),
    'document_tags': {('document_id','documents','id','NO ACTION','CASCADE'),
                      ('tag_id','tags','id','NO ACTION','CASCADE')},
    'rules': set(), 'rule_runs': set(),
    'rule_run_changes': {('run_id','rule_runs','id','NO ACTION','CASCADE'),
                         ('document_id','documents','id','NO ACTION','CASCADE')},
    'chunk_vectors': {('chunk_id','chunks','id','NO ACTION','CASCADE')},
    'metadata_changes': {('document_id','documents','id','NO ACTION','CASCADE'),
                         ('run_id','rule_runs','id','NO ACTION','SET NULL')},
    'collections': set(),
    'collection_documents': {('collection_id','collections','id','NO ACTION','CASCADE'),
                             ('document_id','documents','id','NO ACTION','CASCADE')},
    'favorites': {('document_id','documents','id','NO ACTION','CASCADE')},
    'saved_searches': set(),
    'document_relations': {('source_id','documents','id','NO ACTION','CASCADE'),
                           ('target_id','documents','id','NO ACTION','CASCADE')},
    'entity_types': set(), 'relation_types': set(),
    'entities': {('type_id','entity_types','id','NO ACTION','NO ACTION')},
    'entity_mentions': {('entity_id','entities','id','NO ACTION','CASCADE'),
                        ('document_id','documents','id','NO ACTION','CASCADE')},
    'graph_relations': {('source_id','entities','id','NO ACTION','CASCADE'),
                        ('target_id','entities','id','NO ACTION','CASCADE'),
                        ('type_id','relation_types','id','NO ACTION','NO ACTION'),
                        ('document_id','documents','id','NO ACTION','CASCADE')},
    'graph_changes': set(), 'ai_usage': set(), 'organizations': set(),
    'accounts': {('organization_id','organizations','id','NO ACTION','CASCADE')},
    'grants': {('account_id','accounts','id','NO ACTION','CASCADE'), ('document_id','documents','id','NO ACTION','CASCADE'),
               ('collection_id','collections','id','NO ACTION','CASCADE')},
    'web_sessions': {('account_id','accounts','id','NO ACTION','CASCADE')}, 'login_requests': set(),
    'audit_log': set(),
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
    ('ai_runs','account_id','integer','YES','NO',None,None),
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
    ('import_jobs','id','bigint','NO','YES',None,'ALWAYS'),
    ('import_jobs','kind','text','NO','NO',None,None),
    ('import_jobs','root','text','NO','NO',"''::text",None),
    ('import_jobs','category','text','NO','NO',"'Allgemein'::text",None),
    ('import_jobs','status','text','NO','NO',None,None),
    ('import_jobs','created_at','timestamp with time zone','NO','NO','CURRENT_TIMESTAMP',None),
    ('import_jobs','updated_at','timestamp with time zone','NO','NO','CURRENT_TIMESTAMP',None),
    ('import_items','id','bigint','NO','YES',None,'ALWAYS'),
    ('import_items','job_id','bigint','NO','NO',None,None),
    ('import_items','ordinal','integer','NO','NO',None,None),
    ('import_items','path','text','NO','NO',None,None),
    ('import_items','locator','text','NO','NO',None,None),
    ('import_items','status','text','NO','NO',"'pending'::text",None),
    ('import_items','reason','text','NO','NO',"''::text",None),
    ('import_items','size','bigint','YES','NO',None,None),
    ('import_items','sha256','text','NO','NO',"''::text",None),
    ('import_items','document_id','bigint','YES','NO',None,None),
    ('import_items','attempts','integer','NO','NO','0',None),
    ('import_items','updated_at','timestamp with time zone','NO','NO','CURRENT_TIMESTAMP',None),
    ('import_item_errors','id','bigint','NO','YES',None,'ALWAYS'),
    ('import_item_errors','item_id','bigint','NO','NO',None,None),
    ('import_item_errors','attempt','integer','NO','NO',None,None),
    ('import_item_errors','reason','text','NO','NO',None,None),
    ('import_item_errors','message','text','NO','NO',None,None),
    ('import_item_errors','created_at','timestamp with time zone','NO','NO','CURRENT_TIMESTAMP',None),
    ('document_versions','document_id','bigint','NO','NO',None,None),
    ('document_versions','lineage_id','bigint','NO','NO',None,None),
    ('document_versions','version','integer','NO','NO',None,None),
    ('email_attachments','document_id','bigint','NO','NO',None,None),
    ('email_attachments','email_document_id','bigint','YES','NO',None,None),
    ('email_attachments','name','text','NO','NO',None,None),
    ('email_attachments','content_type','text','NO','NO',"''::text",None),
    ('email_attachments','size','bigint','YES','NO',None,None),
    ('tags','id','bigint','NO','YES',None,'ALWAYS'),
    ('tags','name','text','NO','NO',None,None),
    ('tags','normalized','text','NO','NO',None,None),
    ('document_tags','document_id','bigint','NO','NO',None,None),
    ('document_tags','tag_id','bigint','NO','NO',None,None),
    ('rules','id','bigint','NO','YES',None,'ALWAYS'),
    ('rules','position','integer','NO','NO',None,None),
    ('rules','name','text','NO','NO',None,None),
    ('rules','enabled','integer','NO','NO','1',None),
    ('rules','source_type','text','NO','NO',"''::text",None),
    ('rules','path_pattern','text','NO','NO',"''::text",None),
    ('rules','sender_domain','text','NO','NO',"''::text",None),
    ('rules','extension','text','NO','NO',"''::text",None),
    ('rules','keyword','text','NO','NO',"''::text",None),
    ('rules','category','text','NO','NO',"''::text",None),
    ('rules','tags','text','NO','NO',"''::text",None),
    ('rules','created_at','timestamp with time zone','NO','NO','CURRENT_TIMESTAMP',None),
    ('rule_runs','id','bigint','NO','YES',None,'ALWAYS'),
    ('rule_runs','status','text','NO','NO',"'applied'::text",None),
    ('rule_runs','created_at','timestamp with time zone','NO','NO','CURRENT_TIMESTAMP',None),
    ('rule_runs','reverted_at','timestamp with time zone','YES','NO',None,None),
    ('rule_run_changes','run_id','bigint','NO','NO',None,None),
    ('rule_run_changes','document_id','bigint','NO','NO',None,None),
    ('rule_run_changes','previous_category','text','NO','NO',None,None),
    ('rule_run_changes','new_category','text','NO','NO',None,None),
    ('rule_run_changes','added_tags','text','NO','NO',"''::text",None),
    ('chunk_vectors','chunk_id','bigint','NO','NO',None,None),
    ('chunk_vectors','model','text','NO','NO',None,None),
    ('chunk_vectors','dimension','integer','NO','NO',None,None),
    ('chunk_vectors','vector','bytea','NO','NO',None,None),
    ('metadata_changes','id','bigint','NO','YES',None,'ALWAYS'),
    ('metadata_changes','document_id','bigint','NO','NO',None,None),
    ('metadata_changes','field','text','NO','NO',None,None),
    ('metadata_changes','old_value','text','NO','NO',"''::text",None),
    ('metadata_changes','new_value','text','NO','NO',"''::text",None),
    ('metadata_changes','origin','text','NO','NO',None,None),
    ('metadata_changes','run_id','bigint','YES','NO',None,None),
    ('metadata_changes','created_at','timestamp with time zone','NO','NO','CURRENT_TIMESTAMP',None),
    ('chunks','search_vector','tsvector','YES','NO',None,None),
    ('documents','search_vector','tsvector','YES','NO',None,None),
    ('documents','status','text','NO','NO',"'new'::text",None),
    ('documents','owner','text','NO','NO',"''::text",None),
    ('documents','review_date','text','YES','NO',None,None),
    ('documents','revision','integer','NO','NO','1',None),
    ('collections','id','bigint','NO','YES',None,'ALWAYS'),
    ('collections','name','text','NO','NO',None,None),
    ('collections','normalized','text','NO','NO',None,None),
    ('collections','created_at','timestamp with time zone','NO','NO','CURRENT_TIMESTAMP',None),
    ('collections','revision','integer','NO','NO','1',None),
    ('collection_documents','collection_id','bigint','NO','NO',None,None),
    ('collection_documents','document_id','bigint','NO','NO',None,None),
    ('collection_documents','added_at','timestamp with time zone','NO','NO','CURRENT_TIMESTAMP',None),
    ('favorites','document_id','bigint','NO','NO',None,None),
    ('favorites','created_at','timestamp with time zone','NO','NO','CURRENT_TIMESTAMP',None),
    ('saved_searches','id','bigint','NO','YES',None,'ALWAYS'),
    ('saved_searches','name','text','NO','NO',None,None),
    ('saved_searches','normalized','text','NO','NO',None,None),
    ('saved_searches','query','text','NO','NO',None,None),
    ('saved_searches','mode','text','YES','NO',None,None),
    ('saved_searches','tag','text','YES','NO',None,None),
    ('saved_searches','created_at','timestamp with time zone','NO','NO','CURRENT_TIMESTAMP',None),
    ('document_relations','id','bigint','NO','YES',None,'ALWAYS'),
    ('document_relations','source_id','bigint','NO','NO',None,None),
    ('document_relations','target_id','bigint','NO','NO',None,None),
    ('document_relations','kind','text','NO','NO',None,None),
    ('document_relations','note','text','NO','NO',"''::text",None),
    ('document_relations','created_at','timestamp with time zone','NO','NO','CURRENT_TIMESTAMP',None),
    ('entity_types','id','bigint','NO','YES',None,'ALWAYS'),
    ('entity_types','name','text','NO','NO',None,None),
    ('entity_types','normalized','text','NO','NO',None,None),
    ('entity_types','created_at','timestamp with time zone','NO','NO','CURRENT_TIMESTAMP',None),
    ('relation_types','id','bigint','NO','YES',None,'ALWAYS'),
    ('relation_types','name','text','NO','NO',None,None),
    ('relation_types','normalized','text','NO','NO',None,None),
    ('relation_types','created_at','timestamp with time zone','NO','NO','CURRENT_TIMESTAMP',None),
    ('entities','id','bigint','NO','YES',None,'ALWAYS'),
    ('entities','type_id','bigint','NO','NO',None,None),
    ('entities','name','text','NO','NO',None,None),
    ('entities','normalized','text','NO','NO',None,None),
    ('entities','status','text','NO','NO',"'confirmed'::text",None),
    ('entities','created_at','timestamp with time zone','NO','NO','CURRENT_TIMESTAMP',None),
    ('entities','deleted_at','timestamp with time zone','YES','NO',None,None),
    ('entities','revision','integer','NO','NO','1',None),
    ('entity_mentions','id','bigint','NO','YES',None,'ALWAYS'),
    ('entity_mentions','entity_id','bigint','NO','NO',None,None),
    ('entity_mentions','document_id','bigint','NO','NO',None,None),
    ('entity_mentions','start_offset','bigint','NO','NO',None,None),
    ('entity_mentions','end_offset','bigint','NO','NO',None,None),
    ('entity_mentions','quote','text','NO','NO',None,None),
    ('entity_mentions','created_at','timestamp with time zone','NO','NO','CURRENT_TIMESTAMP',None),
    ('entity_mentions','deleted_at','timestamp with time zone','YES','NO',None,None),
    ('entity_mentions','status','text','NO','NO',"'confirmed'::text",None),
    ('graph_relations','id','bigint','NO','YES',None,'ALWAYS'),
    ('graph_relations','source_id','bigint','NO','NO',None,None),
    ('graph_relations','target_id','bigint','NO','NO',None,None),
    ('graph_relations','type_id','bigint','NO','NO',None,None),
    ('graph_relations','document_id','bigint','NO','NO',None,None),
    ('graph_relations','start_offset','bigint','NO','NO',None,None),
    ('graph_relations','end_offset','bigint','NO','NO',None,None),
    ('graph_relations','quote','text','NO','NO',None,None),
    ('graph_relations','status','text','NO','NO',"'confirmed'::text",None),
    ('graph_relations','created_at','timestamp with time zone','NO','NO','CURRENT_TIMESTAMP',None),
    ('graph_relations','deleted_at','timestamp with time zone','YES','NO',None,None),
    ('graph_relations','revision','integer','NO','NO','1',None),
    ('graph_changes','id','bigint','NO','YES',None,'ALWAYS'),
    ('graph_changes','action','text','NO','NO',None,None),
    ('graph_changes','object_type','text','NO','NO',None,None),
    ('graph_changes','object_id','bigint','NO','NO',None,None),
    ('graph_changes','old_value','text','NO','NO',"''::text",None),
    ('graph_changes','new_value','text','NO','NO',"''::text",None),
    ('graph_changes','created_at','timestamp with time zone','NO','NO','CURRENT_TIMESTAMP',None),
    ('graph_changes','reverted_at','timestamp with time zone','YES','NO',None,None),
    ('ai_usage','id','bigint','NO','YES',None,'ALWAYS'),
    ('ai_usage','provider','text','NO','NO',None,None),
    ('ai_usage','model','text','NO','NO',None,None),
    ('ai_usage','input_tokens','bigint','NO','NO',None,None),
    ('ai_usage','output_tokens','bigint','NO','NO',None,None),
    ('ai_usage','created_at','timestamp with time zone','NO','NO','CURRENT_TIMESTAMP',None),
    ('organizations','id','bigint','NO','YES',None,'ALWAYS'),
    ('organizations','name','text','NO','NO',None,None),
    ('organizations','created_at','timestamp with time zone','NO','NO','CURRENT_TIMESTAMP',None),
    ('accounts','id','bigint','NO','YES',None,'ALWAYS'),
    ('accounts','organization_id','bigint','NO','NO',None,None),
    ('accounts','tenant_id','text','NO','NO',None,None),
    ('accounts','object_id','text','NO','NO',None,None),
    ('accounts','display_name','text','NO','NO',"''::text",None),
    ('accounts','role','text','NO','NO',None,None),
    ('accounts','disabled','integer','NO','NO','0',None),
    ('accounts','version','integer','NO','NO','1',None),
    ('accounts','created_at','timestamp with time zone','NO','NO','CURRENT_TIMESTAMP',None),
    ('grants','id','bigint','NO','YES',None,'ALWAYS'),
    ('grants','account_id','bigint','NO','NO',None,None),
    ('grants','document_id','bigint','YES','NO',None,None),
    ('grants','collection_id','bigint','YES','NO',None,None),
    ('grants','access','text','NO','NO',None,None),
    ('grants','created_at','timestamp with time zone','NO','NO','CURRENT_TIMESTAMP',None),
    ('grants','revision','integer','NO','NO','1',None),
    ('web_sessions','id','text','NO','NO',None,None),
    ('web_sessions','account_id','bigint','NO','NO',None,None),
    ('web_sessions','csrf_token','text','NO','NO',None,None),
    ('web_sessions','created_at','bigint','NO','NO',None,None),
    ('web_sessions','last_seen','bigint','NO','NO',None,None),
    ('login_requests','state','text','NO','NO',None,None),
    ('login_requests','nonce','text','NO','NO',None,None),
    ('login_requests','verifier','text','NO','NO',None,None),
    ('login_requests','created_at','bigint','NO','NO',None,None),
    ('audit_log','id','bigint','NO','NO',None,None),
    ('audit_log','created_at','bigint','NO','NO',None,None),
    ('audit_log','account_id','bigint','YES','NO',None,None),
    ('audit_log','event','text','NO','NO',None,None),
    ('audit_log','target','text','NO','NO',"''::text",None),
    ('audit_log','source','text','NO','NO',"''::text",None),
    ('audit_log','details','text','NO','NO',"'{}'::text",None),
    ('audit_log','previous_hash','text','NO','NO',None,None),
    ('audit_log','hash','text','NO','NO',None,None),
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
    ('import_jobs', 'p', ('id',), None, (), ' '),
    ('import_items', 'p', ('id',), None, (), ' '),
    ('import_items', 'u', ('job_id', 'ordinal'), None, (), ' '),
    ('import_items', 'f', ('job_id',), 'import_jobs', ('id',), 'c'),
    ('import_items', 'f', ('document_id',), 'documents', ('id',), 'n'),
    ('import_item_errors', 'p', ('id',), None, (), ' '),
    ('import_item_errors', 'f', ('item_id',), 'import_items', ('id',), 'c'),
    ('document_versions', 'p', ('document_id',), None, (), ' '),
    ('document_versions', 'u', ('lineage_id', 'version'), None, (), ' '),
    ('document_versions', 'f', ('document_id',), 'documents', ('id',), 'c'),
    ('email_attachments', 'p', ('document_id',), None, (), ' '),
    ('email_attachments', 'f', ('document_id',), 'documents', ('id',), 'c'),
    ('email_attachments', 'f', ('email_document_id',), 'documents', ('id',), 'n'),
    ('tags', 'p', ('id',), None, (), ' '),
    ('tags', 'u', ('normalized',), None, (), ' '),
    ('document_tags', 'p', ('document_id', 'tag_id'), None, (), ' '),
    ('document_tags', 'f', ('document_id',), 'documents', ('id',), 'c'),
    ('document_tags', 'f', ('tag_id',), 'tags', ('id',), 'c'),
    ('rules', 'p', ('id',), None, (), ' '),
    ('rule_runs', 'p', ('id',), None, (), ' '),
    ('rule_run_changes', 'p', ('run_id', 'document_id'), None, (), ' '),
    ('rule_run_changes', 'f', ('run_id',), 'rule_runs', ('id',), 'c'),
    ('rule_run_changes', 'f', ('document_id',), 'documents', ('id',), 'c'),
    ('chunk_vectors', 'p', ('chunk_id',), None, (), ' '),
    ('chunk_vectors', 'f', ('chunk_id',), 'chunks', ('id',), 'c'),
    ('metadata_changes', 'p', ('id',), None, (), ' '),
    ('metadata_changes', 'f', ('document_id',), 'documents', ('id',), 'c'),
    ('metadata_changes', 'f', ('run_id',), 'rule_runs', ('id',), 'n'),
    ('collections', 'p', ('id',), None, (), ' '),
    ('collections', 'u', ('normalized',), None, (), ' '),
    ('collection_documents', 'p', ('collection_id', 'document_id'), None, (), ' '),
    ('collection_documents', 'f', ('collection_id',), 'collections', ('id',), 'c'),
    ('collection_documents', 'f', ('document_id',), 'documents', ('id',), 'c'),
    ('favorites', 'p', ('document_id',), None, (), ' '),
    ('favorites', 'f', ('document_id',), 'documents', ('id',), 'c'),
    ('saved_searches', 'p', ('id',), None, (), ' '),
    ('saved_searches', 'u', ('normalized',), None, (), ' '),
    ('document_relations', 'p', ('id',), None, (), ' '),
    ('document_relations', 'u', ('source_id', 'target_id', 'kind'), None, (), ' '),
    ('document_relations', 'f', ('source_id',), 'documents', ('id',), 'c'),
    ('document_relations', 'f', ('target_id',), 'documents', ('id',), 'c'),
    ('entity_types', 'p', ('id',), None, (), ' '),
    ('entity_types', 'u', ('normalized',), None, (), ' '),
    ('relation_types', 'p', ('id',), None, (), ' '),
    ('relation_types', 'u', ('normalized',), None, (), ' '),
    ('entities', 'p', ('id',), None, (), ' '),
    ('entities', 'f', ('type_id',), 'entity_types', ('id',), 'a'),
    ('entity_mentions', 'p', ('id',), None, (), ' '),
    ('entity_mentions', 'f', ('entity_id',), 'entities', ('id',), 'c'),
    ('entity_mentions', 'f', ('document_id',), 'documents', ('id',), 'c'),
    ('graph_relations', 'p', ('id',), None, (), ' '),
    ('graph_relations', 'f', ('source_id',), 'entities', ('id',), 'c'),
    ('graph_relations', 'f', ('target_id',), 'entities', ('id',), 'c'),
    ('graph_relations', 'f', ('type_id',), 'relation_types', ('id',), 'a'),
    ('graph_relations', 'f', ('document_id',), 'documents', ('id',), 'c'),
    ('graph_changes', 'p', ('id',), None, (), ' '),
    ('ai_usage', 'p', ('id',), None, (), ' '),
    ('organizations', 'p', ('id',), None, (), ' '),
    ('accounts', 'p', ('id',), None, (), ' '),
    ('accounts', 'f', ('organization_id',), 'organizations', ('id',), 'c'),
    ('accounts', 'u', ('tenant_id', 'object_id'), None, (), ' '),
    ('grants', 'p', ('id',), None, (), ' '),
    ('grants', 'f', ('account_id',), 'accounts', ('id',), 'c'),
    ('grants', 'f', ('document_id',), 'documents', ('id',), 'c'),
    ('grants', 'f', ('collection_id',), 'collections', ('id',), 'c'),
    ('web_sessions', 'p', ('id',), None, (), ' '),
    ('web_sessions', 'f', ('account_id',), 'accounts', ('id',), 'c'),
    ('login_requests', 'p', ('state',), None, (), ' '),
    ('audit_log', 'p', ('id',), None, (), ' '),
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
        if columns != tuple(column for column in SQLITE_COLUMNS[table]
                            if SQLITE_COLUMN_VERSIONS.get((table, column[0]), 0) <= version):
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
    if version >= 21:
        triggers = {row[0]: _canonical_sql(row[1]) for row in db.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='audit_log'")}
        if triggers != {name: _canonical_sql(sql) for name, sql in SQLITE_AUDIT_TRIGGERS.items()}:
            raise ValueError('Der Schutz des Audit-Verlaufs fehlt oder wurde verändert.')


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
+        is_identity, column_default, identity_generation, generation_expression
+        FROM information_schema.columns
+        WHERE table_schema='wissensraum'
+          AND table_name IN (""".replace('\n+', '\n') + _APPLICATION_TABLE_LIST + """)
        ORDER BY table_name, ordinal_position""").fetchall()
    actual_columns = {
        (_value(row, 'table_name'), _value(row, 'column_name', 1),
         _value(row, 'data_type', 2), _value(row, 'is_nullable', 3),
         _value(row, 'is_identity', 4), _value(row, 'column_default', 5),
         _value(row, 'identity_generation', 6))
        for row in columns
    }
    expected_columns = {column for column in POSTGRES_LEGACY_COLUMNS
                        if column[0] in expected_tables
                        and POSTGRES_COLUMN_VERSIONS.get(column[:2], 0) <= version}
    generated = {(_value(row, 'table_name'), _value(row, 'column_name', 1)):
                 ''.join(str(_value(row, 'generation_expression', 7)).lower().split())
                 for row in columns if _value(row, 'generation_expression', 7)}
    expected_generated = {key: ''.join(expression.lower().split())
                          for key, expression in POSTGRES_GENERATED_COLUMNS.items()
                          if POSTGRES_COLUMN_VERSIONS[key] <= version}

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
          AND rel.relname IN (""" + _APPLICATION_TABLE_LIST + ')').fetchall()
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
        WHERE schemaname='wissensraum' AND tablename IN ('chunks','documents')""").fetchall()
    index_definitions = {
        _value(row, 'index_name', 1): str(_value(row, 'index_definition', 2) or '').lower()
        for row in indexes
    }
    document_index = ''.join(index_definitions.get('chunks_document_id_idx', '').split())
    search_index = index_definitions.get('chunks_search_idx', '')
    title_index = index_definitions.get('documents_search_idx', '')
    if version >= 11:
        search_valid = ('using gin (search_vector)' in search_index
                        and 'using gin (search_vector)' in title_index)
    else:
        search_valid = 'using gin' in search_index and 'to_tsvector' in search_index
    indexes_valid = version < 1 or ('(document_id)' in document_index and search_valid)
    if (actual_columns != expected_columns
            or actual_constraints != expected_constraints
            or generated != expected_generated
            or not indexes_valid):
        prefix = 'Legacy-' if legacy else ''
        raise ValueError(f'{prefix}PostgreSQL-Schema ist unvollständig oder inkompatibel.')
    if version >= 21:
        triggers = {_value(row, 'tgname'): _value(row, 'enabled', 1) for row in db.execute(
            """SELECT t.tgname, t.tgenabled AS enabled FROM pg_catalog.pg_trigger t
            JOIN pg_catalog.pg_class c ON c.oid=t.tgrelid JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='wissensraum' AND c.relname='audit_log' AND NOT t.tgisinternal""").fetchall()}
        if triggers != {'audit_log_no_change': 'O', 'audit_log_no_truncate': 'O'}:
            raise ValueError('Der Schutz des Audit-Verlaufs fehlt oder wurde verändert.')


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


def check_postgres_current(db):
    """Check a current schema without any DDL (step 10.8): the server's runtime role has no CREATE right."""
    try:
        db.execute('SET search_path TO wissensraum, public')
        history = [_value(row, 'version') for row in
                   db.execute('SELECT version FROM wissensraum.schema_migrations ORDER BY version').fetchall()]
    except Exception:
        history = None
    if history != list(range(1, CURRENT_SCHEMA_VERSION + 1)):
        raise ValueError('Das Serverschema ist nicht aktuell. Zuerst „python server.py migrate“ ausführen.')
    _validate_postgres_schema(db, CURRENT_SCHEMA_VERSION)


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

