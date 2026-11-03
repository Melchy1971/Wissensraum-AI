"""Hybrid retrieval for search and questions (Release 2.1, step 7.3).

Full-text and vector search each return at most ``CANDIDATE_LIMIT`` chunks. Per list, only the
best chunk of a document counts; documents are then merged with Reciprocal Rank Fusion:

    RRF(d) = sum over lists of 1 / (k + rank(d)),   ranks start at 1, k = 60

Raw scores (BM25, ts_rank_cd, cosine) are never mixed; they are kept for diagnosis only. Equal
RRF scores are ordered by document id. The vector part is used only when semantic search is
activated and vectors exist; otherwise, and whenever the local model does not answer, the
result is the full-text search with a visible status. Questions and their vectors are not
logged and not stored; a small in-memory cache keeps the vectors of the last 100 questions.
"""
from __future__ import annotations

import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from core import CANDIDATE_LIMIT, VECTOR_CACHE, Embedder

RRF_K = 60
RESULT_LIMIT = 10
QUESTION_CACHE_SIZE = 100
QUESTION_TIMEOUT_SECONDS = 15  # a hanging Ollama must not hold the search for minutes

FULLTEXT, VECTOR, HYBRID = 'fulltext', 'vector', 'hybrid'
MODES = (FULLTEXT, VECTOR, HYBRID)
STATUS_FULLTEXT = 'Volltextsuche'
STATUS_HYBRID = 'Hybride Suche'
STATUS_PARTIAL = 'Hybride Suche – Vektorbestand unvollständig'
STATUS_FALLBACK = 'Volltext-Fallback – semantisches Modell nicht erreichbar'
STATUS_VECTOR_ERROR = 'Volltext-Fallback – Vektorbestand nicht lesbar, wird neu geladen'


def best_per_document(chunks):
    """Documents in order of their first (best) chunk; each keeps that chunk."""
    best = {}
    for chunk in chunks:
        best.setdefault(chunk['document_id'], chunk)
    return list(best.values())


def rrf(fulltext, vector, k=RRF_K, limit=RESULT_LIMIT):
    """Fuse two ranked chunk lists into at most ``limit`` documents (see module docstring)."""
    merged = {}
    for method, chunks in (('fulltext', fulltext), ('vector', vector)):
        for rank, chunk in enumerate(best_per_document(chunks), start=1):
            entry = merged.setdefault(chunk['document_id'], {
                'document_id': chunk['document_id'], 'title': chunk['title'], 'category': chunk['category'],
                'fulltext_rank': None, 'vector_rank': None, 'fulltext_score': None, 'vector_score': None,
                'rrf_score': 0.0, 'chunk': chunk, 'chunk_rank': rank})
            entry[f'{method}_rank'] = rank
            entry[f'{method}_score'] = chunk.get('rank') if method == 'fulltext' else chunk.get('score')
            entry['rrf_score'] += 1.0 / (k + rank)
            if rank < entry['chunk_rank']:  # the list where the document ranks better supplies the passage
                entry['chunk'], entry['chunk_rank'] = chunk, rank
    results = sorted(merged.values(), key=lambda entry: (-entry['rrf_score'], entry['document_id']))[:limit]
    for entry in results:
        chunk = entry.pop('chunk')
        del entry['chunk_rank']
        entry.update(chunk_id=chunk['chunk_id'], ordinal=chunk['ordinal'], content=chunk['content'],
                     retrieval_method=('hybrid' if entry['fulltext_rank'] and entry['vector_rank']
                                       else 'fulltext' if entry['fulltext_rank'] else 'vector'))
    return results


@dataclass
class Retrieval:
    results: list
    mode: str
    status: str
    timings: dict = field(default_factory=dict)
    vectors: tuple = (0, 0)  # (chunks with a vector, all chunks)
    alternates: dict = field(default_factory=dict)  # document id -> best chunk of the other list (answer context)


def alternate_passages(fulltext, vector, results):
    """For each result document the best chunk of the list that did not supply its passage.

    ``rrf`` keeps one passage per document, from the list where the document ranks better (the
    full-text list on equal ranks). The other list may rank a different section of the same
    document first; that section is returned here for the answer context. Documents found by one
    list only, or whose best chunk is the same in both lists, get none. The search result itself
    and its ranking are not changed.
    """
    best = {method: {chunk['document_id']: chunk for chunk in best_per_document(chunks)}
            for method, chunks in (('fulltext', fulltext), ('vector', vector))}
    alternates = {}
    for result in results:
        for method in ('fulltext', 'vector'):
            chunk = best[method].get(result['document_id'])
            if chunk and chunk['chunk_id'] != result['chunk_id']:
                alternates[result['document_id']] = chunk
    return alternates


class Retriever:
    """The one retrieval path for the search list, questions to the AI and the CLI."""

    def __init__(self, store, embedder=None, clock=time.perf_counter):
        self.store, self.embedder, self.clock = store, embedder or Embedder, clock
        self.questions = OrderedDict()  # (model, normalized question) -> vector, at most 100
        self.fallbacks = 0
        # Full text runs beside question embedding and vector search: the database (PostgreSQL
        # server, SQLite in C) and numpy both release the GIL, so the latencies overlap.
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='volltext')

    def _semantic_model(self):
        from vector_sync import VectorSync
        state = VectorSync(self.store).config()
        return state['model'] if state['enabled'] else None

    def _question_vector(self, model, question):
        key = (model, ' '.join(question.split()).casefold())
        if key in self.questions:
            self.questions.move_to_end(key)
            return self.questions[key]
        embedder = self.embedder('ollama', model)
        embedder.timeout = QUESTION_TIMEOUT_SECONDS
        vector = embedder.embed([question], query=True)[0]
        self.questions[key] = vector
        if len(self.questions) > QUESTION_CACHE_SIZE:
            self.questions.popitem(last=False)
        return vector

    def search(self, question, limit=RESULT_LIMIT, mode=None, tag=None, all_versions=False):
        """Ranked documents with their best passage; ``mode`` None means hybrid when possible."""
        if mode is not None and mode not in MODES:
            raise ValueError('Suchmodus: fulltext, vector oder hybrid.')
        timings = {}
        started = self.clock()

        def full_text():
            if mode == VECTOR:
                return []
            hits = self.store.search(question, limit=CANDIDATE_LIMIT, all_versions=all_versions, tag=tag)
            timings['fulltext'] = self.clock() - started
            return hits
        pending = self.pool.submit(full_text)
        try:
            return self._combine(question, limit, mode, tag, all_versions, pending, timings, started)
        except Exception:
            pending.result()  # a full-text error comes first, as when both ran one after the other
            raise

    def _combine(self, question, limit, mode, tag, all_versions, pending, timings, started):
        model = self._semantic_model() if mode != FULLTEXT else None
        if model is None:
            if mode == VECTOR:
                pending.result()
                raise ValueError('Semantische Suche ist nicht aktiviert.')
            return self._fulltext_only(pending.result(), limit, STATUS_FULLTEXT, timings, started)
        done, total = self.store.vector_counts(model)
        if not done:
            return self._fulltext_only(pending.result(), limit, STATUS_FULLTEXT, timings, started, (done, total))
        try:
            step = self.clock()
            probe = self._question_vector(model, question)
            timings['embedding'] = self.clock() - step
        except ValueError:  # Ollama stopped or the model is missing
            return self._fallback(mode, pending.result(), limit, STATUS_FALLBACK, timings, started, (done, total))
        try:
            step = self.clock()
            vector = self.store.vector_search(model, probe, limit=CANDIDATE_LIMIT, all_versions=all_versions, tag=tag)
            timings['vector'] = self.clock() - step
        except ValueError:  # wrong dimension or damaged vector: drop the matrix, the next search reloads it
            VECTOR_CACHE.clear()
            return self._fallback(mode, pending.result(), limit, STATUS_VECTOR_ERROR, timings, started,
                                  (done, total))
        fulltext = pending.result()
        step = self.clock()
        results = rrf(fulltext, vector, limit=limit)
        timings['fusion'] = self.clock() - step
        timings['total'] = self.clock() - started
        status = STATUS_HYBRID if done == total else STATUS_PARTIAL
        return Retrieval(results, VECTOR if mode == VECTOR else HYBRID, status, timings, (done, total),
                         alternate_passages(fulltext, vector, results))

    def _fallback(self, mode, fulltext, limit, status, timings, started, vectors):
        self.fallbacks += 1
        if mode == VECTOR:
            raise ValueError(status)
        return self._fulltext_only(fulltext, limit, status, timings, started, vectors)

    def _fulltext_only(self, fulltext, limit, status, timings, started, vectors=(0, 0)):
        results = rrf(fulltext, [], limit=limit)
        timings['total'] = self.clock() - started
        return Retrieval(results, FULLTEXT, status, timings, vectors)


def as_sources(retrieval):
    """The passages in the shape the AI adapter expects (title, document, section, text)."""
    return [{'chunk_id': r['chunk_id'], 'document_id': r['document_id'], 'title': r['title'],
             'category': r['category'], 'ordinal': r['ordinal'], 'content': r['content']}
            for r in retrieval.results]


CONTEXT_SECTIONS = 10  # sections in one AI request
NEIGHBOR_DOCUMENTS = 3  # only the three best documents get a neighbouring section
ALTERNATE_DOCUMENTS = 3  # the three best documents also get the best section of the other search list
NARROW_DOCUMENTS, NARROW_SECTIONS = 3, 6  # second evidence pass after a refusal
CONTEXT_CHARS = 14400  # without a caller's budget: eight full sections of 1,800 characters


def _within(budget):
    return lambda sources: sum(len(source['content']) for source in sources) <= budget


def word_coverage(question, text):
    """Share of the question's content words (first five letters) in ``text``, as in the evidence check."""
    from core import search_words
    words = [word.casefold()[:5] for word in search_words(question)]
    text = text.casefold()
    return sum(word in text for word in words) / len(words) if words else 0.0


def answer_context(retrieval, store, question, fits=None, limit=CONTEXT_SECTIONS, neighbors=NEIGHBOR_DOCUMENTS,
                   alternates=ALTERNATE_DOCUMENTS):
    """The passages for ``AI.ask``: the hits of step 7.3, alternate and neighbouring sections of the best documents.

    The document ranking stays as it is. First the passages of the ``alternates`` best documents,
    then, for each of them, the best section of the other search list if it differs
    (``Retrieval.alternates``, step 7.4), then the passages of the remaining hit documents, each
    while ``fits`` accepts the selection (the AI adapter checks its context window, default:
    14,400 characters). Then, for the ``neighbors`` best documents, at most one more section: of the
    sections directly before and after the hit, the one sharing more of the question's words;
    none if it shares none. A section already chosen, or one whose text equals or is contained in
    a chosen one, is dropped. At most ``limit`` sections. Sections of a document stay together in
    reading order, documents keep their rank, so the labels Q1, Q2, ... follow from the retrieval
    result and each one names exactly one chunk.
    """
    fits = fits or _within(CONTEXT_CHARS)
    hits = as_sources(retrieval)
    chosen = []

    def add(source):
        text = ' '.join(source['content'].split())
        if (len(chosen) >= limit or not fits(chosen + [source])
                or any(source['chunk_id'] == other['chunk_id'] or text in ' '.join(other['content'].split())
                       for other in chosen)):
            return False
        chosen.append(source)
        return True
    for hit in hits[:alternates]:
        add(hit)
    for hit in [hit for hit in hits[:alternates] if hit in chosen]:
        other = getattr(retrieval, 'alternates', {}).get(hit['document_id'])
        if other:
            add({'chunk_id': other['chunk_id'], 'document_id': hit['document_id'], 'title': hit['title'],
                 'category': hit['category'], 'ordinal': other['ordinal'], 'content': other['content'],
                 'alternate_of': hit['chunk_id']})
    for hit in hits[alternates:]:
        add(hit)
    for hit in [hit for hit in hits if hit in chosen][:neighbors]:
        rows = [row for row in store.adjacent_chunks(hit['document_id'], hit['ordinal'])
                if row['chunk_id'] != hit['chunk_id']]
        scored = sorted(rows, key=lambda row: (-word_coverage(question, row['content']), row['ordinal']))
        if scored and word_coverage(question, scored[0]['content']) > 0:
            add({'chunk_id': scored[0]['chunk_id'], 'document_id': hit['document_id'], 'title': hit['title'],
                 'category': hit['category'], 'ordinal': scored[0]['ordinal'], 'content': scored[0]['content'],
                 'neighbor_of': hit['chunk_id']})
    rank = {hit['document_id']: number for number, hit in enumerate(hits)}
    return sorted(chosen, key=lambda source: (rank[source['document_id']], source['ordinal']))


def narrow_context(sources, documents=NARROW_DOCUMENTS, limit=NARROW_SECTIONS):
    """Source numbers (1-based, as labelled) of the ``documents`` best documents, at most ``limit``."""
    best = list(dict.fromkeys(source['document_id'] for source in sources))[:documents]
    return [number for number, source in enumerate(sources, start=1) if source['document_id'] in best][:limit]
