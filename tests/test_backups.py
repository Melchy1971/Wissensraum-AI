"""Integrity check, daily backup and restore drill (Release 2.4, step 9.4)."""
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from collections import namedtuple
from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import backups
from core import Store

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)


def check_integrity_contract(case, store):
    """Integrity of the active database; same on both backends, ``store`` freshly migrated."""
    first, _ = store.add('Plan', 'Inhalt eins')
    store.add('Notiz', 'Inhalt zwei')
    result = backups.integrity(store)
    case.assertEqual((result['ok'], result['probleme'], result['dokumente']), (True, [], 2))
    with store.connect() as db:
        db.execute("UPDATE documents SET sha256='0' WHERE id=?", (first,))
    result = backups.integrity(store)
    case.assertFalse(result['ok'])
    case.assertEqual(result['probleme'], [f'Prüfsumme weicht ab bei Dokument {first}'])


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.store = Store(self.base / 'wissen.db')
        self.store.add('Plan', 'Inhalt eins')

    def tearDown(self):
        self.temp.cleanup()

    def test_integrity_contract(self):
        check_integrity_contract(self, Store(self.base / 'zweite.db'))

    def test_integrity_reports_a_damaged_file(self):
        damaged = self.base / 'kaputt.db'
        damaged.write_bytes((self.base / 'wissen.db').read_bytes()[:4096] + b'\0' * 8192)
        problems, _ = backups._check_sqlite_file(damaged)
        self.assertTrue(problems)

    def test_daily_backups_are_verified_due_after_24_hours_and_pruned(self):
        backups.set_settings(self.store, keep=2)
        self.assertTrue(backups.due(self.store, NOW))
        made = backups.create(self.store, NOW)
        self.assertEqual((made['datei'], made['dokumente']), ('wissen-20261107-120000.db', 1))
        self.assertFalse(backups.due(self.store, NOW + timedelta(hours=23)))
        self.assertTrue(backups.due(self.store, NOW + timedelta(hours=24)))
        self.assertIsNone(backups.auto(self.store, NOW + timedelta(hours=1)))  # not due yet
        backups.auto(self.store, NOW + timedelta(days=1))
        backups.auto(self.store, NOW + timedelta(days=2))
        names = [path.name for path, _ in backups.existing(self.base / 'backups')]
        self.assertEqual(names, ['wissen-20261109-120000.db', 'wissen-20261108-120000.db'])  # two kept
        with closing(sqlite3.connect(self.base / 'backups' / names[0])) as db:
            self.assertEqual(db.execute('SELECT title FROM documents').fetchone()[0], 'Plan')
        self.assertEqual(sorted(p.name for p in (self.base / 'backups').iterdir()), sorted(names))  # no leftovers
        backups.set_settings(self.store, auto=False)
        self.assertIsNone(backups.auto(self.store, NOW + timedelta(days=5)))

    def test_backup_needs_space_and_another_folder_can_be_chosen(self):
        usage = namedtuple('usage', 'total used free')
        with mock.patch('backups.shutil.disk_usage', return_value=usage(10, 10, 0)):
            with self.assertRaisesRegex(ValueError, 'Zu wenig freier Speicher'):
                backups.create(self.store, NOW)
        self.assertEqual(backups.existing(self.base / 'backups'), [])
        backups.set_settings(self.store, folder=self.base / 'extern')
        backups.create(self.store, NOW)
        self.assertEqual(len(backups.existing(self.base / 'extern')), 1)
        for bad in (0, backups.MAX_KEEP + 1):
            with self.assertRaises(ValueError):
                backups.set_settings(self.store, keep=bad)

    def test_restore_drill_is_recorded_and_due_every_quarter(self):
        with self.assertRaisesRegex(ValueError, 'Keine Sicherung'):
            backups.drill(self.store, NOW)
        backups.create(self.store, NOW)
        result = backups.drill(self.store, NOW)
        self.assertEqual((result['ok'], result['dokumente'], result['sicherung']), (True, 1, 'wissen-20261107-120000.db'))
        self.assertLess(result['sekunden'], backups.RTO_SECONDS)
        self.assertEqual(self.store.get(1)['title'], 'Plan')  # the active database is untouched
        state = backups.status(self.store, NOW + timedelta(hours=2))
        self.assertEqual((state['rpo_ok'], state['uebung_faellig'], state['anzahl']), (True, False, 1))
        later = backups.status(self.store, NOW + timedelta(days=91))
        self.assertEqual((later['rpo_ok'], later['uebung_faellig']), (False, True))
        text = '\n'.join(backups.lines(later))
        self.assertIn('älter als 24 Stunden', text)
        self.assertIn('Wiederherstellungsübung fällig', text)
        self.assertIn('Letzte Wiederherstellungsübung 2026-11-07: bestanden', text)

    def test_overview_shows_the_backup_state_without_paths(self):
        import operations
        backups.create(self.store, NOW)
        data = operations.overview(self.store, now=NOW + timedelta(hours=3))
        self.assertNotIn('ordner', data['sicherung'])
        self.assertNotIn(str(self.base), json.dumps(data))
        self.assertIn('Letzte Sicherung vor 3,0 Stunden (1 vorhanden)', operations.report(data))

    def test_cli(self):
        import cli
        db = self.base / 'wissen.db'

        def run(*argv):
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli.main(['--db', str(db), *argv])
            return code, out.getvalue()
        self.assertEqual(run('integrity')[0], 0)
        code, out = run('backup-daily')
        self.assertEqual((code, json.loads(out)['dokumente']), (0, 1))
        code, out = run('backup-daily')
        self.assertEqual(json.loads(out)['grund'], 'aktuell')
        code, out = run('restore-drill')
        self.assertEqual((code, json.loads(out)['ok']), (0, True))
        code, out = run('backup-settings', '--keep', '3', '--auto', 'off')
        self.assertEqual((json.loads(out)['behalten'], json.loads(out)['auto']), (3, False))


if __name__ == '__main__':
    unittest.main()
