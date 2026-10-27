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

    def test_doctor_reports_pending_recovery_without_recovery_or_marker_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            data = base / 'data'
            data.mkdir()
            journal = data / 'configuration-import-journal.json'
            authority = data / 'authority-resync-pending.json'
            journal.write_bytes(b'PRIVATE journal contents')
            authority.write_bytes(b'PRIVATE authority contents')
            before = {path: path.read_bytes() for path in (journal, authority)}
            output = io.StringIO()

            with patch.object(cli, 'BASE', base), \
                 patch.object(cli, 'SystemVault'), \
                 patch.object(cli, 'DatabaseSettings'), \
                 patch.object(cli, 'recover_pending_configuration') as recover, \
                 patch.object(cli, 'run_diagnostics', return_value=()), \
                 redirect_stdout(output):
                exit_code = cli.main(['--db', 'PRIVATE-origin.db', 'doctor'])

            self.assertEqual(exit_code, 1)
            recover.assert_not_called()
            self.assertEqual({path: path.read_bytes() for path in before}, before)
            self.assertEqual(
                output.getvalue(),
                '[FAIL] configuration_recovery: '
                'Konfigurationswiederherstellung ist ausstehend; Anwendung normal starten.\n'
                'Ergebnis: 0 bestanden, 0 Warnungen, 1 fehlgeschlagen.\n')
            self.assertNotIn('PRIVATE', output.getvalue())

    def test_doctor_without_pending_recovery_never_calls_recovery(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(cli, 'BASE', Path(folder)), \
             patch.object(cli, 'SystemVault'), \
             patch.object(cli, 'DatabaseSettings'), \
             patch.object(cli, 'recover_pending_configuration') as recover, \
             patch.object(cli, 'run_diagnostics', return_value=()), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['doctor']), 0)
        recover.assert_not_called()


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

    def test_normal_command_recovers_before_opening_store(self):
        events = []
        settings = Mock()
        store = Mock(backend='sqlite')
        store.list.return_value = []
        settings.open_store.side_effect = lambda _path: events.append('open') or store

        with patch.object(cli, 'SystemVault'), \
             patch.object(cli, 'DatabaseSettings', return_value=settings), \
             patch.object(cli, 'recover_pending_configuration',
                          side_effect=lambda *_: events.append('recover')), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['list']), 0)

        self.assertEqual(events, ['recover', 'open'])


class CliConfigurationExportTests(unittest.TestCase):
    def test_export_fails_closed_on_authority_pending_marker_without_recovery(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            marker = base / 'data' / 'authority-resync-pending.json'
            marker.parent.mkdir()
            marker.write_bytes(b'PRIVATE malformed marker')
            error = io.StringIO()
            with patch.object(cli, 'BASE', base), \
                 patch.object(cli, 'SystemVault'), \
                 patch.object(cli, 'DatabaseSettings'), \
                 patch.object(cli, 'recover_pending_configuration') as recover, \
                 self.assertRaises(SystemExit) as raised, \
                 patch('sys.stderr', error):
                cli.main(['config-export', str(base / 'export.json')])

            self.assertEqual(raised.exception.code, 1)
            recover.assert_not_called()
            self.assertTrue(marker.exists())
            self.assertNotIn('PRIVATE', error.getvalue())
            self.assertIn('Konfigurationswiederherstellung ist unvollständig', error.getvalue())


class CliRestoreTests(unittest.TestCase):
    def test_restore_requires_exact_target_name_and_restores_sqlite(self):
        from core import Store
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            path = base / 'ziel.db'
            backup = base / 'sicherung.db'
            store = Store(path)
            store.add('Gesichert', 'Inhalt A')
            store.backup(backup)
            store.add('Später', 'Inhalt B')
            with patch.object(cli, 'BASE', base):
                for confirm in ([], ['--confirm', 'falsch.db']):
                    error = io.StringIO()
                    with self.subTest(confirm=confirm), patch('sys.stderr', error), \
                            self.assertRaises(SystemExit) as raised:
                        cli.main(['--db', str(path), 'restore', str(backup), *confirm])
                    self.assertEqual(raised.exception.code, 1)
                    self.assertIn('--confirm ziel.db', error.getvalue())
                self.assertEqual(len(store.list()), 2)
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(cli.main(['--db', str(path), 'restore', str(backup),
                                               '--confirm', 'ziel.db']), 0)
            self.assertEqual(json.loads(output.getvalue())['documents'], 1)
            self.assertEqual([doc['title'] for doc in store.list()], ['Gesichert'])

    def test_status_reports_health_and_fails_only_on_failures(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            vault = Mock()
            vault.get.return_value = None
            output = io.StringIO()
            with patch.object(cli, 'BASE', base), patch.object(cli, 'SystemVault', return_value=vault), \
                    patch.dict(os.environ, {}, clear=True), redirect_stdout(output):
                self.assertEqual(cli.main(['--db', str(base / 'wissen.db'), 'status']), 0)
            text = output.getvalue()
            self.assertIn('[PASS] database', text)
            self.assertIn('[WARN] ai_provider', text)
            self.assertIn('[PASS] mail_accounts: Kein E-Mail-Konto eingerichtet.', text)

    def test_import_folder_jobs_and_report_commands(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            source = base / 'quelle'
            source.mkdir()
            (source / 'a.txt').write_text('CLI Auftrag', encoding='utf-8')
            (source / 'b.exe').write_bytes(b'MZ')
            vault = Mock()
            vault.get.return_value = None
            database = base / 'wissen.db'
            def run(*argv):
                output = io.StringIO()
                with patch.object(cli, 'BASE', base), patch.object(cli, 'SystemVault', return_value=vault), \
                        redirect_stdout(output):
                    self.assertEqual(cli.main(['--db', str(database), *argv]), 0)
                return json.loads(output.getvalue())
            summary = run('import-folder', str(source))
            self.assertEqual((summary['imported'], summary['quarantined'], summary['status']), (1, 1, 'completed'))
            self.assertEqual([job['job_id'] for job in run('jobs')], [summary['job_id']])
            self.assertFalse(run('job-resume', str(summary['job_id']))['resumable'])
            report = base / 'bericht.csv'
            run('job-report', str(summary['job_id']), str(report))
            self.assertIn('Dateityp nicht unterstützt', report.read_text(encoding='utf-8-sig'))
            (source / 'a.txt').write_text('CLI Auftrag geändert', encoding='utf-8')
            preview = {row['locator']: row for row in run('preview', str(source))}
            self.assertEqual((preview['a.txt']['outcome'], preview['a.txt']['version']), ('version', 2))
            self.assertEqual(len(run('list')), 1)  # preview stored nothing
            self.assertEqual(run('import-folder', str(source))['versioned'], 1)
            current = run('list')
            self.assertEqual([row['version'] for row in current], [2])
            self.assertEqual(len(run('list', '--all-versions')), 2)
            self.assertEqual([row['version'] for row in run('versions', str(current[0]['id']))], [2, 1])
            single = run('preview', str(source / 'a.txt'))
            self.assertEqual((single[0]['outcome'], single[0]['title']), ('duplicate', 'a'))

    def test_restore_rejects_configured_postgresql(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            (base / 'data').mkdir()
            (base / 'data' / 'database.json').write_text('{"backend":"postgresql"}', encoding='utf-8')
            error = io.StringIO()
            with patch.object(cli, 'BASE', base), patch('sys.stderr', error), \
                    self.assertRaises(SystemExit):
                cli.main(['restore', str(base / 'x.db'), '--confirm', 'wissen.db'])
            self.assertIn('nur für SQLite', error.getvalue())


class CorruptedConfigurationCliTests(unittest.TestCase):
    CORRUPT = {'leer': b'', 'binaer': b'\xff\xfe\x00', 'abgeschnitten': b'{"format": "wiss',
               'liste': b'[]', 'null': b'null', 'tief': b'[' * 100000 + b']' * 100000}
    INTERNALS = ('Error', 'Expecting', 'recursion', 'Traceback', 'object has no', 'line ')

    def run_cli(self, base, vault, argv):
        error = io.StringIO()
        with patch.object(cli, 'BASE', base), patch.object(cli, 'SystemVault', return_value=vault), \
                patch.dict(os.environ, {}, clear=True), redirect_stdout(io.StringIO()), \
                patch('sys.stderr', error):
            try:
                code = cli.main(argv)
            except SystemExit as exit_:
                code = exit_.code
        return code, error.getvalue()

    def prepared_base(self):
        base = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (base / 'data').mkdir()
        vault = Mock()
        vault.get.return_value = None
        self.assertEqual(self.run_cli(base, vault, ['config-sync'])[0], 0)
        return base, vault

    def test_corrupted_configuration_files_fail_with_fixed_messages_and_stay_unchanged(self):
        cases = (('database.json', ['list']),
                 ('configuration-authority.json', ['config-export', 'export.json']),
                 ('configuration-store-identity.json', ['config-export', 'export.json']),
                 ('authority-resync-pending.json', ['list']),
                 ('configuration-import-journal.json', ['list']))
        for name, argv in cases:
            for label, data in self.CORRUPT.items():
                with self.subTest(file=name, content=label):
                    base, vault = self.prepared_base()
                    target = base / 'data' / name
                    target.write_bytes(data)
                    argv = [str(base / arg) if arg.endswith('.json') else arg for arg in argv]
                    code, error = self.run_cli(base, vault, argv)
                    self.assertEqual(code, 1)
                    self.assertTrue(error.startswith('Fehler: '), error)
                    self.assertFalse(any(text in error for text in self.INTERNALS), error)
                    self.assertEqual(target.read_bytes(), data)
                    self.assertFalse((base / 'export.json').exists())

    def test_interrupted_authority_write_keeps_previous_authority(self):
        base, vault = self.prepared_base()
        authority = base / 'data' / 'configuration-authority.json'
        before = authority.read_bytes()
        with patch('configuration_authority.os.replace', side_effect=OSError(28, 'voll')):
            code, error = self.run_cli(base, vault, ['config-sync'])
        self.assertEqual(code, 1)
        self.assertEqual(authority.read_bytes(), before)
        self.assertEqual([p.name for p in (base / 'data').iterdir() if '.tmp-' in p.name], [])


if __name__ == '__main__':
    unittest.main()
