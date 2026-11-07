"""Retention rules for AI history and deleted content (Release 2.4, step 9.3)."""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import budgets
import retention
from core import Store
from test_graph_view import build

OLD = '2020-01-01 00:00:00'
SECRET = 'Vertraulich-Zebra-4711'


def count(store, table):
    with store.connect() as db:
        return int(db.execute(f'SELECT count(*) AS n FROM {table}').fetchone()['n'])


def fill(store):
    """Old and new rows in every category."""
    _, _, ids, doc = build(store)
    store.delete_entity(ids['Einkauf'])       # its relation stays, hidden with the entity
    store.delete_entity(ids['Anna Berg'])
    with store.connect() as db:
        responsible = db.execute('SELECT id FROM graph_relations WHERE source_id=? AND target_id=?',
                                 (ids['Tom Kranz'], ids['SAP'])).fetchone()['id']
    store.delete_graph_relation(responsible)
    store.log('question', f'Alte Frage {SECRET}', f'Alte Antwort {SECRET}', {}, 'gpt')
    store.log('question', 'Neue Frage', 'Neue Antwort', {}, 'gpt')
    budgets.record(store, 'openai', 'gpt', {'usage': {'input_tokens': 1, 'output_tokens': 1}})
    budgets.record(store, 'openai', 'gpt', {'usage': {'input_tokens': 2, 'output_tokens': 2}})
    with store.connect() as db:
        job = db.execute("INSERT INTO import_jobs(kind,status) VALUES('files','completed') RETURNING id").fetchone()['id']
        item = db.execute("INSERT INTO import_items(job_id,ordinal,path,locator) VALUES(?,0,'p','0') RETURNING id",
                          (job,)).fetchone()['id']
        for attempt in (1, 2):
            db.execute("INSERT INTO import_item_errors(item_id,attempt,reason,message) VALUES(?,?,'parse','x')",
                       (item, attempt))
        db.execute('UPDATE ai_runs SET created_at=? WHERE question LIKE ?', (OLD, 'Alte%'))
        db.execute(f"UPDATE ai_usage SET created_at='{OLD}' WHERE input_tokens=1")
        db.execute(f"UPDATE import_item_errors SET created_at='{OLD}' WHERE attempt=1")
        db.execute(f"UPDATE entities SET deleted_at='{OLD}' WHERE id=?", (ids['Einkauf'],))
        db.execute(f"UPDATE graph_relations SET deleted_at='{OLD}' WHERE deleted_at IS NOT NULL")
        first = db.execute('SELECT min(id) AS id FROM graph_changes').fetchone()['id']
        db.execute(f"UPDATE graph_changes SET created_at='{OLD}' WHERE id=?", (first,))
    return ids, doc


def check_retention_contract(case, store):
    """Preview, apply and limits; same on both backends, ``store`` freshly migrated."""
    ids, doc = fill(store)
    case.assertEqual(retention.rules(store), {key: 0 for key in retention.CATEGORIES})
    case.assertEqual((retention.preview(store), retention.apply(store)), ({}, {}))  # nothing without a rule
    before = {table: count(store, table) for table in ('ai_runs', 'ai_usage', 'import_item_errors', 'entities',
                                                        'graph_relations', 'graph_changes', 'entity_mentions')}
    for key in retention.CATEGORIES:
        retention.set_rule(store, key, 365)
    expected = {'ki_verlauf': {'ai_runs': 1}, 'ki_nutzung': {'ai_usage': 1},
                'importfehler': {'import_item_errors': 1},
                'graph_geloescht': {'graph_relations': 1, 'entity_mentions': 0, 'entities': 1, 'graph_changes': 1}}
    case.assertEqual(retention.preview(store), expected)
    case.assertEqual(before['ai_runs'], count(store, 'ai_runs'))  # the preview removes nothing
    case.assertEqual(retention.apply(store), expected)
    case.assertEqual(retention.preview(store), {key: {t: 0 for t in tables} for key, tables in expected.items()})
    case.assertEqual(count(store, 'ai_runs'), before['ai_runs'] - 1)
    case.assertEqual(count(store, 'ai_usage'), before['ai_usage'] - 1)
    case.assertEqual(count(store, 'import_item_errors'), before['import_item_errors'] - 1)
    case.assertEqual(count(store, 'entities'), before['entities'] - 1)
    case.assertEqual(count(store, 'graph_changes'), before['graph_changes'] - 1)
    case.assertEqual(count(store, 'entity_mentions'), before['entity_mentions'] - 1)  # with its entity
    case.assertEqual(count(store, 'graph_relations'), before['graph_relations'] - 2)  # and its hidden relation
    case.assertEqual([r['question'] for r in store.history()], ['Neue Frage'])
    case.assertIsNotNone(store.entity(ids['Tom Kranz']))  # nothing that is still in use
    case.assertEqual(store.get(doc)['title'], 'Organigramm')
    case.assertEqual(budgets.status(store, 'openai')['token'], 4)  # this month stays for the budget
    with store.connect() as db:  # the current month is kept even with a one-day rule
        db.execute("UPDATE ai_usage SET created_at=?", (datetime.now(timezone.utc).replace(day=1, hour=0, minute=0,
                    second=1, microsecond=0).strftime('%Y-%m-%d %H:%M:%S+00:00'),))
    retention.set_rule(store, 'ki_nutzung', 1)
    now = datetime.now(timezone.utc).replace(day=1, hour=23)
    case.assertEqual(retention.preview(store, now)['ki_nutzung'], {'ai_usage': 0})
    for bad in (-1, retention.MAX_DAYS + 1, 'viel'):
        with case.assertRaises(ValueError):
            retention.set_rule(store, 'ki_verlauf', bad)
    with case.assertRaises(ValueError):
        retention.set_rule(store, 'dokumente', 1)
    case.assertEqual(retention.rules(store)['ki_verlauf'], 365)


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'wissen.db'
        self.store = Store(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_contract(self):
        check_retention_contract(self, self.store)

    def test_removed_text_is_not_left_in_the_sqlite_file(self):
        wal = Path(str(self.path) + '-wal')
        stored = lambda: self.path.read_bytes() + (wal.read_bytes() if wal.exists() else b'')
        fill(self.store)
        self.assertIn(SECRET.encode(), stored())
        retention.set_rule(self.store, 'ki_verlauf', 30)
        self.assertEqual(retention.apply(self.store)['ki_verlauf'], {'ai_runs': 1})
        self.assertNotIn(SECRET.encode(), stored())

    def test_report_and_cli(self):
        import cli
        self.assertEqual(retention.report({}), 'Keine Aufbewahrungsregel aktiv; es wird nichts entfernt.')
        self.assertEqual(retention.report({'ki_verlauf': {'ai_runs': 2}}), 'KI-Verlauf (Fragen und Antworten): ai_runs 2')
        fill(self.store)

        def run(*argv):
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli.main(['--db', str(self.path), 'retention', *argv])
            return code, out.getvalue()
        code, out = run('ki_verlauf', '--days', '30')
        self.assertEqual((code, json.loads(out)['ki_verlauf']), (0, 30))
        code, out = run()
        self.assertEqual(code, 0)
        self.assertIn('ai_runs 1', out)
        self.assertEqual(count(self.store, 'ai_runs'), 2)  # the preview removes nothing
        code, out = run('--apply')
        self.assertEqual(code, 2)  # no confirmation
        self.assertEqual(count(self.store, 'ai_runs'), 2)
        code, out = run('--apply', '--confirm')
        self.assertEqual((code, count(self.store, 'ai_runs')), (0, 1))


if __name__ == '__main__':
    unittest.main()
