"""Remove attachment content from IMAP originals stored before step 6.3a.

Until step 6.2 the IMAP import fetched the full message (``BODY.PEEK[]``) and stored it,
attachments included, as the document original. This tool rewrites those originals to
header and text parts only. The searchable text and its checksum stay unchanged; a message
whose text would change is skipped. SQLite gets a verified backup first and is compacted
afterwards (VACUUM) so that the removed bytes leave the database file. For PostgreSQL the
caller must confirm an external pg_dump backup.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from email import policy
from email.parser import BytesParser
from pathlib import Path

import mail_structure
from importer import email_body

MARKER = b'X-Wissensraum-Anhaenge-nicht-gespeichert:'


def _split(raw):
    for separator in (b'\r\n\r\n', b'\n\n'):
        if separator in raw:
            head, body = raw.split(separator, 1)
            return head + separator, body
    return raw, b''


def _is_body_text(part):
    return (part.get_content_type() in ('text/plain', 'text/html') and part.get_content_disposition() != 'attachment'
            and not part.get_filename())


def strip_message(raw):
    """Return (new_raw, removed_count); new_raw is None when nothing is to be removed."""
    if not raw or MARKER in _split(raw)[0]:
        return None, 0
    message = BytesParser(policy=policy.compat32).parsebytes(raw)
    header, _ = _split(raw)
    texts, removed = [], 0

    def walk(part):
        nonlocal removed
        if part.is_multipart():
            for child in part.get_payload():
                walk(child)
        elif _is_body_text(part):
            texts.append(part)
        else:
            removed += 1  # attachment, inline image or forwarded message

    if message.is_multipart():
        walk(message)
    elif not _is_body_text(message):
        return mail_structure.rebuild_message(header, [], 1), 1
    if not removed:
        return None, 0
    parts = [_split(part.as_bytes(policy=policy.compat32)) for part in texts]
    return mail_structure.rebuild_message(header, parts, removed), removed


def _same_text(old, new):
    parse = lambda raw: BytesParser(policy=policy.default).parsebytes(raw)
    return email_body(parse(old)).strip() == email_body(parse(new)).strip()


def _candidates(db):
    return db.execute("SELECT d.id, d.original FROM documents d JOIN sources s ON s.document_id=d.id "
                      "WHERE s.source_type='imap' AND d.original IS NOT NULL ORDER BY d.id").fetchall()


def plan(store):
    """Dry run: what would change, without writing."""
    result = {'checked': 0, 'changed': 0, 'attachments': 0, 'skipped': 0, 'bytes_before': 0, 'bytes_after': 0}
    changes = []
    with store.connect() as db:
        rows = _candidates(db)
    for row in rows:
        result['checked'] += 1
        original = bytes(row['original'])
        new, removed = strip_message(original)
        if new is None:
            continue
        if not _same_text(original, new):
            result['skipped'] += 1
            continue
        result['changed'] += 1
        result['attachments'] += removed
        result['bytes_before'] += len(original)
        result['bytes_after'] += len(new)
        changes.append((row['id'], original, new))
    return result, changes


def _verified_backup(store):
    target = store.path.with_name(f'{store.path.name}.vor-anhangsbereinigung-'
                                  f'{datetime.now().strftime("%Y%m%d-%H%M%S")}.bak')
    store.backup(target)
    with closing(sqlite3.connect(target)) as copy, store.connect() as live:
        if copy.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Sicherung vor der Bereinigung ist beschädigt. Es wurde nichts geändert.')
        if (copy.execute('SELECT count(*) FROM documents').fetchone()[0]
                != live.execute('SELECT count(*) FROM documents').fetchone()[0]):
            raise ValueError('Sicherung vor der Bereinigung ist unvollständig. Es wurde nichts geändert.')
    return target


def strip_stored_attachments(store, dry_run=False, backup_confirmed=False):
    """Rewrite affected IMAP originals; returns counts and, for SQLite, the backup path."""
    result, changes = plan(store)
    result['backup'] = ''
    if dry_run or not changes:
        return result
    if store.backend == 'postgresql':
        if not backup_confirmed:
            raise ValueError('Vor der Bereinigung eine pg_dump-Sicherung der PostgreSQL-Datenbank erstellen und '
                             'die Sicherung bestätigen.')
    else:
        result['backup'] = str(_verified_backup(store))
    with store.connect() as db:
        for document_id, original, new in changes:
            cursor = db.execute('UPDATE documents SET original=? WHERE id=? AND original=?',
                                (new, document_id, original))
            if getattr(cursor, 'rowcount', 1) != 1:
                raise ValueError('Ein Dokument wurde während der Bereinigung geändert. Es wurde nichts geändert.')
    if store.backend != 'postgresql':
        with closing(sqlite3.connect(store.path)) as db:  # release freed pages from the file
            db.execute('VACUUM')
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    return result


def describe(result):
    text = (f'{result["checked"]} IMAP-E-Mails geprüft · {result["changed"]} mit Anhängen im Original · '
            f'{result["attachments"]} Anhänge · {result["bytes_before"] - result["bytes_after"]} Bytes')
    if result['skipped']:
        text += f' · {result["skipped"]} übersprungen (Text wäre verändert worden)'
    if result.get('backup'):
        text += f' · Sicherung: {Path(result["backup"]).name}'
    return text
