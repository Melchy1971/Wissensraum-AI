"""Generate vectors.json with the reference model over a local Ollama (D-014).

    python tests/eval/build_vectors.py [--host http://127.0.0.1:11434] [--model qwen3-embedding:0.6b]

Run again whenever corpus.json or questions.json change; the test suite rejects vectors whose text
hash no longer matches. Only the synthetic evaluation texts are sent, and only to the given host.
"""
from __future__ import annotations

import argparse
import json
import urllib.request

# QUERY_INSTRUCTION is the product's query prefix, so evaluation and search agree.
from evaluate import HERE, QUERY_INSTRUCTION, corpus_chunks, encode_vector, load, sha256


def embed(host, model, texts):
    request = urllib.request.Request(host.rstrip('/') + '/api/embed', method='POST',
                                     data=json.dumps({'model': model, 'input': texts}).encode('utf-8'),
                                     headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=600) as response:
        vectors = json.load(response)['embeddings']
    if len(vectors) != len(texts):
        raise ValueError('Ollama lieferte nicht für jeden Text einen Vektor')
    return vectors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--host', default='http://127.0.0.1:11434')
    parser.add_argument('--model', default='qwen3-embedding:0.6b')
    args = parser.parse_args(argv)
    corpus, questions = load('corpus.json'), load('questions.json')
    parts = list(corpus_chunks(corpus))
    chunk_vectors = embed(args.host, args.model, [text for _, _, text in parts])
    question_vectors = embed(args.host, args.model, [QUERY_INSTRUCTION + q['question'] for q in questions])
    dimension = len(chunk_vectors[0])
    if any(len(v) != dimension for v in chunk_vectors + question_vectors):
        raise ValueError('Vektoren mit unterschiedlicher Dimension')
    result = {'model': args.model, 'dimension': dimension, 'encoding': 'float16-le-base64, L2-normiert',
              'query_instruction': QUERY_INSTRUCTION,
              'chunks': {key: {'sha256': sha256(text), 'vector': encode_vector(vector)}
                         for (key, _, text), vector in zip(parts, chunk_vectors)},
              'questions': {q['id']: {'sha256': sha256(q['question']), 'vector': encode_vector(vector)}
                            for q, vector in zip(questions, question_vectors)}}
    (HERE / 'vectors.json').write_text(json.dumps(result, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')
    print(f'{len(parts)} Abschnitte, {len(questions)} Fragen, Dimension {dimension}, Modell {args.model}')


if __name__ == '__main__':
    main()
