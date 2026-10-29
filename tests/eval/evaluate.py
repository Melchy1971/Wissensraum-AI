"""Retrieval evaluation for Release 2.1 (Phase 7, decision D-014).

The versioned dataset in this folder holds a synthetic corpus, questions with their expected main
source and exclusion questions without an answer, and the vectors of the reference model for every
chunk and question. ``vectors.json`` is generated once on a machine with Ollama
(``build_vectors.py``) and checked in, so the evaluation itself needs no model and no network.

    python tests/eval/evaluate.py            # baseline report
    python tests/eval/evaluate.py --json     # machine-readable

Metrics per method (answerable questions only): Recall@1/3/10 of the main source (first entry of
``expected``) and the mean reciprocal rank. Exclusion questions report how many got no hit at all
and, for vectors, the top similarity, which a refusal threshold will have to separate.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import struct
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from core import QUERY_INSTRUCTION, Store, chunks  # noqa: F401  (re-exported for build_vectors)

KS = (1, 3, 10)


def load(name):
    return json.loads((HERE / name).read_text(encoding='utf-8'))


def sha256(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def corpus_chunks(corpus):
    """(key, document id, text) for every chunk, cut exactly as ``Store.add`` cuts it."""
    for doc in corpus:
        for ordinal, part in enumerate(chunks(doc['content'].strip()), start=1):
            yield f'{doc["id"]}#{ordinal}', doc['id'], part


def encode_vector(values):
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return base64.b64encode(struct.pack(f'<{len(values)}e', *(v / norm for v in values))).decode('ascii')


def decode_vector(text, dimension):
    raw = base64.b64decode(text)
    if len(raw) != 2 * dimension:
        raise ValueError(f'Vektor hat {len(raw) // 2} statt {dimension} Werte')
    return struct.unpack(f'<{dimension}e', raw)


def ranked_documents(hits, limit=10):
    """Document ids in order of their best hit, without repetition."""
    seen = []
    for doc_id in hits:
        if doc_id not in seen:
            seen.append(doc_id)
        if len(seen) == limit:
            break
    return seen


def fulltext_rankings(corpus, questions):
    """Rank documents with today's product search on a throw-away SQLite store."""
    with tempfile.TemporaryDirectory() as folder:
        store = Store(Path(folder) / 'eval.db')
        by_row = {}
        for doc in corpus:
            row_id, created = store.add(doc['title'], doc['content'], doc['category'])
            if not created:
                raise ValueError(f'{doc["id"]}: Inhalt doppelt im Korpus')
            by_row[row_id] = doc['id']
        rankings, seconds = {}, []
        for item in questions:
            started = time.perf_counter()
            rows = store.search(item['question'], limit=30)
            seconds.append(time.perf_counter() - started)
            rankings[item['id']] = ranked_documents(by_row[row['document_id']] for row in rows)
        return rankings, seconds, {}


def vector_rankings(corpus, questions, vectors):
    """Rank documents by cosine similarity of question and chunk vectors (exact, all chunks)."""
    dimension = vectors['dimension']
    chunk_vectors = [(doc_id, decode_vector(vectors['chunks'][key]['vector'], dimension))
                     for key, doc_id, _ in corpus_chunks(corpus)]
    rankings, seconds, top = {}, [], {}
    for item in questions:
        query = decode_vector(vectors['questions'][item['id']]['vector'], dimension)
        started = time.perf_counter()
        scored = sorted(((sum(a * b for a, b in zip(query, vector)), doc_id) for doc_id, vector in chunk_vectors),
                        reverse=True)
        seconds.append(time.perf_counter() - started)
        rankings[item['id']] = ranked_documents(doc_id for _, doc_id in scored)
        top[item['id']] = round(scored[0][0], 4)
    return rankings, seconds, top


def percentile(values, share):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(share * len(ordered)) - 1)] if ordered else 0.0


def score(questions, rankings, seconds, top):
    answerable = [q for q in questions if q['expected']]
    excluded = [q for q in questions if not q['expected']]
    result = {'answerable': len(answerable), 'excluded': len(excluded),
              'p95_ms': round(1000 * percentile(seconds, 0.95), 2)}
    for k in KS:
        hits = sum(q['expected'][0] in rankings[q['id']][:k] for q in answerable)
        result[f'recall@{k}'] = round(hits / len(answerable), 3)
    result['mrr'] = round(sum(1 / (rankings[q['id']].index(q['expected'][0]) + 1)
                              for q in answerable if q['expected'][0] in rankings[q['id']]) / len(answerable), 3)
    for kind in sorted({q['type'] for q in answerable}):
        group = [q for q in answerable if q['type'] == kind]
        result[f'recall@10[{kind}]'] = round(sum(q['expected'][0] in rankings[q['id']][:10] for q in group)
                                             / len(group), 3)
    result['excluded_without_hit'] = sum(not rankings[q['id']] for q in excluded)
    result['missed'] = [q['id'] for q in answerable if q['expected'][0] not in rankings[q['id']][:10]]
    if top:
        result['top_similarity_answerable_min'] = min(top[q['id']] for q in answerable)
        if excluded:
            result['top_similarity_excluded_max'] = max(top[q['id']] for q in excluded)
    return result


def evaluate():
    corpus, questions, vectors = load('corpus.json'), load('questions.json'), load('vectors.json')
    return {'model': vectors['model'], 'documents': len(corpus),
            'chunks': sum(1 for _ in corpus_chunks(corpus)), 'questions': len(questions),
            'fulltext': score(questions, *fulltext_rankings(corpus, questions)),
            'vector': score(questions, *vector_rankings(corpus, questions, vectors))}


def store_rankings(store, questions, embedder, model):
    """Rank documents with the product's own search paths on an existing store.

    Documents are identified by file name (title when there is none). The vector timing includes
    the question vector, as the P95 target of the gate requires.
    """
    names = {}

    def name(document_id):
        if document_id not in names:
            doc = store.get(document_id)
            names[document_id] = doc.get('filename') or doc['title']
        return names[document_id]
    fulltext, fulltext_seconds, vector, vector_seconds, top = {}, [], {}, [], {}
    for item in questions:
        started = time.perf_counter()
        rows = store.search(item['question'], limit=30)
        fulltext_seconds.append(time.perf_counter() - started)
        fulltext[item['id']] = ranked_documents(name(row['document_id']) for row in rows)
        started = time.perf_counter()
        hits = store.vector_search(model, embedder.embed([item['question']], query=True)[0], limit=30)
        vector_seconds.append(time.perf_counter() - started)
        vector[item['id']] = ranked_documents(name(hit['document_id']) for hit in hits)
        top[item['id']] = round(hits[0]['score'], 4) if hits else 0.0
    return (fulltext, fulltext_seconds, {}), (vector, vector_seconds, top)


def evaluate_own(db_path, questions_path, model):
    """Own questions against an existing database (a copy: opening it migrates the schema)."""
    from core import Embedder
    store = Store(db_path)
    questions = json.loads(Path(questions_path).read_text(encoding='utf-8'))
    with store.connect() as db:
        known = {row['filename'] or row['title'] for row in db.execute('SELECT title,filename FROM documents')}
    missing = sorted({source for q in questions for source in q['expected'] if source not in known})
    usable = [q for q in questions if set(q['expected']) <= known]
    done, total = store.vector_counts(model)
    result = {'model': model, 'documents': len(known), 'chunks': total, 'questions': len(questions),
              'chunks_with_vector': done, 'missing_sources': missing,
              'questions_without_source': [q['id'] for q in questions if q not in usable]}
    if usable:
        fulltext, vector = store_rankings(store, usable, Embedder('ollama', model), model)
        result.update(fulltext=score(usable, *fulltext), vector=score(usable, *vector))
    return result


def report(result):
    lines = [(f'Evaluationsdatensatz: {result["documents"]} Dokumente, {result["chunks"]} Abschnitte, '
              f'{result["questions"]} Fragen, Modell {result["model"]}'), '']
    names = {'fulltext': 'Volltext (heute)', 'vector': 'Vektor (exakt)'}
    rows = [key for key in result['fulltext'] if key not in ('missed',)]
    lines.append('| Kennzahl | ' + ' | '.join(names.values()) + ' |')
    lines.append('|---|' + '---:|' * len(names))
    for key in rows + ['top_similarity_answerable_min', 'top_similarity_excluded_max']:
        values = [str(result[method].get(key, '')) for method in names]
        if any(values):
            lines.append(f'| {key} | ' + ' | '.join(values) + ' |')
    for method, name in names.items():
        lines.append(f'\n{name}, Hauptquelle nicht unter den ersten 10: {", ".join(result[method]["missed"]) or "keine"}')
    if result.get('missing_sources'):
        lines.append('\nNicht im Bestand, Fragen nicht gemessen: ' + ', '.join(result['missing_sources'])
                     + ' (' + ', '.join(result['questions_without_source']) + ')')
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Retrieval-Basislinie aus dem versionierten Datensatz')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--own', type=Path, help='eigene Fragen (JSON, nicht im Repository)')
    parser.add_argument('--db', type=Path, help='mit --own: Kopie einer SQLite-Datenbank mit Vektoren')
    parser.add_argument('--model', default='qwen3-embedding:0.6b')
    args = parser.parse_args(argv)
    if args.own and not args.db:
        parser.error('--own braucht --db')
    result = evaluate_own(args.db, args.own, args.model) if args.own else evaluate()
    if args.own and 'fulltext' not in result:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    print(json.dumps(result, indent=2, ensure_ascii=False) if args.json else report(result))


if __name__ == '__main__':
    main()
