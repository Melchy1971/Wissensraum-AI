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


def change_document(store, record, account, doc_id, revision, title=None, category=None, status=None, owner=None,
                    review_date=None, tags=None):
    """All metadata of the document page at once; ``tags`` is the complete new list (comma separated).

    The tag list becomes additions and removals against the current tags, as on the desktop. Only
    changed fields count; the audit entry names them, never their values.
    """
    import rules
    changes = {key: value for key, value in (('title', title), ('category', category), ('status', status),
                                              ('owner', owner), ('review_date', review_date)) if value is not None}
    with store.transaction():
        if tags is not None:
            wanted = rules.parse_tags(tags)
            current = store.document_tags(doc_id)
            keep = {name.casefold() for name in wanted}
            have = {name.casefold() for name in current}
            changes['add_tags'] = [name for name in wanted if name.casefold() not in have]
            changes['remove_tags'] = [name for name in current if name.casefold() not in keep]
        changed = store.update_metadata(doc_id, revision=revision, **changes)
        if changed:
            record('document_changed', account['id'], f'document:{doc_id}',
                   fields=','.join(sorted({c['field'] for c in changed})))
    return changed


def delete_document(store, record, account, doc_id, revision=None):
    """Delete with everything that hangs on it (sections, vectors, grants); audit ``document_deleted``.

    ``revision`` as read by the caller; a document changed meanwhile raises ``Conflict`` and stays.
    """
    from core import _revise
    with store.transaction() as db:
        if revision is not None:
            _revise(db, 'documents', doc_id, revision, 'Das Dokument')
        store.delete(doc_id)
        record('document_deleted', account['id'], f'document:{doc_id}')


def create_collection(store, record, account, name):
    """New collection with owner grant for ``account`` (so editors can share it) and audit entry."""
    with store.transaction():
        collection_id = store.create_collection(name)
        accounts.grant(store, account['id'], 'owner', collection_id=collection_id, end_sessions=False)
        record('collection_changed', account['id'], f'collection:{collection_id}', created=True)
    return collection_id


def change_collection(store, record, account, collection_id, revision, name=None, add=(), remove=()):
    """Exactly one change: rename, add documents or remove documents. Visibility checked by the caller."""
    if [bool(name), bool(add), bool(remove)].count(True) != 1:
        raise ValueError('Genau eine Änderung angeben: Name, hinzufügen oder entfernen.')
    with store.transaction():
        if name:
            store.rename_collection(collection_id, name, revision)
        elif add:
            store.add_to_collection(collection_id, list(add), revision)
        else:
            store.remove_from_collection(collection_id, list(remove), revision)
        record('collection_changed', account['id'], f'collection:{collection_id}', added=list(add),
               removed=list(remove), renamed=bool(name))


def delete_collection(store, record, account, collection_id, revision=None):
    """Delete the collection; its documents stay. Grants on it go with it (foreign key)."""
    with store.transaction():
        store.delete_collection(collection_id, revision)
        record('collection_changed', account['id'], f'collection:{collection_id}', deleted=True)


def relate(store, record, account, source_id, target_id, kind, note=''):
    with store.transaction():
        relation_id = store.relate(source_id, target_id, kind, note)
        record('document_changed', account['id'], f'document:{source_id}', fields='relation')
    return relation_id


def unrelate(store, record, account, doc_id, relation_id):
    with store.transaction():
        store.unrelate(relation_id)
        record('document_changed', account['id'], f'document:{doc_id}', fields='relation')


def summarize(scoped, ai, record, account, doc_id):
    """Summary of one visible document; audit ``ai_question`` with kind summary and the document id."""
    try:
        answer, _ = ai(scoped).summarize(doc_id)
    except ProviderError as error:
        record('ai_question', account['id'], f'document:{doc_id}', status='unavailable', kind=error.kind)
        raise Unavailable(str(error)) from None
    record('ai_question', account['id'], f'document:{doc_id}', status='summary')
    return answer


def set_exclude_retired(store, record, admin, enabled):
    """Whether AI answers leave out outdated and archived documents, for the whole installation."""
    with store.transaction():
        store.set_exclude_retired(bool(enabled))
        record('settings_changed', admin['id'], 'settings:exclude_retired', enabled=bool(enabled))


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


def set_semantic_search(store, record, admin, enabled, model=None):
    """Switch semantic search for the whole installation; only administrators call this.

    Switching on first sends one probe to Ollama, outside the transaction (it can take seconds); a
    missing model or service raises ``ValueError`` and changes nothing. The worker embeds afterwards.
    """
    from vector_sync import VectorSync
    sync = VectorSync(store)
    model = sync.probe(model) if enabled else None
    with store.transaction():
        state = sync.enable(model) if enabled else sync.deactivate()
        record('settings_changed', admin['id'], 'settings:vectors', enabled=bool(enabled), model=state['model'])
    return state


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
