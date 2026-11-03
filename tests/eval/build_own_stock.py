"""Build and validate the private Phase 7 evaluation stock from a local manifest."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import unicodedata
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
DATA = ROOT / 'data'
sys.path.insert(0, str(ROOT))

from core import Embedder, Store, SUPPORTED_DOCUMENT_SUFFIXES  # noqa: E402
from import_jobs import ImportJobs  # noqa: E402

MANIFEST_FORMAT = 'wissensraum-own-evaluation-stock'
REPORT_FORMAT = MANIFEST_FORMAT + '-report'
MANIFEST_FIELDS = frozenset({'format', 'version', 'base', 'files', 'folders', 'exclude_names'})
OPTIONAL_MANIFEST_FIELDS = frozenset({'source_aliases'})
ALIAS_FIELDS = frozenset({'expected_title', 'canonical_title', 'content_sha256', 'reason'})
REQUIRED_EXCLUDES = frozenset({'.git', 'node_modules', 'venv', '.venv', 'dist', 'build', '__pycache__'})
SQLITE_SUFFIXES = frozenset({'.db', '.sqlite', '.sqlite3'})
IMPORT_ERROR_CODES = frozenset({'unsupported', 'too_large', 'no_text', 'damaged', 'encrypted',
                                'ocr_unavailable', 'unreadable', 'error'})


@dataclass(frozen=True)
class ManifestDiscovery:
    entries: list[tuple[Path, str, str]]
    skipped_unsupported: int
    duplicate_manifest_entries: int


@dataclass(frozen=True)
class CoverageResult:
    expected: int
    answerable: int
    excluded: int
    exact_sources: int
    aliased_sources: int
    missing: list
    duplicates: list
    without_chunks: list
    without_vectors: list
    invalid_aliases: list
    conflicting_aliases: list


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
    if (type(data) is not dict or not MANIFEST_FIELDS <= set(data)
            or set(data) - MANIFEST_FIELDS - OPTIONAL_MANIFEST_FIELDS):
        raise ValueError('Manifest enth\u00e4lt unbekannte oder fehlende Felder.')
    if data.get('format') != MANIFEST_FORMAT or data.get('version') != 1:
        raise ValueError('Manifest hat nicht das erwartete Format Version 1.')
    if type(data['base']) is not str or not data['base'].strip():
        raise ValueError('Manifestfeld base enthält keinen gültigen Pfad.')
    for key in ('files', 'folders', 'exclude_names'):
        if type(data[key]) is not list or any(type(value) is not str or not value.strip() for value in data[key]):
            raise ValueError(f'Manifestfeld {key} enth\u00e4lt ung\u00fcltige Werte.')
        normalized = [value.casefold() for value in data[key]]
        if key == 'exclude_names' and len(normalized) != len(set(normalized)):
            raise ValueError(f'Manifestfeld {key} enth\u00e4lt doppelte Werte.')
    aliases = data.setdefault('source_aliases', [])
    if type(aliases) is not list:
        raise ValueError('Manifestfeld source_aliases muss eine Liste sein.')
    for number, alias in enumerate(aliases, start=1):
        if (type(alias) is not dict or set(alias) != ALIAS_FIELDS
                or any(type(alias[key]) is not str or not alias[key].strip() for key in ALIAS_FIELDS)):
            raise ValueError(f'Quellenalias A{number:04d} ist ungültig.')
        if alias['reason'] != 'content_duplicate':
            raise ValueError(f'Quellenalias A{number:04d} hat keinen zulässigen Grund.')
        if not re.fullmatch(r'[0-9a-fA-F]{64}', alias['content_sha256']):
            raise ValueError(f'Quellenalias A{number:04d} hat keinen gültigen Content-Hash.')
    return data


def canonical_path(path):
    """Normalised absolute path text: stable sort order and fallback identity for missing files.

    ``resolve`` makes relative paths absolute (against the working directory) and follows the
    final path the system reports. ``normcase`` applies the platform's case and separator rules
    and keeps the drive.
    """
    return os.path.normcase(str(Path(path).resolve()))


def path_identity(path):
    """The one identity used to match manifest inputs, protected databases and import job items.

    An existing file is identified by its volume and file number (``st_dev``, ``st_ino``): a
    relative path, another spelling of the case on Windows, an 8.3 short name or a second path
    to the same file all give the same identity, while the same name on another drive or, on a
    case-sensitive file system, a name differing only in case gives a different one. A path
    that does not exist, or a file system without file numbers, falls back to
    ``canonical_path``.
    """
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    try:
        status = candidate.stat()
    except OSError:
        status = None
    if status is not None and status.st_ino:
        return ('file', status.st_dev, status.st_ino)
    return ('path', canonical_path(candidate))


def manifest_entries(manifest, base, output):
    if Path(manifest['base']).resolve() != Path(base).resolve():
        raise ValueError('Manifestbasis stimmt nicht mit --base überein.')
    excludes = REQUIRED_EXCLUDES | {name.casefold() for name in manifest['exclude_names']}
    protected = {path_identity(base), path_identity(output)}
    entries = []
    seen = set()
    skipped_unsupported = 0
    duplicate_manifest_entries = 0
    for number, value in enumerate(manifest['files'], start=1):
        supplied = Path(value)
        if supplied.is_symlink():
            raise ValueError(f'Manifestdatei F{number:04d} ist nicht verfügbar.')
        path = supplied.resolve()
        key = path_identity(path)
        if key in protected:
            raise ValueError(f'Manifestdatei F{number:04d} ist eine gesch\u00fctzte Datenbank.')
        if not path.is_file():
            raise ValueError(f'Manifestdatei F{number:04d} ist nicht verf\u00fcgbar.')
        if path.suffix.casefold() not in SUPPORTED_DOCUMENT_SUFFIXES:
            raise ValueError(f'Manifestdatei F{number:04d} hat kein unterstütztes Dokumentformat.')
        if key in seen:
            duplicate_manifest_entries += 1
            continue
        seen.add(key)
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
                resolved = path.resolve()
                key = path_identity(resolved)
                if path.is_symlink() or not path.is_file() or key in protected:
                    continue
                item += 1
                if key in seen:
                    duplicate_manifest_entries += 1
                    continue
                seen.add(key)
                if path.suffix.casefold() not in SUPPORTED_DOCUMENT_SUFFIXES:
                    skipped_unsupported += 1
                    continue
                entries.append((resolved, path.relative_to(root).as_posix(),
                                f'D{folder_number:04d}-{item:06d}'))
    entries.sort(key=lambda entry: canonical_path(entry[0]))
    return ManifestDiscovery(entries, skipped_unsupported, duplicate_manifest_entries)


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
    by_path = {path_identity(path): ref for path, _, ref in entries}
    job = jobs.create_selection('files', '', [(path, locator) for path, locator, _ in entries])
    jobs.run(job)
    imported, duplicates, failed = 0, [], []
    for item in jobs.items(job):
        ref = by_path.get(path_identity(item['path']))
        if ref is None:  # never a KeyError: its message would carry the private path
            raise ValueError('Importergebnis l\u00e4sst sich keiner Manifestquelle zuordnen.')
        if item['status'] in ('imported', 'versioned'):
            imported += 1
        elif item['status'] == 'duplicate':
            duplicates.append(ref)
        else:
            reason = item['reason'] if item['reason'] in IMPORT_ERROR_CODES else 'error'
            failed.append({'source': ref, 'status': 'quarantined', 'reason': reason})
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


def normalized_title(value):
    value = unicodedata.normalize('NFKD', value).casefold()
    return ''.join(character for character in value if character.isalnum())


def coverage(store, questions_path, model, aliases=None):
    expected, answerable, excluded = question_contract(questions_path)
    aliases = aliases or []
    with store.connect() as db:
        rows = db.execute('''SELECT d.id,COALESCE(d.filename,d.title) AS name,d.sha256,
              COUNT(DISTINCT c.id) AS chunks,
              COUNT(DISTINCT CASE WHEN v.model=? THEN v.chunk_id END) AS vectors
            FROM documents d LEFT JOIN chunks c ON c.document_id=d.id
            LEFT JOIN chunk_vectors v ON v.chunk_id=c.id
            WHERE NOT EXISTS (SELECT 1 FROM document_versions mine JOIN document_versions newer
              ON newer.lineage_id=mine.lineage_id AND newer.version>mine.version
              WHERE mine.document_id=d.id)
            GROUP BY d.id,COALESCE(d.filename,d.title),d.sha256''', (model,)).fetchall()
    by_name = {}
    by_normalized = {}
    for row in rows:
        by_name.setdefault(row['name'], []).append(row)
        by_normalized.setdefault(normalized_title(row['name']), []).append(row)
    aliases_by_expected = {}
    for alias in aliases:
        aliases_by_expected.setdefault(alias['expected_title'], []).append(alias)
    alias_expected_titles = set(aliases_by_expected)
    missing, duplicates, without_chunks, without_vectors = [], [], [], []
    invalid_aliases, conflicting_aliases = [], []
    exact_sources = aliased_sources = 0

    def finding(name, ids, reason=None):
        result = {'source': source_id(name), 'question_ids': sorted(ids)}
        if reason:
            result['reason'] = reason
        return result

    for name, ids in sorted(expected.items()):
        matches = by_name.get(name, [])
        if not matches:
            matches = by_normalized.get(normalized_title(name), [])
        if matches:
            exact_sources += 1
            if len(matches) > 1:
                duplicates.append(finding(name, ids))
            elif matches[0]['chunks'] == 0:
                without_chunks.append(finding(name, ids))
            elif matches[0]['vectors'] < matches[0]['chunks']:
                without_vectors.append(finding(name, ids))
            continue
        definitions = aliases_by_expected.get(name, [])
        unique_definitions = {(item['canonical_title'], item['content_sha256'].casefold())
                              for item in definitions}
        if len(unique_definitions) > 1:
            conflicting_aliases.append(finding(name, ids, 'multiple_targets'))
            missing.append(finding(name, ids))
            continue
        if not definitions:
            missing.append(finding(name, ids))
            continue
        alias = definitions[0]
        if alias['canonical_title'] in alias_expected_titles:
            invalid_aliases.append(finding(name, ids, 'alias_chain_or_cycle'))
            missing.append(finding(name, ids))
            continue
        targets = by_name.get(alias['canonical_title'], [])
        if not targets:
            targets = by_normalized.get(normalized_title(alias['canonical_title']), [])
        reason = None
        if len(targets) != 1:
            reason = 'unknown_or_ambiguous_target'
        elif targets[0]['sha256'].casefold() != alias['content_sha256'].casefold():
            reason = 'content_hash_mismatch'
        elif targets[0]['chunks'] == 0:
            reason = 'target_without_chunks'
        elif targets[0]['vectors'] < targets[0]['chunks']:
            reason = 'target_without_vectors'
        if reason:
            invalid_aliases.append(finding(name, ids, reason))
            missing.append(finding(name, ids))
        else:
            aliased_sources += 1
    return CoverageResult(len(expected), answerable, excluded, exact_sources, aliased_sources,
                          missing, duplicates, without_chunks, without_vectors,
                          invalid_aliases, conflicting_aliases)


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


class ReadOnlyStore:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def connect(self):
        uri = self.path.resolve().as_uri() + '?mode=ro'
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()


def dry_run(base, manifest_path, questions_path, output):
    validate_paths(base, manifest_path, questions_path, output)
    manifest = load_manifest(manifest_path)
    discovery = manifest_entries(manifest, base, output)
    expected, answerable, excluded = question_contract(questions_path)
    result = {'dry_run': True, 'files': len(discovery.entries),
            'skipped_unsupported': discovery.skipped_unsupported,
            'duplicate_manifest_entries': discovery.duplicate_manifest_entries,
            'expected_sources': len(expected), 'answerable_questions': answerable,
            'exclusion_questions': excluded}
    if Path(output).is_file():
        read_only = ReadOnlyStore(output)
        covered = coverage(read_only, questions_path, 'qwen3-embedding:0.6b',
                           manifest['source_aliases'])
        with read_only.connect() as db:
            integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
        report_path = Path(output).with_suffix('.report.json')
        previous_report = (json.loads(report_path.read_text(encoding='utf-8'))
                           if report_path.is_file() else {})
        failed = previous_report.get('failed', 0)
        ready = (not covered.missing and not covered.duplicates and not covered.without_chunks
                 and not covered.without_vectors and not covered.invalid_aliases
                 and not covered.conflicting_aliases and failed == 0 and integrity == 'ok')
        result.update({
            'exact_sources': covered.exact_sources, 'aliased_sources': covered.aliased_sources,
            'missing_sources': covered.missing, 'duplicate_sources': covered.duplicates,
            'sources_without_chunks': covered.without_chunks,
            'sources_without_vectors': covered.without_vectors,
            'invalid_aliases': covered.invalid_aliases,
            'conflicting_aliases': covered.conflicting_aliases,
            'failed': failed, 'integrity': integrity, 'ready': ready,
        })
    return result


def build(base, manifest_path, questions_path, output, model, replace=False, skip_vectors=False,
          progress=None, embedder_factory=Embedder):
    base, manifest_path, questions_path, output = map(Path, (base, manifest_path, questions_path, output))
    validate_paths(base, manifest_path, questions_path, output)
    manifest = load_manifest(manifest_path)
    discovery = manifest_entries(manifest, base, output)
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
        imported, duplicate_inputs, import_errors = import_all(store, discovery.entries)
        if not skip_vectors:
            embed_missing(store, model, progress, embedder_factory)
        health = store.health()
        with store.connect() as db:
            integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
        if integrity != 'ok':
            raise ValueError('SQLite-Integrit\u00e4tspr\u00fcfung fehlgeschlagen.')
        covered = coverage(store, questions_path, model, manifest['source_aliases'])
        schema, documents, current, chunks, vectors = counts(store, model)
        base_after = sha256_file(base)
        if base_before != base_after:
            raise ValueError('Basisdatenbank wurde w\u00e4hrend des Aufbaus ver\u00e4ndert.')
        ready = (not skip_vectors and vectors == chunks and integrity == 'ok'
                 and not covered.missing and not covered.duplicates
                 and not covered.without_chunks and not covered.without_vectors
                 and not covered.invalid_aliases and not covered.conflicting_aliases
                 and not import_errors)
        content_hash = semantic_sha256(store)
        os.replace(temporary, output)
        report = {
            'format': REPORT_FORMAT, 'version': 1, 'ready': ready, 'schema_version': schema,
            'base_sha256': base_before, 'manifest_sha256': sha256_file(manifest_path),
            'database_sha256': sha256_file(output), 'documents': documents,
            'reproducible_content_sha256': content_hash,
            'current_documents': current, 'chunks': chunks, 'vectors': vectors,
            'embedding_model': model, 'answerable_questions': covered.answerable,
            'exclusion_questions': covered.excluded, 'expected_sources': covered.expected,
            'exact_sources': covered.exact_sources, 'aliased_sources': covered.aliased_sources,
            'missing_sources': covered.missing, 'duplicate_sources': covered.duplicates,
            'sources_without_chunks': covered.without_chunks,
            'sources_without_vectors': covered.without_vectors,
            'invalid_aliases': covered.invalid_aliases,
            'conflicting_aliases': covered.conflicting_aliases,
            'imported': imported, 'duplicates': len(duplicate_inputs), 'failed': len(import_errors),
            'skipped_unsupported': discovery.skipped_unsupported,
            'duplicate_manifest_entries': discovery.duplicate_manifest_entries,
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
