"""Reproducible reference stock and latency benchmark for hybrid retrieval (Phase 7, step 7.3).

    python tests/bench/benchmark.py                       # SQLite, synthetic question vectors
    python tests/bench/benchmark.py --ollama              # question vectors from the local Ollama
    python tests/bench/benchmark.py --postgres            # PostgreSQL test database (PGTEST_* variables)

Builds 10,000 synthetic documents (about 50,000 chunks, fixed seed, eight topic areas with near
neighbours, lengths from one to twelve chunks) in a throw-away store, plus one 1,024-dimensional,
L2-normalized vector per chunk drawn around topic centres. Nothing personal is generated and no
database file is kept or checked in. Then it measures a cold first search, 100 warm searches over
40 different questions (12 of them asked six times) and reports median, P90, P95, P99 and maximum
per stage, cache memory, cache reloads and full-text fallbacks.

PostgreSQL uses the destructive test rules of tests/test_postgres.py: PGTEST_ALLOW_DESTRUCTIVE=1
and a database whose name ends in _test; the schema is reset before and after the run.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import numpy  # noqa: E402

import core  # noqa: E402
from core import VECTOR_CACHE, Store, _load_vector_matrix  # noqa: E402
from retrieval import Retriever  # noqa: E402
from vector_sync import VectorSync  # noqa: E402

SEED = 20261029
DOCUMENTS = 10_000
DIMENSION = 1024
MODEL = 'qwen3-embedding:0.6b'
TOPICS = {
    'Haus': 'heizung therme wartung dach fenster keller garten hecke rasen pumpe zisterne druck ventil',
    'Versicherung': 'police beitrag schaden kasko haftpflicht hausrat selbstbeteiligung kündigung frist deckung',
    'Finanzen': 'konto abschlag rechnung steuer beleg sparplan depot kreditkarte budget gebühr zins',
    'Beruf': 'prozess ticket freigabe workflow projekt meeting protokoll urlaub gleitzeit vertrag kunde',
    'Mobilität': 'auto wallbox akku reifen inspektion bahn fahrrad kilometer ladung werkstatt tank',
    'Technik': 'server backup router netzwerk festplatte container update drucker notebook passwort',
    'Familie': 'schule klasse elternabend nachhilfe geburtstag kalender musikschule ferien betreuung',
    'Freizeit': 'verein training reise hotel wanderung buch spiel kamera lauf turnier strand',
}
FILLER = ('der die das und mit für im am zum von bei nach ist wird wurde hat sind laut gemäß bitte noch '
          'wieder zusätzlich jährlich monatlich geplant erledigt offen geprüft vereinbart').split()


def generate_documents(rng):
    """(title, category, text) for every document; neighbours share a topic and most words."""
    names = list(TOPICS)
    documents = []
    for number in range(DOCUMENTS):
        topic = names[number % len(names)]
        words = TOPICS[topic].split()
        focus = rng.sample(words, 3)
        length = rng.choice((1, 2, 3, 4, 5, 5, 6, 6, 7, 8, 10, 12))  # chunks of about 1,600 new characters
        sentences = []
        while sum(len(s) + 1 for s in sentences) < length * 1600 - 200:
            sentence = [rng.choice(focus if rng.random() < 0.4 else words) if rng.random() < 0.45
                        else rng.choice(FILLER) for _ in range(rng.randint(8, 16))]
            sentences.append(' '.join(sentence).capitalize() + f' {rng.randint(1, 9999)}.')
        documents.append((f'{topic} {" ".join(focus)} {number}', topic, ' '.join(sentences)))
    return documents


def topic_vectors(nrng, count):
    centres = nrng.standard_normal((len(TOPICS), DIMENSION)).astype(numpy.float32)
    return centres / numpy.linalg.norm(centres, axis=1, keepdims=True)


def chunk_vector(nrng, centre):
    vector = centre + 0.6 * nrng.standard_normal(DIMENSION).astype(numpy.float32) / math.sqrt(DIMENSION) * 8
    return vector / numpy.linalg.norm(vector)


class SyntheticEmbedder:
    """Question vectors near a topic centre, for runs without Ollama (embedding time then ~0)."""
    centres = None

    def __init__(self, provider, model):
        self.rng = numpy.random.default_rng(SEED + 1)

    def embed(self, texts, query=False):
        vectors = []
        for text in texts:
            topic = next((i for i, name in enumerate(TOPICS) if name.casefold() in text.casefold()), 0)
            vectors.append(chunk_vector(self.rng, self.centres[topic]).tolist())
        return vectors


def build(store, log):
    rng = random.Random(SEED)
    nrng = numpy.random.default_rng(SEED)
    documents = generate_documents(rng)
    started = time.perf_counter()
    for index, (title, category, text) in enumerate(documents, start=1):
        store.add(title, text, category)
        if index % 2000 == 0:
            log(f'  {index} Dokumente ({time.perf_counter() - started:.0f} s)')
    centres = topic_vectors(nrng, len(TOPICS))
    SyntheticEmbedder.centres = centres
    with store.connect() as db:
        rows = [(row['id'], row['category']) for row in db.execute(
            'SELECT c.id,d.category FROM chunks c JOIN documents d ON d.id=c.document_id ORDER BY c.id')]
    names = list(TOPICS)
    for start in range(0, len(rows), 2000):
        store.save_vectors(MODEL, {chunk_id: chunk_vector(nrng, centres[names.index(category)])
                                   for chunk_id, category in rows[start:start + 2000]})
    log(f'  {len(rows)} Abschnitte mit Vektoren ({time.perf_counter() - started:.0f} s)')
    return len(documents), len(rows)


def questions(rng):
    distinct = []
    for topic, words in TOPICS.items():
        for _ in range(5):
            picked = rng.sample(words.split(), 2)
            distinct.append(f'Was gilt für {picked[0]} und {picked[1]} im Bereich {topic}?')
    sequence = distinct + [question for question in distinct[:12] for _ in range(5)]  # 40 + 60 = 100
    rng.shuffle(sequence)
    return distinct, sequence


def stats(values):
    ordered = sorted(values)

    def at(share):
        return ordered[min(len(ordered) - 1, max(0, math.ceil(share * len(ordered)) - 1))]
    return {'median': round(1000 * at(0.5), 2), 'p90': round(1000 * at(0.9), 2), 'p95': round(1000 * at(0.95), 2),
            'p99': round(1000 * at(0.99), 2), 'max': round(1000 * ordered[-1], 2), 'n': len(ordered)}


def measure(store, embedder, log):
    rng = random.Random(SEED + 2)
    distinct, sequence = questions(rng)
    VectorSync(store, embedder).activate(MODEL)
    VECTOR_CACHE.clear()
    with store.connect() as db:
        started = time.perf_counter()
        matrix = _load_vector_matrix(db, 'messung', MODEL)
        load_seconds = time.perf_counter() - started
    del matrix
    VECTOR_CACHE.clear()
    retriever = Retriever(store, embedder)
    loads_before = VECTOR_CACHE.loads
    cold = retriever.search(distinct[0])
    cold_timings = {name: round(1000 * value, 2) for name, value in cold.timings.items()}
    retriever.questions.clear()
    for question in distinct[:5]:  # warm-up: cache in place, Ollama model loaded
        retriever.search(question)
    retriever.questions.clear()
    per_stage, modes = {}, {}
    warm_loads = VECTOR_CACHE.loads
    for question in sequence:
        result = retriever.search(question)
        modes[result.mode] = modes.get(result.mode, 0) + 1
        for name, value in result.timings.items():
            per_stage.setdefault(name, []).append(value)
        if embedder is SyntheticEmbedder:  # full text overlaps a real embedding, total minus it would mislead
            per_stage.setdefault('retrieval_ohne_frage_vektor', []).append(
                result.timings['total'] - result.timings.get('embedding', 0.0))
    entry = VECTOR_CACHE.entry
    return {'fragen_verschieden': len(set(sequence)), 'suchen_warm': len(sequence),
            'cache_laden_db_ms': round(1000 * load_seconds, 1),
            'kalt_erste_suche_ms': cold_timings,
            'cache_neuladungen_kalt': warm_loads - loads_before,
            'cache_neuladungen_warm': VECTOR_CACHE.loads - warm_loads,
            'cache_speicher_mib': round(entry.nbytes / 2**20, 1) if entry else 0,
            'modi': modes, 'volltext_fallbacks': retriever.fallbacks,
            'warm_ms': {name: stats(values) for name, values in per_stage.items()}}


def postgres_store():
    sys.path.insert(0, str(ROOT / 'tests'))
    from test_postgres import _disposable_database_name
    from core import PostgresStore
    database = _disposable_database_name(os.environ)
    profile = {'host': os.environ.get('PGTEST_HOST', '127.0.0.1'), 'port': int(os.environ.get('PGTEST_PORT', '5432')),
               'database': database, 'username': os.environ.get('PGTEST_USERNAME', 'wissensraum_test'),
               'sslmode': os.environ.get('PGTEST_SSLMODE', 'disable')}
    password = os.environ['PGTEST_PASSWORD']
    reset(profile, password)
    return PostgresStore(profile, password), lambda: reset(profile, password)


def reset(profile, password):
    from core import PostgresStore
    with PostgresStore(profile, password, initialize=False).connect() as db:
        db.execute('DROP SCHEMA IF EXISTS wissensraum CASCADE')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--ollama', action='store_true', help='Frage-Vektoren vom lokalen Ollama')
    parser.add_argument('--postgres', action='store_true', help='PostgreSQL-Testdatenbank statt SQLite')
    parser.add_argument('--output', type=Path, help='Ergebnis als JSON schreiben')
    args = parser.parse_args(argv)
    log = lambda text: print(text, file=sys.stderr, flush=True)  # noqa: E731
    cleanup = None
    with tempfile.TemporaryDirectory() as folder:
        if args.postgres:
            store, cleanup = postgres_store()
        else:
            store = Store(Path(folder) / 'referenz.db')
        try:
            log('Referenzbestand wird erzeugt …')
            documents, chunk_count = build(store, log)
            embedder = core.Embedder if args.ollama else SyntheticEmbedder
            log('Messung …')
            result = {'backend': store.backend, 'dokumente': documents, 'abschnitte': chunk_count,
                      'dimension': DIMENSION, 'frage_vektoren': 'ollama' if args.ollama else 'synthetisch',
                      **measure(store, embedder, log)}
        finally:
            VECTOR_CACHE.clear()
            if cleanup:
                cleanup()
    text = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        args.output.write_text(text + '\n', encoding='utf-8')
    print(text)


if __name__ == '__main__':
    main()
