"""Parser and gate decision of the local matrix gate (tests/local_ci.py)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import local_ci

LINE = '=' * 70
CLEANUP = (f'{LINE}\nERROR: test_a (test_cli.CliTests.test_a)\n{"-" * 70}\nTraceback (most recent call last):\n'
           "OSError: [WinError 145] Das Verzeichnis ist nicht leer: 'C:\\\\tmp\\\\x'\n")
REAL = f'{LINE}\nERROR: test_b (test_core.CoreTests.test_b)\n{"-" * 70}\nKeyError: 1\n'
FAIL = f'{LINE}\nFAIL: test_c (test_core.CoreTests.test_c)\n{"-" * 70}\nAssertionError: 1 != 2\n'


def run_text(*blocks, summary):
    return 'test_x ... ok\n' + ''.join(blocks) + f'{"-" * 70}\nRan 10 tests in 1.2s\n\n{summary}\n'


class LocalCiTests(unittest.TestCase):
    def test_summary_counts(self):
        parsed = local_ci.parse_unittest_output(run_text(summary='OK (skipped=3)'))
        self.assertEqual((parsed['ran'], parsed['ok'], parsed['skipped'], parsed['errors']), (10, True, 3, 0))
        parsed = local_ci.parse_unittest_output(run_text(CLEANUP, FAIL, summary='FAILED (failures=1, errors=1, skipped=2)'))
        self.assertEqual((parsed['ok'], parsed['failures'], parsed['errors'], parsed['skipped']), (False, 1, 1, 2))
        self.assertEqual([p['test'] for p in parsed['problems']], ['test_cli.CliTests.test_a', 'test_core.CoreTests.test_c'])

    def test_crash_without_summary_is_no_pass(self):
        parsed = local_ci.parse_unittest_output('ImportError: kaputt\n')
        self.assertIsNone(parsed['ran'])
        self.assertFalse(parsed['ok'])

    def test_only_cleanup_errors_on_windows_are_retried(self):
        cleanup_only = local_ci.parse_unittest_output(run_text(CLEANUP, summary='FAILED (errors=1)'))
        self.assertEqual(local_ci.retry_candidates(cleanup_only, 'windows'), ['test_cli.CliTests.test_a'])
        self.assertEqual(local_ci.retry_candidates(cleanup_only, 'linux'), [])
        mixed = local_ci.parse_unittest_output(run_text(CLEANUP, REAL, summary='FAILED (errors=2)'))
        self.assertEqual(local_ci.retry_candidates(mixed, 'windows'), [])
        with_failure = local_ci.parse_unittest_output(run_text(CLEANUP, FAIL, summary='FAILED (failures=1, errors=1)'))
        self.assertEqual(local_ci.retry_candidates(with_failure, 'windows'), [])

    def test_gate_needs_all_seven_jobs(self):
        passing = {'status': 'PASS', 'ran': 488, 'skipped': 1, 'retried': [], 'system': 'x', 'seconds': 1}
        results = {job: dict(passing) for job in local_ci.JOBS}
        self.assertTrue(local_ci.gate_table('abc1234', results)[1])
        results.pop('postgres')
        text, passed = local_ci.gate_table('abc1234', results)
        self.assertFalse(passed)
        self.assertIn('| postgres | FEHLT', text)
        results['postgres'] = {**passing, 'status': 'FAIL'}
        self.assertFalse(local_ci.gate_table('abc1234', results)[1])

    def test_skip_reasons_are_counted(self):
        text = ("test_a (m.C.test_a) ... skipped 'PostgreSQL-Testdienst nicht konfiguriert'\n"
                "test_b (m.C.test_b) ... skipped 'PostgreSQL-Testdienst nicht konfiguriert'\n"
                'test_c (m.C.test_c) ... skipped "open-file replacement unsupported: \'x\'"\n')
        self.assertEqual(local_ci.skip_reasons(text), {'PostgreSQL-Testdienst nicht konfiguriert': 2,
                                                      "open-file replacement unsupported: 'x'": 1})


if __name__ == '__main__':
    unittest.main()
