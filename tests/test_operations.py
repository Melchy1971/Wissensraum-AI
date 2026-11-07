"""Operations overview (Release 2.4, step 9.1)."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import operations
from core import Store

SECRET = 'Vertraulich-Zebra-4711'  # appears in titles, texts, questions, paths and messages, never in the overview


def build(store):
    first, _ = store.add(f'Titel {SECRET}', f'Inhalt {SECRET} ' * 40, filename=f'C:/{SECRET}.txt', original=b'abcdef')
    store.add('Notiz', f'Zweiter Inhalt {SECRET}')
    store.update_metadata(first, status='archived')
    store.log('question', f'Frage {SECRET}', f'Antwort {SECRET}', {'answer_status': 'answered', 'graph_relations': [1]},
              'qwen3:8b')
    store.log('question', f'Frage {SECRET}', 'keine', {'answer_status': 'refused'}, 'qwen3:8b')
    store.log('summary', f'Frage {SECRET}', 'kurz', [], 'gpt')
    with store.connect() as db:
        db.execute("UPDATE ai_runs SET created_at='2020-01-01 00:00:00' WHERE model='gpt'")
        job = db.execute("INSERT INTO import_jobs(kind,root,status) VALUES('files',?,'completed') RETURNING id",
                         (f'C:/{SECRET}',)).fetchone()['id']
        for ordinal, status in enumerate(('done', 'failed', 'failed')):
            item = db.execute('INSERT INTO import_items(job_id,ordinal,path,locator,status) VALUES(?,?,?,?,?) RETURNING id',
                              (job, ordinal, f'C:/{SECRET}/{ordinal}', str(ordinal), status)).fetchone()['id']
            if status == 'failed':
                db.execute('INSERT INTO import_item_errors(item_id,attempt,reason,message) VALUES(?,1,?,?)',
                           (item, 'parse' if ordinal == 1 else 'size', f'Fehler {SECRET}'))
        db.execute("UPDATE import_item_errors SET created_at='2020-01-01 00:00:00' WHERE reason='size'")


def check_operations_contract(case, store):
    """Counts of ``operations.overview``; same on both backends, ``store`` freshly migrated."""
    build(store)
    data = operations.overview(store, days=30)
    documents = data['dokumente']
    case.assertEqual(documents['anzahl'], 2)
    case.assertEqual(documents['nach_status'], {'archived': 1, 'new': 1})
    case.assertEqual(sum(documents['nach_quelle'].values()), 2)
    case.assertEqual(documents['originale_bytes'], 6)
    case.assertGreater(documents['text_zeichen'], 0)
    chunks = data['abschnitte']
    case.assertGreater(chunks['anzahl'], 0)
    case.assertEqual((chunks['vektoren_je_modell'], chunks['ohne_vektor']), ({}, chunks['anzahl']))
    case.assertEqual(data['import']['auftraege_nach_status'], {'completed': 1})
    case.assertEqual(data['import']['eintraege_nach_status'], {'done': 1, 'failed': 2})
    case.assertEqual(data['import']['fehler_im_zeitraum_nach_grund'], {'parse': 1})  # the old one is outside
    ai = data['ki']
    case.assertEqual((ai['anfragen'], ai['nach_art'], ai['nach_modell']), (2, {'question': 2}, {'qwen3:8b': 2}))
    case.assertEqual((ai['nach_status'], ai['mit_graphkontext']), ({'answered': 1, 'refused': 1}, 1))
    case.assertEqual(data['wissensgraph'], {'entitaeten': {}, 'relationen': {}})
    wide = operations.overview(store, days=3650 * 10)
    case.assertEqual((wide['zeitraum_tage'], wide['ki']['anfragen']), (3650, 3))  # capped at 3650 days
    text = operations.report(data) + json.dumps(data, ensure_ascii=False)
    case.assertNotIn(SECRET, text)
    case.assertNotIn('Titel', text)


class OperationsTests(unittest.TestCase):
    def test_contract(self):
        with tempfile.TemporaryDirectory() as folder:
            check_operations_contract(self, Store(Path(folder) / 'wissen.db'))

    def test_report_formats_german_numbers(self):
        data = {'zeitraum_tage': 7,
                'dokumente': {'anzahl': 1, 'nach_status': {'new': 1}, 'nach_quelle': {'file': 1},
                              'text_zeichen': 1234567, 'originale_bytes': 3 * 1048576 // 2, 'wiedervorlage_faellig': 0},
                'abschnitte': {'anzahl': 3, 'vektoren_je_modell': {'bge-m3': 2}, 'ohne_vektor': 1},
                'import': {'auftraege_nach_status': {}, 'eintraege_nach_status': {}, 'fehler_im_zeitraum_nach_grund': {}},
                'ki': {'anfragen': 0, 'nach_art': {}, 'nach_modell': {}, 'nach_status': {}, 'mit_graphkontext': 0},
                'wissensgraph': {'entitaeten': {'confirmed': 2}, 'relationen': {}}}
        text = operations.report(data)
        self.assertIn('1.234.567 Zeichen Text, 1,5 MiB Originale', text)
        self.assertIn('Vektoren: bge-m3 2, ohne Vektor: 1', text)
        self.assertIn('Importfehler der letzten 7 Tage: keine', text)

    def test_due_reviews_follow_the_given_day(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / 'wissen.db')
            doc, _ = store.add('A', 'Inhalt')
            with store.connect() as db:
                db.execute("UPDATE documents SET review_date='2026-05-01' WHERE id=?", (doc,))
            due = lambda day: operations.overview(store, now=datetime(2026, 5, day, tzinfo=timezone.utc))['dokumente']
            self.assertEqual((due(1)['wiedervorlage_faellig'], due(2)['wiedervorlage_faellig']), (1, 1))
            self.assertEqual(operations.overview(store, now=datetime(2026, 4, 30, tzinfo=timezone.utc))
                             ['dokumente']['wiedervorlage_faellig'], 0)


if __name__ == '__main__':
    unittest.main()
