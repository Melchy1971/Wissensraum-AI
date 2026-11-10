"""Application services of the multi-user server (parity roadmap phase 1, docs/web-parity-roadmap.md).

One place for the writes that the API (``server.py``) and the pages (``web.py``) share. Each runs the
change, a needed owner grant and its audit entry in one database transaction (``store.transaction``):
if any part fails, nothing is stored, so there is neither a change without audit entry nor a document
without owner. Visibility (404) and role (403) are checked by the caller before, as until now.

``record(event, account_id, target='', **details)`` writes the audit entry; the server passes its
``audited`` bound to the request. Rate limits per account for questions, uploads and searches live
here too (``Limits``), so both entry points count together.
"""
import collections
import threading
import time

import accounts
from core import ProviderError


class Duplicate(ValueError):
    """The exact text exists already. Answer 409 without id or title (decision 10.11.2026, S-05)."""


class Unavailable(Exception):
    """The AI could not answer (not reachable, error, budget used up); the message is safe to show."""


def create_document(store, record, account, title, content, category='Allgemein', filename=None, original=None,
                    **details):
    """New document with owner grant for ``account`` and audit entry ``document_created``; returns its id."""
    with store.transaction():
        doc_id, created = store.add(title, content, category, filename=filename, original=original)
        if not created:
            raise Duplicate('Dieser Inhalt ist bereits vorhanden.')
        accounts.grant(store, account['id'], 'owner', document_id=doc_id, end_sessions=False)
        record('document_created', account['id'], f'document:{doc_id}', **details)
    return doc_id


def change_metadata(store, record, account, doc_id, revision, **changes):
    """Title or area with the revision read (``Conflict`` if stale) and audit entry ``document_changed``."""
    with store.transaction():
        store.update_metadata(doc_id, revision=revision, **changes)
        record('document_changed', account['id'], f'document:{doc_id}', fields=','.join(sorted(changes)))


def grant(store, record, admin, account_id, access, document_id=None, collection_id=None, revision=None):
    """Grant with audit entry ``grant_created``; ``revision`` as in ``accounts.grant``."""
    with store.transaction():
        created = accounts.grant(store, account_id, access, document_id, collection_id, revision=revision)
        target = f'document:{document_id}' if document_id is not None else f'collection:{collection_id}'
        record('grant_created', admin['id'], target, account=account_id, access=access)
    return created


def create_account(store, record, admin, tenant_id, object_id, role, display_name=''):
    with store.transaction():
        created = accounts.create_account(store, tenant_id, object_id, role, display_name)
        record('account_created', admin['id'], f'account:{created}', role=role)
    return created


def ask(scoped, ai, record, account, question):
    """Answer from the visible stock; audit entry ``ai_question`` with status and source ids only.

    The AI request itself runs outside any transaction (it can take minutes). A failure of the
    provider or a used-up budget raises ``Unavailable`` after an audit entry with status
    ``unavailable`` and the error kind, never the question.
    """
    try:
        result = ai(scoped).ask(question)
    except ProviderError as error:
        record('ai_question', account['id'], status='unavailable', kind=error.kind)
        raise Unavailable(str(error)) from None
    record('ai_question', account['id'], status=result.status,
           sources=sorted({s['document_id'] for s in result.sources if s.get('document_id') is not None}))
    return result


class Limits:
    """Sliding windows per account and kind (S-06, docs/sicherheit-3.0.md: search, AI, upload).

    ponytail: kept in process memory like the sign-in limit; the supported setup is one server process.
    Upgrade path: a counter table in PostgreSQL.
    """
    DEFAULTS = {'question': (20, 10 * 60), 'upload': (60, 10 * 60), 'search': (120, 60)}

    def __init__(self, limits=None, clock=time.monotonic):
        self.limits, self.clock = {**self.DEFAULTS, **(limits or {})}, clock
        self._hits = collections.defaultdict(collections.deque)
        self._lock = threading.Lock()

    def allow(self, kind, account_id):
        """Count one request; False if ``account_id`` used up its ``kind`` in the current window."""
        limit, window = self.limits[kind]
        with self._lock:
            hits, edge = self._hits[(kind, account_id)], self.clock() - window
            while hits and hits[0] <= edge:
                hits.popleft()
            if len(hits) >= limit:
                return False
            hits.append(self.clock())
            return True
