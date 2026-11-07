"""Token and cost budgets per AI provider (Release 2.4, step 9.2)."""
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import budgets
from core import AI, ProviderError, Store

SECRET = 'Vertraulich-Zebra-4711'


class OpenAI:
    """Fake OpenAI transport reporting 300 input and 100 output tokens per answer."""

    def __init__(self):
        self.requests = 0

    def __call__(self, request, timeout):
        self.requests += 1
        return io.BytesIO(json.dumps({'status': 'completed', 'usage': {'input_tokens': 300, 'output_tokens': 100},
                                      'output': [{'content': [{'type': 'output_text', 'text': 'Antwort [Q1]'}]}]})
                          .encode())


def check_budget_contract(case, store):
    """Counting, month, warning and lock; same on both backends, ``store`` freshly migrated."""
    now = datetime.now(timezone.utc)
    case.assertEqual(budgets.status(store, 'openai', now)['stufe'], 'ok')  # no budget, no use
    budgets.record(store, 'openai', 'gpt-4.1-mini', {'usage': {'input_tokens': 600, 'output_tokens': 200}})
    budgets.record(store, 'openai', 'gpt-4.1-mini', {'usage': {'input_tokens': 5000, 'output_tokens': 5000}})
    budgets.record(store, 'openai', 'gpt-4.1-mini', {'output': []})  # no numbers: nothing stored
    budgets.record(store, 'ollama', 'qwen3:8b', {'prompt_eval_count': 50, 'eval_count': 25})
    with store.connect() as db:
        case.assertEqual(int(db.execute('SELECT count(*) AS n FROM ai_usage').fetchone()['n']), 3)
        db.execute("UPDATE ai_usage SET created_at='2020-01-15 00:00:00' WHERE input_tokens=5000")  # earlier month
        stored = [dict(row) for row in db.execute('SELECT * FROM ai_usage')]
    case.assertNotIn(SECRET, json.dumps(stored, default=str))
    status = budgets.status(store, 'openai', now)
    case.assertEqual((status['anfragen'], status['eingabe'], status['ausgabe'], status['token']), (1, 600, 200, 800))
    case.assertEqual((status['monat'], status['stufe']), (now.strftime('%Y-%m'), 'ok'))
    case.assertEqual(budgets.status(store, 'ollama', now)['token'], 75)
    budgets.set_limits(store, 'openai', tokens=1000)
    case.assertEqual(budgets.status(store, 'openai', now)['stufe'], 'warnung')  # 80 %
    budgets.set_limits(store, 'openai', tokens=800)
    case.assertEqual(budgets.status(store, 'openai', now)['stufe'], 'gesperrt')
    with case.assertRaises(ProviderError) as refused:
        budgets.check(store, 'openai', now)
    case.assertEqual(refused.exception.kind, 'budget')
    budgets.check(store, 'ollama', now)  # other providers are not affected
    budgets.set_limits(store, 'openai', tokens=0, eur=1, price_in=500, price_out=2000)  # 0.3 + 0.4 euro
    status = budgets.status(store, 'openai', now)
    case.assertEqual((status['kosten_eur'], status['anteil'], status['stufe']), (0.7, 0.7, 'ok'))
    budgets.set_limits(store, 'openai', eur=0.7)
    case.assertEqual(budgets.status(store, 'openai', now)['stufe'], 'gesperrt')
    case.assertEqual(budgets.limits(store, 'openai'), {'tokens': 0, 'eur': 0.7, 'price_in': 500, 'price_out': 2000})
    for bad in ({'tokens': -1}, {'eur': 'viel'}, {'price_in': 10 ** 9}):
        with case.assertRaises(ValueError):
            budgets.set_limits(store, 'openai', **bad)
    with case.assertRaises(ValueError):
        budgets.set_limits(store, 'unbekannt', tokens=1)
    case.assertEqual([s['anbieter'] for s in budgets.statuses(store, now)], ['openai', 'ollama'])


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'wissen.db')

    def tearDown(self):
        self.temp.cleanup()

    def test_contract(self):
        check_budget_contract(self, self.store)

    def test_tokens_of_every_provider_response(self):
        self.assertEqual(budgets.tokens('openai', {'usage': {'input_tokens': 3, 'output_tokens': 4}}), (3, 4))
        self.assertEqual(budgets.tokens('anthropic', {'usage': {'input_tokens': 5, 'output_tokens': 6}}), (5, 6))
        self.assertEqual(budgets.tokens('gemini', {'usageMetadata': {'promptTokenCount': 7, 'candidatesTokenCount': 8,
                                                                     'thoughtsTokenCount': 2}}), (7, 10))
        self.assertEqual(budgets.tokens('ollama', {'prompt_eval_count': 9, 'eval_count': 1}), (9, 1))
        for broken in (None, [], {'usage': None}, {'usage': {'input_tokens': -1, 'output_tokens': True}},
                       {'usage': {'input_tokens': '5', 'output_tokens': 1.5}}):
            self.assertEqual(budgets.tokens('openai', broken), (0, 0))

    def test_requests_are_counted_and_refused_before_sending_once_the_budget_is_used(self):
        self.store.add('Wartungsplan', 'Die Therme bekommt jährlich im März eine Wartung.')
        transport = OpenAI()
        ai = AI(self.store, 'sk-synthetic-key-1234', 'gpt-4.1-mini', transport=transport)
        source = [{'document_id': 1, 'title': 'Wartungsplan', 'ordinal': 1, 'content': 'jährlich im März'}]
        ai.generate('Wann?', source)
        self.assertEqual(budgets.status(self.store, 'openai')['token'], 400)
        budgets.set_limits(self.store, 'openai', tokens=400)
        with self.assertRaises(ProviderError) as refused:
            ai.generate('Wann?', source)
        self.assertEqual((refused.exception.kind, transport.requests), ('budget', 1))  # nothing sent, no retry
        self.assertIn('Monatsbudget für OpenAI', str(refused.exception))

    def test_graph_suggestions_stop_at_the_budget(self):
        import graph_suggest
        from test_graph_suggest import FakeOllama
        doc, _ = self.store.add('Organigramm', 'Anna Berg arbeitet für die Nordwerk GmbH. ' * 200)
        budgets.set_limits(self.store, 'ollama', tokens=1)
        budgets.record(self.store, 'ollama', 'qwen3:8b', {'prompt_eval_count': 1, 'eval_count': 0})
        transport = FakeOllama()
        with self.assertRaises(ProviderError) as refused:
            graph_suggest.suggest(self.store, doc, transport=transport)
        self.assertEqual((refused.exception.kind, transport.requests), ('budget', []))

    def test_lines_in_german(self):
        budgets.set_limits(self.store, 'openai', tokens=1_000_000, eur=12.5, price_in=2, price_out=8)
        budgets.record(self.store, 'openai', 'gpt-4.1-mini', {'usage': {'input_tokens': 500_000, 'output_tokens': 400_000}})
        text = '\n'.join(budgets.lines(budgets.statuses(self.store)))
        self.assertIn('1 Anfragen, 900.000 Token von 1.000.000, 4,20 € von 12,50 € · Warnung', text)
        self.assertEqual(budgets.lines([]), ['Kein Budget gesetzt und keine gezählte KI-Nutzung in diesem Monat.'])

    def test_cli_sets_and_shows_the_budget(self):
        import cli
        from contextlib import redirect_stdout
        db = Path(self.temp.name) / 'cli.db'
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(['--db', str(db), 'budget', 'anthropic', '--tokens', '5000']), 0)
        self.assertEqual(json.loads(out.getvalue())['budget']['tokens'], 5000)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(['--db', str(db), 'budget']), 0)
        self.assertIn('Anthropic', out.getvalue())


if __name__ == '__main__':
    unittest.main()
