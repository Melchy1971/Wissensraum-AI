"""Gate 6.1: reference corpus of 1,000 mixed files and a repeatable cancel/resume test."""
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import import_fixtures as fx
from core import Store
from import_jobs import ImportJobs


class ReferenceCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.corpus = Path(cls.temp.name) / 'korpus'
        cls.expected = fx.build_reference_corpus(cls.corpus)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def results(self, jobs, job_id):
        return {item['locator']: (item['status'], item['reason']) for item in jobs.items(job_id)}

    def document_count(self, store):
        with store.connect() as db:
            return (db.execute('SELECT count(*) FROM documents').fetchone()[0],
                    db.execute('SELECT count(*) FROM sources').fetchone()[0])

    def test_reference_corpus_is_classified_at_least_99_percent_correctly(self):
        store = Store(Path(self.temp.name) / 'ungestoert.db')
        jobs = ImportJobs(store)
        job_id = jobs.create_for_folder(self.corpus)
        summary = jobs.run(job_id)
        results = self.results(jobs, job_id)
        correct = sum(results.get(locator) == outcome for locator, outcome in self.expected.items())
        wrong = {locator: (results.get(locator), outcome) for locator, outcome in self.expected.items()
                 if results.get(locator) != outcome}
        self.assertEqual(summary['total'], 1000)
        self.assertGreaterEqual(correct / 1000, 0.99, wrong)
        self.assertEqual(correct, 1000, wrong)  # observed today; the gate threshold above is 99 %
        quarantined = [locator for locator, (status, _) in results.items() if status == 'quarantined']
        self.assertEqual(len(quarantined), 140)
        self.assertTrue(all(reason for status, reason in results.values() if status == 'quarantined'))
        self.assertEqual(self.document_count(store), (850, 850))

    def test_repeated_cancel_and_crash_resume_ends_identically_without_duplicates(self):
        store = Store(Path(self.temp.name) / 'unterbrochen.db')
        jobs = ImportJobs(store)
        job_id = jobs.create_for_folder(self.corpus)
        for stop_at in (137, 290, 401):  # three user cancellations
            cancel = threading.Event()
            def progress(done, total, locator, stop_at=stop_at, cancel=cancel):
                if done == stop_at:
                    cancel.set()
            self.assertEqual(jobs.run(job_id, cancel, progress)['status'], 'cancelled')
        real_add = store.add
        calls = {'count': 0}
        def crash_on_fifth_document(*args, **kwargs):
            result = real_add(*args, **kwargs)
            calls['count'] += 1
            if calls['count'] == 5:
                raise KeyboardInterrupt  # process dies after the commit, before the status update
            return result
        with patch.object(store, 'add', side_effect=crash_on_fifth_document):
            with self.assertRaises(KeyboardInterrupt):
                jobs.run(job_id)
        summary = jobs.run(job_id)
        self.assertEqual(summary['status'], 'completed')
        self.assertFalse(summary['resumable'])
        self.assertEqual(self.results(jobs, job_id), self.expected)
        self.assertEqual(self.document_count(store), (850, 850))
        self.assertEqual(sum(item['attempts'] for item in jobs.items(job_id)), 1001)  # only the crashed item ran twice


if __name__ == '__main__':
    unittest.main()
