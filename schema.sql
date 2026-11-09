-- SQLite / FTS5 schema snapshot · Release 2.3 · Version 17 (version 11 changes only PostgreSQL)
-- Runtime upgrades are defined as ordered migrations in migrations.py.

PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS documents (
 id INTEGER PRIMARY KEY, title TEXT NOT NULL, category TEXT NOT NULL,
 filename TEXT, original BLOB, content TEXT NOT NULL,
 sha256 TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 status TEXT NOT NULL DEFAULT 'new', owner TEXT NOT NULL DEFAULT '', review_date TEXT, revision INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS chunks (
 id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
 ordinal INTEGER NOT NULL, content TEXT NOT NULL, UNIQUE(document_id, ordinal)
);
CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
 title, category, content, content='chunks_search', content_rowid='id',
 tokenize='unicode61 remove_diacritics 2'
);
CREATE VIEW IF NOT EXISTS chunks_search AS
 SELECT c.id, d.title, d.category, c.content FROM chunks c JOIN documents d ON d.id=c.document_id;
CREATE TRIGGER IF NOT EXISTS chunk_insert AFTER INSERT ON chunks BEGIN
 INSERT INTO search_index(rowid,title,category,content)
 SELECT NEW.id,title,category,NEW.content FROM documents WHERE id=NEW.document_id;
END;
CREATE TRIGGER IF NOT EXISTS chunk_delete AFTER DELETE ON chunks BEGIN
 INSERT INTO search_index(search_index,rowid,title,category,content)
 SELECT 'delete',OLD.id,title,category,OLD.content FROM documents WHERE id=OLD.document_id;
END;
CREATE TABLE IF NOT EXISTS ai_runs (
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL, question TEXT NOT NULL, answer TEXT NOT NULL,
 sources_json TEXT NOT NULL, model TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, account_id INTEGER
);
CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS email_accounts (
 id TEXT PRIMARY KEY, label TEXT NOT NULL, provider TEXT NOT NULL,
 email TEXT NOT NULL, username TEXT NOT NULL, host TEXT NOT NULL DEFAULT '', port INTEGER NOT NULL DEFAULT 0,
 client_id TEXT NOT NULL DEFAULT '', tenant TEXT NOT NULL DEFAULT 'common',
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS email_imports (
 account_id TEXT NOT NULL REFERENCES email_accounts(id) ON DELETE CASCADE,
 remote_id TEXT NOT NULL, document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
 imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 PRIMARY KEY(account_id,remote_id)
);
CREATE TABLE IF NOT EXISTS sources (
 document_id INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
 source_type TEXT NOT NULL, account_id TEXT NOT NULL DEFAULT '',
 locator TEXT NOT NULL DEFAULT '',
 recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS import_jobs (
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL, root TEXT NOT NULL DEFAULT '',
 category TEXT NOT NULL DEFAULT 'Allgemein', status TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS import_items (
 id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES import_jobs(id) ON DELETE CASCADE,
 ordinal INTEGER NOT NULL, path TEXT NOT NULL, locator TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', reason TEXT NOT NULL DEFAULT '',
 size INTEGER, sha256 TEXT NOT NULL DEFAULT '',
 document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
 attempts INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(job_id, ordinal)
);
CREATE TABLE IF NOT EXISTS import_item_errors (
 id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES import_items(id) ON DELETE CASCADE,
 attempt INTEGER NOT NULL, reason TEXT NOT NULL, message TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS document_versions (
 document_id INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
 lineage_id INTEGER NOT NULL, version INTEGER NOT NULL,
 UNIQUE(lineage_id, version)
);
CREATE TABLE IF NOT EXISTS email_attachments (
 document_id INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
 email_document_id INTEGER REFERENCES documents(id) ON DELETE SET NULL,
 name TEXT NOT NULL, content_type TEXT NOT NULL DEFAULT '', size INTEGER
);
CREATE TABLE IF NOT EXISTS tags (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, normalized TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS document_tags (
 document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
 tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
 PRIMARY KEY(document_id, tag_id)
);
CREATE TABLE IF NOT EXISTS rules (
 id INTEGER PRIMARY KEY, position INTEGER NOT NULL, name TEXT NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 1, source_type TEXT NOT NULL DEFAULT '',
 path_pattern TEXT NOT NULL DEFAULT '', sender_domain TEXT NOT NULL DEFAULT '',
 extension TEXT NOT NULL DEFAULT '', keyword TEXT NOT NULL DEFAULT '',
 category TEXT NOT NULL DEFAULT '', tags TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS rule_runs (
 id INTEGER PRIMARY KEY, status TEXT NOT NULL DEFAULT 'applied',
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, reverted_at TEXT
);
CREATE TABLE IF NOT EXISTS rule_run_changes (
 run_id INTEGER NOT NULL REFERENCES rule_runs(id) ON DELETE CASCADE,
 document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
 previous_category TEXT NOT NULL, new_category TEXT NOT NULL,
 added_tags TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(run_id, document_id)
);
CREATE TABLE IF NOT EXISTS chunk_vectors (
 chunk_id INTEGER PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
 model TEXT NOT NULL, dimension INTEGER NOT NULL, vector BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS metadata_changes (
 id INTEGER PRIMARY KEY,
 document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
 field TEXT NOT NULL, old_value TEXT NOT NULL DEFAULT '', new_value TEXT NOT NULL DEFAULT '',
 origin TEXT NOT NULL, run_id INTEGER REFERENCES rule_runs(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS collections (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, normalized TEXT NOT NULL UNIQUE,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, revision INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS collection_documents (
 collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
 document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
 added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 PRIMARY KEY(collection_id, document_id)
);
CREATE TABLE IF NOT EXISTS favorites (
 document_id INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS saved_searches (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, normalized TEXT NOT NULL UNIQUE,
 query TEXT NOT NULL, mode TEXT, tag TEXT,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS document_relations (
 id INTEGER PRIMARY KEY,
 source_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
 target_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
 kind TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(source_id, target_id, kind)
);
CREATE TABLE IF NOT EXISTS entity_types ( id INTEGER PRIMARY KEY, name TEXT NOT NULL, normalized TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP );
CREATE TABLE IF NOT EXISTS relation_types ( id INTEGER PRIMARY KEY, name TEXT NOT NULL, normalized TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP );
CREATE TABLE IF NOT EXISTS entities ( id INTEGER PRIMARY KEY, type_id INTEGER NOT NULL REFERENCES entity_types(id), name TEXT NOT NULL, normalized TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'confirmed', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, deleted_at TEXT, revision INTEGER NOT NULL DEFAULT 1 );
CREATE TABLE IF NOT EXISTS entity_mentions ( id INTEGER PRIMARY KEY, entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE, document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE, start_offset INTEGER NOT NULL, end_offset INTEGER NOT NULL, quote TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, deleted_at TEXT, status TEXT NOT NULL DEFAULT 'confirmed' );
CREATE TABLE IF NOT EXISTS graph_relations ( id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE, target_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE, type_id INTEGER NOT NULL REFERENCES relation_types(id), document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE, start_offset INTEGER NOT NULL, end_offset INTEGER NOT NULL, quote TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'confirmed', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, deleted_at TEXT, revision INTEGER NOT NULL DEFAULT 1 );
CREATE TABLE IF NOT EXISTS graph_changes ( id INTEGER PRIMARY KEY, action TEXT NOT NULL, object_type TEXT NOT NULL, object_id INTEGER NOT NULL, old_value TEXT NOT NULL DEFAULT '', new_value TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, reverted_at TEXT );
INSERT INTO entity_types(name,normalized) VALUES ('Person','person'), ('Organisation','organisation'), ('Prozess','prozess'), ('System','system'), ('Entscheidung','entscheidung');
INSERT INTO relation_types(name,normalized) VALUES ('arbeitet für','arbeitet für'), ('verantwortet','verantwortet'), ('nutzt','nutzt'), ('entscheidet über','entscheidet über'), ('gehört zu','gehört zu');
CREATE TABLE IF NOT EXISTS ai_usage ( id INTEGER PRIMARY KEY, provider TEXT NOT NULL, model TEXT NOT NULL, input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP );
CREATE TABLE IF NOT EXISTS organizations ( id INTEGER PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP );
CREATE TABLE IF NOT EXISTS accounts ( id INTEGER PRIMARY KEY, organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE, tenant_id TEXT NOT NULL, object_id TEXT NOT NULL, display_name TEXT NOT NULL DEFAULT '', role TEXT NOT NULL, disabled INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(tenant_id, object_id) );
CREATE TABLE IF NOT EXISTS grants ( id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE, document_id INTEGER REFERENCES documents(id) ON DELETE CASCADE, collection_id INTEGER REFERENCES collections(id) ON DELETE CASCADE, access TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, revision INTEGER NOT NULL DEFAULT 1 );
INSERT INTO organizations(name) SELECT 'Standard' WHERE NOT EXISTS (SELECT 1 FROM organizations);
CREATE TABLE IF NOT EXISTS web_sessions ( id TEXT PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE, csrf_token TEXT NOT NULL, created_at INTEGER NOT NULL, last_seen INTEGER NOT NULL );
CREATE TABLE IF NOT EXISTS login_requests ( state TEXT PRIMARY KEY, nonce TEXT NOT NULL, verifier TEXT NOT NULL, created_at INTEGER NOT NULL );
CREATE TABLE IF NOT EXISTS audit_log ( id INTEGER PRIMARY KEY, created_at INTEGER NOT NULL, account_id INTEGER, event TEXT NOT NULL, target TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '', details TEXT NOT NULL DEFAULT '{}', previous_hash TEXT NOT NULL, hash TEXT NOT NULL );
CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
        BEGIN SELECT RAISE(ABORT, 'Der Audit-Verlauf ist unveränderlich.'); END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
        BEGIN SELECT RAISE(ABORT, 'Der Audit-Verlauf ist unveränderlich.'); END;
PRAGMA user_version = 23;
