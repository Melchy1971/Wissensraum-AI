"""Build and validate the private Phase 7 evaluation stock from a local manifest."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
DATA = ROOT / 'data'
sys.path.insert(0, str(ROOT))

from core import Embedder, Store  # noqa: E402
from import_jobs import ImportJobs  # noqa: E402

MANIFEST_FORMAT = 'wissensraum-own-evaluation-stock'
REPORT_FORMAT = MANIFEST_FORMAT + '-report'
MANIFEST_FIELDS = frozenset({'format', 'version', 'files', 'folders', 'exclude_names'})
REQUIRED_EXCLUDES = frozenset({'.git', 'node_modules', 'venv', '.venv', 'dist', 'build', '__pycache__'})
SQLITE_SUFFIXES = frozenset({'.db', '.sqlite', '.sqlite3'})


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def source_id(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]


def under_data(path):
    try:
        Path(path).resolve().relative_to(DATA.resolve())
        return True
    except ValueError:
        return False


def load_manifest(path):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if type(data) is not dict or set(data) != MANIFEST_FIELDS:
        raise ValueError('Manifest enth\u00e4lt unbekannte oder fehlende Felder.')
    if data.get('format') != MANIFEST_FORMAT or data.get('version') != 1:
        raise ValueError('Manifest hat nicht das erwartete Format Version 1.')
    for key in ('files', 'folders', 'exclude_names'):
        if type(data[key]) is not list or any(type(value) is not str or not value.strip() for value in data[key]):
            raise ValueError(f'Manifestfeld {key} enth\u00e4lt ung\u00fcltige Werte.')
        normalized = [os.path.normcase(str(Path(value).resolve())) if key != 'exclude_names' else value.casefold()
                      for value in data[key]]
        if len(normalized) != len(set(normalized)):
            raise ValueError(f'Manifestfeld {key} enth\u00e4lt doppelte Werte.')
    return data


def manifest_entries(manifest, base, output):
    excludes = REQUIRED_EXCLUDES | {name.casefold() for name in manifest['exclude_names']}
    protected = {Path(base).resolve(), Path(output).resolve()}
    entries = []
    for number, value in enumerate(manifest['files'], start=1):
        path = Path(value).resolve()
        if path in protected:
            raise ValueError(f'Manifestdatei F{number:04d} ist eine gesch\u00fctzte Datenbank.')
        if path.is_symlink() or not path.is_file():
            raise ValueError(f'Manifestdatei F{number:04d} ist nicht verf\u00fcgbar.')
        entries.append((path, path.name, f'F{number:04d}'))
    for folder_number, value in enumerate(manifest['folders'], start=1):
        root = Path(value).resolve()
        if root.is_symlink() or not root.is_dir() or not os.access(root, os.R_OK):
            raise ValueError(f'Manifestordner D{folder_number:04d} ist nicht lesbar.')
        item = 0
        for current, directories, filenames in os.walk(root, topdown=True, followlinks=False):
            current_path = Path(current)
            directories[:] = sorted((name for name in directories
                                     if name.casefold() not in excludes and not (current_path / name).is_symlink()),
                                    key=str.casefold)
            for name in sorted(filenames, key=str.casefold):
                path = current_path / name
                if path.is_symlink() or not path.is_file() or path.resolve() in protected:
                    continue
                item += 1
                entries.append((path.resolve(), path.relative_to(root).as_posix(),
                                f'D{folder_number:04d}-{item:06d}'))
    return sorted(entries, key=lambda entry: os.path.normcase(str(entry[0])))


def clone_sqlite(base, target):
    uri = Path(base).resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True)) as source, closing(sqlite3.connect(target)) as destination:
        with destination:
            source.backup(destination)


def backup_output(output):
    digest = sha256_file(output)
    backup = output.with_name(f'{output.stem}-{datetime.now().strftime("%Y%m%d")}-{digest[:12]}{output.suffix}')
    if backup.exists() and sha256_file(backup) != digest:
        raise ValueError('Vorhandene lokale Sicherung stimmt nicht mit der Ausgabe \u00fcberein.')
    if not backup.exists():
        shutil.copy2(output, backup)
    return backup


def import_all(store, entries):
    if not entries:
        return 0, [], []
    jobs = ImportJobs(store)
    by_path = {str(path): ref for path, _, ref in entries}
    job = jobs.create_selection('files', '', [(path, locator) for path, locator, _ in entries])
    jobs.run(job)
    imported, duplicates, failed = 0, [], []
    for item in jobs.items(job):
        ref = by_path[item['path']]
        if item['status'] in ('imported', 'versioned'):
            imported += 1
        elif item['status'] == 'duplicate':
            duplicates.append(ref)
        else:
            failed.append({'source': ref, 'status': item['status'], 'reason': item['reason'] or 'error'})
    return imported, duplicates, failed


def embed_missing(store, model, progress=None, embedder_factory=Embedder):
    embedder, embedded = embedder_factory('ollama', model), 0
    while True:
        pending = store.pending_vectors(model, limit=32)
        if not pending:
            return embedded
        vectors = embedder.embed([row['content'] for row in pending])
        store.save_vectors(model, {row['chunk_id']: vector for row, vector in zip(pending, vectors)})
        embedded += len(pending)
        if progress:
            progress(embedded)


def question_contract(path):
    questions = json.loads(Path(path).read_text(encoding='utf-8'))
    if type(questions) is not list:
        raise ValueError('Fragensatz ist ung\u00fcltig.')
    expected, answerable, excluded = {}, 0, 0
    for item in questions:
        if type(item) is not dict or type(item.get('id')) is not str or type(item.get('expected')) is not list:
            raise ValueError('Fragensatz ist ung\u00fcltig.')
        if item['expected']:
            answerable += 1
            for name in item['expected']:
                if type(name) is not str or not name:
                    raise ValueError('Fragensatz ist ung\u00fcltig.')
                expected.setdefault(name, []).append(item['id'])
        else:
            excluded += 1
    return expected, answerable, excluded


def coverage(store, questions_path, model):
    expected, answerable, excluded = question_contract(questions_path)
    with store.connect() as db:
        rows = db.execute('''SELECT d.id,COALESCE(d.filename,d.title) AS name,
              COUNT(DISTINCT c.id) AS chunks,
              COUNT(DISTINCT CASE WHEN v.model=? THEN v.chunk_id END) AS vectors
            FROM documents d LEFT JOIN chunks c ON c.document_id=d.id
            LEFT JOIN chunk_vectors v ON v.chunk_id=c.id
            WHERE NOT EXISTS (SELECT 1 FROM document_versions mine JOIN document_versions newer
              ON newer.lineage_id=mine.lineage_id AND newer.version>mine.version
              WHERE mine.document_id=d.id)
            GROUP BY d.id,COALESCE(d.filename,d.title)''', (model,)).fetchall()
    by_name = {}
    for row in rows:
        by_name.setdefault(row['name'], []).append(row)

    def findings(predicate):
        return [{'source': source_id(name), 'question_ids': sorted(ids)}
                for name, ids in sorted(expected.items()) if predicate(by_name.get(name, []))]

    missing = findings(lambda matches: not matches)
    duplicates = findings(lambda matches: len(matches) > 1)
    without_chunks = findings(lambda matches: len(matches) == 1 and matches[0]['chunks'] == 0)
    without_vectors = findings(lambda matches: len(matches) == 1 and matches[0]['vectors'] < matches[0]['chunks'])
    return len(expected), answerable, excluded, missing, duplicates, without_chunks, without_vectors


def counts(store, model):
    with store.connect() as db:
        schema = db.execute('PRAGMA user_version').fetchone()[0]
        documents = db.execute('SELECT COUNT(*) FROM documents').fetchone()[0]
        current = db.execute('''SELECT COUNT(*) FROM documents d WHERE NOT EXISTS (
          SELECT 1 FROM document_versions mine JOIN document_versions newer
          ON newer.lineage_id=mine.lineage_id AND newer.version>mine.version
          WHERE mine.document_id=d.id)''').fetchone()[0]
        chunks = db.execute('SELECT COUNT(*) FROM chunks').fetchone()[0]
    vectors, _ = store.vector_counts(model)
    return schema, documents, current, chunks, vectors


def semantic_sha256(store):
    """Stable hash of evaluation-relevant state, excluding timestamps and import job bookkeeping."""
    queries = (
        ('documents', 'SELECT id,title,category,filename,hex(original),content,sha256 FROM documents ORDER BY id'),
        ('chunks', 'SELECT id,document_id,ordinal,content FROM chunks ORDER BY id'),
        ('sources', 'SELECT document_id,source_type,account_id,locator FROM sources ORDER BY document_id'),
        ('versions', 'SELECT document_id,lineage_id,version FROM document_versions ORDER BY document_id'),
        ('vectors', 'SELECT chunk_id,model,dimension,hex(vector) FROM chunk_vectors ORDER BY chunk_id'),
    )
    digest = hashlib.sha256()
    with store.connect() as db:
        for name, query in queries:
            digest.update(name.encode())
            for row in db.execute(query):
                digest.update(json.dumps(tuple(row), ensure_ascii=False, separators=(',', ':')).encode())
    return digest.hexdigest()


def validate_paths(base, manifest, questions, output):
    for path, label in ((manifest, 'Manifest'), (questions, 'Fragensatz'), (output, 'Ausgabe')):
        if not under_data(path):
            raise ValueError(f'{label} muss unter data liegen.')
    if Path(output).suffix.casefold() not in SQLITE_SUFFIXES:
        raise ValueError('Ausgabe muss eine SQLite-Datenbank sein.')
    if not Path(base).is_file():
        raise ValueError('Basisdatenbank ist nicht verf\u00fcgbar.')


def dry_run(base, manifest_path, questions_path, output):
    validate_paths(base, manifest_path, questions_path, output)
    manifest = load_manifest(manifest_path)
    entries = manifest_entries(manifest, base, output)
    expected, answerable, excluded = question_contract(questions_path)
    return {'dry_run': True, 'files': len(entries), 'expected_sources': len(expected),
            'answerable_questions': answerable, 'exclusion_questions': excluded}


def build(base, manifest_path, questions_path, output, model, replace=False, skip_vectors=False,
          progress=None, embedder_factory=Embedder):
    base, manifest_path, questions_path, output = map(Path, (base, manifest_path, questions_path, output))
    validate_paths(base, manifest_path, questions_path, output)
    manifest = load_manifest(manifest_path)
    entries = manifest_entries(manifest, base, output)
    if output.exists() and not replace:
        raise FileExistsError('Ausgabedatenbank existiert bereits; --replace ist erforderlich.')
    base_before = sha256_file(base)
    if output.exists():
        backup_output(output)
    descriptor, name = tempfile.mkstemp(prefix=output.name + '.', suffix='.tmp', dir=output.parent)
    os.close(descriptor)
    temporary, failed_output = Path(name), Path(str(output) + '.failed')
    temporary.unlink()
    published = False
    try:
        clone_sqlite(base, temporary)
        with closing(sqlite3.connect(temporary)) as db:
            if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Kopie der Basisdatenbank ist besch\u00e4digt.')
        store = Store(temporary)
        imported, duplicate_inputs, import_errors = import_all(store, entries)
        if not skip_vectors:
            embed_missing(store, model, progress, embedder_factory)
        health = store.health()
        with store.connect() as db:
            integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
        if integrity != 'ok':
            raise ValueError('SQLite-Integrit\u00e4tspr\u00fcfung fehlgeschlagen.')
        expected, answerable, excluded, missing, duplicates, no_chunks, no_vectors = coverage(
            store, questions_path, model)
        schema, documents, current, chunks, vectors = counts(store, model)
        base_after = sha256_file(base)
        if base_before != base_after:
            raise ValueError('Basisdatenbank wurde w\u00e4hrend des Aufbaus ver\u00e4ndert.')
        ready = (not skip_vectors and vectors == chunks and integrity == 'ok' and not missing and
                 not duplicates and not no_chunks and not no_vectors and not import_errors)
        content_hash = semantic_sha256(store)
        os.replace(temporary, output)
        report = {
            'format': REPORT_FORMAT, 'version': 1, 'ready': ready, 'schema_version': schema,
            'base_sha256': base_before, 'manifest_sha256': sha256_file(manifest_path),
            'database_sha256': sha256_file(output), 'documents': documents,
            'reproducible_content_sha256': content_hash,
            'current_documents': current, 'chunks': chunks, 'vectors': vectors,
            'embedding_model': model, 'answerable_questions': answerable,
            'exclusion_questions': excluded, 'expected_sources': expected,
            'missing_sources': missing, 'duplicate_sources': duplicates,
            'sources_without_chunks': no_chunks, 'sources_without_vectors': no_vectors,
            'imported': imported, 'duplicates': len(duplicate_inputs), 'failed': len(import_errors),
            'duplicate_inputs': duplicate_inputs, 'import_errors': import_errors,
            'integrity': integrity, 'health_backend': health['backend'],
            'non_deterministic_metadata': ['documents.created_at', 'import_jobs timestamps'],
        }
        output.with_suffix('.report.json').write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        published = True
        return report
    except BaseException:
        if temporary.exists():
            os.replace(temporary, failed_output)
        elif output.exists() and not published:
            os.replace(output, failed_output)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', required=True, type=Path)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--questions', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--model', default='qwen3-embedding:0.6b')
    parser.add_argument('--replace', action='store_true')
    parser.add_argument('--skip-vectors', action='store_true')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.dry_run:
            result = dry_run(args.base, args.manifest, args.questions, args.output)
        else:
            progress = None if args.json else lambda count: print(f'{count} Abschnitte eingebettet.', file=sys.stderr)
            result = build(args.base, args.manifest, args.questions, args.output, args.model,
                           args.replace, args.skip_vectors, progress)
        print(json.dumps(result, ensure_ascii=False) if args.json else
              json.dumps({key: result[key] for key in result if key not in ('base_sha256', 'manifest_sha256',
                         'database_sha256')}, ensure_ascii=False))
        return 0 if result.get('ready', True) else 2
    except Exception as error:
        print(json.dumps({'error': type(error).__name__, 'message': str(error)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
