"""Retrieval evaluation for Release 2.1 (Phase 7, decision D-014).

The versioned dataset in this folder holds a synthetic corpus, questions with their expected main
source and exclusion questions without an answer, and the vectors of the reference model for every
chunk and question. ``vectors.json`` is generated once on a machine with Ollama
(``build_vectors.py``) and checked in, so the evaluation itself needs no model and no network.

    python tests/eval/evaluate.py            # baseline report
    python tests/eval/evaluate.py --json     # machine-readable
    python tests/eval/evaluate.py --answers qwen2.5:7b-instruct   # evidence end to end (local Ollama)

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


METHODS = ('fulltext', 'vector', 'hybrid')


def method_rankings(store, questions, question_vector, model, name):
    """Document rankings of full text, vector and hybrid search through the product paths.

    ``question_vector(item)`` supplies the question's vector, ``name(document_id)`` the key the
    questions use for documents. Times: full text alone; vector including the question vector;
    hybrid including both lists and the fusion, as the gate counts it.
    """
    from retrieval import CANDIDATE_LIMIT, rrf
    rankings = {method: {} for method in METHODS}
    seconds = {method: [] for method in METHODS}
    top = {}
    for item in questions:
        started = time.perf_counter()
        rows = store.search(item['question'], limit=CANDIDATE_LIMIT)
        fulltext_time = time.perf_counter() - started
        started = time.perf_counter()
        hits = store.vector_search(model, question_vector(item), limit=CANDIDATE_LIMIT)
        vector_time = time.perf_counter() - started
        started = time.perf_counter()
        fused = rrf(rows, hits, limit=10)
        fusion_time = time.perf_counter() - started
        rankings['fulltext'][item['id']] = ranked_documents(name(row['document_id']) for row in rows)
        rankings['vector'][item['id']] = ranked_documents(name(hit['document_id']) for hit in hits)
        rankings['hybrid'][item['id']] = [name(entry['document_id']) for entry in fused]
        seconds['fulltext'].append(fulltext_time)
        seconds['vector'].append(vector_time)
        seconds['hybrid'].append(fulltext_time + vector_time + fusion_time)
        top[item['id']] = round(hits[0]['score'], 4) if hits else 0.0
    return {method: (rankings[method], seconds[method], top if method == 'vector' else {}) for method in METHODS}


GROUPS = ('wortgleich', 'umschreibung', 'eigen', 'ausschluss')
ACCEPTED = ('answered', 'weak_answer')  # shown as an answer, with at least one valid source


REFUSAL_REASONS = {'refused': 'modell_verweigert', 'unverified': 'ohne_quellenkennung',
                   'invalid_citation': 'ungueltige_kennung', 'error': 'providerfehler'}


def evidence_outcomes(store, questions, question_vector, model, answer=None, name=lambda doc_id: doc_id):
    """Evidence state before the AI and, with ``answer(question, sources, state)``, the final outcome.

    Uses the retrieval of ``AI.ask`` (hybrid, at most 8 documents), its answer context and the
    product's ``assess`` and ``check_answer``. Without ``answer`` only the state before the AI is
    reported. Each entry also carries a diagnosis (rank of the expected source, passed documents
    and sections, error class, duration) without any document text.
    """
    from core import ProviderError
    from evidence import INSUFFICIENT, InvalidCitation, assess, check_answer
    from retrieval import CANDIDATE_LIMIT, HYBRID, STATUS_HYBRID, Retrieval, answer_context, rrf
    outcomes = {}
    for item in questions:
        rows = store.search(item['question'], limit=CANDIDATE_LIMIT)
        hits = store.vector_search(model, question_vector(item), limit=CANDIDATE_LIMIT)
        ranking = [name(entry['document_id']) for entry in rrf(rows, hits, limit=CANDIDATE_LIMIT)]
        retrieval = Retrieval(rrf(rows, hits, limit=8), HYBRID, STATUS_HYBRID, {}, (1, 1))
        assessment = assess(retrieval, item['question'])
        sources = answer_context(retrieval, store)  # as in AI.ask
        passed = [{'label': f'Q{number}', 'document_id': s['document_id'], 'document': name(s['document_id']),
                   'title': s['title'], 'chunk_id': s['chunk_id'], 'section': s['ordinal'],
                   'role': 'nachbar' if 'neighbor_of' in s else 'treffer'}
                  for number, s in enumerate(sources, start=1)]
        expected = item['expected'][0] if item['expected'] else None
        entry = {'state': assessment.state, 'reasons': list(assessment.reasons), 'expected': expected,
                 'expected_rank': ranking.index(expected) + 1 if expected in ranking else None,
                 'expected_passed': any(p['document'] == expected for p in passed) if expected else None,
                 'passed': passed}
        if assessment.state == INSUFFICIENT:
            entry.update(status='refused', reason='keine_treffer')
        elif answer:
            started = time.perf_counter()
            try:
                status, cited = check_answer(answer(item['question'], sources, assessment.state), len(sources))
            except InvalidCitation:
                status, cited = 'invalid_citation', ()
            except ProviderError as error:  # transport, timeout, cut-off or empty answer: no answer shown
                status, cited = 'error', ()
                entry.update(error_kind=error.kind, error_detail=error.detail)
            except ValueError:
                status, cited = 'error', ()
                entry.update(error_kind='other')
            if status == 'answered' and assessment.state == 'weak':
                status = 'weak_answer'
            entry.update(status=status, seconds=round(time.perf_counter() - started, 2),
                         cited=[name(sources[number - 1]['document_id']) for number in cited],
                         cited_chunks=[sources[number - 1]['chunk_id'] for number in cited])
            if status in REFUSAL_REASONS:
                entry['reason'] = REFUSAL_REASONS[status]
        outcomes[item['id']] = entry
    return outcomes


def evidence_score(questions, outcomes):
    """Confusion matrix and rates; accept means shown as an answer, reject means refused."""
    by_group = {}
    for group in GROUPS:
        members = [q for q in questions if q['type'] == group]
        if members:
            states = {state: sum(outcomes[q['id']]['state'] == state for q in members)
                      for state in ('sufficient', 'weak', 'insufficient')}
            by_group[group] = {'questions': len(members), 'before_ai': states}
    result = {'groups': by_group}
    if not all('status' in outcomes[q['id']] for q in questions):
        return result
    answerable = [q for q in questions if q['expected']]
    excluded = [q for q in questions if not q['expected']]
    accepted = lambda q: outcomes[q['id']]['status'] in ACCEPTED  # noqa: E731
    ta, fr = sum(map(accepted, answerable)), sum(not accepted(q) for q in answerable)
    fa, tr = sum(map(accepted, excluded)), sum(not accepted(q) for q in excluded)
    shown = [q for q in questions if accepted(q)]
    with_source = [q for q in shown if outcomes[q['id']]['cited']]
    result.update({
        'confusion': {'beantwortbar': {'angenommen': ta, 'verweigert': fr},
                      'ausschluss': {'angenommen': fa, 'verweigert': tr}},
        'true_accept': ta, 'true_reject': tr, 'false_accept': fa, 'false_reject': fr,
        'precision_answerable': round(ta / (ta + fa), 3) if ta + fa else None,
        'recall_answerable': round(ta / len(answerable), 3) if answerable else None,
        'false_accept_rate': round(fa / len(excluded), 3) if excluded else None,
        'false_reject_rate': round(fr / len(answerable), 3) if answerable else None,
        'accepted_with_source': f'{len(with_source)}/{len(shown)}',
        'main_source_cited': f'{sum(q["expected"][0] in outcomes[q["id"]]["cited"] for q in answerable if accepted(q))}/{ta}',
        'status': {status: sum(o['status'] == status for o in outcomes.values())
                   for status in sorted({o['status'] for o in outcomes.values()})},
        'answer_p95_ms': round(1000 * percentile([o['seconds'] for o in outcomes.values() if 'seconds' in o], 0.95), 1),
        'per_question': {q['id']: outcomes[q['id']]['status'] for q in questions},  # ids and status only
        'false_accepts': [q['id'] for q in excluded if accepted(q)],
        'false_rejects': [q['id'] for q in answerable if not accepted(q)],
        # technical errors count as false rejects for the gate but are shown apart from model refusals
        'false_reject_causes': {cause: [q['id'] for q in answerable if outcomes[q['id']]['status'] == status]
                                for status, cause in REFUSAL_REASONS.items()},
        'diagnosis': {q['id']: outcomes[q['id']] for q in questions}})
    for group, data in by_group.items():
        members = [q for q in questions if q['type'] == group]
        data['accepted'] = sum(map(accepted, members))
    return result


def ollama_answerer(model):
    """The product's answer request against the local Ollama, as ``answer(question, sources, state)``."""
    from core import AI
    ai = AI(None, '', model, provider='ollama')
    return lambda question, sources, state: ai.generate(question, sources, state, check=False)


def corpus_store(folder, corpus, vectors):
    """The corpus in a throw-away SQLite store, with the checked-in vectors saved like the product does."""
    store = Store(Path(folder) / 'eval.db')
    by_row = {}
    for doc in corpus:
        row_id, created = store.add(doc['title'], doc['content'], doc['category'])
        if not created:
            raise ValueError(f'{doc["id"]}: Inhalt doppelt im Korpus')
        by_row[row_id] = doc['id']
    with store.connect() as db:
        keys = {f'{by_row[row["document_id"]]}#{row["ordinal"]}': row['id']
                for row in db.execute('SELECT id,document_id,ordinal FROM chunks')}
    store.save_vectors(vectors['model'], {keys[key]: decode_vector(entry['vector'], vectors['dimension'])
                                          for key, entry in vectors['chunks'].items()})
    return store, by_row


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


def evaluate(answers=None):
    corpus, questions, vectors = load('corpus.json'), load('questions.json'), load('vectors.json')
    vector = lambda item: decode_vector(vectors['questions'][item['id']]['vector'], vectors['dimension'])  # noqa: E731
    with tempfile.TemporaryDirectory() as folder:
        store, by_row = corpus_store(folder, corpus, vectors)
        measured = method_rankings(store, questions, vector, vectors['model'], by_row.__getitem__)
        outcomes = evidence_outcomes(store, questions, vector, vectors['model'],
                                     ollama_answerer(answers) if answers else None, by_row.__getitem__)
    return {'model': vectors['model'], 'documents': len(corpus),
            'chunks': sum(1 for _ in corpus_chunks(corpus)), 'questions': len(questions),
            **{method: score(questions, *measured[method]) for method in METHODS},
            'evidence': evidence_score(questions, outcomes), 'answer_model': answers}


def store_rankings(store, questions, embedder, model):
    """Own questions on an existing store; documents are identified by file name, else title."""
    names = {}

    def name(document_id):
        if document_id not in names:
            doc = store.get(document_id)
            names[document_id] = doc.get('filename') or doc['title']
        return names[document_id]
    return method_rankings(store, questions, lambda item: embedder.embed([item['question']], query=True)[0],
                           model, name)


def evaluate_own(db_path, questions_path, model, answers=None):
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
        embedder = Embedder('ollama', model)
        measured = store_rankings(store, usable, embedder, model)
        result.update({method: score(usable, *measured[method]) for method in METHODS})
        names = {}

        def name(document_id):
            if document_id not in names:
                doc = store.get(document_id)
                names[document_id] = doc.get('filename') or doc['title']
            return names[document_id]
        outcomes = evidence_outcomes(store, usable, lambda item: embedder.embed([item['question']], query=True)[0],
                                     model, ollama_answerer(answers) if answers else None, name)
        result.update(evidence=evidence_score(usable, outcomes), answer_model=answers)
    return result


def report(result):
    lines = [(f'Evaluationsdatensatz: {result["documents"]} Dokumente, {result["chunks"]} Abschnitte, '
              f'{result["questions"]} Fragen, Modell {result["model"]}'), '']
    names = {'fulltext': 'Volltext', 'vector': 'Vektor (exakt)', 'hybrid': 'Hybrid (RRF k=60)'}
    rows = [key for key in result['fulltext'] if key not in ('missed',)]
    lines.append('| Kennzahl | ' + ' | '.join(names.values()) + ' |')
    lines.append('|---|' + '---:|' * len(names))
    for key in rows + ['top_similarity_answerable_min', 'top_similarity_excluded_max']:
        values = [str(result[method].get(key, '')) for method in names]
        if any(values):
            lines.append(f'| {key} | ' + ' | '.join(values) + ' |')
    for method, name in names.items():
        lines.append(f'\n{name}, Hauptquelle nicht unter den ersten 10: {", ".join(result[method]["missed"]) or "keine"}')
    evidence = result.get('evidence')
    if evidence:
        lines.append('\nQuellenlage vor der KI-Anfrage (sufficient / weak / insufficient):')
        for group, data in evidence['groups'].items():
            states = data['before_ai']
            accepted = f', angenommen nach Antwort {data["accepted"]}' if 'accepted' in data else ''
            lines.append(f'- {group}: {data["questions"]} Fragen, {states["sufficient"]} / {states["weak"]} / '
                         f'{states["insufficient"]}{accepted}')
        if 'confusion' in evidence:
            matrix = evidence['confusion']
            lines += [f'\nEnd-to-end mit {result.get("answer_model")} (angenommen = als Antwort gezeigt):', '',
                      '| tatsächlich | angenommen | verweigert |', '|---|---:|---:|',
                      f'| beantwortbar | {matrix["beantwortbar"]["angenommen"]} | {matrix["beantwortbar"]["verweigert"]} |',
                      f'| Ausschluss | {matrix["ausschluss"]["angenommen"]} | {matrix["ausschluss"]["verweigert"]} |', '']
            for key in ('precision_answerable', 'recall_answerable', 'false_accept_rate', 'false_reject_rate',
                        'accepted_with_source', 'main_source_cited', 'status', 'answer_p95_ms',
                        'false_accepts', 'false_rejects', 'false_reject_causes'):
                lines.append(f'- {key}: {evidence[key]}')
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
    parser.add_argument('--answers', metavar='OLLAMA_MODELL',
                        help='Quellenlage end-to-end: Antworten vom lokalen Ollama-Modell (kein Cloud-Dienst)')
    args = parser.parse_args(argv)
    if args.own and not args.db:
        parser.error('--own braucht --db')
    result = evaluate_own(args.db, args.own, args.model, args.answers) if args.own else evaluate(args.answers)
    if args.own and 'fulltext' not in result:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    print(json.dumps(result, indent=2, ensure_ascii=False) if args.json else report(result))


if __name__ == '__main__':
    main()
