import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cli
from diagnostics import DiagnosticResult


class CliDoctorTests(unittest.TestCase):
    def test_doctor_prints_results_and_returns_nonzero_on_required_failure(self):
        results = (
            DiagnosticResult('python', 'PASS', 'Python ok.', {'version': '3.13.7'}),
            DiagnosticResult('database', 'FAIL', 'Datenbank prüfen.', {'backend': 'SQLite'}),
        )
        output = io.StringIO()
        with patch.object(cli, 'run_diagnostics', return_value=results), redirect_stdout(output):
            exit_code = cli.main(['doctor'])

        self.assertEqual(exit_code, 1)
        self.assertEqual(output.getvalue(),
                         '[PASS] python: Python ok. (version=3.13.7)\n'
                         '[FAIL] database: Datenbank prüfen. (backend=SQLite)\n'
                         'Ergebnis: 1 bestanden, 0 Warnungen, 1 fehlgeschlagen.\n')

    def test_doctor_marks_explicit_database_as_sqlite_override(self):
        with patch.dict(os.environ, {'WISSEN_DB': 'environment.db'}, clear=False), \
             patch.object(cli, 'run_diagnostics', return_value=()) as run, \
             redirect_stdout(io.StringIO()):
            before = dict(os.environ)
            self.assertEqual(cli.main(['--db', 'alternate.db', 'doctor']), 0)
            self.assertEqual(dict(os.environ), before)
        self.assertEqual(run.call_args.args[3], Path('alternate.db'))
        self.assertTrue(run.call_args.kwargs['force_sqlite'])

    def test_doctor_uses_wissen_db_without_mutating_environment(self):
        configured = str(Path('configured') / 'wissen.db')
        with patch.dict(os.environ, {'WISSEN_DB': configured}, clear=False):
            before = dict(os.environ)
            with patch.object(cli, 'run_diagnostics', return_value=()) as run, redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(['doctor']), 0)
            self.assertEqual(dict(os.environ), before)

        self.assertEqual(run.call_args.args[3], Path(configured))
        self.assertFalse(run.call_args.kwargs['force_sqlite'])


class CliDatabasePathTests(unittest.TestCase):
    def test_normal_command_passes_wissen_db_to_database_settings(self):
        configured = str(Path('configured') / 'wissen.db')
        store = Mock()
        store.list.return_value = []
        settings = Mock()
        settings.open_store.return_value = store

        with patch.dict(os.environ, {'WISSEN_DB': configured}, clear=False), \
             patch.object(cli, 'SystemVault'), \
             patch.object(cli, 'DatabaseSettings', return_value=settings), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['list']), 0)

        settings.open_store.assert_called_once_with(Path(configured))

    def test_wissen_db_does_not_override_configured_postgresql(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            profile = {
                'backend': 'postgresql', 'host': 'db.example.test', 'port': '5432',
                'database': 'wissen', 'username': 'user', 'sslmode': 'require',
            }
            (base / 'data').mkdir()
            (base / 'data' / 'database.json').write_text(json.dumps(profile), encoding='utf-8')
            vault = Mock()
            vault.get.return_value = 'safe-password'
            store = Mock()
            store.list.return_value = []

            with patch.dict(os.environ, {'WISSEN_DB': 'environment.db'}, clear=False), \
                 patch.object(cli, 'BASE', base), \
                 patch.object(cli, 'SystemVault', return_value=vault), \
                 patch('core.PostgresStore', return_value=store) as postgres_store, \
                 redirect_stdout(io.StringIO()):
                before = dict(os.environ)
                self.assertEqual(cli.main(['list']), 0)
                self.assertEqual(dict(os.environ), before)

        postgres_store.assert_called_once()

    def test_explicit_db_forces_sqlite_over_configured_postgresql_and_wissen_db(self):
        settings = Mock()
        store = Mock()
        store.list.return_value = []
        explicit = Path('explicit.db')

        with patch.dict(os.environ, {'WISSEN_DB': 'environment.db'}, clear=False), \
             patch.object(cli, 'SystemVault'), \
             patch.object(cli, 'DatabaseSettings', return_value=settings), \
             patch.object(cli, 'Store', return_value=store) as sqlite_store, \
             redirect_stdout(io.StringIO()):
            before = dict(os.environ)
            self.assertEqual(cli.main(['--db', str(explicit), 'list']), 0)
            self.assertEqual(dict(os.environ), before)

        sqlite_store.assert_called_once_with(explicit)
        settings.open_store.assert_not_called()


if __name__ == '__main__':
    unittest.main()
