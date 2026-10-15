-- Schema SQLite / FTS5 · Version 1

PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS documents (
 id INTEGER PRIMARY KEY, title TEXT NOT NULL, category TEXT NOT NULL,
 filename TEXT, original BLOB, content TEXT NOT NULL,
 sha256 TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
PRAGMA user_version = 1;
