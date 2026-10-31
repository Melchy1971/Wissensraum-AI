"""Evidence assessment before and after an AI answer (Phase 7, step 7.4, decision D-019).

Two stages. Before the AI request, ``assess`` rates the unified retrieval result of step 7.3:
``unavailable`` (retrieval failed, no AI request), ``insufficient`` (nothing found, no AI request),
``weak`` (answer only with a visible uncertainty notice) or ``sufficient``. Retrieval signals alone
cannot tell an excluded question from a paraphrase: in the evaluation the best pair of signals that
let no excluded question through rejected 28 of 54 answerable ones. So the second stage is the
answering model itself: it has to reply with ``NO_SOURCE_TOKEN`` when the sources do not contain
the answer, and the application then shows the fixed refusal. ``check_answer`` validates every
answer after the request: an invented label rejects the answer, and an answer without any valid
label is not shown as an answer either (a factual answer needs at least one source).

The weak rules carry no tuned weights or thresholds; each one names an observable condition of the
retrieval result (fallback, incomplete vectors, no agreement of the two lists, full-text hits on
general words only). The evaluation report shows how often each applies.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

SUFFICIENT, WEAK, INSUFFICIENT, UNAVAILABLE = 'sufficient', 'weak', 'insufficient', 'unavailable'
STATES = (SUFFICIENT, WEAK, INSUFFICIENT, UNAVAILABLE)
LABELS = {SUFFICIENT: 'Quellenlage ausreichend', WEAK: 'Quellenlage schwach',
          INSUFFICIENT: 'Keine ausreichende Quelle', UNAVAILABLE: 'Retrieval nicht verfügbar'}
WEAK_NOTICE = 'Die vorhandenen Quellen reichen nur für eine unsichere Antwort.'
REFUSAL = 'Im Wissensbestand wurde keine ausreichend belastbare Quelle gefunden.'
UNAVAILABLE_MESSAGE = ('Die Suche im Wissensbestand ist gerade nicht verfügbar. '
                       'Es wurde keine KI-Anfrage gestellt.')
UNVERIFIED_NOTICE = 'Nicht belegt: Die KI-Antwort enthält keinen Quellenverweis und ist nicht geprüft.'
UNVERIFIED_MESSAGE = ('Die KI-Antwort enthielt keinen Quellenverweis und wird deshalb nicht als Antwort '
                      'angezeigt. Die gefundenen Dokumente stehen unten als Suchhinweise.')
NO_SOURCE_TOKEN = 'KEINE_AUSREICHENDE_QUELLE'
AGREEMENT_DEPTH = 3  # sufficient needs a document found by both lists among the first three

# Answer status stored with each run.
ANSWERED, WEAK_ANSWER, REFUSED, UNVERIFIED = 'answered', 'weak_answer', 'refused', 'unverified'

_LABEL = re.compile(r'\[Q(\d+)\]')


class InvalidCitation(ValueError):
    """The AI answer cites a source that was not provided; the answer is not stored."""


@dataclass(frozen=True)
class Assessment:
    state: str
    reasons: tuple = ()
    signals: dict = field(default_factory=dict)

    @property
    def label(self):
        return LABELS[self.state]


@dataclass
class AnswerResult:
    """What GUI and CLI show: text, sources (full for answers, without passage for refusals)."""
    text: str
    sources: list
    assessment: Assessment
    status: str  # answered, weak_answer, refused, unverified or unavailable
    mode: str | None
    cited: tuple = ()
    second_pass: bool = False  # the narrow evidence pass after a refusal ran (``AI.answer``)

    @property
    def state(self):
        return self.assessment.state

    @property
    def label(self):
        return self.assessment.label


def _coverage(question, result):
    """Share of the question's content words (first five letters) in the passage and its title."""
    from core import search_words
    words = [word.casefold()[:5] for word in search_words(question)]
    text = (str(result.get('title', '')) + ' ' + str(result.get('content', ''))).casefold()
    return sum(word in text for word in words) / len(words) if words else 0.0


def signals(retrieval, question=''):
    """Observable properties of a retrieval result; stored only as counts and flags."""
    from retrieval import HYBRID
    results = retrieval.results
    top = results[:AGREEMENT_DEPTH]
    done, total = retrieval.vectors
    return {
        'mode': retrieval.mode,
        'results': len(results),
        'documents': len({r['document_id'] for r in results}),
        'agreement': sum(bool(r.get('fulltext_rank') and r.get('vector_rank')) for r in top),
        'vectors_complete': bool(total) and done == total,
        'rrf_gap': (results[0]['rrf_score'] - results[1]['rrf_score']) if len(results) > 1 else None,
        'top_coverage': round(_coverage(question, results[0]), 3) if results else 0.0,
        'hybrid': retrieval.mode == HYBRID,
    }


def assess(retrieval, question=''):
    """Classify the evidence of one retrieval result before any AI request."""
    from retrieval import STATUS_FALLBACK, STATUS_PARTIAL, STATUS_VECTOR_ERROR
    found = signals(retrieval, question)
    if not retrieval.results:
        return Assessment(INSUFFICIENT, ('keine_treffer',), found)
    reasons = []
    if retrieval.status in (STATUS_FALLBACK, STATUS_VECTOR_ERROR):
        reasons.append('semantische_suche_ausgefallen')
    elif retrieval.status == STATUS_PARTIAL:
        reasons.append('vektorbestand_unvollstaendig')
    if found['hybrid']:
        if not found['agreement']:
            reasons.append('keine_uebereinstimmung_der_listen')
    elif found['top_coverage'] < 0.5:  # full text only: the best hit shares few of the question's words
        reasons.append('nur_allgemeine_begriffe')
    return Assessment(WEAK if reasons else SUFFICIENT, tuple(reasons), found)


def unavailable():
    return Assessment(UNAVAILABLE, ('retrieval_fehler',))


def check_answer(answer, allowed):
    """(status, cited source numbers) for a raw AI answer; raises for empty or invented citations.

    ``allowed`` is the number of sources (labels Q1 to Qn) or the source numbers of a narrower pass.
    """
    text = (answer or '').strip()
    if not text:
        raise ValueError('KI-Dienst lieferte keine Textantwort.')
    if NO_SOURCE_TOKEN in text:
        return REFUSED, ()
    cited = tuple(dict.fromkeys(int(label) for label in _LABEL.findall(text)))
    allowed = range(1, allowed + 1) if isinstance(allowed, int) else set(allowed)
    if any(number not in allowed for number in cited):
        raise InvalidCitation('KI-Antwort enthält ungültige Quellenverweise. Die Antwort wurde nicht gespeichert.')
    return (ANSWERED if cited else UNVERIFIED), cited
