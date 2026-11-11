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
import json
from contextlib import contextmanager, nullcontext as _nothing
from pathlib import Path

from core import (MAX_DOCUMENT_BYTES, QUARANTINE_REASONS, ImportRejected, OcrDeferred, discover_documents,
                  extract_document, import_document)

FINAL_STATUSES = ('imported', 'versioned', 'duplicate', 'quarantined')
OPEN_STATUSES = ('pending', 'running', 'failed')
STATUS_LABELS = {'pending': 'offen', 'running': 'in Bearbeitung', 'imported': 'importiert',
                 'versioned': 'neue Version',
                 'duplicate': 'bereits vorhanden', 'quarantined': 'Quarantäne', 'failed': 'fehlgeschlagen'}
REASON_LABELS = {**QUARANTINE_REASONS, 'error': 'Unerwarteter Fehler'}
MAIL_KINDS = ('imap', 'graph', 'pst')
OUTCOME_LABELS = {'new': 'neu', 'version': 'neue Version', 'duplicate': 'bereits vorhanden',
                  'ocr': 'Texterkennung beim Import', 'quarantined': 'Quarantäne'}
PREVIEW_EXCERPT = 400
# Server uploads (parity roadmap phase 3): a ZIP archive is unpacked into one item per file, within limits.
UPLOAD = 'upload'
MAX_ARCHIVE_ENTRIES = 1000
MAX_ARCHIVE_BYTES = 300 * 1024 * 1024
MAX_ARCHIVE_RATIO = 100
ITEM_COLUMNS = ('id,job_id,ordinal,path,locator,status,reason,size,sha256,document_id,attempts,updated_at,'
                '(upload IS NOT NULL) AS kept')
KEEP_NO_TEXT_DAYS = 30  # decision 11.11.2026: an upload without readable text stays retryable this long


def upload_name(name):
    """The file name of an upload or archive entry: no folders, no control characters, at most 200 characters."""
    name = str(name or '').replace('\\', '/').rsplit('/', 1)[-1]
    name = ''.join(c for c in name if c.isprintable()).strip(' .')
    return name[:200] or 'Datei'


def archive_entries(archive_name, data):
    """``(entries, problems)`` of a ZIP upload: entries are (name, bytes), problems (name, reason, message).

    Only regular files; folders and macOS metadata are skipped. Nested archives stay archives and land
    in quarantine as unsupported. Limits: entries, unpacked total, size per file and compression ratio.
    """
    import io
    import zipfile
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return [], [(archive_name, 'damaged', 'ZIP-Archiv ist beschädigt.')]
    with archive:
        infos = [i for i in archive.infolist() if not i.is_dir() and not i.filename.startswith('__MACOSX/')
                 and not i.filename.replace('\\', '/').rsplit('/', 1)[-1].startswith('.')]
        unpacked = sum(i.file_size for i in infos)
        packed = max(1, sum(i.compress_size for i in infos))
        if len(infos) > MAX_ARCHIVE_ENTRIES or unpacked > MAX_ARCHIVE_BYTES or unpacked / packed > MAX_ARCHIVE_RATIO:
            return [], [(archive_name, 'limits', 'ZIP-Archiv überschreitet die Entpackgrenzen.')]
        entries, problems = [], []
        for info in infos:
            name = f'{upload_name(archive_name)}/{upload_name(info.filename)}'
            if info.flag_bits & 0x1:
                problems.append((name, 'encrypted', 'Datei im Archiv ist verschlüsselt.'))
            elif info.file_size > MAX_DOCUMENT_BYTES:
                problems.append((name, 'too_large', 'Datei im Archiv ist zu groß.'))
            else:
                try:
                    with archive.open(info) as member:
                        content = member.read(MAX_DOCUMENT_BYTES + 1)
                except (zipfile.BadZipFile, OSError, ValueError, NotImplementedError):
                    problems.append((name, 'damaged', 'Datei im Archiv ist nicht lesbar.'))
                    continue
                if len(content) > MAX_DOCUMENT_BYTES:  # the header lied about the size
                    problems.append((name, 'limits', 'Datei im Archiv überschreitet die Entpackgrenzen.'))
                else:
                    entries.append((name, content))
        return entries, problems


def file_source(path):
    """Source of a local file: the absolute path identifies the place across imports."""
    return ('file', '', str(Path(path).resolve()))


class ImportJobs:
    """Create, run, cancel, resume and report import jobs on SQLite or PostgreSQL."""

    def __init__(self, store, importer=None):
        self.store = store
        self.importer = importer  # needed for e-mail and PST jobs (step 6.3b)

    def create_selection(self, kind, root, entries, category='Allgemein'):
        """Job for a subset of a preview: (path, locator) pairs as returned by the preview."""
        return self._create(kind, str(Path(root).resolve()) if root else '', category, entries, ())

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

    def create_uploads(self, account_id, files, category='Allgemein'):
        """Server job for ``account_id``: ``files`` are (name, bytes); ZIP archives are unpacked.

        The bytes wait in ``import_items.upload`` until the worker imported the item and are removed then.
        """
        entries, problems = [], []
        for name, data in files:
            if upload_name(name).lower().endswith('.zip'):
                found, failed = archive_entries(name, data)
                entries += found
                problems += failed
            else:
                entries.append((upload_name(name), data))
        if not entries and not problems:
            raise ValueError('Keine Dateien gefunden.')
        with self.store.transaction() as db:
            job_id = db.execute('INSERT INTO import_jobs(kind,root,category,status,account_id) VALUES(?,?,?,?,?) '
                                'RETURNING id', (UPLOAD, '', category, 'pending', account_id)).fetchone()['id']
            for ordinal, (name, data) in enumerate(entries, start=1):
                db.execute('INSERT INTO import_items(job_id,ordinal,path,locator,size,upload) VALUES(?,?,?,?,?,?)',
                           (job_id, ordinal, name, name, len(data), data))
            for ordinal, (name, reason, message) in enumerate(problems, start=len(entries) + 1):
                item_id = db.execute(
                    'INSERT INTO import_items(job_id,ordinal,path,locator,status,reason,attempts) '
                    'VALUES(?,?,?,?,?,?,?) RETURNING id',
                    (job_id, ordinal, name, name, 'quarantined', reason, 1)).fetchone()['id']
                db.execute('INSERT INTO import_item_errors(item_id,attempt,reason,message) VALUES(?,?,?,?)',
                           (item_id, 1, reason, message))
        return job_id

    def next_upload_job(self):
        """The oldest open server job (pending, or running after an interruption), or None.

        ponytail: one worker reads this; two could pick the same job. Upgrade path: FOR UPDATE SKIP LOCKED.
        """
        with self.store.connect() as db:
            row = db.execute("SELECT id FROM import_jobs WHERE (kind=? OR kind IN ('imap','graph') AND account_id IS NOT "
                             "NULL) AND status IN ('pending','running') ORDER BY id LIMIT 1", (UPLOAD,)).fetchone()
        return row['id'] if row else None

    def upload_jobs(self, account_id=None, limit=50):
        """Summaries of server jobs, newest first; ``account_id`` None: all (administrators)."""
        with self.store.connect() as db:
            ids = [row['id'] for row in db.execute(
                "SELECT id FROM import_jobs WHERE (kind=? OR kind IN ('imap','graph') AND account_id IS NOT NULL) "
                'AND (? IS NULL OR account_id=?) ORDER BY id DESC LIMIT ?', (UPLOAD, account_id, account_id, limit))]
        return [self.summary(job_id) for job_id in ids]

    def set_status(self, job_id, status):
        """Cancel (``cancelled``) an unfinished job, or resume (``pending``) one with open or failed items."""
        if status not in ('cancelled', 'pending'):
            raise ValueError('Unbekannter Auftragsstatus.')
        allowed = ("status IN ('pending','running')" if status == 'cancelled' else
                   "EXISTS (SELECT 1 FROM import_items i WHERE i.job_id=import_jobs.id "
                   "AND i.status IN ('pending','running','failed'))")
        with self.store.connect() as db:
            return db.execute(f'UPDATE import_jobs SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND {allowed}',
                              (status, job_id)).rowcount == 1

    def retry_candidates(self, reason='no_text'):
        """Items whose latest outcome is quarantine for ``reason``, grouped by source; oldest first.

        An item counts once per place (job kind, source and locator); a later successful or
        duplicate outcome for the same place removes it, so repeating a retry creates nothing twice.
        """
        with self.store.connect() as db:  # server uploads have no path to read again: retry_uploads
            rows = db.execute('SELECT j.kind,j.root,j.category,i.path,i.locator,i.status,i.reason '
                              'FROM import_items i JOIN import_jobs j ON j.id=i.job_id '
                              "WHERE (j.kind <> 'folder' OR i.reason <> 'unreadable') AND j.kind <> ? "
                              'ORDER BY j.id,i.ordinal', (UPLOAD,)).fetchall()
        latest = {}
        for row in rows:
            mail = row['kind'] in MAIL_KINDS
            # One place per file path (folder and file jobs alike) or per mailbox item.
            key = (row['kind'] if mail else 'files', row['root'] if mail else '', row['locator'] if mail else row['path'])
            latest[key] = row
        groups = {}
        for row in latest.values():
            if row['status'] == 'quarantined' and row['reason'] == reason:
                kind = 'files' if row['kind'] in ('files', 'folder') else row['kind']
                group = (kind, row['root'] if kind in MAIL_KINDS else '', row['category'])
                groups.setdefault(group, []).append((row['path'], row['locator']))
        return groups

    def retry_uploads(self, job_id):
        """Put the kept items without readable text of a server job back to pending; returns their count."""
        with self.store.connect() as db:
            count = db.execute("UPDATE import_items SET status='pending',reason='',updated_at=CURRENT_TIMESTAMP "
                               "WHERE job_id=? AND status='quarantined' AND reason='no_text' AND upload IS NOT NULL",
                               (job_id,)).rowcount
            if count:
                db.execute("UPDATE import_jobs SET status='pending',updated_at=CURRENT_TIMESTAMP WHERE id=? AND kind=?",
                           (job_id, UPLOAD))
        return count

    def drop_kept_uploads(self, now=None, days=KEEP_NO_TEXT_DAYS):
        """Remove the bytes of uploads without readable text older than ``days``; returns their count."""
        from datetime import datetime, timedelta, timezone
        from retention import _utc
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
        with self.store.connect() as db:
            # ponytail: compares the time in Python, the same on both backends (as retention.py); few rows.
            old = [row['id'] for row in db.execute(
                "SELECT id,updated_at FROM import_items WHERE status='quarantined' AND reason='no_text' "
                'AND upload IS NOT NULL') if _utc(row['updated_at']) < cutoff]
            db.executemany('UPDATE import_items SET upload=NULL WHERE id=?', [(i,) for i in old])
        return len(old)

    def retry_quarantined(self, reason='no_text'):
        """Create one job per source for quarantined items, e.g. after OCR was installed."""
        return [self._create(kind, root, category, entries, ())
                for (kind, root, category), entries in self.retry_candidates(reason).items()]

    def run(self, job_id, cancel=None, progress=None, pause=None):
        """Process all open items in order; stop between items when ``cancel`` is set.

        ``pause`` (the worker's shutdown) also stops between items but leaves the job running, so that
        the next start resumes it; ``cancel`` marks it cancelled.
        """
        job = self.job(job_id)
        items = [item for item in self.items(job_id) if item['status'] in OPEN_STATUSES]
        self._set_job_status(job_id, 'running')
        total = len(items)
        mail = job['kind'] in MAIL_KINDS
        with self._handler(job) as handle:
            for done, item in enumerate(items, start=1):
                if pause is not None and pause.is_set():
                    return self.summary(job_id)
                if cancel is not None and cancel.is_set() or job['kind'] == UPLOAD and self._cancelled(job_id):
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
                if job.get('account_id') is not None:  # a server account's mailbox (phase 8, D-030)
                    self._check_mail_owner(job)
                with self.importer.job_handler(job) as handle:
                    yield self._server_mail(job, handle) if job.get('account_id') is not None else handle
            except Exception:
                self._set_job_status(job['id'], 'cancelled')  # e.g. login failed: job stays resumable
                raise
            return
        if job['kind'] == UPLOAD:
            yield self._upload_handler(job)
            return
        yield lambda item, recovering: self._file_item(job, item, recovering)

    def _previous_version(self, document_id):
        with self.store.connect() as db:
            return db.execute('SELECT p.document_id FROM document_versions v JOIN document_versions p '
                              'ON p.lineage_id=v.lineage_id AND p.version=v.version-1 WHERE v.document_id=?',
                              (document_id,)).fetchone()['document_id']

    def _cancelled(self, job_id):
        return self.job(job_id)['status'] == 'cancelled'

    def _check_mail_owner(self, job):
        """The account may still import and the mailbox still belongs to it; else the job stops (cancelled)."""
        import accounts
        account = accounts.account(self.store, job['account_id'])
        mailbox = json.loads(job['root']).get('account_id')
        with self.store.connect() as db:
            row = db.execute('SELECT account_id FROM account_mailboxes WHERE mailbox_id=?', (mailbox,)).fetchone()
        if account['disabled'] or account['role'] not in ('admin', 'editor') or row is None \
                or row['account_id'] != account['id']:
            raise PermissionError('Das Konto darf aus diesem Postfach nicht mehr importieren.')

    def _server_mail(self, job, handle):
        """A new e-mail or attachment document gets the owner grant of the job's account and an audit entry,
        in one transaction with the item. An existing text stays without access (decision 10.11.2026)."""
        import accounts
        import audit

        def wrapped(item, recovering):
            status, document_id, data = handle(item, recovering)
            if status == 'imported' and document_id is not None:
                accounts.grant(self.store, job['account_id'], 'owner', document_id=document_id, end_sessions=False)
                audit.record(self.store, 'document_created', job['account_id'], f'document:{document_id}', 'worker',
                             {'job': job['id'], 'mail': True})
            return status, document_id, data
        wrapped.atomic = True
        return wrapped

    def assign(self, job_id, account_id):
        """Mark a job as one of a server account (worker, pages and rights), e.g. an e-mail job of its mailbox."""
        with self.store.connect() as db:
            db.execute('UPDATE import_jobs SET account_id=? WHERE id=?', (account_id, job_id))

    def _upload_handler(self, job):
        """Import one uploaded item for the job's account: document, owner grant and audit entry together.

        The account is read again for every job: a locked account or one that lost the right to import
        stops the job (status cancelled), nothing is created in its name.
        """
        import accounts
        import audit
        account = accounts.account(self.store, job['account_id'])
        if account['disabled'] or account['role'] not in ('admin', 'editor'):
            self._set_job_status(job['id'], 'cancelled')
            raise PermissionError('Das Konto darf nicht mehr importieren.')

        def handle(item, recovering):
            with self.store.connect() as db:
                data = bytes(db.execute('SELECT upload FROM import_items WHERE id=?', (item['id'],)).fetchone()['upload'])
            # Phase 6: the worker image has Tesseract; without it (or with WISSENSRAUM_OCR=0) images and
            # scans go to quarantine no_text and keep their bytes for a later run.
            title, text, filename, original = extract_document(item['locator'], data, allow_ocr=True)
            # The same file name from the same account is the same place: a changed file becomes a new version,
            # as on the desktop. The account in the source keeps lineages of different accounts apart.
            source = ('file', f'server:{account["id"]}', item['locator'])
            document_id, created = self.store.add(title, text, job['category'], filename, original, source,
                                                  versioned=True)
            if not created:
                # Decision 10.11.2026: an existing text gives no access; the item only says "already there".
                return 'duplicate', None, original
            accounts.grant(self.store, account['id'], 'owner', document_id=document_id, end_sessions=False)
            versioned = self._version(document_id) > 1
            if versioned:  # decision 11.11.2026: whoever could read or change the previous version keeps that
                accounts.inherit_grants(self.store, self._previous_version(document_id), document_id)
            audit.record(self.store, 'document_created', account['id'], f'document:{document_id}', 'worker',
                         {'job': job['id'], 'version': versioned})
            return ('versioned' if versioned else 'imported'), document_id, original
        handle.atomic = True
        return handle

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
            # Server uploads: the item's result and what it created in one transaction (document, owner grant,
            # audit entry). Desktop jobs keep their two steps and the recovery after an interruption.
            with self.store.transaction() if getattr(handle, 'atomic', False) else _nothing():
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
            db.execute('UPDATE import_items SET status=?,reason=?,document_id=?,size=COALESCE(?,size),sha256=?,'
                       'updated_at=CURRENT_TIMESTAMP WHERE id=?',
                       (status, reason, document_id, size, sha256, item_id))
            # An upload's bytes are not kept once the item is decided, except without readable text: those
            # stay KEEP_NO_TEXT_DAYS days so that the owner can run them again (D05, decision 11.11.2026).
            if status in FINAL_STATUSES and not (status == 'quarantined' and reason == 'no_text'):
                db.execute('UPDATE import_items SET upload=NULL WHERE id=? AND upload IS NOT NULL', (item_id,))
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

        OCR is not run here: scans and images report outcome ``ocr`` with their page count, and the
        import recognizes them once. Whether such a file is a duplicate is known only after OCR.

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
                   'document_id': None, 'existing_title': '', 'version': None, 'pages': None}
            try:
                row['size'] = path.stat().st_size
                title, text, _, _ = import_document(path, allow_ocr=False)
                row.update(title=title, excerpt=text.strip()[:PREVIEW_EXCERPT])
                match = self.store.match(text, file_source(path), versioned=True)
                row.update(outcome=match['outcome'], document_id=match.get('document_id'),
                           existing_title=match.get('title', ''), version=match.get('version'))
            except OcrDeferred as deferred:
                row.update(outcome='ocr', title=path.stem, pages=deferred.pages)
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
            return [dict(row) for row in db.execute(  # without the upload bytes
                f'SELECT {ITEM_COLUMNS} FROM import_items WHERE job_id=? ORDER BY ordinal', (job_id,))]

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
