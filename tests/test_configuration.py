import io
import json
import os
import re
from pathlib import Path
import tempfile
import subprocess
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app
import cli
from configuration import (ConfigurationTransfer, ConfigurationTransferError,
                           APPLY_ERROR, AUTHORITY_ERROR, EXPORT_ERROR, IMPORT_ERROR,
                           RECOVERY_ERROR, _open_origin_store,
                           recover_pending_configuration)
from configuration_authority import (AUTHORITY_RECOVERY_ERROR, ConfigurationAuthority,
                                     TRANSFER_SETTING_NAMES, identity_for_profile,
                                     recover_pending_authority, store_identity)
from core import Store
from database import DatabaseSettings
from design import DesignSettings
from settings import Settings
from test_settings import FakeVault


class HostileString(str):
    called = False

    @classmethod
    def reset(cls):
        cls.called = False

    def _hook(self, *_args, **_kwargs):
        type(self).called = True
        raise AssertionError('hostile string hook called')

    __eq__ = __ne__ = __hash__ = __str__ = __iter__ = _hook
    lower = replace = isdigit = strip = _hook


class HostileInteger(int):
    called = False

    @classmethod
    def reset(cls):
        cls.called = False

    def _hook(self, *_args, **_kwargs):
        type(self).called = True
        raise AssertionError('hostile integer hook called')

    __eq__ = __ne__ = __hash__ = __int__ = __index__ = _hook
    __lt__ = __le__ = __gt__ = __ge__ = _hook


class HostileDict(dict):
    called = False

    @classmethod
    def reset(cls):
        cls.called = False

    def _hook(self, *_args, **_kwargs):
        type(self).called = True
        raise AssertionError('hostile dict hook called')

    __iter__ = __getitem__ = __contains__ = get = items = keys = values = _hook


class HostileList(list):
    called = False

    @classmethod
    def reset(cls):
        cls.called = False

    def _hook(self, *_args, **_kwargs):
        type(self).called = True
        raise AssertionError('hostile list hook called')

    __iter__ = __getitem__ = __len__ = _hook


class ConfigurationTransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.store = Store(self.base / 'wissen.db')
        self.vault = FakeVault()
        self.database = DatabaseSettings(self.base, self.vault, environ={})
        self.design = DesignSettings(self.base)
        self.transfer = ConfigurationTransfer(self.store, self.base, self.database, self.design)

    def tearDown(self):
        self.temp.cleanup()

    def configure_all_domains(self):
        settings = Settings(self.store, self.base, self.vault, environ={})
        settings.put({
            'active_provider': 'anthropic',
            'openai.model': 'gpt-custom',
            'openai.disabled': '1',
            'anthropic.model': 'claude-custom',
            'anthropic.disabled': '0',
            'gemini.model': 'gemini-custom',
            'gemini.disabled': '1',
            'ollama.model': 'local/custom:7b',
            'ollama.disabled': '0',
            'unrelated.setting': 'must-not-export',
        })
        with self.store.connect() as db:
            db.execute('''INSERT INTO email_accounts
                (id,label,provider,email,username,host,port,client_id,tenant)
                VALUES(?,?,?,?,?,?,?,?,?)''',
                ('mail-one', 'Privat', 'gmail', 'owner@example.test', 'owner',
                 'imap.example.test', 993, '', 'common'))
        self.database._write({
            'backend': 'postgresql', 'host': 'db.example.test', 'port': 5433,
            'database': 'wissen', 'username': 'wissen_user', 'sslmode': 'verify-full',
        })
        self.design.save_theme('dark')

    def test_round_trip_exports_every_allowlisted_domain_without_reading_or_changing_secrets(self):
        self.configure_all_domains()
        secrets = {
            'openai': 'OPENAI_SECRET_MARKER',
            'database:postgresql': 'DATABASE_SECRET_MARKER',
            'mail:mail-one': 'MAIL_SECRET_MARKER',
        }
        self.vault.values.update(secrets)
        with patch.object(self.vault, 'get', side_effect=AssertionError('export must not read secrets')):
            document = self.transfer.export_document()

        serialized = json.dumps(document)
        for secret in secrets.values():
            self.assertNotIn(secret, serialized)
        for forbidden in ('password', 'token', 'api_key', 'secret', 'auth_status'):
            self.assertNotIn(forbidden, serialized.lower())
        self.assertEqual(document['format'], 'wissensraum-configuration')
        self.assertEqual(document['version'], 1)
        self.assertEqual(document['configuration']['ai']['active_provider'], 'anthropic')
        self.assertEqual(document['configuration']['ai']['providers']['openai'],
                         {'model': 'gpt-custom', 'disabled': True})
        self.assertEqual(document['configuration']['database']['port'], 5433)
        self.assertEqual(document['configuration']['design'], {'theme': 'dark'})
        self.assertEqual(document['configuration']['email_accounts'][0]['id'], 'mail-one')
        self.assertNotIn('unrelated.setting', serialized)

        target_base = self.base / 'target'
        target_store = Store(target_base / 'wissen.db')
        target_vault = FakeVault()
        target_vault.values.update({'openai': 'TARGET_KEY', 'mail:mail-one': 'TARGET_MAIL_KEY',
                                    'database:postgresql': 'TARGET_DB_KEY'})
        target_database = DatabaseSettings(target_base, target_vault, environ={})
        target_design = DesignSettings(target_base)
        result = ConfigurationTransfer(target_store, target_base, target_database, target_design).import_document(document)

        self.assertTrue(result['restart_required'])
        target_settings = Settings(target_store, target_base, target_vault, environ={})
        self.assertEqual(target_settings.config()['active_provider'], 'anthropic')
        self.assertEqual(target_settings.config()['ollama.model'], 'local/custom:7b')
        imported_profile = target_database.profile()
        self.assertEqual({**imported_profile, 'port': int(imported_profile['port'])},
                         document['configuration']['database'])
        self.assertEqual(target_design.theme(), 'dark')
        with target_store.connect() as db:
            account = dict(db.execute('SELECT * FROM email_accounts WHERE id=?', ('mail-one',)).fetchone())
        for name, value in document['configuration']['email_accounts'][0].items():
            self.assertEqual(account[name], value)
        self.assertEqual(target_vault.values,
                         {'openai': 'TARGET_KEY', 'mail:mail-one': 'TARGET_MAIL_KEY',
                          'database:postgresql': 'TARGET_DB_KEY'})

    def test_export_file_is_atomic_and_does_not_leave_temporary_file(self):
        target = self.base / 'exports' / 'config.json'
        self.transfer.export_file(target)
        self.assertEqual(json.loads(target.read_text(encoding='utf-8')), self.transfer.export_document())
        self.assertFalse(target.with_suffix(target.suffix + '.tmp').exists())

    def test_atomic_write_uses_unique_temporary_name(self):
        target = self.base / 'atomic.json'
        collision = target.with_name(target.name + '.tmp')
        collision.write_bytes(b'owned-by-another-operation')
        self.transfer._write_json_atomic(target, {'safe': True})
        self.assertEqual(json.loads(target.read_text(encoding='utf-8')), {'safe': True})
        self.assertEqual(collision.read_bytes(), b'owned-by-another-operation')

    def test_invalid_unknown_version_unknown_key_and_secret_bearing_documents_change_nothing(self):
        self.configure_all_domains()
        original = self.transfer.export_document()
        before_database = self.database.path.read_bytes()
        before_design = self.design.path.read_bytes()
        with self.store.connect() as db:
            before_settings = [tuple(row) for row in db.execute('SELECT name,value FROM settings ORDER BY name')]
            before_accounts = [tuple(row) for row in db.execute('SELECT * FROM email_accounts ORDER BY id')]

        cases = []
        malformed = json.loads(json.dumps(original)); malformed['configuration']['design']['theme'] = 1
        cases.append(malformed)
        unknown_version = json.loads(json.dumps(original)); unknown_version['version'] = 2
        cases.append(unknown_version)
        unknown_key = json.loads(json.dumps(original)); unknown_key['configuration']['ai']['extra'] = False
        cases.append(unknown_key)
        secret_key = json.loads(json.dumps(original)); secret_key['configuration']['database']['password'] = 'MARKER'
        cases.append(secret_key)
        bool_port = json.loads(json.dumps(original)); bool_port['configuration']['database']['port'] = True
        cases.append(bool_port)
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaisesRegex(ValueError, 'Konfigurationsdatei'):
                self.transfer.import_document(payload)

        self.assertEqual(self.database.path.read_bytes(), before_database)
        self.assertEqual(self.design.path.read_bytes(), before_design)
        with self.store.connect() as db:
            self.assertEqual([tuple(row) for row in db.execute('SELECT name,value FROM settings ORDER BY name')], before_settings)
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM email_accounts ORDER BY id')], before_accounts)

    def test_failure_mid_apply_rolls_back_sql_and_both_files(self):
        self.configure_all_domains()
        imported = self.transfer.export_document()
        imported['configuration']['ai']['active_provider'] = 'ollama'
        imported['configuration']['design']['theme'] = 'light'
        imported['configuration']['database']['backend'] = 'sqlite'
        original_database = self.database.path.read_bytes()
        original_design = self.design.path.read_bytes()
        original_active = Settings(self.store, self.base, self.vault, {}).active()
        real_write = self.transfer._write_json_atomic
        calls = []

        def fail_second(path, value):
            calls.append(path)
            if len(calls) == 2:
                raise OSError('SENSITIVE_MARKER')
            return real_write(path, value)

        with patch.object(self.transfer, '_write_json_atomic', side_effect=fail_second):
            with self.assertRaisesRegex(ValueError, '^Konfiguration konnte nicht importiert werden; es wurde nichts geändert\\.$'):
                self.transfer.import_document(imported)

        self.assertEqual(Settings(self.store, self.base, self.vault, {}).active(), original_active)
        self.assertEqual(self.database.path.read_bytes(), original_database)
        self.assertEqual(self.design.path.read_bytes(), original_design)

    def test_same_backend_profile_change_requires_restart_but_noop_does_not(self):
        document = self.transfer.export_document()
        self.assertFalse(self.transfer.import_document(document)['restart_required'])
        document['configuration']['database']['host'] = 'changed.example.test'
        self.assertTrue(self.transfer.import_document(document)['restart_required'])

    def test_email_accounts_are_a_complete_replacement_without_touching_vault(self):
        self.configure_all_domains()
        self.vault.values.update({'mail:mail-one': 'KEEP_ONE', 'mail:extra': 'KEEP_EXTRA'})
        with self.store.connect() as db:
            db.execute('''INSERT INTO email_accounts
                (id,label,provider,email,username,host,port,client_id,tenant)
                VALUES(?,?,?,?,?,?,?,?,?)''',
                ('extra', 'Extra', 'gmail', 'extra@example.test', 'extra',
                 'imap.example.test', 993, '', 'common'))
        document = self.transfer.export_document()
        document['configuration']['email_accounts'] = []
        with patch.object(self.vault, 'get', side_effect=AssertionError('vault read')), \
             patch.object(self.vault, 'set', side_effect=AssertionError('vault write')), \
             patch.object(self.vault, 'delete', side_effect=AssertionError('vault delete')):
            self.transfer.import_document(document)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) AS count FROM email_accounts').fetchone()['count'], 0)
        self.assertEqual(self.vault.values, {'mail:mail-one': 'KEEP_ONE', 'mail:extra': 'KEEP_EXTRA'})

    def test_subset_email_accounts_removes_extras_and_updates_payload(self):
        self.configure_all_domains()
        with self.store.connect() as db:
            db.execute('''INSERT INTO email_accounts
                (id,label,provider,email,username,host,port,client_id,tenant)
                VALUES(?,?,?,?,?,?,?,?,?)''',
                ('extra', 'Extra', 'gmail', 'extra@example.test', 'extra',
                 'imap.example.test', 993, '', 'common'))
        document = self.transfer.export_document()
        account = next(item for item in document['configuration']['email_accounts']
                       if item['id'] == 'mail-one')
        document['configuration']['email_accounts'] = [account]
        account['label'] = 'Aktualisiert'
        self.transfer.import_document(document)
        with self.store.connect() as db:
            rows = [dict(row) for row in db.execute('SELECT id,label FROM email_accounts ORDER BY id')]
        self.assertEqual(rows, [{'id': 'mail-one', 'label': 'Aktualisiert'}])

    def test_account_payload_order_does_not_break_transaction_verification(self):
        self.configure_all_domains()
        document = self.transfer.export_document()
        second = dict(document['configuration']['email_accounts'][0])
        second.update({'id': 'mail-two', 'label': 'Zweitkonto',
                       'email': 'second@example.test', 'username': 'second'})
        document['configuration']['email_accounts'] = [second,
                                                        document['configuration']['email_accounts'][0]]
        self.transfer.import_document(document)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) AS count FROM email_accounts').fetchone()['count'], 2)

    def test_import_reads_at_most_limit_plus_one_without_stat(self):
        source = self.base / 'large.json'
        source.write_bytes(b'x' * (self.transfer.MAX_IMPORT_BYTES + 1))
        with patch.object(Path, 'stat', side_effect=AssertionError('stat must not be used')):
            with self.assertRaisesRegex(ConfigurationTransferError, '^' + IMPORT_ERROR.replace('.', r'\.') + '$'):
                self.transfer.import_file(source)

    def test_deep_and_excessive_json_structures_are_redacted(self):
        deep = '[' * 2000 + '0' + ']' * 2000
        source = self.base / 'deep.json'
        source.write_text(deep, encoding='utf-8')
        with self.assertRaisesRegex(ConfigurationTransferError, '^Konfigurationsdatei ist ungültig'):
            self.transfer.import_file(source)
        document = self.transfer.export_document()
        document['configuration']['email_accounts'] = [None] * 1001
        with self.assertRaisesRegex(ConfigurationTransferError, '^Konfigurationsdatei ist ungültig'):
            self.transfer.import_document(document)

    def test_export_and_import_service_boundaries_redact_unexpected_failures(self):
        with patch.object(self.store, 'connect', side_effect=RuntimeError('PRIVATE/PATH')):
            with self.assertRaisesRegex(ConfigurationTransferError, '^Konfiguration konnte nicht sicher exportiert werden\\.$') as raised:
                self.transfer.export_document()
        self.assertEqual(raised.exception.code, 'CONFIG_EXPORT_FAILED')
        self.assertNotIn('PRIVATE', str(raised.exception))
        document = self.transfer.export_document()
        with patch.object(self.transfer, '_import_validated', side_effect=RuntimeError('PRIVATE/PATH')):
            with self.assertRaises(ConfigurationTransferError) as raised:
                self.transfer.import_document(document)
        self.assertNotIn('PRIVATE', str(raised.exception))

    def test_real_export_and_import_io_errors_are_redacted(self):
        bad_target = self.base / 'directory'
        bad_target.mkdir()
        with self.assertRaisesRegex(ConfigurationTransferError, '^Konfiguration konnte nicht sicher exportiert werden\\.$'):
            self.transfer.export_file(bad_target)
        with self.assertRaisesRegex(ConfigurationTransferError, '^Konfigurationsdatei ist ungültig'):
            self.transfer.import_file(self.base / 'PRIVATE' / 'missing.json')

    def test_restore_uses_platform_newlines_for_local_configuration_files(self):
        self.configure_all_domains()
        for path in (self.database.path, self.design.path):
            with self.subTest(path=path.name):
                snapshot = self.transfer._read_semantic_snapshot(path)
                expected_lf = self.transfer._json_bytes(snapshot)
                with patch('configuration.os.linesep', '\n'):
                    self.transfer._restore(path, snapshot)
                self.assertEqual(path.read_bytes(), expected_lf)

    def test_each_file_restore_failure_keeps_recovery_journal_and_uses_fatal_message(self):
        self.configure_all_domains()
        imported = self.transfer.export_document()
        imported['configuration']['design']['theme'] = 'light'
        targets = (self.database.path, self.design.path)
        originals = {target: target.read_bytes() for target in targets}
        for target in targets:
            for initially_exists in (True, False):
                with self.subTest(target=target.name, initially_exists=initially_exists):
                    if initially_exists:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(originals[target])
                    else:
                        target.unlink(missing_ok=True)
                    real_restore = self.transfer._restore
                    def fail_target(path, snapshot, failed=target):
                        if path == failed:
                            raise OSError('PRIVATE restore detail')
                        return real_restore(path, snapshot)
                    with patch.object(self.transfer, '_write_json_atomic', side_effect=OSError('apply failure')), \
                         patch.object(self.transfer, '_restore', side_effect=fail_target):
                        with self.assertRaisesRegex(ConfigurationTransferError, '^' + RECOVERY_ERROR.replace('.', r'\.') + '$'):
                            self.transfer.import_document(imported)
                    self.assertTrue(self.transfer.journal_path.exists())
                    self.transfer._recover_pending()
                    self.assertFalse(self.transfer.journal_path.exists())

    def test_restore_verification_and_journal_cleanup_failures_are_fatal_and_recoverable(self):
        document = self.transfer.export_document()
        with patch.object(self.transfer, '_write_json_atomic', side_effect=OSError('apply')), \
             patch.object(self.transfer, '_snapshot_matches', return_value=False):
            with self.assertRaisesRegex(ConfigurationTransferError, '^' + RECOVERY_ERROR.replace('.', r'\.') + '$'):
                self.transfer.import_document(document)
        self.assertTrue(self.transfer.journal_path.exists())
        self.transfer._recover_pending()
        self.assertFalse(self.transfer.journal_path.exists())

        real_unlink = Path.unlink
        def fail_journal(path, *args, **kwargs):
            if path == self.transfer.journal_path:
                raise OSError('PRIVATE cleanup detail')
            return real_unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', fail_journal):
            with self.assertRaisesRegex(ConfigurationTransferError, '^' + RECOVERY_ERROR.replace('.', r'\.') + '$'):
                self.transfer.import_document(document)
        self.assertTrue(self.transfer.journal_path.exists())
        self.transfer._recover_pending()
        self.assertFalse(self.transfer.journal_path.exists())

    def test_storage_contract_uses_backend_neutral_queries(self):
        self.configure_all_domains()
        document = self.transfer.export_document()

        class BackendProxy:
            backend = 'postgresql'
            profile = {'host':'contract.example.test','port':5432,'database':'contract',
                       'username':'contract','sslmode':'require'}
            def __init__(self, store): self.store = store
            def connect(self): return self.store.connect()

        proxy = BackendProxy(Store(self.base / 'postgres-contract.db'))
        transfer = ConfigurationTransfer(proxy, self.base / 'postgres-target',
                                         DatabaseSettings(self.base / 'postgres-target', FakeVault(), {}),
                                         DesignSettings(self.base / 'postgres-target'))
        transfer.import_document(document)
        self.assertEqual(ConfigurationTransfer(proxy, self.base / 'postgres-target',
                                               transfer.database_settings,
                                               transfer.design_settings).export_document(), document)

    def test_authority_fetches_only_allowlisted_setting_rows(self):
        requested = []

        class Result:
            def __init__(self, one=None, many=None):
                self.one, self.many = one, many or []
            def fetchone(self): return self.one
            def fetchall(self): return self.many

        class BoundaryDatabase:
            def execute(self, sql, parameters=()):
                if 'FROM settings' in sql:
                    self_name = parameters[0]
                    requested.append(self_name)
                    if self_name == 'unknown.secret':
                        raise AssertionError('secret-like row was fetched')
                    return Result({'value': 'openai'} if self_name == 'active_provider' else None)
                return Result(many=[])

        document = ConfigurationAuthority._document(
            store_identity(self.store), BoundaryDatabase())
        self.assertEqual(set(requested), set(TRANSFER_SETTING_NAMES))
        self.assertNotIn('unknown.secret', requested)
        self.assertEqual(document['settings'], {'active_provider': 'openai'})

    def test_authority_versions_require_exact_int_type(self):
        class IntVersion(int):
            pass

        identity = store_identity(self.store)
        for version in (True, IntVersion(1)):
            with self.subTest(version=version), self.assertRaises(ValueError):
                ConfigurationAuthority.validate_document({
                    'format':'wissensraum-configuration-authority', 'version':version,
                    'store':identity, 'settings':{}, 'email_accounts':[]})

        authority = ConfigurationAuthority(self.base)
        authority._write_json(authority.pending_path, {
            'format':'wissensraum-authority-resync', 'version':True,
            'store':identity})
        with self.assertRaises(ValueError):
            authority.read_pending()

    def test_transfer_validation_rejects_hostile_subclasses_without_hooks(self):
        document = self.transfer.export_document()
        paths = (
            (('format',), HostileString(document['format']), HostileString),
            (('version',), HostileInteger(document['version']), HostileInteger),
            (('configuration', 'ai', 'active_provider'), HostileString('openai'), HostileString),
            (('configuration', 'ai', 'providers', 'openai', 'model'), HostileString('gpt-4o'), HostileString),
            (('configuration', 'database', 'backend'), HostileString('sqlite'), HostileString),
            (('configuration', 'database', 'port'), HostileInteger(5432), HostileInteger),
            (('configuration', 'database', 'host'), HostileString(''), HostileString),
            (('configuration', 'design', 'theme'), HostileString('system'), HostileString),
        )
        for path, hostile, hostile_type in paths:
            with self.subTest(path=path):
                candidate = __import__('copy').deepcopy(document)
                parent = candidate
                for part in path[:-1]:
                    parent = parent[part]
                parent[path[-1]] = hostile
                hostile_type.reset()
                with self.assertRaises(ValueError):
                    ConfigurationTransfer._validate(candidate)
                self.assertFalse(hostile_type.called)

        containers = (
            HostileDict(document),
            {'format': document['format'], 'version': document['version'],
             'configuration': HostileDict(document['configuration'])},
            {**document, 'configuration': {**document['configuration'],
             'email_accounts': HostileList(document['configuration']['email_accounts'])}},
        )
        for candidate in containers:
            with self.subTest(container=type(candidate).__name__):
                HostileDict.reset(); HostileList.reset()
                with self.assertRaises(ValueError):
                    ConfigurationTransfer._validate(candidate)
                self.assertFalse(HostileDict.called or HostileList.called)

    def test_authority_identity_marker_journal_reject_hostile_subclasses_without_hooks(self):
        authority = ConfigurationAuthority(self.base)
        identity = store_identity(self.store)
        authority_document = {'format':'wissensraum-configuration-authority','version':1,
                              'store':identity,'settings':{},'email_accounts':[]}
        cases = (
            lambda: ConfigurationAuthority.validate_document(
                {**authority_document, 'format':HostileString(authority_document['format'])}),
            lambda: ConfigurationAuthority.validate_document(
                {**authority_document, 'version':HostileInteger(1)}),
            lambda: __import__('configuration_authority').normalize_store_identity(
                {**identity, 'backend':HostileString('sqlite')}),
            lambda: __import__('configuration_authority').normalize_store_identity(
                {**identity, 'path':HostileString(identity['path'])}),
            lambda: ConfigurationAuthority.validate_document(HostileDict(authority_document)),
        )
        for invoke in cases:
            with self.subTest(invoke=invoke):
                HostileString.reset(); HostileInteger.reset(); HostileDict.reset()
                with self.assertRaises(ValueError):
                    invoke()
                self.assertFalse(HostileString.called or HostileInteger.called or HostileDict.called)

        local = {'format':'wissensraum-local-store-identity','version':1,
                 'store':identity,
                 'binding':__import__('configuration_authority')._filesystem_binding(self.store.path)}
        marker = {'format':'wissensraum-authority-resync','version':1,
                  'operation_id':'0' * 32,'store':identity}
        journal = {'format':'wissensraum-configuration-journal','version':3,
                   'transaction_id':'0' * 32,'phase':'prepared','origin_store':identity,
                   'snapshots':{'database':None,'design':None,'authority':None},
                   'configuration':self.transfer.export_document()['configuration']}
        readers = (
            (authority.local_identity_path, authority.load_local_identity,
             {**local, 'format':HostileString(local['format'])}),
            (authority.pending_path, authority.read_pending,
             {**marker, 'format':HostileString(marker['format'])}),
            (self.transfer.journal_path, self.transfer._read_journal,
             {**journal, 'phase':HostileString('prepared')}),
        )
        for path, reader, value in readers:
            with self.subTest(path=path.name):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('{}', encoding='utf-8')
                HostileString.reset()
                with patch('json.loads', return_value=value), self.assertRaises(ValueError):
                    reader()
                self.assertFalse(HostileString.called)

    def test_import_lock_cleanup_preserves_baseexceptions_and_releases_operation_lock(self):
        validated = ConfigurationTransfer._validate(self.transfer.export_document())
        for raised in (KeyboardInterrupt('interrupt'), SystemExit('exit')):
            with self.subTest(exception=type(raised).__name__):
                with patch.object(self.transfer.authority_lock, 'acquire', side_effect=raised):
                    with self.assertRaises(type(raised)) as caught:
                        self.transfer._import_validated(validated)
                self.assertIs(caught.exception, raised)
                independent = __import__('configuration_authority').InterProcessLock(
                    self.base / 'data' / 'configuration-import.lock')
                independent.acquire()
                independent.release()

    def test_authority_marker_has_unique_owner_and_foreign_finalize_cannot_remove_it(self):
        authority = ConfigurationAuthority(self.base)
        with self.store.connect() as db:
            prepared = authority.synchronize(self.store, db)
        marker = authority.read_pending()
        self.assertRegex(marker['operation_id'], r'^[0-9a-f]{32}$')
        with self.assertRaises(ValueError):
            authority.finalize_synchronize(self.store, dict(prepared))
        self.assertEqual(authority.read_pending()['operation_id'], marker['operation_id'])
        authority.finalize_synchronize(self.store, prepared)
        self.assertFalse(authority.pending_path.exists())

    def test_active_authority_owner_blocks_independent_process_mutation(self):
        ready = self.base / 'authority-ready'
        release = self.base / 'authority-release'
        import_document = self.transfer.export_document()
        import_document['configuration']['design']['theme'] = 'dark'
        script = """
import pathlib, sys, time
sys.path.insert(0, {repo!r})
from configuration_authority import ConfigurationAuthority
from core import Store
base = pathlib.Path({base!r})
store = Store(base / 'wissen.db')
authority = ConfigurationAuthority(base)
prepared = None
try:
    with store.connect() as db:
        db.execute('INSERT INTO settings(name,value) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value', ('active_provider','ollama'))
        prepared = authority.synchronize(store, db)
    pathlib.Path({ready!r}).write_text('ready')
    while not pathlib.Path({release!r}).exists(): time.sleep(.01)
    authority.finalize_synchronize(store, prepared)
finally:
    authority.abort_synchronize(prepared)
""".format(repo=str(Path(__file__).parents[1]), base=str(self.base),
           ready=str(ready), release=str(release))
        process = subprocess.Popen([sys.executable, '-c', script])
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(ready.exists())
            with self.assertRaisesRegex(ValueError, '^' + re.escape(AUTHORITY_RECOVERY_ERROR) + '$'):
                Settings(self.store, self.base, self.vault, {}).put({'active_provider':'anthropic'})
            with self.assertRaisesRegex(ConfigurationTransferError, '^' + re.escape(APPLY_ERROR) + '$'):
                self.transfer.import_document(import_document)
            self.assertTrue(ConfigurationAuthority(self.base).pending_path.exists())
            release.write_text('release', encoding='utf-8')
            self.assertEqual(process.wait(timeout=5), 0)
            self.assertFalse(ConfigurationAuthority(self.base).pending_path.exists())
            self.assertEqual(Settings(self.store, self.base, self.vault, {}).active(), 'ollama')
        finally:
            release.write_text('release', encoding='utf-8')
            if process.poll() is None:
                process.kill()
            process.wait()

    def test_local_identity_version_rejects_bool(self):
        authority = ConfigurationAuthority(self.base)
        identity = store_identity(self.store)
        authority._write_json(authority.local_identity_path, {
            'format':'wissensraum-local-store-identity', 'version':True,
            'store':identity, 'binding':__import__('configuration_authority')._filesystem_binding(
                self.store.path)})
        with self.assertRaises(ValueError):
            authority.load_local_identity(self.store.path)

    def test_import_lock_blocks_an_independent_process_and_releases_after_exit(self):
        ready = self.base / 'lock-ready'
        release = self.base / 'lock-release'
        document = self.transfer.export_document()
        script = (
            "import pathlib,time; from configuration_authority import InterProcessLock; "
            "lock=InterProcessLock(pathlib.Path(r'" + str(self.base) + "')/'data'/'configuration-import.lock'); "
            "lock.acquire(); pathlib.Path(r'" + str(ready) + "').write_text('ready'); "
            "\nwhile not pathlib.Path(r'" + str(release) + "').exists(): time.sleep(.01)\n"
            "lock.release()")
        process = subprocess.Popen([sys.executable, '-c', script], cwd=str(Path(__file__).parents[1]))
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(ready.exists())
            other = ConfigurationTransfer(self.store, self.base, self.database, self.design)
            with self.assertRaisesRegex(ConfigurationTransferError, '^' + re.escape(APPLY_ERROR) + '$'):
                other.import_document(document)
            release.write_text('release', encoding='utf-8')
            self.assertEqual(process.wait(timeout=5), 0)
            self.assertEqual(other.import_document(document), {'restart_required': False})
        finally:
            release.write_text('release', encoding='utf-8')
            if process.poll() is None:
                process.kill()
            process.wait()

    def test_export_lock_blocks_an_independent_process_and_releases_after_exit(self):
        ready = self.base / 'export-lock-ready'
        release = self.base / 'export-lock-release'
        script = (
            "import pathlib,time; from configuration_authority import configuration_operation_lock; "
            "lock=configuration_operation_lock(pathlib.Path(r'" + str(self.base) + "')); "
            "lock.acquire(); pathlib.Path(r'" + str(ready) + "').write_text('ready'); "
            "\nwhile not pathlib.Path(r'" + str(release) + "').exists(): time.sleep(.01)\n"
            "lock.release()")
        process = subprocess.Popen([sys.executable, '-c', script],
                                   cwd=str(Path(__file__).parents[1]))
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(ready.exists())
            with self.assertRaisesRegex(ConfigurationTransferError,
                                        '^' + re.escape(EXPORT_ERROR) + '$'):
                self.transfer.export_document()
            release.write_text('release', encoding='utf-8')
            self.assertEqual(process.wait(timeout=5), 0)
            self.assertEqual(self.transfer.export_document()['format'],
                             'wissensraum-configuration')
        finally:
            release.write_text('release', encoding='utf-8')
            if process.poll() is None:
                process.kill()
            process.wait()

    def test_post_commit_failures_finalize_as_success_instead_of_false_no_change(self):
        for failure in ('verify', 'committed-journal', 'finish'):
            with self.subTest(failure=failure):
                base = self.base / failure
                store = Store(base / 'wissen.db')
                database = DatabaseSettings(base, FakeVault(), {})
                design = DesignSettings(base)
                transfer = ConfigurationTransfer(store, base, database, design)
                document = transfer.export_document()
                document['configuration']['ai']['active_provider'] = 'ollama'
                if failure == 'verify':
                    original = transfer._verify_applied
                    calls = 0
                    def injected(*args):
                        nonlocal calls
                        calls += 1
                        if calls == 1:
                            raise OSError('private verification failure')
                        return original(*args)
                    patched = patch.object(transfer, '_verify_applied', side_effect=injected)
                elif failure == 'committed-journal':
                    original = transfer._write_journal
                    calls = 0
                    def injected(journal):
                        nonlocal calls
                        calls += 1
                        if calls == 2:
                            raise OSError('private journal failure')
                        return original(journal)
                    patched = patch.object(transfer, '_write_journal', side_effect=injected)
                else:
                    original = transfer._finish_committed
                    calls = 0
                    def injected(transaction_id):
                        nonlocal calls
                        calls += 1
                        if calls == 1:
                            raise OSError('private finish failure')
                        return original(transaction_id)
                    patched = patch.object(transfer, '_finish_committed', side_effect=injected)
                with patched:
                    result = transfer.import_document(document)
                self.assertEqual(result, {'restart_required': False})
                self.assertEqual(Settings(store, base, FakeVault(), {}).active(), 'ollama')
                self.assertFalse(transfer.journal_path.exists())

    def test_recovery_uses_journal_origin_before_changed_profile_and_blocks_on_failure(self):
        document = self.transfer.export_document()
        document['configuration']['database'].update({
            'backend': 'postgresql', 'host': 'new.example.test', 'port': 5432,
            'database': 'newdb', 'username': 'newuser', 'sslmode': 'require',
        })
        with patch.object(self.transfer, '_verify_applied', side_effect=OSError('crash')), \
             patch.object(self.transfer, '_recover_pending',
                          side_effect=['no_pending', OSError('restart required')]):
            with self.assertRaisesRegex(ConfigurationTransferError, '^' + RECOVERY_ERROR.replace('.', r'\.') + '$'):
                self.transfer.import_document(document)
        journal = json.loads(self.transfer.journal_path.read_text(encoding='utf-8'))
        self.assertEqual(journal['origin_store']['backend'], 'sqlite')
        self.assertEqual(Path(journal['origin_store']['path']), self.store.path.resolve())

        opened = []
        def open_origin(identity, *_args):
            opened.append(identity)
            return self.store
        with patch('configuration._open_origin_store', side_effect=open_origin):
            self.assertEqual(recover_pending_configuration(
                self.base, self.database, self.store.path), 'committed_finalized')
        self.assertEqual(opened, [journal['origin_store']])
        self.assertFalse(self.transfer.journal_path.exists())

        # A failed exact-origin connection must preserve the journal and block recovery.
        self.transfer._write_journal(journal)
        with patch('configuration._open_origin_store', side_effect=OSError('PRIVATE host')):
            with self.assertRaisesRegex(ConfigurationTransferError, '^' + RECOVERY_ERROR.replace('.', r'\.') + '$'):
                recover_pending_configuration(self.base, self.database, self.store.path)
        self.assertTrue(self.transfer.journal_path.exists())

    def test_postgresql_origin_is_preserved_for_backend_and_same_backend_profile_changes(self):
        origins = (
            {'host':'old.example.test','port':5432,'database':'old_db',
             'username':'old_user','sslmode':'require'},
            {'host':'other.example.test','port':5433,'database':'other_db',
             'username':'other_user','sslmode':'verify-full'},
        )
        targets = (
            {'backend':'sqlite','host':'','port':5432,'database':'','username':'','sslmode':'require'},
            {'backend':'postgresql','host':'new.example.test','port':5432,'database':'new_db',
             'username':'new_user','sslmode':'require'},
        )
        for index, (origin_profile, target_profile) in enumerate(zip(origins, targets)):
            with self.subTest(origin=origin_profile, target=target_profile):
                base = self.base / ('pg-origin-' + str(index))
                sql = Store(base / 'pg-contract.db')
                class PostgresProxy:
                    backend = 'postgresql'
                    profile = origin_profile
                    def connect(self): return sql.connect()
                proxy = PostgresProxy()
                database = DatabaseSettings(base, FakeVault(), {})
                database._write({'backend':'postgresql', **origin_profile})
                transfer = ConfigurationTransfer(proxy, base, database, DesignSettings(base))
                document = transfer.export_document()
                document['configuration']['database'] = target_profile
                with patch.object(transfer, '_verify_applied', side_effect=OSError('crash')), \
                     patch.object(transfer, '_recover_pending',
                                  side_effect=['no_pending', OSError('restart')]):
                    with self.assertRaises(ConfigurationTransferError):
                        transfer.import_document(document)
                journal = json.loads(transfer.journal_path.read_text(encoding='utf-8'))
                self.assertEqual(journal['origin_store'], {'backend':'postgresql', **origin_profile})
                opened = []
                with patch('configuration._open_origin_store',
                           side_effect=lambda identity, *_: opened.append(identity) or proxy):
                    self.assertEqual(recover_pending_configuration(
                        base, database, base / 'unused.db'), 'committed_finalized')
                self.assertEqual(opened, [{'backend':'postgresql', **origin_profile}])

    def test_postgresql_recovery_opens_origin_without_schema_initialization(self):
        identity = {'backend':'postgresql', 'host':'old.example.test', 'port':5432,
                    'database':'old_db', 'username':'old_user', 'sslmode':'require'}
        profile = {name:value for name,value in identity.items() if name != 'backend'}
        sentinel = object()
        with patch.object(self.database, 'connection_values',
                          return_value=(profile, 'synthetic-password')) as connection_values, \
             patch('core.PostgresStore', return_value=sentinel) as postgres_store:
            self.assertIs(_open_origin_store(identity, self.database), sentinel)
        connection_values.assert_called_once_with(identity)
        postgres_store.assert_called_once_with(profile, 'synthetic-password', initialize=False)

    def test_normal_ai_and_mail_mutations_keep_authority_current(self):
        settings = Settings(self.store, self.base, self.vault, {})
        settings.save('ollama', 'qwen3:8b')
        authority = ConfigurationAuthority(self.base).load()
        self.assertEqual(authority['settings']['active_provider'], 'ollama')
        from importer import MailAccounts
        accounts = MailAccounts(self.store, self.vault, self.base)
        account_id = accounts.save({
            'label':'Current','provider':'imap','email':'current@example.test',
            'username':'current','host':'imap.example.test','port':993,
            'client_id':'','tenant':'common'}, 'synthetic-password')
        authority = ConfigurationAuthority(self.base).load()
        self.assertEqual(authority['email_accounts'][0]['id'], account_id)
        accounts.delete(account_id)
        self.assertEqual(ConfigurationAuthority(self.base).load()['email_accounts'], [])

    def test_authority_export_uses_active_metadata_not_divergent_sqlite_cache(self):
        class ActivePostgresProxy:
            backend = 'postgresql'
            profile = {'host': 'active.example.test', 'port': 5432, 'database': 'active',
                       'username': 'active_user', 'sslmode': 'require'}
            def __init__(self, store): self.store = store
            def connect(self): return self.store.connect()

        stale = self.store
        Settings(stale, self.base, self.vault, {}).put({'active_provider': 'openai'})
        active_sql = Store(self.base / 'active-pg-contract.db')
        Settings(active_sql, self.base, self.vault, {}).put({'active_provider': 'ollama'})
        with active_sql.connect() as db:
            db.execute('''INSERT INTO email_accounts
                (id,label,provider,email,username,host,port,client_id,tenant)
                VALUES(?,?,?,?,?,?,?,?,?)''',
                ('active-mail','Active','gmail','active@example.test','active',
                 'imap.example.test',993,'','common'))
        proxy = ActivePostgresProxy(active_sql)
        self.database._write({'backend': 'postgresql', **proxy.profile})
        ConfigurationTransfer(proxy, self.base, self.database, self.design).synchronize_authority()

        secret_free = ConfigurationTransfer(None, self.base, self.database, self.design,
                                             expected_store_identity={'backend':'postgresql', **proxy.profile})
        with patch.object(stale, 'connect', side_effect=AssertionError('stale SQLite opened')):
            exported = secret_free.export_document()
        self.assertEqual(exported['configuration']['ai']['active_provider'], 'ollama')
        self.assertEqual(exported['configuration']['email_accounts'][0]['id'], 'active-mail')

    def test_secret_free_export_fails_closed_when_authority_is_missing_or_mismatched(self):
        transfer = ConfigurationTransfer(None, self.base, self.database, self.design,
                                         expected_store_identity=store_identity(self.store))
        for authority in (None, {'format':'wissensraum-configuration-authority','version':1,
                                 'store':store_identity(Store(self.base/'other.db')),
                                 'settings':{},'email_accounts':[]}):
            with self.subTest(authority=authority):
                transfer.authority_path.unlink(missing_ok=True)
                if authority is not None:
                    transfer._write_json_atomic(transfer.authority_path, authority)
                with patch('configuration._expected_identity_matches', wraps=__import__(
                        'configuration')._expected_identity_matches) as matches:
                    with self.assertRaisesRegex(ConfigurationTransferError, '^' + AUTHORITY_ERROR.replace('.', r'\.') + '$'):
                        transfer.export_document()
                if authority is not None:
                    matches.assert_called_once()

    def test_transfer_rejects_unknown_secret_fields_before_journaling_or_mutation(self):
        self.configure_all_domains()
        document = self.transfer.export_document()
        targets = (self.database.path, self.design.path, self.transfer.authority_path)
        for target, field in zip(targets, ('password', 'api_token', 'client_secret')):
            with self.subTest(target=target.name, field=field):
                originals = [path.read_bytes() for path in targets]
                value = json.loads(target.read_text(encoding='utf-8'))
                value[field] = 'DO_NOT_JOURNAL'
                target.write_text(json.dumps(value), encoding='utf-8')
                before = [path.read_bytes() for path in targets]
                with patch.object(self.transfer, '_write_journal') as write_journal:
                    with self.assertRaisesRegex(ConfigurationTransferError, '^' + re.escape(APPLY_ERROR) + '$'):
                        self.transfer.import_document(document)
                write_journal.assert_not_called()
                self.assertFalse(self.transfer.journal_path.exists())
                self.assertEqual([path.read_bytes() for path in targets], before)
                for path, original in zip(targets, originals):
                    path.write_bytes(original)

    def test_secret_free_sqlite_identity_rejects_replacement_at_same_path_without_sql(self):
        self.transfer.synchronize_authority()
        original_identity = identity_for_profile({'backend':'sqlite'}, self.store.path,
                                                 base=self.base)
        self.store.path.unlink()
        Store(self.store.path)
        with patch.object(self.store, 'connect', side_effect=AssertionError('SQL opened')):
            with self.assertRaises(ValueError):
                identity_for_profile({'backend':'sqlite'}, self.store.path, base=self.base)
        self.assertEqual(original_identity['path'], str(self.store.path.resolve()))

    def test_export_never_serializes_environment_derived_database_profile(self):
        environment = {'DB_BACKEND':'postgresql', 'DB_HOST':'environment.example.test',
                       'DB_PORT':'5439', 'DB_DATABASE':'environment_db',
                       'DB_USERNAME':'environment_user', 'DB_SSLMODE':'verify-full'}
        database = DatabaseSettings(self.base, self.vault, environ=environment)
        authority = self.transfer.synchronize_authority()
        database.path.parent.mkdir(parents=True, exist_ok=True)
        database.path.write_text('{"backend":"postgresql"}', encoding='utf-8')
        authority['store'] = {'backend':'postgresql', 'host':'environment.example.test',
                              'port':5439, 'database':'environment_db',
                              'username':'environment_user', 'sslmode':'verify-full'}
        self.transfer._write_json_atomic(self.transfer.authority_path, authority)
        transfer = ConfigurationTransfer(None, self.base, database, self.design,
                                         expected_store_identity=authority['store'])
        with self.assertRaises(ConfigurationTransferError):
            transfer.export_document()

    def test_export_rejects_environment_only_active_postgresql_profile(self):
        environment = {'DB_BACKEND':'postgresql', 'DB_HOST':'environment.example.test',
                       'DB_PORT':'5439', 'DB_DATABASE':'environment_db',
                       'DB_USERNAME':'environment_user', 'DB_SSLMODE':'verify-full'}
        database = DatabaseSettings(self.base, self.vault, environ=environment)
        authority = self.transfer.synchronize_authority()
        database.path.unlink(missing_ok=True)
        authority['store'] = {'backend':'postgresql', 'host':'environment.example.test',
                              'port':5439, 'database':'environment_db',
                              'username':'environment_user', 'sslmode':'verify-full'}
        self.transfer._write_json_atomic(self.transfer.authority_path, authority)
        transfer = ConfigurationTransfer(None, self.base, database, self.design,
                                         expected_store_identity=authority['store'])
        with self.assertRaises(ConfigurationTransferError):
            transfer.export_document()

    def test_settings_crash_before_commit_retains_marker_and_recovers_rolled_back_sql(self):
        settings = Settings(self.store, self.base, self.vault, {})
        settings.put({'active_provider':'openai'})
        real = ConfigurationAuthority.synchronize
        def crash_after_sync(authority, store, db=None):
            real(authority, store, db)
            raise KeyboardInterrupt
        with patch.object(ConfigurationAuthority, 'synchronize', crash_after_sync):
            with self.assertRaises(KeyboardInterrupt):
                settings.put({'active_provider':'ollama'})
        self.assertTrue(ConfigurationAuthority(self.base).pending_path.exists())
        self.assertEqual(settings.active(), 'openai')
        self.assertEqual(recover_pending_authority(self.base, self.store), 'resynchronized')
        self.assertEqual(ConfigurationAuthority(self.base).load()['settings']['active_provider'],
                         'openai')

    def test_mail_crash_before_commit_retains_marker_and_recovers_rolled_back_sql(self):
        from importer import MailAccounts
        accounts = MailAccounts(self.store, self.vault, self.base)
        real = ConfigurationAuthority.synchronize
        def crash_after_sync(authority, store, db=None):
            real(authority, store, db)
            raise KeyboardInterrupt
        with patch.object(ConfigurationAuthority, 'synchronize', crash_after_sync):
            with self.assertRaises(KeyboardInterrupt):
                accounts._change_metadata(lambda db: db.execute('''INSERT INTO email_accounts
                    (id,label,provider,email,username,host,port,client_id,tenant)
                    VALUES(?,?,?,?,?,?,?,?,?)''', ('crash-mail','Crash','gmail',
                    'crash@example.test','crash','imap.example.test',993,'','common')))
        self.assertTrue(ConfigurationAuthority(self.base).pending_path.exists())
        with self.store.connect() as db:
            self.assertIsNone(db.execute('SELECT id FROM email_accounts WHERE id=?',
                                         ('crash-mail',)).fetchone())
        self.assertEqual(recover_pending_authority(self.base, self.store), 'resynchronized')

    def test_atomic_writes_sync_containing_directory_after_replace(self):
        operations = []
        real_replace = os.replace
        def replace(source, target):
            operations.append('replace')
            return real_replace(source, target)
        with patch('configuration.os.fsync', side_effect=lambda _fd: operations.append('file')), \
             patch('configuration.os.replace', side_effect=replace), \
             patch('configuration._sync_directory',
                   side_effect=lambda _path: operations.append('directory')):
            self.transfer._write_json_atomic(self.base/'atomic.json', {'safe':True})
        self.assertEqual(operations, ['file','replace','directory'])

    def test_marker_delete_sync_failure_restores_marker_and_fails(self):
        authority = ConfigurationAuthority(self.base)
        authority._write_json(authority.pending_path, {
            'format':'wissensraum-authority-resync','version':1,
            'store':store_identity(self.store)})
        with patch('configuration_authority._sync_directory', side_effect=OSError('sync failed')):
            with self.assertRaises(OSError):
                authority._remove_durable(authority.pending_path)
        self.assertTrue(authority.pending_path.exists())

        with patch('configuration_authority._sync_directory',
                   side_effect=KeyboardInterrupt('interrupted')):
            with self.assertRaises(KeyboardInterrupt):
                authority._remove_durable(authority.pending_path)
        self.assertTrue(authority.pending_path.exists())

    def test_pending_transfer_journal_contains_only_semantic_snapshots(self):
        self.configure_all_domains()
        document = self.transfer.export_document()
        with patch.object(self.transfer, '_apply_database', side_effect=OSError('stop')), \
             patch.object(self.transfer, '_recover_pending', side_effect=['no_pending', OSError('keep')]):
            with self.assertRaises(ConfigurationTransferError):
                self.transfer.import_document(document)
        journal = json.loads(self.transfer.journal_path.read_text(encoding='utf-8'))
        self.assertIsInstance(journal['snapshots'], dict)
        self.assertEqual(set(journal['snapshots']), {'database', 'design', 'authority'})
        self.assertNotIn('DO_NOT_JOURNAL', json.dumps(journal))
        self.assertNotIn('base64', json.dumps(journal).lower())

    def test_exact_origin_recovery_rejects_deleted_or_replaced_sqlite(self):
        document = self.transfer.export_document()
        document['configuration']['design']['theme'] = 'dark'
        with patch.object(self.transfer, '_verify_applied', side_effect=OSError('crash')), \
             patch.object(self.transfer, '_recover_pending', side_effect=['no_pending', OSError('keep')]):
            with self.assertRaises(ConfigurationTransferError):
                self.transfer.import_document(document)
        original = self.store.path.read_bytes()
        self.store.path.unlink()
        with self.assertRaisesRegex(ConfigurationTransferError, '^' + re.escape(RECOVERY_ERROR) + '$'):
            recover_pending_configuration(self.base, self.database, self.store.path)
        self.assertFalse(self.store.path.exists())
        self.assertTrue(self.transfer.journal_path.exists())
        replacement = Store(self.store.path)
        self.assertNotEqual(store_identity(replacement), json.loads(
            self.transfer.journal_path.read_text(encoding='utf-8'))['origin_store'])
        with self.assertRaisesRegex(ConfigurationTransferError, '^' + re.escape(RECOVERY_ERROR) + '$'):
            recover_pending_configuration(self.base, self.database, self.store.path)
        self.assertTrue(self.transfer.journal_path.exists())
        self.store.path.unlink()
        self.store.path.write_bytes(original)

    def test_authority_pending_blocks_secret_free_export_and_recovers_from_sql(self):
        settings = Settings(self.store, self.base, self.vault, {})
        settings.put({'active_provider': 'ollama'})
        pending = self.base/'data'/'authority-resync-pending.json'
        pending.write_text(json.dumps({'format':'wissensraum-authority-resync','version':1,
                                       'store':store_identity(self.store)}), encoding='utf-8')
        transfer = ConfigurationTransfer(None, self.base, self.database, self.design,
                                         expected_store_identity=store_identity(self.store))
        with patch.object(self.store, 'connect', side_effect=AssertionError('secret-free export opened SQL')):
            with self.assertRaisesRegex(ConfigurationTransferError, '^' + re.escape(AUTHORITY_ERROR) + '$'):
                transfer.export_document()
        recover_pending_authority(self.base, self.store)
        self.assertFalse(pending.exists())
        self.assertEqual(ConfigurationAuthority(self.base).load()['settings']['active_provider'], 'ollama')


class ConfigurationCliTests(unittest.TestCase):
    def test_actual_cli_export_of_postgresql_profile_never_opens_active_store_or_reads_secrets(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            database = DatabaseSettings(base, FakeVault(), environ={})
            database._write({'backend': 'postgresql', 'host': 'db.example.test', 'port': 5432,
                             'database': 'wissen', 'username': 'user', 'sslmode': 'require'})
            ConfigurationAuthority(base).write({
                'format':'wissensraum-configuration-authority','version':1,
                'store':{'backend':'postgresql','host':'db.example.test','port':5432,
                         'database':'wissen','username':'user','sslmode':'require'},
                'settings':{},'email_accounts':[]})
            target = base / 'export.json'
            vault = Mock()
            vault.get.side_effect = AssertionError('secret getter called')
            output = io.StringIO()
            with patch.object(cli, 'BASE', base), patch.object(cli, 'SystemVault', return_value=vault), \
                 patch.object(cli, 'Store', side_effect=AssertionError('SQLite store opened')), \
                 patch.object(cli.DatabaseSettings, 'open_store', side_effect=AssertionError('active store opened')), \
                 patch.object(cli, 'create_operational_logger', return_value=Mock()), redirect_stdout(output):
                self.assertEqual(cli.main(['config-export', str(target)]), 0)
            document = json.loads(target.read_text(encoding='utf-8'))
            self.assertEqual(document['configuration']['database']['backend'], 'postgresql')
            self.assertEqual(set(document['configuration']), {'ai','database','design','email_accounts'})
            vault.get.assert_not_called()

    def test_cli_export_and_import_have_deterministic_redacted_messages(self):
        transfer = Mock()
        transfer.export_file.return_value = None
        transfer.import_file.return_value = {'restart_required': True}
        store = Mock(backend='sqlite')
        database = Mock()
        database.open_store.return_value = store
        logger = Mock()
        output = io.StringIO()
        with patch.object(cli, 'SystemVault'), patch.object(cli, 'DatabaseSettings', return_value=database), \
             patch.object(cli, 'identity_for_profile', return_value={'backend':'sqlite','path':'C:/test.db'}), \
             patch.object(cli, 'ConfigurationTransfer', return_value=transfer), \
             patch.object(cli, 'create_operational_logger', return_value=logger), redirect_stdout(output):
            self.assertEqual(cli.main(['config-export', 'PRIVATE/PATH/config.json']), 0)
            self.assertEqual(json.loads(output.getvalue()),
                             {'status': 'exportiert', 'restart_required': False})
        self.assertNotIn('PRIVATE', output.getvalue())

        output = io.StringIO()
        with patch.object(cli, 'SystemVault'), patch.object(cli, 'DatabaseSettings', return_value=database), \
             patch.object(cli, 'ConfigurationTransfer', return_value=transfer), \
             patch.object(cli, 'create_operational_logger', return_value=logger), redirect_stdout(output):
            self.assertEqual(cli.main(['config-import', 'PRIVATE/PATH/config.json']), 0)
        self.assertEqual(json.loads(output.getvalue()),
                         {'status': 'importiert', 'restart_required': True,
                          'hinweis': 'Programm neu starten, damit die konfigurierte Datenquelle verwendet wird.'})
        self.assertNotIn('PRIVATE', output.getvalue())

    def test_cli_import_failure_is_redacted_and_nonzero(self):
        transfer = Mock()
        transfer.import_file.side_effect = ConfigurationTransferError(
            IMPORT_ERROR, 'CONFIG_IMPORT_INVALID')
        database = Mock(); database.open_store.return_value = Mock(backend='sqlite')
        error = io.StringIO()
        with patch.object(cli, 'SystemVault'), patch.object(cli, 'DatabaseSettings', return_value=database), \
             patch.object(cli, 'ConfigurationTransfer', return_value=transfer), \
             patch.object(cli, 'create_operational_logger', return_value=Mock()), redirect_stderr(error):
            with self.assertRaises(SystemExit) as raised:
                cli.main(['config-import', 'SENSITIVE_PATH.json'])
        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(error.getvalue(), 'Fehler: Konfigurationsdatei ist ungültig; es wurde nichts geändert.\n')
        self.assertNotIn('SENSITIVE_PATH', error.getvalue())

    def test_cli_export_and_import_unexpected_errors_are_fixed_and_redacted(self):
        database = Mock(); database.open_store.return_value = Mock(backend='sqlite')
        for command, method, expected in (
                ('config-export', 'export_file', EXPORT_ERROR),
                ('config-import', 'import_file', APPLY_ERROR)):
            transfer = Mock()
            getattr(transfer, method).side_effect = RuntimeError('PRIVATE/PATH raw detail')
            error = io.StringIO()
            with patch.object(cli, 'SystemVault'), patch.object(cli, 'DatabaseSettings', return_value=database), \
                 patch.object(cli, 'ConfigurationTransfer', return_value=transfer), \
                 patch.object(cli, 'create_operational_logger', return_value=Mock()), redirect_stderr(error):
                with self.assertRaises(SystemExit):
                    cli.main([command, 'PRIVATE/PATH.json'])
            self.assertEqual(error.getvalue(), 'Fehler: ' + expected + '\n')


class ConfigurationGuiFlowTests(unittest.TestCase):
    def make_window(self):
        window = object.__new__(app.App)
        window.configuration_transfer = Mock()
        window.refresh_settings = Mock()
        window.load_database_settings = Mock()
        window.refresh_mail_accounts = Mock()
        window.refresh_design_settings = Mock()
        window.apply_theme = Mock()
        window.design_settings = Mock()
        window.design_settings.theme.return_value = 'dark'
        return window

    def test_gui_export_and_import_cancel_without_action(self):
        window = self.make_window()
        with patch.object(app.filedialog, 'asksaveasfilename', return_value=''):
            window.export_configuration()
        with patch.object(app.filedialog, 'askopenfilename', return_value=''):
            window.import_configuration()
        window.configuration_transfer.export_file.assert_not_called()
        window.configuration_transfer.import_file.assert_not_called()

    def test_gui_export_error_and_import_success_are_reported(self):
        window = self.make_window()
        window.configuration_transfer.export_file.side_effect = ConfigurationTransferError(
            EXPORT_ERROR, 'CONFIG_EXPORT_FAILED')
        with patch.object(app.filedialog, 'asksaveasfilename', return_value='config.json'), \
             patch.object(app.messagebox, 'showerror') as showerror:
            window.export_configuration()
        showerror.assert_called_once_with('Konfiguration exportieren', EXPORT_ERROR, parent=window)

        window.configuration_transfer.import_file.return_value = {'restart_required': True}
        with patch.object(app.filedialog, 'askopenfilename', return_value='config.json'), \
             patch.object(app.messagebox, 'showinfo') as showinfo:
            window.import_configuration()
        window.refresh_settings.assert_called_once_with()
        window.load_database_settings.assert_called_once_with()
        window.refresh_mail_accounts.assert_called_once_with()
        window.refresh_design_settings.assert_called_once_with()
        window.apply_theme.assert_called_once_with('dark')
        showinfo.assert_called_once()
        self.assertIn('neu starten', showinfo.call_args.args[1])

    def test_gui_export_success_and_import_expected_error_are_safe(self):
        window = self.make_window()
        with patch.object(app.filedialog, 'asksaveasfilename', return_value='PRIVATE/export.json'), \
             patch.object(app.messagebox, 'showinfo') as showinfo:
            window.export_configuration()
        self.assertNotIn('PRIVATE', showinfo.call_args.args[1])
        window.configuration_transfer.import_file.side_effect = ConfigurationTransferError(
            IMPORT_ERROR, 'CONFIG_IMPORT_INVALID')
        with patch.object(app.filedialog, 'askopenfilename', return_value='PRIVATE/import.json'), \
             patch.object(app.messagebox, 'showerror') as showerror:
            window.import_configuration()
        showerror.assert_called_once_with('Konfiguration importieren', IMPORT_ERROR, parent=window)
        self.assertNotIn('PRIVATE', showerror.call_args.args[1])

    def test_gui_unexpected_export_and_import_errors_are_fixed_and_redacted(self):
        for operation, dialog, title, expected in (
                ('export_configuration', 'asksaveasfilename', 'Konfiguration exportieren', EXPORT_ERROR),
                ('import_configuration', 'askopenfilename', 'Konfiguration importieren', APPLY_ERROR)):
            window = self.make_window()
            method = ('export_file' if operation.startswith('export') else 'import_file')
            getattr(window.configuration_transfer, method).side_effect = RuntimeError('PRIVATE/PATH raw')
            with patch.object(app.filedialog, dialog, return_value='PRIVATE/path.json'), \
                 patch.object(app.messagebox, 'showerror') as showerror:
                getattr(window, operation)()
            showerror.assert_called_once_with(title, expected, parent=window)
            self.assertNotIn('PRIVATE', showerror.call_args.args[1])

    def test_gui_refresh_failures_report_committed_import_and_restart(self):
        refreshes = ('refresh_settings', 'load_database_settings', 'refresh_mail_accounts',
                     'refresh_design_settings', 'apply_theme')
        for failed in refreshes:
            with self.subTest(failed=failed):
                window = self.make_window()
                window.configuration_transfer.import_file.return_value = {'restart_required': False}
                getattr(window, failed).side_effect = RuntimeError('PRIVATE refresh detail')
                with patch.object(app.filedialog, 'askopenfilename', return_value='config.json'), \
                     patch.object(app.messagebox, 'showwarning') as warning, \
                     patch.object(app.messagebox, 'showerror') as error:
                    window.import_configuration()
                error.assert_not_called()
                warning.assert_called_once_with(
                    'Konfiguration importieren',
                    'Konfiguration wurde importiert, aber die Anzeige konnte nicht aktualisiert werden. Programm neu starten.',
                    parent=window)


if __name__ == '__main__':
    unittest.main()
