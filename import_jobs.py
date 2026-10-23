"""Persistent, resumable import jobs for local files and folders (Phase 6.1).

A job lists every discovered file as an item. Items move from ``pending`` through
``running`` to a final status. A changed file at a known place (same absolute
path) becomes a new version of the earlier document (status ``versioned``). ``failed`` items (transient problems such as a locked
file) are retried when a job is resumed; ``quarantined`` items have content problems
that a retry cannot fix and keep the reason from ``core.QUARANTINE_REASONS``.
"""
from __future__ import annotations

import csv
import hashlib
from contextlib import contextmanager
from pathlib import Path

from core import QUARANTINE_REASONS, ImportRejected, discover_documents, import_document

FINAL_STATUSES = ('imported', 'versioned', 'duplicate', 'quarantined')
OPEN_STATUSES = ('pending', 'running', 'failed')
STATUS_LABELS = {'pending': 'offen', 'running': 'in Bearbeitung', 'imported': 'importiert',
                 'versioned': 'neue Version',
                 'duplicate': 'bereits vorhanden', 'quarantined': 'Quarantäne', 'failed': 'fehlgeschlagen'}
REASON_LABELS = {**QUARANTINE_REASONS, 'error': 'Unerwarteter Fehler'}
MAIL_KINDS = ('imap', 'graph', 'pst')
OUTCOME_LABELS = {'new': 'neu', 'version': 'neue Version', 'duplicate': 'bereits vorhanden',
                  'quarantined': 'Quarantäne'}
PREVIEW_EXCERPT = 400


def file_source(path):
    """Source of a local file: the absolute path identifies the place across imports."""
    return ('file', '', str(Path(path).resolve()))


class ImportJobs:
    """Create, run, cancel, resume and report import jobs on SQLite or PostgreSQL."""

    def __init__(self, store, importer=None):
        self.store = store
        self.importer = importer  # needed for e-mail and PST jobs (step 6.3b)

    def create_entries(self, kind, root, category, entries):
        """Create a job from (label, locator) pairs; used for e-mail and PST jobs."""
        return self._create(kind, root, category, entries, ())

    def create_for_files(self, paths, category='Allgemein'):
        entries = [(Path(path), Path(path).name) for path in paths]
        return self._create('files', '', category, entries, ())

    def create_for_folder(self, folder, category='Allgemein'):
        root = Path(folder).resolve()
        entries, _, scan_errors = discover_documents(root, include_unsupported=True)
        return self._create('folder', str(root), category, entries, scan_errors)

    def _create(self, kind, root, category, entries, scan_errors):
        if not entries and not scan_errors:
            raise ValueError('Keine Dateien gefunden.')
        with self.store.connect() as db:
            job_id = db.execute('INSERT INTO import_jobs(kind,root,category,status) VALUES(?,?,?,?) '
                                'RETURNING id', (kind, root, category, 'pending')).fetchone()['id']
            for ordinal, (path, locator) in enumerate(entries, start=1):
                # Files keep their absolute path; e-mail and PST items keep a readable label.
                value = str(path) if kind in MAIL_KINDS else str(Path(path).resolve())
                db.execute('INSERT INTO import_items(job_id,ordinal,path,locator) VALUES(?,?,?,?)',
                           (job_id, ordinal, value, locator))
            for ordinal, message in enumerate(scan_errors, start=len(entries) + 1):
                item_id = db.execute(
                    'INSERT INTO import_items(job_id,ordinal,path,locator,status,reason,attempts) '
                    'VALUES(?,?,?,?,?,?,?) RETURNING id',
                    (job_id, ordinal, root, message, 'quarantined', 'unreadable', 1)).fetchone()['id']
                db.execute('INSERT INTO import_item_errors(item_id,attempt,reason,message) VALUES(?,?,?,?)',
                           (item_id, 1, 'unreadable', message))
        return job_id

    def run(self, job_id, cancel=None, progress=None):
        """Process all open items in order; stop between items when ``cancel`` is set."""
        job = self.job(job_id)
        items = [item for item in self.items(job_id) if item['status'] in OPEN_STATUSES]
        self._set_job_status(job_id, 'running')
        total = len(items)
        mail = job['kind'] in MAIL_KINDS
        with self._handler(job) as handle:
            for done, item in enumerate(items, start=1):
                if cancel is not None and cancel.is_set():
                    self._set_job_status(job_id, 'cancelled')
                    return self.summary(job_id)
                self._process(item, handle)
                if progress is not None:
                    progress(done, total, item['path'] if mail else item['locator'])
        self._set_job_status(job_id, 'completed')
        return self.summary(job_id)

    @contextmanager
    def _handler(self, job):
        if job['kind'] in MAIL_KINDS:
            if self.importer is None:
                raise ValueError('E-Mail-Aufträge benötigen den E-Mail-Import.')
            try:
                with self.importer.job_handler(job) as handle:
                    yield handle
            except Exception:
                self._set_job_status(job['id'], 'cancelled')  # e.g. login failed: job stays resumable
                raise
            return
        yield lambda item, recovering: self._file_item(job, item, recovering)

    def _file_item(self, job, item, recovering):
        title, text, filename, data = import_document(item['path'])
        source = file_source(item['path'])
        document_id, created = self.store.add(title, text, job['category'], item['locator'], data,
                                              source, versioned=True)
        if not created and recovering and self._source_locator(document_id) == source[2]:
            created = True  # created by this item before the interruption: no second document
        status = 'duplicate'
        if created:
            status = 'versioned' if self._version(document_id) > 1 else 'imported'
        return status, document_id, data

    def _process(self, item, handle):
        recovering = item['status'] == 'running'  # a previous run stopped inside this item
        attempt = item['attempts'] + 1
        with self.store.connect() as db:
            db.execute("UPDATE import_items SET status='running',attempts=?,updated_at=CURRENT_TIMESTAMP "
                       'WHERE id=?', (attempt, item['id']))
        try:
            status, document_id, data = handle(item, recovering)
            self._finish(item['id'], status, '', document_id, len(data), hashlib.sha256(data).hexdigest())
        except ImportRejected as rejected:
            self._finish(item['id'], 'quarantined', rejected.reason, error=(attempt, str(rejected)))
        except OSError as error:
            self._finish(item['id'], 'failed', 'unreadable',
                         error=(attempt, f'{type(error).__name__}: {error.strerror or error}'))
        except Exception as error:
            self._finish(item['id'], 'failed', 'error', error=(attempt, f'{type(error).__name__}: {error}'))

    def _finish(self, item_id, status, reason, document_id=None, size=None, sha256='', error=None):
        with self.store.connect() as db:
            db.execute('UPDATE import_items SET status=?,reason=?,document_id=?,size=?,sha256=?,'
                       'updated_at=CURRENT_TIMESTAMP WHERE id=?',
                       (status, reason, document_id, size, sha256, item_id))
            if error is not None:
                db.execute('INSERT INTO import_item_errors(item_id,attempt,reason,message) VALUES(?,?,?,?)',
                           (item_id, error[0], reason, error[1]))

    def _source_locator(self, document_id):
        with self.store.connect() as db:
            row = db.execute("SELECT locator FROM sources WHERE document_id=? AND source_type='file'",
                             (document_id,)).fetchone()
        return row['locator'] if row else None

    def _version(self, document_id):
        with self.store.connect() as db:
            row = db.execute('SELECT version FROM document_versions WHERE document_id=?',
                             (document_id,)).fetchone()
        return row['version'] if row else 1

    def preview(self, entries, cancel=None, progress=None):
        """Read files and report what an import would do; never writes to the database.

        ``entries`` are (path, locator) pairs as returned by ``discover_documents``.
        Returns None when cancelled.
        """
        rows = []
        total = len(entries)
        for done, (path, locator) in enumerate(entries, start=1):
            if cancel is not None and cancel.is_set():
                return None
            path = Path(path)
            row = {'path': str(path), 'locator': locator, 'type': path.suffix.lower().lstrip('.') or '?',
                   'size': None, 'title': '', 'excerpt': '', 'outcome': 'quarantined', 'reason': '',
                   'document_id': None, 'existing_title': '', 'version': None}
            try:
                row['size'] = path.stat().st_size
                title, text, _, _ = import_document(path)
                row.update(title=title, excerpt=text.strip()[:PREVIEW_EXCERPT])
                match = self.store.match(text, file_source(path), versioned=True)
                row.update(outcome=match['outcome'], document_id=match.get('document_id'),
                           existing_title=match.get('title', ''), version=match.get('version'))
            except ImportRejected as rejected:
                row['reason'] = rejected.reason
            except OSError:
                row['reason'] = 'unreadable'
            rows.append(row)
            if progress is not None:
                progress(done, total, locator)
        return rows

    def preview_files(self, paths, cancel=None, progress=None):
        return self.preview([(Path(path), Path(path).name) for path in paths], cancel, progress)

    def preview_folder(self, folder, cancel=None, progress=None):
        entries, _, _ = discover_documents(Path(folder).resolve(), include_unsupported=True)
        return self.preview(entries, cancel, progress)

    def _set_job_status(self, job_id, status):
        with self.store.connect() as db:
            db.execute('UPDATE import_jobs SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
                       (status, job_id))

    def job(self, job_id):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM import_jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise ValueError('Importauftrag wurde nicht gefunden.')
        return dict(row)

    def items(self, job_id):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute(
                'SELECT * FROM import_items WHERE job_id=? ORDER BY ordinal', (job_id,))]

    def errors(self, item_id):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute(
                'SELECT * FROM import_item_errors WHERE item_id=? ORDER BY id', (item_id,))]

    def summary(self, job_id):
        counts = {status: 0 for status in STATUS_LABELS}
        with self.store.connect() as db:
            for row in db.execute('SELECT status,count(*) AS total FROM import_items WHERE job_id=? '
                                  'GROUP BY status', (job_id,)):
                counts[row['status']] = row['total']
        job = self.job(job_id)
        return {'job_id': job_id, 'kind': job['kind'], 'status': job['status'], 'total': sum(counts.values()), **counts,
                'resumable': any(counts[status] for status in OPEN_STATUSES)}

    def jobs(self, limit=20):
        with self.store.connect() as db:
            ids = [row['id'] for row in db.execute('SELECT id FROM import_jobs ORDER BY id DESC LIMIT ?',
                                                   (limit,))]
        return [self.summary(job_id) for job_id in ids]

    def write_report(self, job_id, target):
        """CSV report with one row per item; semicolon and BOM so that Excel opens it directly."""
        job = self.job(job_id)
        rows = self.items(job_id)
        with Path(target).open('w', encoding='utf-8-sig', newline='') as output:
            writer = csv.writer(output, delimiter=';')
            writer.writerow(['Auftrag', 'Nr', 'Fundort', 'Status', 'Grund', 'Dokument-ID', 'Bytes',
                             'SHA-256 Original', 'Versuche', 'Letzte Meldung'])
            mail = job['kind'] in MAIL_KINDS
            for row in rows:
                history = self.errors(row['id'])
                writer.writerow([job['id'], row['ordinal'], row['path'] if mail else row['locator'],
                                 STATUS_LABELS[row['status']],
                                 REASON_LABELS.get(row['reason'], row['reason']), row['document_id'] or '',
                                 '' if row['size'] is None else row['size'], row['sha256'], row['attempts'],
                                 history[-1]['message'] if history else ''])
        return Path(target)
