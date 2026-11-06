"""AI suggestions for the knowledge graph (Release 2.3, step 8B.2); the model is a fake transport."""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import graph_suggest
from core import Store

TEXT = 'Anna Berg leitet den Einkauf bei der Nordwerk GmbH. Der Einkauf nutzt das System SAP.'


class FakeOllama:
    """Answers every request with the next prepared JSON; records the requests."""

    def __init__(self, *answers):
        self.answers, self.requests = list(answers), []

    def __call__(self, request, timeout=None):
        self.requests.append(json.loads(request.data))
        answer = self.answers.pop(0) if self.answers else {'entitaeten': [], 'relationen': []}
        body = json.dumps({'done': True, 'done_reason': 'stop',
                           'message': {'content': answer if isinstance(answer, str) else json.dumps(answer)}})
        return io.BytesIO(body.encode())


ANSWER = {'entitaeten': [
    {'art': 'Person', 'name': 'Anna Berg', 'zitat': 'Anna Berg'},
    {'art': 'Organisation', 'name': 'Nordwerk GmbH', 'zitat': 'Nordwerk GmbH'},
    {'art': 'System', 'name': 'SAP', 'zitat': 'System SAP'},
    {'art': 'Person', 'name': 'Erfunden', 'zitat': 'steht nicht im Text'},   # invented quote
    {'art': 'Planet', 'name': 'Mars', 'zitat': 'Anna'},                      # unknown type
    'kein Objekt'],
    'relationen': [
    {'quelle': 'Anna Berg', 'art': 'arbeitet für', 'ziel': 'Nordwerk GmbH',
     'zitat': 'Anna Berg leitet den Einkauf bei der Nordwerk GmbH'},
    {'quelle': 'Anna Berg', 'art': 'nutzt', 'ziel': 'SAP', 'zitat': 'Anna nutzt SAP'},  # not verbatim
    {'quelle': 'Unbekannt', 'art': 'nutzt', 'ziel': 'SAP', 'zitat': 'nutzt das System SAP'}]}


def check_suggestion_contract(case, store):
    """Suggestions stay proposed until confirmed; same on both backends; ``store`` freshly migrated."""
    doc, _ = store.add('Organigramm', TEXT)
    before = store.get(doc)
    fake = FakeOllama(ANSWER)
    counts = graph_suggest.suggest(store, doc, transport=fake)
    case.assertEqual(counts, {'abschnitte': 1, 'entitaeten': 3, 'erwaehnungen': 3, 'relationen': 1, 'verworfen': 5})
    request = fake.requests[0]
    case.assertEqual((request['model'], request['format'], request['options']['temperature']), ('qwen3:8b', 'json', 0))
    case.assertEqual(json.loads(request['messages'][1]['content'])['text'], TEXT)  # text only as JSON data
    graph = store.document_graph(doc)
    case.assertEqual({(m['entity'], m['status']) for m in graph['mentions']},
                     {('Anna Berg', 'proposed'), ('Nordwerk GmbH', 'proposed'), ('SAP', 'proposed')})
    relation = graph['relations'][0]
    case.assertEqual((relation['status'], relation['quote']),
                     ('proposed', 'Anna Berg leitet den Einkauf bei der Nordwerk GmbH'))
    case.assertEqual({row['status'] for row in store.entities()}, {'proposed'})
    case.assertEqual(store.get(doc), before)

    # A relation cannot be confirmed before its entities; confirming is logged and revertible.
    with case.assertRaises(ValueError):
        store.confirm_graph_item('relation', relation['id'])
    entities = {row['name']: row['id'] for row in store.entities()}
    store.confirm_graph_item('entity', entities['Anna Berg'])
    store.confirm_graph_item('entity', entities['Nordwerk GmbH'])
    store.confirm_graph_item('relation', relation['id'])
    with case.assertRaises(ValueError):
        store.confirm_graph_item('relation', relation['id'])  # already confirmed
    with case.assertRaises(ValueError):
        store.confirm_graph_item('dokument', 1)
    case.assertEqual(store.document_graph(doc)['relations'][0]['status'], 'confirmed')
    last = store.graph_changes()[0]
    case.assertEqual((last['action'], last['object_type'], last['old_value'], last['new_value']),
                     ('confirm', 'relation', 'proposed', 'confirmed'))
    with case.assertRaises(ValueError):  # an entity with confirmed relations keeps its status
        store.revert_graph_change(store.graph_changes()[2]['id'])
    store.revert_graph_change(last['id'])
    case.assertEqual(store.document_graph(doc)['relations'][0]['status'], 'proposed')

    # Running again adds nothing twice; a confirmed entity is reused, the mention stays proposed.
    again = graph_suggest.suggest(store, doc, transport=FakeOllama(ANSWER))
    case.assertEqual((again['entitaeten'], again['erwaehnungen'], again['relationen']), (0, 0, 0))
    # Rejecting is deleting; broken model output is counted, not stored.
    sap = entities['SAP']
    store.delete_entity(sap)
    case.assertNotIn('SAP', [row['name'] for row in store.entities()])
    broken = graph_suggest.suggest(store, doc, transport=FakeOllama('kein JSON', ))
    case.assertEqual(broken['verworfen'], 1)


class SuggestionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'wissen.db')

    def tearDown(self):
        self.temp.cleanup()

    def test_contract(self):
        check_suggestion_contract(self, self.store)

    def test_long_documents_are_read_section_by_section_with_document_offsets(self):
        text = 'x' * 1700 + ' Anna Berg arbeitet hier. ' + 'y' * 1700 + ' Nordwerk GmbH ist der Arbeitgeber.'
        doc, _ = self.store.add('Lang', text)
        fake = FakeOllama({'entitaeten': [{'art': 'Person', 'name': 'Anna Berg', 'zitat': 'Anna Berg'}]},
                          {'entitaeten': []},
                          {'entitaeten': [{'art': 'Organisation', 'name': 'Nordwerk GmbH', 'zitat': 'Nordwerk GmbH'}],
                           'relationen': [{'quelle': 'Anna Berg', 'art': 'arbeitet für', 'ziel': 'Nordwerk GmbH',
                                           'zitat': 'Nordwerk GmbH ist der Arbeitgeber'}]})
        counts = graph_suggest.suggest(self.store, doc, transport=fake)
        self.assertEqual((counts['abschnitte'], counts['entitaeten'], counts['relationen']), (3, 2, 1))
        for mention in self.store.document_graph(doc)['mentions']:
            self.assertEqual(text[mention['start_offset']:mention['end_offset']], mention['quote'])

    def test_cli(self):
        import os
        from contextlib import redirect_stderr, redirect_stdout
        from unittest.mock import patch
        import cli
        folder = Path(self.temp.name)
        doc, _ = self.store.add('Organigramm', TEXT)

        def run(*argv):
            out = io.StringIO()
            with patch.object(cli, 'BASE', folder), patch.dict(os.environ, {}, clear=True), \
                    patch('urllib.request.urlopen', FakeOllama(ANSWER)), redirect_stdout(out), redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(['--db', str(folder / 'wissen.db'), *argv]), 0)
            return json.loads(out.getvalue())
        self.assertEqual(run('graph-suggest', str(doc))['relationen'], 1)
        anna = next(row['id'] for row in run('entities') if row['name'] == 'Anna Berg')
        run('graph-confirm', 'entity', str(anna))
        self.assertEqual(run('entity', str(anna))['status'], 'confirmed')


if __name__ == '__main__':
    unittest.main()
