"""Graph context as a limited, supplementary source of answers (Release 2.3, step 8B.5)."""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import AI, GRAPH_CONTEXT_RELATIONS, Store

TEXT = 'Anna Berg arbeitet für die Nordwerk GmbH. Tom Kranz verantwortet das System SAP im Einkauf.'


def build(store):
    types = {row['name']: row['id'] for row in store.entity_types()}
    rtypes = {row['name']: row['id'] for row in store.relation_types()}
    doc, _ = store.add('Organigramm', TEXT)
    other, _ = store.add('Ferienplan', 'Lea Wolf nutzt das Wiki im Sommer.')
    anna = store.create_entity(types['Person'], 'Anna Berg', doc, 'Anna Berg')
    firm = store.create_entity(types['Organisation'], 'Nordwerk GmbH', doc, 'Nordwerk GmbH')
    tom = store.create_entity(types['Person'], 'Tom Kranz', doc, 'Tom Kranz')
    lea = store.create_entity(types['Person'], 'Lea Wolf', other, 'Lea Wolf')
    wiki = store.create_entity(types['System'], 'Wiki', other, 'Wiki')
    works = store.relate_entities(anna, firm, rtypes['arbeitet für'], doc, 'Anna Berg arbeitet für die Nordwerk GmbH')
    store.relate_entities(lea, wiki, rtypes['nutzt'], other, 'Lea Wolf nutzt das Wiki')
    sap, _, _ = store.propose_entity(types['System'], 'SAP', doc, 'SAP', TEXT.index('SAP'))
    store.propose_relation(tom, sap, rtypes['verantwortet'], doc, 'Tom Kranz verantwortet das System SAP',
                           TEXT.index('Tom Kranz verantwortet'))
    return doc, other, works


def check_graph_context_contract(case, store):
    """Only confirmed relations between confirmed entities with evidence in the given documents."""
    case.assertFalse(store.has_graph_context())  # nothing confirmed yet: answers stay exactly as before
    doc, other, works = build(store)
    case.assertTrue(store.has_graph_context())
    context = store.graph_context([doc])
    case.assertEqual(context, [{'id': works, 'document_id': doc,
                                'aussage': 'Anna Berg (Person) arbeitet für Nordwerk GmbH (Organisation)',
                                'zitat': 'Anna Berg arbeitet für die Nordwerk GmbH'}])  # proposed SAP relation left out
    case.assertEqual(len(store.graph_context([doc, other])), 2)
    case.assertEqual(store.graph_context([]), [])
    store.set_graph_context(False)
    case.assertFalse(store.has_graph_context())
    case.assertFalse(store.graph_context_enabled())
    store.set_graph_context(True)
    case.assertTrue(store.graph_context_enabled())


class FakeOllama:
    def __init__(self):
        self.sent = []

    def __call__(self, request, timeout=None):
        self.sent.append(json.loads(request.data))
        return io.BytesIO(json.dumps({'done': True, 'done_reason': 'stop',
                                      'message': {'content': 'Anna Berg arbeitet für die Nordwerk GmbH [Q1].'}}).encode())


class GraphContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'wissen.db')

    def tearDown(self):
        self.temp.cleanup()

    def test_contract(self):
        check_graph_context_contract(self, self.store)

    def test_input_without_graph_is_unchanged_and_with_graph_cites_the_source_label(self):
        sources = [{'title': 'A', 'document_id': 7, 'ordinal': 1, 'content': 'x'},
                   {'title': 'B', 'document_id': 9, 'ordinal': 2, 'content': 'y'}]
        plain = AI.user_input('Frage?', sources)
        self.assertEqual(AI.user_input('Frage?', sources, graph=[]), plain)
        self.assertNotIn('graphkontext', plain)
        graph = [{'id': 1, 'document_id': 9, 'aussage': 'A arbeitet für B', 'zitat': 'A arbeitet'},
                 {'id': 2, 'document_id': 99, 'aussage': 'C nutzt D', 'zitat': 'C nutzt'}]  # source not passed
        data = json.loads(AI.user_input('Frage?', sources, graph=graph))
        self.assertEqual(data['graphkontext'], [{'aussage': 'A arbeitet für B', 'zitat': 'A arbeitet', 'kennung': 'Q2'}])
        self.assertEqual(json.loads(AI.user_input('Frage?', sources, graph=graph[1:])), json.loads(plain))

    def answer(self, question='Für wen arbeitet Anna Berg?'):
        fake = FakeOllama()
        result = AI(self.store, '', 'qwen3:8b', transport=fake, provider='ollama').ask(question)
        payload = json.loads(fake.sent[0]['messages'][1]['content'])
        return result, fake.sent[0]['messages'][0]['content'], payload

    def test_answers_use_the_graph_only_when_confirmed_relations_exist(self):
        self.store.add('Organigramm', TEXT)
        result, instructions, payload = self.answer()
        self.assertNotIn('graphkontext', payload)
        self.assertNotIn('graphkontext', instructions)  # the request of Release 2.1, byte for byte
        self.assertEqual(instructions, AI.INSTRUCTIONS)

    def test_answers_carry_the_graph_context_with_its_rule_and_log_it(self):
        doc, _, works = build(self.store)
        result, instructions, payload = self.answer()
        self.assertEqual(result.status, 'answered')
        self.assertTrue(instructions.endswith(AI.GRAPH_RULE))
        labels = {s['dokument']: s['kennung'] for s in payload['quellen']}
        self.assertEqual(payload['graphkontext'][0]['kennung'], labels[doc])
        self.assertNotIn('verantwortet', json.dumps(payload['graphkontext'], ensure_ascii=False))  # proposed stays out
        with self.store.connect() as db:
            meta = json.loads(db.execute('SELECT sources_json FROM ai_runs ORDER BY id DESC LIMIT 1').fetchone()['sources_json'])
        self.assertEqual(meta['graph_relations'], [works])
        self.store.set_graph_context(False)
        _, instructions, payload = self.answer()
        self.assertNotIn('graphkontext', payload)
        self.assertEqual(instructions, AI.INSTRUCTIONS)

    def test_cli_switch(self):
        import os
        from contextlib import redirect_stderr, redirect_stdout
        from unittest.mock import patch
        import cli
        folder = Path(self.temp.name)

        def run(*argv):
            out = io.StringIO()
            with patch.object(cli, 'BASE', folder), patch.dict(os.environ, {}, clear=True), \
                    redirect_stdout(out), redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(['--db', str(folder / 'wissen.db'), *argv]), 0)
            return json.loads(out.getvalue())
        self.assertEqual(run('ai-graph'), {'graphkontext_in_ki_antworten': True})
        self.assertEqual(run('ai-graph', 'off'), {'graphkontext_in_ki_antworten': False})

    def test_graph_context_is_capped(self):
        types = {row['name']: row['id'] for row in self.store.entity_types()}
        rtypes = {row['name']: row['id'] for row in self.store.relation_types()}
        text = ' '.join(f'Person{n} nutzt System{n} sehr regelmäßig und ausführlich.' for n in range(20))
        doc, _ = self.store.add('Liste', text)
        for n in range(20):
            person = self.store.create_entity(types['Person'], f'Person{n}', doc, f'Person{n} ')
            system = self.store.create_entity(types['System'], f'System{n}', doc, f'System{n} ')
            self.store.relate_entities(person, system, rtypes['nutzt'], doc, f'Person{n} nutzt System{n}')
        self.assertEqual(len(self.store.graph_context([doc])), GRAPH_CONTEXT_RELATIONS)
        _, _, payload = self.answer('Welches System nutzt Person3 regelmäßig?')
        self.assertTrue(1 <= len(payload['graphkontext']) <= GRAPH_CONTEXT_RELATIONS)


if __name__ == '__main__':
    unittest.main()
